"""Wysyłka powiadomień przez Apprise z kierowaniem po tagach (ADR-005).

Kanały opisuje sekret `APPRISE_CONFIG` (format tekstowy Apprise: `tagi=URL`).
Reguły `alerts.routes` wybierają, które pozycje (dzieci, rodzaje, waga, typ
powiadomienia) trafiają do których tagów. Każda reguła jest wysyłana osobno:
awaria jednego kanału nie blokuje pozostałych. Adresy kanałów zawierają
tokeny, więc nigdy nie trafiają do logów (zostają w rejestrze redakcji).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from librus_watch.alerts import Alert, render
from librus_watch.config import Config, Route

log = logging.getLogger(__name__)


class Sender(Protocol):
    """To, czego wysyłka potrzebuje od Apprise (podmieniane w testach)."""

    def notify(self, body: str, title: str, tag: list[str]) -> bool:
        """Wysyła wiadomość do kanałów z tagami; `True` przy sukcesie."""
        ...


class AppriseSender:
    """Apprise skonfigurowany treścią sekretu `APPRISE_CONFIG`."""

    def __init__(self, config_text: str) -> None:
        """Wczytuje kanały z tekstowej konfiguracji Apprise."""
        import apprise

        self.apprise = apprise.Apprise()
        conf = apprise.AppriseConfig()
        conf.add_config(config_text, format="text")
        self.apprise.add(conf)

    def notify(self, body: str, title: str, tag: list[str]) -> bool:
        """Wysyła zwykły tekst (bez formatowania, więc bez ryzyka złego escapowania)."""
        import apprise

        return bool(
            self.apprise.notify(
                body=body, title=title, tag=tag, body_format=apprise.NotifyFormat.TEXT
            )
        )


def route_alert(alert: Alert, route: Route) -> Alert | None:
    """Część powiadomienia dla jednej reguły albo `None`, gdy nic jej nie dotyczy.

    Args:
        alert: Pełne powiadomienie.
        route: Reguła (dzieci, rodzaje, minimalna waga, typy powiadomień).

    Returns:
        Powiadomienie z pozycjami pasującymi do reguły.
    """
    if alert.alert_type not in route.alert_types:
        return None
    lines = [
        ln
        for ln in alert.lines
        if (route.children is None or ln.finding.child in route.children)
        and (route.kinds is None or ln.finding.kind in route.kinds)
        and ln.priority.rank >= route.min_priority.rank
    ]
    if not lines and alert.lines:
        return None
    return replace(alert, lines=lines)


@dataclass
class SendReport:
    """Wynik wysyłki (bez treści i adresów).

    Attributes:
        sent: Ile reguł wysłano.
        failed: Indeksy reguł, których wysyłka się nie udała.
    """

    sent: int = 0
    failed: list[int] | None = None


def send(
    alert: Alert,
    cfg: Config,
    sender: Sender,
    today: Any,
    extra: Callable[[Alert], None] | None = None,
) -> SendReport:
    """Wysyła powiadomienie wszystkimi pasującymi regułami.

    Args:
        alert: Powiadomienie.
        cfg: Konfiguracja (reguły, imiona dzieci).
        sender: Apprise albo zamiennik.
        today: Bieżąca data (etykiety dni).
        extra: Dodatkowy kanał (np. bot Telegrama z przyciskami), wołany raz.

    Returns:
        Raport: liczba wysłanych reguł i indeksy nieudanych.
    """
    report = SendReport(failed=[])
    for i, route in enumerate(cfg.alerts.routes):
        part = route_alert(alert, route)
        if part is None:
            continue
        try:
            ok = sender.notify(render(part, cfg, today), "librus-watch", route.tags)
        except Exception as e:  # awaria jednego kanału nie blokuje pozostałych
            log.warning("wysyłka reguły %d nieudana (%s)", i, type(e).__name__)
            ok = False
        if ok:
            report.sent += 1
        else:
            report.failed.append(i)  # type: ignore[union-attr]
            log.warning("wysyłka reguły %d nieudana", i)
    if extra is not None:
        try:
            extra(alert)
        except Exception as e:
            log.warning("dodatkowy kanał nieudany (%s)", type(e).__name__)
    log.info(
        "powiadomienie %s: %d pozycji, reguł wysłanych %d, nieudanych %d",
        alert.alert_type.value,
        len(alert.lines),
        report.sent,
        len(report.failed or []),
    )
    return report
