from datetime import date, time

from librus_watch.baseline import Norm, bells_from, day_norm
from librus_watch.models import Lesson, StandingLesson

MON = date(2026, 11, 9)
BELLS = {
    0: (time(7, 10), time(7, 55)),
    1: (time(8, 0), time(8, 45)),
    5: (time(11, 40), time(12, 25)),
    6: (time(12, 35), time(13, 20)),
}


def standing(weekday: int, *numbers: int, until: date = date(2027, 6, 26)) -> list[StandingLesson]:
    return [
        StandingLesson("ania", f"{weekday}-{n}", weekday, n, date(2026, 9, 1), until)
        for n in numbers
    ]


def test_norm_from_standing_plan_and_bells() -> None:
    norm = day_norm(MON, standing(1, 1, 5, 6), BELLS, [])
    assert norm == Norm(time(8, 0), time(13, 20), "standing")


def test_day_without_standing_lessons_has_no_norm() -> None:
    assert day_norm(MON, standing(2, 1, 5), BELLS, []) is None


def test_expired_standing_entries_are_ignored() -> None:
    old = standing(1, 0, until=date(2026, 9, 30))  # dawna lekcja zerowa
    assert day_norm(MON, old + standing(1, 1, 6), BELLS, []).start == time(8, 0)  # type: ignore[union-attr]


def test_history_is_used_without_standing_plan_or_bells() -> None:
    history = [
        (time(8, 0), time(13, 20), 6),
        (time(8, 0), time(13, 20), 6),
        (time(7, 10), time(13, 20), 7),
        (None, None, 0),
    ]
    assert day_norm(MON, [], BELLS, history) == Norm(time(8, 0), time(13, 20), "history")
    assert day_norm(MON, standing(1, 9), BELLS, history).source == "history"  # type: ignore[union-attr]
    assert day_norm(MON, [], BELLS, [(None, None, 0)]) is None


def test_bells_take_most_common_times() -> None:
    def ls(n: int, s: str, e: str) -> Lesson:
        return Lesson("ania", MON, n, time.fromisoformat(s), time.fromisoformat(e), "x", "")

    lessons = [
        ls(1, "08:00", "08:45"),
        ls(1, "08:00", "08:45"),
        ls(1, "08:10", "08:55"),
        Lesson("ania", MON, 2, None, None, "x", ""),
    ]
    assert bells_from(lessons) == {1: (time(8, 0), time(8, 45))}
