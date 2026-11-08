"""Odstępstwa wyliczane przez program, bez modelu (ADR-001, ADR-004, ADR-008).

Trzy źródła:

* **dzień**: plan lekcji porównany z normą, z uwzględnieniem dni wolnych
  (także na część lekcji) i odwołań: dzień bez lekcji, inny początek, inny koniec;
* **zmiany planu**: odwołania, zastępstwa i przesunięcia z historii zmian Librusa;
* **wpisy z datą**: nowy sprawdzian, wycieczka, zebranie, zadanie, ocena,
  przesunięty termin, odwołane zajęcia, wpis usunięty z terminarza.

Wszystkie funkcje są czyste: dostają dane i zwracają pozycje (`Finding`).
Opisy są krótkie i po polsku, bez nazwisk.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, time

from librus_watch.baseline import Norm
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
)

_TEST_WORDS = ("sprawdzian", "kartków", "kartkow", "klasówk", "klasowk", "test")
_TRIP_WORDS = ("wycieczk", "wyjście", "wyjscie")
_MEETING_WORDS = ("zebran",)


def _hm(t: time | None) -> str:
    """Godzina jako HH:MM (pusty napis, gdy brak)."""
    return t.strftime("%H:%M") if t else ""


def _covered(fd: FreeDay, day: date, number: int) -> bool:
    """Czy lekcja o numerze wypada w dniu wolnym."""
    if not fd.date_from <= day <= fd.date_to:
        return False
    if fd.whole_day:
        return True
    low = fd.lesson_from if fd.lesson_from is not None else number
    high = fd.lesson_to if fd.lesson_to is not None else number
    return low <= number <= high


def held_lessons(
    day: date,
    lessons: list[Lesson],
    free_days: list[FreeDay],
    plan_changes: list[PlanChange],
) -> list[Lesson]:
    """Lekcje, które w danym dniu naprawdę się odbywają.

    Args:
        day: Dzień.
        lessons: Plan lekcji z tego dnia.
        free_days: Dni wolne dziecka.
        plan_changes: Zmiany planu (odwołania wykluczają lekcje).

    Returns:
        Lekcje z przedmiotem, nieodwołane i nie w dniu wolnym.
    """
    cancelled = {
        pc.org_lesson_no
        for pc in plan_changes
        if pc.kind is PlanChangeKind.cancelled and pc.org_day == day
    }
    return [
        ls
        for ls in lessons
        if ls.subject
        and ls.status is not LessonStatus.cancelled
        and ls.number not in cancelled
        and not any(_covered(fd, day, ls.number) for fd in free_days)
    ]


def day_findings(
    child: str,
    day: date,
    lessons: list[Lesson],
    norm: Norm | None,
    free_days: list[FreeDay],
    plan_changes: list[PlanChange],
) -> list[Finding]:
    """Odstępstwa dnia: brak lekcji, inny początek albo koniec niż w normie.

    Args:
        child: Id dziecka.
        day: Dzień.
        lessons: Plan lekcji z tego dnia (pobrany).
        norm: Norma dnia albo `None`, gdy zwykle nie ma lekcji.
        free_days: Dni wolne dziecka.
        plan_changes: Zmiany planu.

    Returns:
        Pozycje dnia (źródło: plan lekcji).
    """
    if norm is None:
        return []

    def f(kind: Kind, what: str, at: time | None = None, before: str = "") -> Finding:
        return Finding(
            child=child,
            day=day,
            kind=kind,
            what=what,
            origin=Origin.timetable,
            source_key=day.isoformat(),
            at=at,
            before=before,
        )

    held = held_lessons(day, lessons, free_days, plan_changes)
    if not held:
        whole = next(
            (fd for fd in free_days if fd.whole_day and fd.date_from <= day <= fd.date_to), None
        )
        return [f(Kind.day_off, (whole.name if whole and whole.name else "dzień bez lekcji"))]
    starts = [ls.start for ls in held if ls.start]
    ends = [ls.end for ls in held if ls.end]
    out: list[Finding] = []
    if starts:
        start = min(starts)
        if start != norm.start:
            kind = Kind.early_start if start < norm.start else Kind.late_start
            out.append(
                f(
                    kind,
                    f"pierwsza lekcja {_hm(start)} zamiast {_hm(norm.start)}",
                    start,
                    _hm(norm.start),
                )
            )
    if ends:
        end = max(ends)
        if end != norm.end:
            kind = Kind.early_end if end < norm.end else Kind.late_end
            out.append(
                f(kind, f"koniec lekcji {_hm(end)} zamiast {_hm(norm.end)}", end, _hm(norm.end))
            )
    return out


def plan_change_findings(
    changes: Iterable[PlanChange], day_from: date, day_to: date
) -> list[Finding]:
    """Odwołania, zastępstwa i przesunięcia w zakresie dni.

    Args:
        changes: Zmiany planu dziecka.
        day_from: Początek zakresu (włącznie).
        day_to: Koniec zakresu (włącznie).

    Returns:
        Pozycje ze zmian planu.
    """
    out: list[Finding] = []
    for pc in changes:
        if not day_from <= pc.org_day <= day_to:
            continue
        no = f"{pc.org_lesson_no}. " if pc.org_lesson_no is not None else ""
        subject = f": {pc.subject}" if pc.subject else ""
        if pc.kind is PlanChangeKind.cancelled:
            kind, what = Kind.cancel, f"odwołana {no}lekcja{subject}"
        elif pc.kind is PlanChangeKind.substitution:
            new = f" → {pc.new_subject}" if pc.new_subject and pc.new_subject != pc.subject else ""
            kind, what = Kind.substitution, f"zastępstwo na {no}lekcji{subject}{new}"
        else:
            target = f"{pc.new_day:%d.%m}" if pc.new_day else "inny termin"
            kind, what = Kind.change, f"przesunięta {no}lekcja{subject} na {target}"
        out.append(
            Finding(
                child=pc.child,
                day=pc.org_day,
                kind=kind,
                what=what,
                origin=Origin.plan_change,
                source_key=pc.key,
            )
        )
    return out


def _entry_kind(e: Entry) -> Kind | None:
    """Rodzaj odstępstwa dla nowego wpisu albo `None`, gdy wpis nie jest odstępstwem."""
    category = e.category.lower()
    if e.source is ContentType.agenda:
        if any(w in category for w in _TEST_WORDS):
            return Kind.test
        if any(w in category for w in _TRIP_WORDS):
            return Kind.trip
        if any(w in category for w in _MEETING_WORDS):
            return Kind.meeting
        return Kind.other
    return {
        ContentType.homework: Kind.homework,
        ContentType.conference: Kind.meeting,
        ContentType.trip: Kind.trip,
        ContentType.grade: Kind.grade,
    }.get(e.source, Kind.cancel if _is_cancelled(e) else None)


def _is_cancelled(e: Entry) -> bool:
    """Czy wpis zajęć dodatkowych jest odwołany."""
    return e.source is ContentType.activity and e.category.upper() == "CANCELED"


QUOTE_MAX = 300


def _entry_quote(e: Entry) -> str:
    """Treść wpisu jako cytat w powiadomieniu (jedna linia, do `QUOTE_MAX` znaków)."""
    text = " ".join(e.detail.split())
    return text if len(text) <= QUOTE_MAX else text[: QUOTE_MAX - 1].rstrip() + "…"


def _entry_finding(e: Entry, kind: Kind, what: str, before: str = "") -> Finding:
    """Pozycja z wpisu; tożsamość po typie i kluczu wpisu."""
    return Finding(
        child=e.child,
        day=e.day,
        kind=kind,
        what=what,
        origin=Origin.homework if e.source is ContentType.homework else Origin.schedule,
        source_key=f"{e.source.value}:{e.key}",
        at=e.at,
        quote=_entry_quote(e),
        before=before,
    )


def _when(e: Entry) -> str:
    """Termin wpisu do pola „było”."""
    return f"{e.day:%d.%m}" + (f" {_hm(e.at)}" if e.at else "")


def entry_findings(
    old: dict[tuple[ContentType, str], Entry],
    new: Iterable[Entry],
    removed: Iterable[Entry],
    today: date,
) -> list[Finding]:
    """Odstępstwa z wpisów z datą (tylko od dziś).

    Args:
        old: Wpisy zapisane wcześniej, po (typ, klucz).
        new: Wpisy z tego cyklu.
        removed: Wpisy, które zniknęły z kompletnie pobranego modułu (ADR-008).
        today: Bieżąca data.

    Returns:
        Pozycje: nowe wpisy, przesunięte terminy, odwołane zajęcia, usunięte wpisy.
    """
    out: list[Finding] = []
    for e in new:
        if e.day < today:
            continue
        before = old.get((e.source, e.key))
        if before is None:
            kind = _entry_kind(e)
            if kind is not None:
                label = f"odwołane zajęcia: {e.title}" if kind is Kind.cancel else e.describe()
                out.append(_entry_finding(e, kind, label))
        elif (before.day, before.at) != (e.day, e.at):
            out.append(_entry_finding(e, Kind.change, e.describe(), _when(before)))
        elif _is_cancelled(e) and not _is_cancelled(before):
            out.append(_entry_finding(e, Kind.cancel, f"odwołane zajęcia: {e.title}"))
    for e in removed:
        if e.day >= today:
            out.append(_entry_finding(e, Kind.cancel, f"usunięte z terminarza: {e.describe()}"))
    return out


def dedupe(findings: list[Finding]) -> list[Finding]:
    """Usuwa duplikaty z różnych modułów (np. zebranie w terminarzu i w module zebrań).

    Dla wycieczek i zebrań zostaje pierwsza pozycja danego dnia; pozostałe
    rodzaje mają własne tożsamości i zostają bez zmian.

    Args:
        findings: Pozycje w kolejności ważności źródeł.

    Returns:
        Pozycje bez duplikatów.
    """
    seen: set[tuple[str, date, Kind]] = set()
    out: list[Finding] = []
    for f in findings:
        key = (f.child, f.day, f.kind)
        if f.kind in (Kind.trip, Kind.meeting):
            if key in seen:
                continue
            seen.add(key)
        out.append(f)
    return out
