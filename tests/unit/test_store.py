import logging
import stat
from collections.abc import Iterator
from datetime import date, datetime, time
from pathlib import Path

import pytest

from librus_watch.config import ContentType, Kind
from librus_watch.models import (
    Entry,
    Finding,
    FreeDay,
    Lesson,
    LessonStatus,
    Origin,
    PlanChange,
    PlanChangeKind,
    TextItem,
)
from librus_watch.store import _MIGRATIONS, SCHEMA_VERSION, Change, FetchState, Store

MON = date(2026, 11, 9)
NOW = datetime(2026, 11, 6, 18, 0)
LATER = datetime(2026, 11, 6, 20, 0)


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "data")
    yield s
    s.close()


def lesson(n: int, start: str, end: str, subject: str = "matematyka", **kw: object) -> Lesson:
    return Lesson(
        child="ania",
        day=MON,
        number=n,
        start=time.fromisoformat(start),
        end=time.fromisoformat(end),
        subject=subject,
        teacher="T. Testowa s. 12",
        **kw,  # type: ignore[arg-type]
    )


def finding(what: str = "zbiórka pod szkołą", at: str = "07:30", **kw: object) -> Finding:
    base: dict[str, object] = {
        "child": "ania",
        "day": MON,
        "kind": Kind.early_start,
        "what": what,
        "origin": Origin.llm,
        "source_key": "m1",
        "at": time.fromisoformat(at),
        "quote": "zbiórka 7:30",
    }
    base.update(kw)
    return Finding(**base)  # type: ignore[arg-type]


# --- bezpieczeństwo i schemat ------------------------------------------------


def test_data_dir_and_db_are_private(tmp_path: Path) -> None:
    data = tmp_path / "data"
    s = Store(data)
    s.set_meta("k", "v")
    assert mode(data) == 0o700
    assert mode(s.path) == 0o600
    for sidecar in data.glob("librus-watch.db-*"):
        assert mode(sidecar) == 0o600, sidecar.name
    s.close()


def test_existing_loose_permissions_are_tightened(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    Store(data).close()
    (data / "librus-watch.db").chmod(0o644)
    Store(data).close()
    assert mode(data) == 0o700
    assert mode(data / "librus-watch.db") == 0o600


def test_schema_version_and_reopen_keeps_data(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.set_meta("k", "v")
    s.close()
    s = Store(tmp_path)
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert s.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert s.get_meta("k") == "v"
    s.close()


def test_transaction_rolls_back_on_error(store: Store) -> None:
    with pytest.raises(RuntimeError), store.tx() as db:
        db.execute("INSERT INTO meta VALUES('a', '1')")
        raise RuntimeError
    assert store.get_meta("a") is None


def test_original_error_survives_when_sqlite_already_rolled_back(store: Store) -> None:
    with pytest.raises(ValueError, match="prawdziwa przyczyna"), store.tx() as db:
        db.execute("ROLLBACK")  # tak kończy się transakcja po np. SQLITE_FULL
        raise ValueError("prawdziwa przyczyna")
    assert not store.db.in_transaction


def test_open_logs_migration_and_loose_permissions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="librus_watch.store")
    data = tmp_path / "data"
    Store(data).close()
    assert "schemat bazy: wersja 0 -> 1" in caplog.text
    assert "schemat bazy: wersja 1 -> 2" in caplog.text
    assert "uprawnienia" not in caplog.text
    caplog.clear()
    (data / "librus-watch.db").chmod(0o644)
    Store(data).close()
    assert "uprawnienia librus-watch.db: 644 -> 600" in caplog.text
    assert "schemat bazy" not in caplog.text


def test_meta_upsert(store: Store) -> None:
    store.set_meta("evening", "2026-11-05")
    store.set_meta("evening", "2026-11-06")
    assert store.get_meta("evening") == "2026-11-06"


# --- stan pobierania ---------------------------------------------------------


def test_fetch_state_failures_reset_next_day(store: Store) -> None:
    blocked = datetime(2026, 11, 7, 6, 0)
    store.save_fetch_state(
        "ania",
        FetchState(day=date(2026, 11, 6), failures=3, blocked_until=blocked, last_error="Auth"),
    )
    same = store.fetch_state("ania", date(2026, 11, 6))
    assert (same.failures, same.blocked_until, same.last_error) == (3, blocked, "Auth")
    nxt = store.fetch_state("ania", date(2026, 11, 7))
    assert nxt.failures == 0
    assert nxt.blocked_until == blocked
    assert store.fetch_state("kuba", date(2026, 11, 6)) == FetchState(day=date(2026, 11, 6))


# --- plan lekcji i profil dnia -----------------------------------------------


def test_save_lessons_replaces_day_and_builds_profile(store: Store) -> None:
    first = [lesson(1, "08:00", "08:45"), lesson(2, "08:55", "09:40")]
    store.save_lessons("ania", {MON: first}, NOW)
    store.save_lessons(
        "ania",
        {
            MON: [
                lesson(1, "08:00", "08:45", status=LessonStatus.cancelled),
                lesson(2, "08:55", "09:40"),
                lesson(3, "09:50", "10:35", subject=""),
            ]
        },
        LATER,
    )
    got = store.lessons("ania", MON)
    assert [ls.number for ls in got] == [1, 2, 3]
    assert got[0].status is LessonStatus.cancelled
    assert store.has_timetable("ania", MON)
    assert not store.has_timetable("ania", date(2026, 11, 10))
    # profil opisuje plan (z odwołaną lekcją), bez okienka bez przedmiotu
    assert store.profiles("ania", MON.weekday(), date(2026, 12, 1), 5) == [
        (time(8, 0), time(9, 40), 2)
    ]


def test_profiles_are_newest_first_and_limited(store: Store) -> None:
    for weeks in range(4):
        day = date.fromordinal(MON.toordinal() - 7 * weeks)
        store.save_lessons("ania", {day: [lesson(1, f"0{8 - weeks % 2}:00", "12:00")]}, NOW)
    got = store.profiles("ania", 0, MON, 2)
    assert [p[0] for p in got] == [time(7, 0), time(8, 0)]


def test_day_without_lessons_has_empty_profile(store: Store) -> None:
    store.save_lessons("ania", {MON: []}, NOW)
    assert store.has_timetable("ania", MON)
    assert store.profiles("ania", 0, date(2026, 12, 1), 5) == [(None, None, 0)]


# --- terminarz: historia tylko przybywa (ADR-008) ----------------------------


def test_removed_entry_stays_in_history_and_can_return(store: Store) -> None:
    e = Entry("ania", ContentType.agenda, "s1", MON, "sprawdzian", subject="matematyka")
    store.upsert_entry(e, NOW)
    store.mark_entry_removed(e, LATER)
    assert store.entries("ania", ContentType.agenda) == []
    assert store.entries("ania", ContentType.agenda, active_only=False) == [e]
    moved = Entry("ania", ContentType.agenda, "s1", date(2026, 11, 11), "sprawdzian")
    store.upsert_entry(moved, LATER)
    assert store.entries("ania", ContentType.agenda) == [moved]
    assert store.entries("ania", ContentType.homework) == []


# --- wiadomości: treść pobierana raz -----------------------------------------


def text(key: str = "m1", **kw: object) -> TextItem:
    base: dict[str, object] = {
        "child": "ania",
        "source": ContentType.message,
        "key": key,
        "sent": MON,
        "author": "T. Testowa",
        "title": "Wycieczka",
    }
    base.update(kw)
    return TextItem(**base)  # type: ignore[arg-type]


def test_message_body_is_kept_and_attempts_count(store: Store) -> None:
    m = text()
    assert store.upsert_text(m, NOW) is Change.new
    assert store.upsert_text(m, LATER) is Change.same
    assert store.known_text("ania", ContentType.message, "m1")
    assert not store.known_text("ania", ContentType.notice, "m1")
    store.set_body(m, None)
    store.set_body(m, None)
    assert store.texts_without_body("ania", ContentType.message, max_attempts=3) == [m]
    assert store.texts_without_body("ania", ContentType.message, max_attempts=2) == []
    store.set_body(m, "zbiórka 7:30")
    assert store.texts_without_body("ania", ContentType.message, 3) == []
    assert store.upsert_text(m, LATER) is Change.same  # lista bez treści jej nie kasuje
    assert [x.body for x in store.texts_to_extract(3)] == ["zbiórka 7:30"]


def test_changed_text_returns_to_extraction_queue(store: Store) -> None:
    n = text("n1", source=ContentType.notice, body="W piątek apel.", valid_to=MON)
    store.upsert_text(n, NOW)
    store.mark_extracted(n, NOW)
    assert store.texts_to_extract(3) == []
    moved = text("n1", source=ContentType.notice, body="Apel w czwartek.", valid_to=MON)
    assert store.upsert_text(moved, LATER) is Change.changed
    assert store.texts_to_extract(3) == [moved]
    assert store.texts("ania", ContentType.notice) == [moved]
    assert store.texts("ania") == [moved]


def test_extract_queue_respects_attempts_and_success(store: Store) -> None:
    n = text("a1", source=ContentType.notice, author="Dyrekcja", title="Wolne", body="treść")
    store.upsert_text(n, NOW)
    store.mark_extracted(n, None)
    assert store.texts_to_extract(1) == []
    assert store.texts_to_extract(2) == [n]
    store.mark_extracted(n, LATER)
    assert store.texts_to_extract(5) == []


def test_entry_upsert_reports_changes(store: Store) -> None:
    e = Entry("ania", ContentType.agenda, "s1", MON, "sprawdzian")
    assert store.upsert_entry(e, NOW) is Change.new
    assert store.upsert_entry(e, LATER) is Change.same
    store.mark_entry_removed(e, LATER)
    assert store.upsert_entry(e, LATER) is Change.changed  # wrócił po usunięciu
    moved = Entry("ania", ContentType.agenda, "s1", date(2026, 11, 11), "sprawdzian")
    assert store.upsert_entry(moved, LATER) is Change.changed


def test_lessons_in_one_slot_for_groups_are_kept(store: Store) -> None:
    groups = [lesson(2, "08:55", "09:40", key="a"), lesson(2, "08:55", "09:40", key="b")]
    store.save_lessons("ania", {MON: groups}, NOW)
    assert [ls.key for ls in store.lessons("ania", MON)] == ["a", "b"]


# --- zmiany planu, dni wolne, słowniki, stan modułów -------------------------


def test_plan_changes_and_free_days_report_changes(store: Store) -> None:
    pc = PlanChange("ania", "1", PlanChangeKind.cancelled, MON, 3, subject="matematyka")
    assert store.upsert_plan_change(pc, NOW) is Change.new
    assert store.upsert_plan_change(pc, LATER) is Change.same
    sub = PlanChange(
        "ania",
        "1",
        PlanChangeKind.substitution,
        MON,
        3,
        subject="matematyka",
        new_day=MON,
        new_lesson_no=3,
        new_subject="przyroda",
        new_teacher="B. Zastępcza",
    )
    assert store.upsert_plan_change(sub, LATER) is Change.changed
    assert store.plan_changes("ania") == [sub]
    fd = FreeDay("ania", "class:5", "class", "", MON, MON, lesson_from=4, lesson_to=6)
    assert store.upsert_free_day(fd, NOW) is Change.new
    assert store.upsert_free_day(fd, LATER) is Change.same
    assert store.free_days("ania") == [fd]
    assert not fd.whole_day


def test_upsert_fp_rejects_unknown_table(store: Store) -> None:
    with pytest.raises(ValueError, match="nieznana tabela"):
        store._upsert_fp("sent; DROP TABLE texts", "ania", "1", "fp", {}, NOW)


def test_lookups_are_replaced_and_modules_tracked(store: Store) -> None:
    store.save_lookups("ania", "subjects", {"501": "Matematyka", "502": "Przyroda"})
    store.save_lookups("ania", "subjects", {"501": "Matematyka"})
    assert store.lookups("ania", "subjects") == {"501": "Matematyka"}
    assert store.lookups("kuba", "subjects") == {}
    assert store.module_fetched_at("ania", "Subjects") is None
    store.mark_module("ania", "Subjects", NOW)
    store.mark_module("ania", "Subjects", LATER)
    assert store.module_fetched_at("ania", "Subjects") == LATER


# --- migracja 1 -> 2 ---------------------------------------------------------


def test_migration_from_v1_keeps_data(tmp_path: Path) -> None:
    import sqlite3

    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    db = sqlite3.connect(data / "librus-watch.db")
    db.executescript(_MIGRATIONS[1] + "PRAGMA user_version=1;")
    db.executescript(
        "INSERT INTO messages(child, source, key, sent, author, title, body, extracted_at, "
        "first_seen) VALUES ('ania','message','m1','2026-11-05','A','Wycieczka','treść',"
        "'2026-11-05T19:00:00','2026-11-05T18:00:00');"
        "INSERT INTO messages(child, source, key, sent, author, title, first_seen) VALUES "
        "('ania','announcement','a1','2026-11-05','B','Apel','2026-11-05T18:00:00');"
        "INSERT INTO lessons VALUES ('ania','2026-11-09',1,'08:00','08:45','matematyka','T',"
        "'normal','','2026-11-05T18:00:00');"
        "INSERT INTO entries VALUES ('ania','schedule','s1','2026-11-12',NULL,'sprawdzian','',"
        "'','','','fp1','2026-11-05T18:00:00','2026-11-05T18:00:00',NULL);"
    )
    db.close()
    s = Store(data)
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2
    m1 = s.texts("ania", ContentType.message)[0]
    assert (m1.key, m1.body) == ("m1", "treść")
    assert [t.key for t in s.texts("ania", ContentType.notice)] == ["a1"]
    assert s.texts_to_extract(3) == []  # odczytana wiadomość nie wraca do modelu
    # pierwszy zapis po migracji przyjmuje odcisk bez oznaczania zmiany
    assert s.upsert_text(text("m1", author="A", sent=date(2026, 11, 5)), NOW) is Change.same
    assert [ls.number for ls in s.lessons("ania", MON)] == [1]
    assert [e.key for e in s.entries("ania", ContentType.agenda)] == ["s1"]
    s.close()


# --- odstępstwa, wersje i wysyłka --------------------------------------------


def test_finding_versions_change_and_retraction(store: Store) -> None:
    first = finding()
    assert store.add_finding(first, NOW)
    assert not store.add_finding(first, LATER)
    changed = finding(at="07:15")
    assert changed.identity == first.identity
    assert store.add_finding(changed, LATER)
    assert store.previous_version(changed) == first
    current = store.current_findings(MON, MON)
    assert [c.finding for c in current] == [changed]
    store.retract_identity(first.identity, LATER)
    assert store.current_findings(MON, MON) == []


def test_current_findings_uses_deadline_range(store: Store) -> None:
    f = finding(kind=Kind.consent, what="zgoda na wycieczkę", deadline=date(2026, 11, 7))
    store.add_finding(f, NOW)
    assert [c.finding for c in store.current_findings(date(2026, 11, 7), date(2026, 11, 7))] == [f]
    assert store.current_findings(date(2026, 11, 10), date(2026, 11, 12)) == []


def test_sent_tracks_last_version(store: Store) -> None:
    f = finding()
    assert store.sent_fp(f.identity) is None
    store.mark_sent(f, "evening", NOW)
    assert store.sent_fp(f.identity) == f.content_fp
    g = finding(at="07:15")
    store.mark_sent(g, "instant", LATER)
    assert store.sent_fp(f.identity) == g.content_fp


def test_purge_removes_only_old_data(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="librus_watch.store")
    old = date(2025, 9, 1)
    store.save_lessons("ania", {old: [lesson(1, "08:00", "08:45")], MON: []}, NOW)
    store.upsert_entry(Entry("ania", ContentType.homework, "h1", old, "zadanie"), NOW)
    store.upsert_text(text("m0", sent=old), NOW)
    store.upsert_text(text("n0", source=ContentType.notice, sent=old, valid_to=MON), NOW)
    store.upsert_plan_change(PlanChange("ania", "p0", PlanChangeKind.cancelled, old, 1), NOW)
    store.upsert_free_day(FreeDay("ania", "school:1", "school", "Wolne", old, old), NOW)
    store.add_finding(finding(day=old), NOW)
    store.add_finding(finding(), NOW)
    store.purge(date(2026, 1, 1))
    assert "usunięto 7 rekordów" in caplog.text
    assert not store.has_timetable("ania", old)
    assert store.has_timetable("ania", MON)
    assert store.entries("ania", ContentType.homework, active_only=False) == []
    assert not store.known_text("ania", ContentType.message, "m0")
    assert store.known_text("ania", ContentType.notice, "n0")  # ważne do MON
    assert store.plan_changes("ania") == [] and store.free_days("ania") == []
    assert [c.finding.day for c in store.current_findings(old, MON)] == [MON]
