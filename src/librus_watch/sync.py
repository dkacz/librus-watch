"""Jeden cykl pobrania danych dziecka: API, wiadomości, adapter, zapis w bazie.

Moduły rzadko zmienne (tożsamość, klasa, plan stały, słowniki) są pobierane
raz na `RARE_MAX_AGE`; w pozostałych cyklach słowniki pochodzą z bazy. Moduły
potrzebne tylko typom z `ignore_content` nie są pobierane wcale (ADR-014).

W trybie `dry_run` nic nie trafia do bazy i nie jest pobierana treść
wiadomości (otwarcie mogłoby oznaczyć wiadomość jako przeczytaną).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from librus_watch.config import Config, ContentType
from librus_watch.errors import SchemaError, TransientError
from librus_watch.librus import messages
from librus_watch.librus.adapter import (
    LOOKUP_MODULES,
    adapt,
    lookups_from,
    modules_needed,
)
from librus_watch.librus.gateway import MODULES, fetch_all
from librus_watch.librus.session import LibrusSession
from librus_watch.models import TextItem
from librus_watch.store import Change, Store

log = logging.getLogger(__name__)

RARE_MODULES = frozenset(
    {
        "Me",
        "Classes",
        "TimetableEntries",
        "Subjects",
        "Users",
        "HomeWorkCategories",
        "NoteCategories",
    }
)
"""Moduły, które zmieniają się rzadko i są pobierane raz na dobę."""
RARE_MAX_AGE = timedelta(hours=24)


@dataclass
class SyncReport:
    """Liczby z jednego cyklu dziecka (bez treści, do logu i wyjścia polecenia).

    Attributes:
        child: Id dziecka.
        dry_run: Czy był to przebieg bez zapisu.
        records: Moduł -> liczba pobranych rekordów (wiadomości jako `messages`).
        not_fetched: Moduły pominięte (świeży cache albo typ ignorowany).
        unavailable: Moduły niedostępne dla konta.
        failed: Moduł -> klasa błędu.
        bad_records: Moduł -> liczba pominiętych błędnych rekordów.
        changes: Typ treści -> liczba nowych/zmienionych/bez zmian (bez dry-run).
        bodies: Liczba pobranych treści wiadomości.
    """

    child: str
    dry_run: bool
    records: dict[str, int] = field(default_factory=dict)
    not_fetched: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    bad_records: dict[str, int] = field(default_factory=dict)
    changes: dict[str, Counter[str]] = field(default_factory=dict)
    bodies: int = 0

    @property
    def ok(self) -> bool:
        """Czy cykl obył się bez błędów modułów."""
        return not self.failed

    def count(self, kind: str, change: Change) -> None:
        """Dolicza wynik zapisu rekordu."""
        self.changes.setdefault(kind, Counter())[change.value] += 1


def _modules_to_skip(cfg: Config, store: Store, child: str, now: datetime) -> set[str]:
    """Moduły, których w tym cyklu nie pobieramy.

    Args:
        cfg: Konfiguracja.
        store: Baza (stan modułów).
        child: Id dziecka.
        now: Bieżący moment.

    Returns:
        Nazwy modułów do pominięcia.
    """
    skip = {m.name for m in MODULES} - modules_needed(cfg.ignore_content)
    for module in RARE_MODULES - skip:
        # pusty słownik też jest wynikiem (np. szkoła bez kategorii uwag), więc
        # o świeżości decyduje moment pobrania, a nie zawartość słownika
        fetched = store.module_fetched_at(child, module)
        if fetched is not None and now - fetched < RARE_MAX_AGE:
            skip.add(module)
    return skip


def _timetable_days(today: date, weeks: int) -> list[date]:
    """Wszystkie dni pobranych tygodni planu."""
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(days=i) for i in range(7 * weeks)]


def _fetch_bodies(
    cfg: Config, session: LibrusSession, store: Store, child: str, today: date
) -> int:
    """Pobiera treść wiadomości z ostatnich `message_body_days` dni, które jej nie mają.

    Starsze wiadomości zostają bez treści: nie zmienią jutra, a otwarcie mogłoby
    oznaczyć je w Librusie jako przeczytane. Błąd jednej wiadomości (przejściowy
    albo zmiana strony) liczy się jako nieudana próba i nie przerywa pozostałych;
    `AuthError` przerywa.

    Returns:
        Liczba pobranych treści.
    """
    done = 0
    since = today - timedelta(days=cfg.fetch.message_body_days)
    pending = store.texts_without_body(child, ContentType.message, cfg.fetch.message_body_attempts)
    for item in (m for m in pending if m.sent >= since):
        try:
            body = messages.fetch_body(session.client, item)
        except (TransientError, SchemaError) as e:
            log.warning("wiadomość %s: treść niepobrana (%s)", item.key, type(e).__name__)
            store.set_body(item, None)
            continue
        store.set_body(item, body)
        done += 1
    return done


def sync_child(
    cfg: Config,
    child: str,
    session: LibrusSession,
    store: Store,
    *,
    now: datetime,
    dry_run: bool = False,
) -> SyncReport:
    """Pobiera, zamienia i zapisuje dane jednego dziecka.

    Args:
        cfg: Konfiguracja.
        child: Id dziecka.
        session: Zalogowana sesja tego dziecka.
        store: Baza.
        now: Bieżący moment (lokalny czas szkoły).
        dry_run: Bez zapisu i bez pobierania treści wiadomości.

    Returns:
        Raport z liczbami.

    Raises:
        AuthError: Gdy sesja przestała działać w trakcie.
    """
    today = now.date()
    report = SyncReport(child=child, dry_run=dry_run)
    skip = _modules_to_skip(cfg, store, child, now)
    report.not_fetched = sorted(skip)
    data = fetch_all(session, today, weeks=cfg.fetch.timetable_weeks, skip=frozenset(skip))
    report.records = {name: len(rs) for name, rs in data.records.items()}
    report.unavailable = data.unavailable
    report.failed = dict(data.failed)

    fresh = lookups_from(data)
    lookups = {kind: store.lookups(child, kind) for kind in LOOKUP_MODULES.values()} | fresh
    snap = adapt(
        child,
        data,
        lookups,
        ignore=cfg.ignore_content,
        today=today,
        timetable_days=_timetable_days(today, cfg.fetch.timetable_weeks),
    )
    report.bad_records = dict(snap.skipped)

    new_messages: list[TextItem] = []
    if ContentType.message not in cfg.ignore_content:
        try:
            new_messages = messages.list_new(
                session.client,
                child,
                lambda key: store.known_text(child, ContentType.message, key),
                cfg.fetch.max_message_pages,
            )
            report.records["messages"] = len(new_messages)
        except (TransientError, SchemaError) as e:
            report.failed["messages"] = type(e).__name__
            log.warning("wiadomości: %s", e)

    if dry_run:
        log.info("dziecko %s (dry-run): %d modułów", child, len(data.records))
        return report

    for kind, values in fresh.items():
        store.save_lookups(child, kind, values)
    for module in data.records:
        store.mark_module(child, module, now)
    if snap.lessons is not None:
        store.save_lessons(child, snap.lessons, now)
    for entry in snap.entries:
        report.count(f"entry:{entry.source.value}", store.upsert_entry(entry, now))
    for text in [*snap.texts, *new_messages]:
        report.count(text.source.value, store.upsert_text(text, now))
    for pc in snap.plan_changes:
        report.count(ContentType.plan_change.value, store.upsert_plan_change(pc, now))
    for fd in snap.free_days:
        report.count(ContentType.free_day.value, store.upsert_free_day(fd, now))
    if cfg.fetch.message_bodies and ContentType.message not in cfg.ignore_content:
        report.bodies = _fetch_bodies(cfg, session, store, child, today)

    new = sum(c[Change.new.value] + c[Change.changed.value] for c in report.changes.values())
    log.info(
        "dziecko %s: %d modułów, %d nowych lub zmienionych, błędy modułów: %d",
        child,
        len(data.records),
        new,
        len(report.failed),
    )
    return report
