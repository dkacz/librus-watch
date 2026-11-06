"""Norma dnia: o której dziecko zwykle zaczyna i kończy lekcje (ADR-004).

Podstawą jest plan stały (`TimetableEntries`): numery lekcji obowiązujące
w danym dniu tygodnia i dacie, zamienione na godziny według dzwonków
(najczęstsze godziny lekcji o danym numerze). Gdy planu stałego nie ma,
normą jest najczęstszy początek i koniec tego dnia tygodnia z historii.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, time

from librus_watch.models import Lesson, StandingLesson


@dataclass(frozen=True)
class Norm:
    """Zwykły dzień dziecka.

    Attributes:
        start: Początek pierwszej lekcji.
        end: Koniec ostatniej lekcji.
        source: `standing` (plan stały) albo `history` (historia planów).
    """

    start: time
    end: time
    source: str


def bells_from(lessons: Iterable[Lesson]) -> dict[int, tuple[time, time]]:
    """Dzwonki z lekcji: najczęstsza para godzin dla każdego numeru lekcji.

    Args:
        lessons: Lekcje z planu (dowolne dni).

    Returns:
        Numer lekcji -> (początek, koniec).
    """
    counts: Counter[tuple[int, time, time]] = Counter(
        (ls.number, ls.start, ls.end) for ls in lessons if ls.start and ls.end
    )
    out: dict[int, tuple[time, time]] = {}
    for (number, start, end), _n in counts.most_common():
        out.setdefault(number, (start, end))
    return out


def day_norm(
    day: date,
    standing: list[StandingLesson],
    bells: dict[int, tuple[time, time]],
    history: list[tuple[time | None, time | None, int]],
) -> Norm | None:
    """Norma dla dnia.

    Args:
        day: Dzień.
        standing: Plan stały dziecka (może być pusty).
        bells: Dzwonki (numer lekcji -> godziny).
        history: Wcześniejsze dni tego dnia tygodnia: (początek, koniec, liczba
            lekcji), od najnowszych (`Store.profiles`).

    Returns:
        Norma albo `None`, gdy tego dnia zwykle nie ma lekcji albo brak danych.
    """
    if standing:
        numbers = sorted({s.lesson_no for s in standing if s.valid_on(day)})
        if not numbers:
            return None
        first, last = bells.get(numbers[0]), bells.get(numbers[-1])
        if first and last:
            return Norm(first[0], last[1], "standing")
    starts = Counter(s for s, _e, n in history if s and n)
    ends = Counter(e for _s, e, n in history if e and n)
    if not starts or not ends:
        return None
    return Norm(starts.most_common(1)[0][0], ends.most_common(1)[0][0], "history")
