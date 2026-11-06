from datetime import date, time, timedelta
from typing import Any

import pytest
from fixtures.gateway import payload

from librus_watch.config import ContentType
from librus_watch.librus.adapter import (
    ALWAYS_MODULES,
    Snapshot,
    adapt,
    lookups_from,
    modules_needed,
)
from librus_watch.librus.gateway import MODULES, GatewayData, fetch_all
from librus_watch.models import LessonStatus, PlanChangeKind

TODAY = date(2026, 11, 6)
MONDAY = date(2026, 11, 2)
DAYS = [MONDAY + timedelta(days=i) for i in range(14)]
C = ContentType


class Source:
    def __init__(self, overrides: dict[str, Any] | None = None) -> None:
        self.overrides = overrides or {}

    def get_json(self, path: str) -> Any:
        return self.overrides.get(path.partition("?")[0]) or payload(path)


def run(
    ignore: frozenset[ContentType] = frozenset({C.grade}),
    overrides: dict[str, Any] | None = None,
) -> tuple[Snapshot, GatewayData]:
    data = fetch_all(Source(overrides), TODAY)
    return adapt(
        "ania", data, lookups_from(data), ignore=ignore, today=TODAY, timetable_days=DAYS
    ), data


def by_source(snap: Snapshot) -> dict[ContentType, list[Any]]:
    out: dict[ContentType, list[Any]] = {}
    for item in [*snap.entries, *snap.texts]:
        out.setdefault(item.source, []).append(item)
    return out


# --- moduły i słowniki -------------------------------------------------------


def test_default_needs_every_module_because_comments_need_grades() -> None:
    assert modules_needed(frozenset({C.grade})) == {m.name for m in MODULES}


@pytest.mark.parametrize(
    ("ignore", "dropped"),
    [
        ({C.grade, C.grade_comment}, {"Grades", "GradeComments"}),
        ({C.grade_comment}, {"GradeComments"}),
        ({C.plan_change}, {"Substitutions"}),
        ({C.free_day, C.note}, {"ClassFreeDays", "SchoolFreeDays", "Notes", "NoteCategories"}),
    ],
)
def test_ignored_types_drop_their_modules(ignore: set[ContentType], dropped: set[str]) -> None:
    needed = modules_needed(frozenset(ignore))
    assert {m.name for m in MODULES} - needed == dropped
    assert needed >= ALWAYS_MODULES


def test_lookups_from_fetched_modules() -> None:
    lk = lookups_from(fetch_all(Source(), TODAY))
    assert lk["subjects"] == {"501": "Matematyka"}
    assert lk["users"] == {"1001": "Anna Testowa"}
    assert lk["homework_categories"] == {"4": "Wycieczka"}
    assert lk["class"] == {"id": "7"}
    assert lk["note_categories"] == {}


# --- plan, zmiany planu, dni wolne ---------------------------------------------


def test_lessons_cover_all_days_and_map_statuses() -> None:
    snap, _ = run()
    assert snap.lessons is not None and set(snap.lessons) == set(DAYS)
    assert snap.lessons[date(2026, 11, 4)] == []  # dzień bez lekcji w API
    first = snap.lessons[MONDAY][0]
    assert (first.number, first.start, first.end) == (1, time(8, 0), time(8, 45))
    assert (first.subject, first.teacher, first.key) == ("Matematyka", "Anna Testowa", "41999")
    assert snap.lessons[date(2026, 11, 10)][0].status is LessonStatus.cancelled


def test_failed_timetable_leaves_lessons_unknown() -> None:
    snap, data = run(overrides={"Timetables": {"Zly": 1}})
    assert snap.lessons is None and data.failed == {"Timetables": "SchemaError"}


def test_plan_changes_and_free_days_of_own_class_only() -> None:
    snap, _ = run()
    kinds = [(pc.key, pc.kind) for pc in snap.plan_changes]
    assert kinds == [("1", PlanChangeKind.cancelled), ("2", PlanChangeKind.substitution)]
    assert snap.plan_changes[0].subject == "Matematyka" and snap.plan_changes[0].org_lesson_no == 3
    assert [(f.key, f.whole_day) for f in snap.free_days] == [
        ("school:1", True),
        ("class:5", False),
    ]
    assert snap.free_days[1].lesson_from == 4


def test_class_free_days_skipped_when_class_unknown(caplog: pytest.LogCaptureFixture) -> None:
    data = fetch_all(Source(), TODAY)
    lk = lookups_from(data)
    del lk["class"]
    snap = adapt("ania", data, lk, ignore=frozenset(), today=TODAY)
    assert [f.scope for f in snap.free_days] == ["school"]
    assert "nieznana klasa" in caplog.text


# --- wpisy, teksty i ignorowanie (ADR-014) -------------------------------------


def test_entries_and_texts_from_every_text_module() -> None:
    snap, _ = run()
    got = by_source(snap)
    agenda_entry, agenda_text = got[C.agenda]
    assert (agenda_entry.day, agenda_entry.at, agenda_entry.title) == (
        date(2026, 11, 18),
        time(7, 30),
        "Wycieczka",
    )
    assert (
        agenda_text.body.startswith("Wycieczka do muzeum")
        and agenda_text.title == "Wycieczka · Matematyka"
    )
    assert agenda_text.sent == date(2026, 11, 5)
    hw_entry, hw_text = got[C.homework]
    assert hw_entry.day == hw_text.valid_to == date(2026, 11, 23)
    notice = got[C.notice][0]
    assert (notice.key, notice.valid_to, notice.author) == (
        "LID-NBOARD-NOTICE-1",
        date(2026, 11, 14),
        "Anna Testowa",
    )
    comment = got[C.grade_comment][0]
    assert (comment.title, comment.sent) == ("Matematyka", date(2026, 11, 4))
    assert [e.title for e in got[C.conference]] == ["Zebranie", "Zebranie"]  # wpis i temat
    trip_entry, trip_text = got[C.trip]
    assert trip_text.body == "Muzeum · autokar" and trip_entry.detail == "autokar"
    assert got[C.activity][0].category == "CANCELED"
    assert C.grade not in got  # domyślnie ignorowane


@pytest.mark.parametrize(
    ("ignore", "present", "absent"),
    [
        (frozenset(), {C.grade, C.grade_comment}, set()),
        (frozenset({C.grade}), {C.grade_comment}, {C.grade}),
        (frozenset({C.grade_comment}), {C.grade}, {C.grade_comment}),
        (frozenset({C.grade, C.grade_comment}), set(), {C.grade, C.grade_comment}),
    ],
)
def test_grades_and_comments_are_independent(
    ignore: frozenset[ContentType], present: set[ContentType], absent: set[ContentType]
) -> None:
    got = by_source(run(ignore)[0])
    assert present <= set(got) and not absent & set(got)


def test_every_ignored_type_produces_nothing() -> None:
    snap, _ = run(frozenset(ContentType))
    assert snap.entries == snap.texts == snap.plan_changes == snap.free_days == []
    assert snap.lessons  # plan lekcji (norma) zostaje zawsze


def test_bad_record_is_skipped_with_id_only(caplog: pytest.LogCaptureFixture) -> None:
    bad = {
        "HomeWorks": {
            "HomeWorks": [
                {"Id": 98, "Content": "Jan Kowalski przynosi"},  # brak Date
                {"Id": 99, "Date": "2026-11-20", "Content": "ok"},
            ]
        }
    }
    snap, _ = run(overrides=bad)
    assert [e.key for e in snap.entries if e.source is C.agenda] == ["99"]
    assert snap.skipped == {"HomeWorks": 1}
    assert "HomeWorks: pominięty rekord 98 (KeyError)" in caplog.text
    assert "Kowalski" not in caplog.text


def test_empty_texts_are_not_created() -> None:
    overrides = {
        "SchoolNotices": {
            "SchoolNotices": [{"Id": "n", "Content": " ", "StartDate": "2026-11-05"}]
        },
        "Notes": {"Notes": [{"Id": 1, "Text": "", "Date": "2026-11-05"}]},
        "Realizations": {
            "Realizations": [
                {"Id": "t1", "Date": "2026-11-05", "Topic": "Ułamki"},
                {"Id": "t2", "Date": "2026-11-05"},
            ]
        },
    }
    got = by_source(run(overrides=overrides)[0])
    assert C.notice not in got and C.note not in got
    assert [t.body for t in got[C.lesson_topic]] == ["Ułamki"]
