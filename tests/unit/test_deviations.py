"""Scenariusze odstępstw wyliczanych przez program (kryteria E4)."""

from datetime import date, time

import pytest

from librus_watch.baseline import Norm
from librus_watch.config import ContentType, Kind
from librus_watch.deviations import (
    day_findings,
    dedupe,
    entry_findings,
    held_lessons,
    plan_change_findings,
)
from librus_watch.models import (
    Entry,
    FreeDay,
    Lesson,
    LessonStatus,
    Origin,
    PlanChange,
    PlanChangeKind,
)

TODAY = date(2026, 11, 5)
DAY = date(2026, 11, 9)
NORM = Norm(time(8, 0), time(13, 20), "standing")
TIMES = {
    0: ("07:10", "07:55"),
    1: ("08:00", "08:45"),
    2: ("08:55", "09:40"),
    3: ("09:50", "10:35"),
    4: ("10:45", "11:30"),
    5: ("11:40", "12:25"),
    6: ("12:35", "13:20"),
    7: ("13:30", "14:15"),
}
C = ContentType


def lesson(
    n: int, status: LessonStatus = LessonStatus.normal, subject: str = "matematyka"
) -> Lesson:
    s, e = TIMES[n]
    return Lesson(
        "ania", DAY, n, time.fromisoformat(s), time.fromisoformat(e), subject, "", status=status
    )


def normal_day() -> list[Lesson]:
    return [lesson(n) for n in range(1, 7)]


def kinds(findings: list) -> list[tuple[Kind, str]]:  # type: ignore[type-arg]
    return [(f.kind, f.what) for f in findings]


# --- dzień ------------------------------------------------------------------


def test_normal_day_is_silent() -> None:
    assert day_findings("ania", DAY, normal_day(), NORM, [], []) == []


def test_no_norm_means_no_day_findings() -> None:
    assert day_findings("ania", DAY, [], None, [], []) == []


def test_whole_free_day_uses_its_name() -> None:
    fd = FreeDay("ania", "school:1", "school", "Dzień Edukacji", DAY, DAY)
    found = day_findings("ania", DAY, normal_day(), NORM, [fd], [])
    assert kinds(found) == [(Kind.day_off, "Dzień Edukacji")]
    assert found[0].origin is Origin.timetable


def test_empty_timetable_day_with_norm_is_day_off() -> None:
    assert kinds(day_findings("ania", DAY, [], NORM, [], [])) == [
        (Kind.day_off, "dzień bez lekcji")
    ]


def test_class_free_on_part_of_day_ends_early() -> None:
    fd = FreeDay("ania", "class:5", "class", "", DAY, DAY, lesson_from=4, lesson_to=6)
    found = day_findings("ania", DAY, normal_day(), NORM, [fd], [])
    assert kinds(found) == [(Kind.early_end, "koniec lekcji 10:35 zamiast 13:20")]
    assert found[0].at == time(10, 35) and found[0].before == "13:20"


def test_cancelled_first_lesson_starts_later() -> None:
    lessons = [lesson(1, LessonStatus.cancelled), *normal_day()[1:]]
    assert kinds(day_findings("ania", DAY, lessons, NORM, [], [])) == [
        (Kind.late_start, "pierwsza lekcja 08:55 zamiast 08:00")
    ]


def test_plan_change_cancel_counts_as_not_held() -> None:
    pc = PlanChange("ania", "1", PlanChangeKind.cancelled, DAY, 6)
    found = day_findings("ania", DAY, normal_day(), NORM, [], [pc])
    assert kinds(found) == [(Kind.early_end, "koniec lekcji 12:25 zamiast 13:20")]


def test_extra_lesson_zero_and_eighth() -> None:
    lessons = [lesson(0), *normal_day(), lesson(7)]
    assert [k for k, _ in kinds(day_findings("ania", DAY, lessons, NORM, [], []))] == [
        Kind.early_start,
        Kind.late_end,
    ]


def test_empty_slots_and_open_lesson_range() -> None:
    gap = Lesson("ania", DAY, 3, time(9, 50), time(10, 35), "", "")  # okienko
    fd = FreeDay("ania", "class:6", "class", "", DAY, DAY, lesson_from=5, lesson_to=None)
    held = held_lessons(DAY, [*normal_day()[:2], gap, *normal_day()[3:]], [fd], [])
    assert [ls.number for ls in held] == [1, 2, 4]


# --- zmiany planu -----------------------------------------------------------------


def test_plan_changes_in_window() -> None:
    changes = [
        PlanChange("ania", "1", PlanChangeKind.cancelled, DAY, 3, subject="matematyka"),
        PlanChange(
            "ania",
            "2",
            PlanChangeKind.substitution,
            DAY,
            4,
            subject="przyroda",
            new_subject="plastyka",
        ),
        PlanChange(
            "ania", "3", PlanChangeKind.substitution, DAY, 5, subject="WF", new_subject="WF"
        ),
        PlanChange("ania", "4", PlanChangeKind.shifted, DAY, 2, new_day=date(2026, 11, 10)),
        PlanChange("ania", "5", PlanChangeKind.cancelled, date(2026, 10, 1), 1),
    ]
    found = plan_change_findings(changes, TODAY, date(2026, 11, 15))
    assert kinds(found) == [
        (Kind.cancel, "odwołana 3. lekcja: matematyka"),
        (Kind.substitution, "zastępstwo na 4. lekcji: przyroda → plastyka"),
        (Kind.substitution, "zastępstwo na 5. lekcji: WF"),
        (Kind.change, "przesunięta 2. lekcja na 10.11"),
    ]
    assert {f.origin for f in found} == {Origin.plan_change}


def test_corrected_plan_change_keeps_identity() -> None:
    a = plan_change_findings(
        [PlanChange("ania", "9", PlanChangeKind.cancelled, DAY, 3)], TODAY, DAY
    )
    b = plan_change_findings(
        [PlanChange("ania", "9", PlanChangeKind.substitution, DAY, 3, new_subject="WF")], TODAY, DAY
    )
    assert a[0].identity == b[0].identity and a[0].content_fp != b[0].content_fp


# --- wpisy -------------------------------------------------------------------------


def entry(source: ContentType, key: str, day: date = DAY, **kw: object) -> Entry:
    return Entry("ania", source, key, day, kw.pop("title", "wpis"), **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("e", "kind"),
    [
        (entry(C.agenda, "1", category="Sprawdzian", subject="matematyka"), Kind.test),
        (entry(C.agenda, "2", category="Kartkówka"), Kind.test),
        (entry(C.agenda, "3", category="Wycieczka"), Kind.trip),
        (entry(C.agenda, "4", category="Zebranie z rodzicami"), Kind.meeting),
        (entry(C.agenda, "5", category="Inne"), Kind.other),
        (entry(C.homework, "6"), Kind.homework),
        (entry(C.conference, "7"), Kind.meeting),
        (entry(C.trip, "8"), Kind.trip),
        (entry(C.grade, "9"), Kind.grade),
        (entry(C.activity, "10", category="CANCELED", title="Chór"), Kind.cancel),
    ],
)
def test_new_entry_kinds(e: Entry, kind: Kind) -> None:
    found = entry_findings({}, [e], [], TODAY)
    assert [f.kind for f in found] == [kind]
    assert found[0].origin is (Origin.homework if e.source is C.homework else Origin.schedule)


def test_active_activity_and_past_entries_are_silent() -> None:
    assert entry_findings({}, [entry(C.activity, "1", category="ACTUAL")], [], TODAY) == []
    assert entry_findings({}, [entry(C.agenda, "2", day=date(2026, 11, 1))], [], TODAY) == []


def test_moved_test_is_change_with_before_and_same_identity() -> None:
    old = entry(C.agenda, "1", category="Sprawdzian", subject="matematyka")
    moved = entry(
        C.agenda, "1", day=date(2026, 11, 12), category="Sprawdzian", subject="matematyka"
    )
    first = entry_findings({}, [old], [], TODAY)[0]
    change = entry_findings({(C.agenda, "1"): old}, [moved], [], TODAY)[0]
    assert (change.kind, change.before) == (Kind.change, "09.11")
    assert change.identity == first.identity


def test_unchanged_entry_is_silent_and_cancelled_activity_reported_once() -> None:
    e = entry(C.agenda, "1", category="Sprawdzian")
    assert entry_findings({(C.agenda, "1"): e}, [e], [], TODAY) == []
    active = entry(C.activity, "2", category="ACTUAL", title="Chór")
    cancelled = entry(C.activity, "2", category="CANCELED", title="Chór")
    found = entry_findings({(C.activity, "2"): active}, [cancelled], [], TODAY)
    assert kinds(found) == [(Kind.cancel, "odwołane zajęcia: Chór")]


def test_removed_future_entry_is_cancel_past_is_not() -> None:
    future = entry(C.agenda, "1", category="Sprawdzian", subject="matematyka")
    past = entry(C.agenda, "2", day=date(2026, 11, 1), category="Sprawdzian")
    found = entry_findings({}, [], [future, past], TODAY)
    assert kinds(found) == [(Kind.cancel, "usunięte z terminarza: wpis · matematyka")]


def test_dedupe_keeps_first_trip_and_meeting_of_a_day() -> None:
    trips = entry_findings(
        {},
        [
            entry(C.agenda, "1", category="Wycieczka"),
            entry(C.trip, "2"),
            entry(C.conference, "3"),
            entry(C.agenda, "4", category="Zebranie"),
            entry(C.agenda, "5", category="Sprawdzian"),
            entry(C.agenda, "6", category="Sprawdzian"),
        ],
        [],
        TODAY,
    )
    kept = dedupe(trips)
    assert [f.kind for f in kept] == [Kind.trip, Kind.meeting, Kind.test, Kind.test]


def test_entry_finding_quotes_the_entry_text() -> None:
    e = entry(C.agenda, "1", category="Informacja", detail="Omawiamy  lekturę\nDzieci z Bullerbyn")
    f = entry_findings({}, [e], [], TODAY)[0]
    assert f.quote == "Omawiamy lekturę Dzieci z Bullerbyn"


def test_entry_quote_is_trimmed_and_does_not_change_the_fingerprint() -> None:
    long = entry(C.agenda, "1", category="Informacja", detail="x" * 500)
    short = entry(C.agenda, "1", category="Informacja", detail="")
    f_long = entry_findings({}, [long], [], TODAY)[0]
    f_short = entry_findings({}, [short], [], TODAY)[0]
    assert len(f_long.quote) == 300 and f_long.quote.endswith("…")
    assert f_short.quote == ""
    assert f_long.content_fp == f_short.content_fp
