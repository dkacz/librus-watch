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
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from librus_watch.baseline import bells_from, day_norm
from librus_watch.config import Config, ContentType, Credentials, llm_api_key
from librus_watch.deviations import day_findings, dedupe, entry_findings, plan_change_findings
from librus_watch.errors import AuthError, SchemaError, TransientError
from librus_watch.extract import ChatClient, ReadReport, make_client, pick_model, read_queue
from librus_watch.feedback import examples_for
from librus_watch.librus import messages
from librus_watch.librus.adapter import (
    LOOKUP_MODULES,
    Snapshot,
    adapt,
    lookups_from,
    modules_needed,
)
from librus_watch.librus.gateway import MODULES, GatewayData, fetch_all
from librus_watch.librus.session import LibrusSession
from librus_watch.models import Entry, Finding, Origin, TextItem
from librus_watch.reading import ReadingPlan, plan_reading
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


class LoginBlocked(RuntimeError):
    """Logowanie tego dziecka jest wstrzymane (blokada po błędzie albo dzienny limit)."""


def login_child(
    cfg: Config,
    store: Store,
    child: str,
    creds: Credentials,
    now: datetime,
    *,
    force: bool = False,
) -> LibrusSession:
    """Loguje dziecko z ochroną konta (ADR-007, ADR-015).

    Logowanie jest liczone przed próbą, więc limit działa także przy awariach.
    Błąd logowania (`AuthError`, w tym możliwa captcha) wstrzymuje próby do
    początku aktywnych godzin następnego dnia. Stan ochrony i `DeviceCookie`
    są zapisywane zawsze, także w przebiegu bez zapisu danych.

    Args:
        cfg: Konfiguracja.
        store: Baza (stan pobierania, `DeviceCookie`).
        child: Id dziecka.
        creds: Login i hasło.
        now: Bieżący moment.
        force: Pomija blokadę i limit (świadoma decyzja użytkownika).

    Returns:
        Zalogowana sesja.

    Raises:
        LoginBlocked: Gdy logowanie jest wstrzymane albo limit wyczerpany.
        AuthError: Logowanie odrzucone (blokada zapisana).
        TransientError: Przejściowy problem (przerwę ustala przebieg).
    """
    st = store.fetch_state(child, now.date())
    if not force:
        if st.blocked_until is not None and now < st.blocked_until:
            raise LoginBlocked(
                f"logowanie wstrzymane do {st.blocked_until:%Y-%m-%d %H:%M} ({st.last_error})"
            )
        if st.logins >= cfg.fetch.max_logins_per_day:
            raise LoginBlocked(f"dzienny limit logowań ({cfg.fetch.max_logins_per_day})")
    elif st.blocked_until is not None or st.logins >= cfg.fetch.max_logins_per_day:
        log.warning("dziecko %s: logowanie mimo blokady lub limitu (--force)", child)
    st = replace(st, logins=st.logins + 1)
    store.save_fetch_state(child, st)
    meta_key = f"device_cookie:{child}"
    device = store.get_meta(meta_key)
    try:
        session = LibrusSession.login(
            child, creds, cfg.fetch.http_timeout_seconds, device_cookie=device
        )
    except AuthError:
        until = datetime.combine(now.date() + timedelta(days=1), cfg.fetch.active_hours.start)
        store.save_fetch_state(
            child,
            replace(st, failures=st.failures + 1, blocked_until=until, last_error="AuthError"),
        )
        log.error("dziecko %s: logowanie wstrzymane do %s", child, f"{until:%Y-%m-%d %H:%M}")
        raise
    except TransientError:
        # licznik porażek i przerwę po błędach przejściowych prowadzi przebieg (scheduler)
        store.save_fetch_state(child, replace(st, last_error="TransientError"))
        raise
    if session.device_cookie and session.device_cookie != device:
        store.set_meta(meta_key, session.device_cookie)
    store.save_fetch_state(
        child, replace(st, blocked_until=None, last_error=None, last_success=now)
    )
    return session


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
        findings: Odstępstwa: w dry-run wszystkie wykryte, w zwykłym cyklu nowe wersje.
        retracted: Ile wcześniejszych odstępstw wycofano (już nieaktualne).
        removed: Ile wpisów zniknęło z kompletnie pobranych modułów (ADR-008).
        reading: Plan odczytu tekstów przez model (sito, cache, kolejka, budżet).
        llm: Wynik odczytu przez model (gdy włączony i nie dry-run).
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
    findings: list[Finding] = field(default_factory=list)
    retracted: int = 0
    removed: int = 0
    reading: ReadingPlan | None = None
    llm: ReadReport | None = None

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


ENTRY_MODULE: dict[ContentType, str] = {
    ContentType.agenda: "HomeWorks",
    ContentType.homework: "HomeWorkAssignments",
    ContentType.conference: "ParentTeacherConferences",
    ContentType.trip: "SchoolTrips",
    ContentType.activity: "OtherActivities",
    ContentType.grade: "Grades",
}
"""Moduł, z którego pochodzi wpis danego typu (do wykrywania usuniętych)."""


@dataclass
class _Detection:
    """Wynik wykrywania odstępstw jednego cyklu (przed zapisem)."""

    findings: list[Finding]
    removed: list[Entry]
    origins: set[Origin]
    day_to: date


def _detect(
    cfg: Config, store: Store, child: str, data: GatewayData, snap: Snapshot, today: date
) -> _Detection:
    """Wylicza odstępstwa z danych cyklu i stanu bazy (bez zapisu).

    Odstępstwa dnia powstają tylko, gdy plan lekcji pobrał się w tym cyklu;
    odstępstwa ze zmian planu, gdy pobrała się historia zmian. Tylko dla tych
    źródeł wolno potem wycofać wcześniejsze pozycje. Wpis uznajemy za usunięty
    wyłącznie wtedy, gdy jego moduł pobrał się w całości, a dla zajęć
    dodatkowych dodatkowo, gdy jego dzień leży w pobranym zakresie (ADR-008).
    """
    days = _timetable_days(today, cfg.fetch.timetable_weeks)
    day_to = days[-1]
    ignore = cfg.ignore_content
    origins: set[Origin] = set()
    standing = snap.standing if snap.standing is not None else store.standing(child)
    if ContentType.free_day in ignore:
        free_days = []
    elif {"ClassFreeDays", "SchoolFreeDays"} <= data.records.keys():
        free_days = snap.free_days
    else:
        free_days = store.free_days(child)
    if ContentType.plan_change in ignore:
        plan_changes, plan_ok = [], True
    elif "Substitutions" in data.records:
        plan_changes, plan_ok = snap.plan_changes, True
    else:
        plan_changes, plan_ok = store.plan_changes(child), False

    findings: list[Finding] = []
    if snap.lessons is not None:
        origins.add(Origin.timetable)
        bells = store.bell_times(child) | bells_from(
            ls for lessons in snap.lessons.values() for ls in lessons
        )
        for day in sorted(d for d in snap.lessons if d >= today):
            history = store.profiles(child, day.weekday(), day, 8)
            norm = day_norm(day, standing, bells, history)
            findings += day_findings(child, day, snap.lessons[day], norm, free_days, plan_changes)
    if plan_ok:
        origins.add(Origin.plan_change)
        findings += plan_change_findings(plan_changes, today, day_to)

    old_entries = store.all_entries(child)
    old = {(e.source, e.key): e for e in old_entries}
    new_keys = {(e.source, e.key) for e in snap.entries}
    removed = [
        e
        for e in old_entries
        if e.source not in ignore
        and ENTRY_MODULE.get(e.source) in data.records
        and (e.source, e.key) not in new_keys
        and (e.source is not ContentType.activity or days[0] <= e.day <= day_to)
    ]
    findings += entry_findings(old, snap.entries, removed, today)
    return _Detection(dedupe(findings), removed, origins, day_to)


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
    llm_client: ChatClient | None = None,
) -> SyncReport:
    """Pobiera, zamienia i zapisuje dane jednego dziecka.

    Args:
        cfg: Konfiguracja.
        child: Id dziecka.
        session: Zalogowana sesja tego dziecka.
        store: Baza.
        now: Bieżący moment (lokalny czas szkoły).
        dry_run: Bez zapisu, bez pobierania treści wiadomości i bez modelu.
        llm_client: Klient modelu (domyślnie z konfiguracji; podmieniany w testach).

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

    detection = _detect(cfg, store, child, data, snap, today)
    report.removed = len(detection.removed)

    if dry_run:
        report.findings = detection.findings
        report.reading = plan_reading(
            [*snap.texts, *new_messages], store, cfg.llm, today, now, dry_run=True
        )
        log.info(
            "dziecko %s (dry-run): %d modułów, %d odstępstw",
            child,
            len(data.records),
            len(detection.findings),
        )
        return report

    for kind, values in fresh.items():
        store.save_lookups(child, kind, values)
    for module in data.records:
        store.mark_module(child, module, now)
    if snap.lessons is not None:
        store.save_lessons(child, snap.lessons, now)
    if snap.standing is not None:
        store.save_standing(child, snap.standing)
    for entry in snap.entries:
        report.count(f"entry:{entry.source.value}", store.upsert_entry(entry, now))
    for entry in detection.removed:
        store.mark_entry_removed(entry, now)
    for text in [*snap.texts, *new_messages]:
        report.count(text.source.value, store.upsert_text(text, now))
    for pc in snap.plan_changes:
        report.count(ContentType.plan_change.value, store.upsert_plan_change(pc, now))
    for fd in snap.free_days:
        report.count(ContentType.free_day.value, store.upsert_free_day(fd, now))
    if cfg.fetch.message_bodies and ContentType.message not in cfg.ignore_content:
        report.bodies = _fetch_bodies(cfg, session, store, child, today)

    report.reading = plan_reading(
        store.texts_to_extract(cfg.llm.max_attempts_per_message, child),
        store,
        cfg.llm,
        today,
        now,
    )
    found = list(detection.findings)
    if cfg.llm.enabled and (report.reading.queue or report.reading.cached):
        if llm_client is None:
            key = llm_api_key(cfg.llm)
            llm_client = make_client(cfg.llm, key.get_secret_value() if key else None)
        llm_cfg = cfg.llm.model_copy(update={"model": pick_model(cfg.llm)})
        names = list(store.lookups(child, "users").values()) if llm_cfg.pseudonymize else []
        report.llm = read_queue(
            report.reading,
            store,
            llm_cfg,
            now,
            llm_client,
            names=names,
            examples=lambda source: examples_for(cfg, store, source),
        )
        found += report.llm.findings
    report.findings = [f for f in found if store.add_finding(f, now)]
    current_ids = {f.identity for f in detection.findings}
    for old_finding in store.child_findings(child, detection.origins, today, detection.day_to):
        if old_finding.identity not in current_ids:
            store.retract_identity(old_finding.identity, now)
            report.retracted += 1

    new = sum(c[Change.new.value] + c[Change.changed.value] for c in report.changes.values())
    log.info(
        "dziecko %s: %d modułów, %d nowych lub zmienionych, odstępstwa: %d nowych, "
        "%d wycofanych, błędy modułów: %d",
        child,
        len(data.records),
        new,
        len(report.findings),
        report.retracted,
        len(report.failed),
    )
    return report
