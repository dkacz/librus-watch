"""Polecenia programu (`librus-watch ...`).

Na razie `fetch`: jeden cykl pobrania dla wszystkich dzieci i raport liczb.
Pełny zestaw poleceń (usługa, podsumowania, test powiadomień) powstaje w E7.

Kody wyjścia: 0 sukces, 1 błąd pobierania (moduł albo dziecko), 2 błąd
konfiguracji lub sekretów, 3 inna instancja używa katalogu danych.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from librus_watch.alerts import Alert, AlertLine, compose, mark_alert_sent, render
from librus_watch.config import AlertType, Config, ConfigError, Kind, child_credentials, load_config
from librus_watch.errors import AuthError, TransientError
from librus_watch.lock import AlreadyRunning, InstanceLock
from librus_watch.models import Finding, Origin
from librus_watch.notify import AppriseSender, Sender, send
from librus_watch.secret_env import MissingSecretError, get_secret, setup_logging
from librus_watch.store import Store
from librus_watch.sync import LoginBlocked, SyncReport, login_child, sync_child
from librus_watch.telegram import chat_ids, http_call, process_updates, send_alert

log = logging.getLogger(__name__)

EXIT_OK, EXIT_FETCH, EXIT_CONFIG, EXIT_RUNNING = 0, 1, 2, 3
MAX_FINDINGS = 25
"""Ile odstępstw wypisać w raporcie (reszta jako liczba)."""


def _parser() -> argparse.ArgumentParser:
    """Parser argumentów wiersza poleceń."""
    parser = argparse.ArgumentParser(prog="librus-watch")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("LIBRUS_WATCH_CONFIG", "config.yaml")),
        help="plik konfiguracji (domyślnie $LIBRUS_WATCH_CONFIG albo config.yaml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch", help="jeden cykl pobrania danych i raport liczb")
    fetch.add_argument("--dry-run", action="store_true", help="bez zapisu w bazie")
    fetch.add_argument("--child", help="tylko to dziecko (id z konfiguracji)")
    fetch.add_argument(
        "--force",
        action="store_true",
        help="loguj mimo blokady po błędzie logowania i dziennego limitu (ostrożnie)",
    )
    brief = sub.add_parser("brief", help="złóż i wyślij powiadomienie (wieczór, rano, pilne)")
    brief.add_argument("kind", choices=[a.value for a in AlertType])
    brief.add_argument("--dry-run", action="store_true", help="tylko wypisz, nie wysyłaj")
    sub.add_parser("test-notify", help="wyślij powiadomienie testowe z wymyśloną pozycją")
    sub.add_parser("telegram-poll", help="odbierz oceny i zgłoszenia z bota Telegrama")
    return parser


def _channels(cfg: Config) -> tuple[Sender | None, Any]:
    """Kanały: Apprise (gdy jest `APPRISE_CONFIG`) i bot Telegrama (gdy jest token)."""
    sender: Sender | None = None
    raw = get_secret("APPRISE_CONFIG", required=False)
    if raw is not None:
        sender = AppriseSender(raw.get_secret_value())
    token = get_secret("TELEGRAM_BOT_TOKEN", required=False)
    bot = (http_call(token.get_secret_value()), chat_ids()) if token is not None else None
    return sender, bot


def _deliver(alert: Alert, cfg: Config, today: date) -> bool:
    """Wysyła powiadomienie wszystkimi kanałami; `True`, gdy któryś zadziałał."""
    sender, bot = _channels(cfg)
    ok = False
    if sender is not None:
        ok = send(alert, cfg, sender, today).sent > 0
    if bot is not None:
        call, chats = bot
        ok = send_alert(alert, cfg, call, chats, today) > 0 or ok
    if sender is None and bot is None:
        print("brak kanałów: ustaw APPRISE_CONFIG albo TELEGRAM_BOT_TOKEN", file=sys.stderr)
    return ok


def cmd_brief(args: argparse.Namespace, cfg: Config) -> int:
    """Polecenie `brief`: składa powiadomienie i wysyła je (albo tylko wypisuje)."""
    now = datetime.now(ZoneInfo(cfg.timezone)).replace(tzinfo=None)
    with InstanceLock(cfg.data_dir):
        store = Store(cfg.data_dir)
        try:
            alert = compose(cfg, store, now, AlertType(args.kind))
            if alert is None:
                print("nic do wysłania")
                return EXIT_OK
            if args.dry_run:
                print(render(alert, cfg, now.date()))
                return EXIT_OK
            if not _deliver(alert, cfg, now.date()):
                return EXIT_FETCH
            mark_alert_sent(alert, store, now)
            print(f"wysłane: {len(alert.lines)} pozycji")
            return EXIT_OK
        finally:
            store.close()


def cmd_test_notify(cfg: Config) -> int:
    """Polecenie `test-notify`: powiadomienie z wymyśloną pozycją (nic nie zapisuje)."""
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    child = cfg.children[0].id
    sample = Finding(
        child,
        today + timedelta(days=1),
        Kind.early_start,
        "zbiórka (TEST)",
        Origin.llm,
        "test:1",
        at=time(7, 30),
        quote="Jutro zbiórka o 7:30 (test)",
    )
    alert = Alert(
        AlertType.instant,
        "librus-watch: powiadomienie testowe",
        [AlertLine(sample, cfg.alerts.priority_of(sample.kind))],
    )
    return EXIT_OK if _deliver(alert, cfg, today) else EXIT_FETCH


def cmd_telegram_poll(cfg: Config) -> int:
    """Polecenie `telegram-poll`: odbiera kliknięcia i `/brak`, zapisuje oceny."""
    token = get_secret("TELEGRAM_BOT_TOKEN")
    if token is None:
        return EXIT_CONFIG
    now = datetime.now(ZoneInfo(cfg.timezone)).replace(tzinfo=None)
    with InstanceLock(cfg.data_dir):
        store = Store(cfg.data_dir)
        try:
            n = process_updates(store, http_call(token.get_secret_value()), chat_ids(), now)
        finally:
            store.close()
    print(f"oceny i zgłoszenia: {n}")
    return EXIT_OK


def format_report(r: SyncReport) -> str:
    """Raport cyklu w kilku liniach, bez treści z Librusa.

    Args:
        r: Raport dziecka.

    Returns:
        Tekst raportu.
    """
    lines = [f"{r.child}{' (dry-run)' if r.dry_run else ''}:"]
    lines.append("  rekordy: " + ", ".join(f"{k}={v}" for k, v in sorted(r.records.items())))
    if r.not_fetched:
        lines.append("  niepobrane (cache lub ignorowane): " + ", ".join(r.not_fetched))
    if r.unavailable:
        lines.append("  niedostępne: " + ", ".join(r.unavailable))
    if r.failed:
        lines.append("  błędy: " + ", ".join(f"{k} ({v})" for k, v in sorted(r.failed.items())))
    if r.bad_records:
        lines.append(
            "  pominięte rekordy: "
            + ", ".join(f"{k}={v}" for k, v in sorted(r.bad_records.items()))
        )
    for kind, counter in sorted(r.changes.items()):
        parts = ", ".join(f"{k}={counter[k]}" for k in ("new", "changed", "same") if counter[k])
        lines.append(f"  {kind}: {parts}")
    if r.bodies:
        lines.append(f"  pobrane treści wiadomości: {r.bodies}")
    if r.removed:
        lines.append(f"  usunięte wpisy: {r.removed}")
    if r.reading is not None:
        p = r.reading
        dropped = ", ".join(f"{k}={v}" for k, v in sorted(p.dropped.items())) or "0"
        lines.append(
            f"  do odczytu przez model: {len(p.queue)} w kolejce (pilne {p.urgent}), "
            f"z cache {len(p.cached)}, odłożone {p.deferred}, bez treści {p.waiting_body}; "
            f"sito odrzuciło: {dropped}"
        )
    if r.llm is not None:
        m = r.llm
        rejected = sum(m.rejected.values())
        lines.append(
            f"  model: przeczytane {m.read}, z cache {m.from_cache}, złe odpowiedzi {m.failed}, "
            f"odrzucone pozycje {rejected}, zapytania {m.requests}, tokeny {m.tokens}"
            + (f"; przerwane: {m.stopped}" if m.stopped else "")
        )
    label = "odstępstwa" if r.dry_run else "nowe odstępstwa"
    lines.append(
        f"  {label}: {len(r.findings)}" + (f", wycofane: {r.retracted}" if r.retracted else "")
    )
    for f in sorted(r.findings, key=lambda f: (f.day, f.kind.value))[:MAX_FINDINGS]:
        at = f" {f.at:%H:%M}" if f.at else ""
        lines.append(f"    {f.day:%Y-%m-%d}{at} {f.kind.value} ({f.origin.value})")
    if len(r.findings) > MAX_FINDINGS:
        lines.append(f"    … i jeszcze {len(r.findings) - MAX_FINDINGS}")
    return "\n".join(lines)


def cmd_fetch(args: argparse.Namespace) -> int:
    """Polecenie `fetch`.

    Args:
        args: Argumenty z parsera.

    Returns:
        Kod wyjścia.
    """
    try:
        cfg = load_config(args.config)
    except (ConfigError, ValidationError, ValueError) as e:
        print(f"błąd konfiguracji: {e}", file=sys.stderr)
        return EXIT_CONFIG
    setup_logging(cfg.log_level)
    children = [c for c in cfg.children if args.child in (None, c.id)]
    if not children:
        print(f"nieznane dziecko: {args.child}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        creds = {c.id: child_credentials(c) for c in children}
    except MissingSecretError as e:
        print(f"błąd sekretów: {e}", file=sys.stderr)
        return EXIT_CONFIG

    code = EXIT_OK
    try:
        with InstanceLock(cfg.data_dir):
            store = Store(cfg.data_dir)
            try:
                for child in children:
                    now = datetime.now(ZoneInfo(cfg.timezone)).replace(tzinfo=None)
                    try:
                        session = login_child(
                            cfg, store, child.id, creds[child.id], now, force=args.force
                        )
                        report = sync_child(
                            cfg, child.id, session, store, now=now, dry_run=args.dry_run
                        )
                    except LoginBlocked as e:
                        log.warning("dziecko %s: pominięte, %s", child.id, e)
                        print(f"{child.id}: pominięte ({e})")
                        code = EXIT_FETCH
                        continue
                    except (AuthError, TransientError) as e:
                        log.error("dziecko %s: %s (%s)", child.id, e, type(e).__name__)
                        print(f"{child.id}: błąd ({type(e).__name__})")
                        code = EXIT_FETCH
                        continue
                    print(format_report(report))
                    if not report.ok:
                        code = EXIT_FETCH
            finally:
                store.close()
    except AlreadyRunning as e:
        print(str(e), file=sys.stderr)
        return EXIT_RUNNING
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """Punkt wejścia `librus-watch`.

    Args:
        argv: Argumenty (domyślnie z `sys.argv`).

    Returns:
        Kod wyjścia.
    """
    args = _parser().parse_args(argv)
    if args.command == "fetch":
        return cmd_fetch(args)
    try:
        cfg = load_config(args.config)
    except (ConfigError, ValidationError, ValueError) as e:
        print(f"błąd konfiguracji: {e}", file=sys.stderr)
        return EXIT_CONFIG
    setup_logging(cfg.log_level)
    try:
        if args.command == "brief":
            return cmd_brief(args, cfg)
        if args.command == "test-notify":
            return cmd_test_notify(cfg)
        if args.command == "telegram-poll":
            return cmd_telegram_poll(cfg)
    except MissingSecretError as e:
        print(f"błąd sekretów: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except AlreadyRunning as e:
        print(str(e), file=sys.stderr)
        return EXIT_RUNNING
    return EXIT_CONFIG  # pragma: no cover - argparse wymusza znane polecenie


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
