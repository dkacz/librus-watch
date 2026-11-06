"""Polityka powiadomień (ADR-005, D6): co, kiedy i w jakiej formie wysłać.

* **wieczór**: sprawy na jutro i bliskie terminy zgód, płatności i rzeczy do
  przyniesienia; w trybie `only_if_unusual` nic, gdy jutro jest zwykłe;
* **rano**: tylko to, co na dziś zmieniło się albo pojawiło od wieczora;
* **od razu**: pilne (dziś i jutro, waga co najmniej `instant.min_priority`),
  nigdy w godzinach ciszy (wtedy czeka na poranne);
* **bez powtórek**: pozycja wychodzi, gdy jej wersja różni się od ostatnio
  wysłanej; zmiana wysłanej rzeczy wychodzi jako „ZMIANA: było… jest…”.

Jedno powiadomienie obejmuje wszystkie dzieci. Tekst powstaje z szablonu na
danych strukturalnych (ADR-003): model nie pisze treści dla rodzica.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from librus_watch.config import AlertType, Config, Kind, Priority
from librus_watch.models import Finding
from librus_watch.store import Store

WEEKDAYS = ("poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela")
DEADLINE_KINDS = (Kind.bring, Kind.consent, Kind.payment)
DEADLINE_LOOKAHEAD = 2

LABELS: dict[Kind, str] = {
    Kind.early_start: "wcześniej do szkoły",
    Kind.late_start: "później do szkoły",
    Kind.early_end: "wcześniejszy koniec",
    Kind.late_end: "późniejszy koniec",
    Kind.day_off: "dzień wolny",
    Kind.cancel: "odwołane",
    Kind.substitution: "zastępstwo",
    Kind.bring: "przynieść",
    Kind.consent: "zgoda",
    Kind.payment: "płatność",
    Kind.trip: "wycieczka",
    Kind.test: "sprawdzian",
    Kind.homework: "zadanie",
    Kind.change: "zmiana",
    Kind.meeting: "zebranie",
    Kind.grade: "ocena",
    Kind.other: "do zrobienia",
}


@dataclass(frozen=True)
class AlertLine:
    """Jedna pozycja w powiadomieniu.

    Attributes:
        finding: Pozycja.
        priority: Waga (z konfiguracji).
        before: Poprzednio wysłana wersja (dla „ZMIANA”) albo `None`.
    """

    finding: Finding
    priority: Priority
    before: Finding | None = None


@dataclass(frozen=True)
class Alert:
    """Powiadomienie gotowe do wysłania.

    Attributes:
        alert_type: Typ (wieczór, rano, od razu).
        title: Pierwsza linia.
        lines: Pozycje (pusta lista: „bez zmian” w trybie `always`).
    """

    alert_type: AlertType
    title: str
    lines: list[AlertLine]


def day_label(day: date, today: date) -> str:
    """Np. „jutro (piątek 7.11)” albo „środa 12.11”."""
    name = f"{WEEKDAYS[day.weekday()]} {day.day}.{day.month:02d}"
    if day == today:
        return f"dziś ({name})"
    if day == today + timedelta(days=1):
        return f"jutro ({name})"
    return name


def in_quiet_hours(cfg: Config, now: datetime) -> bool:
    """Czy trwają godziny ciszy."""
    quiet = cfg.alerts.quiet_hours
    return quiet is not None and quiet.contains(now.time())


def _pending(store: Store, cfg: Config, day_from: date, day_to: date) -> list[AlertLine]:
    """Pozycje w zakresie, których bieżąca wersja nie była jeszcze wysłana."""
    out: list[AlertLine] = []
    for stored in store.current_findings(day_from, day_to):
        f = stored.finding
        sent = store.sent_fp(f.identity)
        if sent == f.content_fp:
            continue
        before = store.previous_version(f) if sent is not None else None
        out.append(AlertLine(f, cfg.alerts.priority_of(f.kind), before))
    return out


def compose(cfg: Config, store: Store, now: datetime, alert_type: AlertType) -> Alert | None:
    """Składa powiadomienie danego typu albo `None`, gdy nie ma czego wysłać.

    Args:
        cfg: Konfiguracja.
        store: Baza (pozycje i stan wysyłki).
        now: Bieżący moment.
        alert_type: `evening`, `morning` albo `instant`.

    Returns:
        Powiadomienie albo `None`.
    """
    today = now.date()
    tomorrow = today + timedelta(days=1)
    a = cfg.alerts
    if alert_type is AlertType.evening:
        lines = [
            ln
            for ln in _pending(store, cfg, tomorrow, today + timedelta(days=DEADLINE_LOOKAHEAD))
            if ln.finding.day == tomorrow
            or (
                ln.finding.kind in DEADLINE_KINDS
                and ln.finding.action_day <= tomorrow + timedelta(days=DEADLINE_LOOKAHEAD - 1)
            )
        ]
        if not lines and a.evening.mode == "only_if_unusual":
            return None
        return Alert(alert_type, f"Jutro: {day_label(tomorrow, today)}", lines)
    if alert_type is AlertType.morning:
        lines = [ln for ln in _pending(store, cfg, today, today) if ln.finding.action_day == today]
        if not lines and a.morning.mode == "only_if_unusual":
            return None
        return Alert(alert_type, f"Dziś: {day_label(today, today)}", lines)
    if alert_type is AlertType.instant:
        if not a.instant.enabled or in_quiet_hours(cfg, now):
            return None
        last = today + timedelta(days=a.instant.lookahead_days)
        lines = [
            ln
            for ln in _pending(store, cfg, today, last)
            if ln.priority.rank >= a.instant.min_priority.rank and ln.finding.action_day <= last
        ]
        return Alert(alert_type, "Pilne", lines) if lines else None
    return None


def render(alert: Alert, cfg: Config, today: date) -> str:
    """Tekst powiadomienia (zwykły tekst, bez formatowania).

    Args:
        alert: Powiadomienie.
        cfg: Konfiguracja (imiona dzieci).
        today: Bieżąca data.

    Returns:
        Tekst: tytuł, pozycje pogrupowane po dniach i dzieciach, cytaty.
    """
    names = {c.id: c.name for c in cfg.children}
    out = [alert.title]
    if not alert.lines:
        out.append("bez zmian")
        return "\n".join(out)
    lines = sorted(
        alert.lines,
        key=lambda ln: (
            ln.finding.action_day,
            ln.finding.child,
            ln.finding.at or datetime.min.time(),
        ),
    )
    current: tuple[date, str] | None = None
    for ln in lines:
        f = ln.finding
        group = (f.action_day, f.child)
        if group != current:
            out.append(f"\n{names.get(f.child, f.child)}, {day_label(f.action_day, today)}:")
            current = group
        out.append("• " + line_text(ln))
        if f.quote:
            out.append(f"  „{f.quote}”")
    return "\n".join(out)


def line_text(ln: AlertLine) -> str:
    """Jedna linia pozycji: rodzaj, opis, godzina, termin, ewentualna ZMIANA."""
    f = ln.finding
    at = f" {f.at:%H:%M}" if f.at else ""
    deadline = f" (do {f.deadline.day}.{f.deadline.month:02d})" if f.deadline else ""
    text = f"{LABELS.get(f.kind, f.kind.value)}: {f.what}{at}{deadline}"
    if f.before:
        text += f" (zwykle {f.before})"
    if ln.before is not None:
        b = ln.before
        was = f"{b.day.day}.{b.day.month:02d}" + (f" {b.at:%H:%M}" if b.at else "")
        text = f"ZMIANA: {text}; było: {was} {b.what}"
    return text


def mark_alert_sent(alert: Alert, store: Store, now: datetime) -> None:
    """Zapisuje, że pozycje powiadomienia zostały wysłane (w tej wersji)."""
    for ln in alert.lines:
        store.mark_sent(ln.finding, alert.alert_type.value, now)
