"""Rekordy JSON-owego API Synergii -> modele domenowe (ADR-012).

Jedyne miejsce, które zna znaczenie pól API (kształty w
`docs/librus-apix-notes.md`). Rekord, którego nie da się odczytać, jest
pomijany z ostrzeżeniem (moduł, id rekordu, typ błędu) i nie przerywa reszty.
Typy treści z `ignore_content` nie powstają wcale (ADR-014).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any, TypeVar

from librus_watch.config import ContentType
from librus_watch.librus.gateway import GatewayData, Record, ref_id
from librus_watch.models import (
    Entry,
    FreeDay,
    Lesson,
    LessonStatus,
    PlanChange,
    PlanChangeKind,
    TextItem,
)

log = logging.getLogger(__name__)
T = TypeVar("T")

ALWAYS_MODULES = frozenset({"Me", "Classes", "Timetables", "TimetableEntries", "Subjects", "Users"})
"""Moduły potrzebne zawsze: tożsamość, klasa, plan (norma dnia) i słowniki."""

MODULES_FOR: dict[ContentType, frozenset[str]] = {
    ContentType.message: frozenset(),
    ContentType.notice: frozenset({"SchoolNotices"}),
    ContentType.agenda: frozenset({"HomeWorks", "HomeWorkCategories"}),
    ContentType.homework: frozenset({"HomeWorkAssignments"}),
    ContentType.grade: frozenset({"Grades"}),
    ContentType.grade_comment: frozenset({"GradeComments", "Grades"}),
    ContentType.note: frozenset({"Notes", "NoteCategories"}),
    ContentType.lesson_topic: frozenset({"Realizations"}),
    ContentType.conference: frozenset({"ParentTeacherConferences"}),
    ContentType.trip: frozenset({"SchoolTrips"}),
    ContentType.activity: frozenset({"OtherActivities"}),
    ContentType.plan_change: frozenset({"Substitutions"}),
    ContentType.free_day: frozenset({"ClassFreeDays", "SchoolFreeDays"}),
}
"""Moduły API potrzebne do każdego typu treści."""

LOOKUP_MODULES: dict[str, str] = {
    "Subjects": "subjects",
    "Users": "users",
    "HomeWorkCategories": "homework_categories",
    "NoteCategories": "note_categories",
    "Classes": "class",
}
"""Moduły-słowniki i nazwa słownika w bazie."""


def modules_needed(ignore: frozenset[ContentType]) -> frozenset[str]:
    """Moduły do pobrania przy danej liście ignorowanych typów (ADR-014).

    Args:
        ignore: Typy treści pomijane w całości.

    Returns:
        Nazwy modułów z `gateway.MODULES`.
    """
    needed = set(ALWAYS_MODULES)
    for content_type, modules in MODULES_FOR.items():
        if content_type not in ignore:
            needed |= modules
    return frozenset(needed)


def lookups_from(data: GatewayData) -> dict[str, dict[str, str]]:
    """Słowniki z modułów pobranych w tym cyklu.

    Args:
        data: Wynik `gateway.fetch_all`.

    Returns:
        Nazwa słownika -> (id -> nazwa); tylko dla pobranych modułów.
    """
    out: dict[str, dict[str, str]] = {}
    for module, kind in LOOKUP_MODULES.items():
        records = data.records.get(module)
        if records is None:
            continue
        values: dict[str, str] = {}
        for r in records:
            key = ref_id(r.get("Id"))
            if key is None:
                continue
            if module == "Users":
                values[key] = f"{r.get('FirstName') or ''} {r.get('LastName') or ''}".strip()
            elif module == "NoteCategories":
                values[key] = str(r.get("CategoryName") or "")
            elif module == "Classes":
                values["id"] = key
            else:
                values[key] = str(r.get("Name") or "")
        out[kind] = values
    return out


@dataclass
class Snapshot:
    """Dane jednego dziecka z jednego cyklu, już jako modele domenowe.

    Attributes:
        lessons: Plan lekcji per dzień; `None`, gdy moduł planu się nie pobrał.
        entries: Wpisy z datą (terminarz, zadania, zebrania, wycieczki, zajęcia, oceny).
        texts: Wolne teksty dla modelu.
        plan_changes: Odwołania i zastępstwa.
        free_days: Dni wolne szkoły i klasy (tylko klasy dziecka).
        skipped: Moduł -> liczba pominiętych rekordów.
    """

    lessons: dict[date, list[Lesson]] | None = None
    entries: list[Entry] = field(default_factory=list)
    texts: list[TextItem] = field(default_factory=list)
    plan_changes: list[PlanChange] = field(default_factory=list)
    free_days: list[FreeDay] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)


def _date(v: Any) -> date:
    """Data z `YYYY-MM-DD` albo `YYYY-MM-DD HH:MM:SS`."""
    return date.fromisoformat(str(v)[:10])


def _opt_date(v: Any) -> date | None:
    """Jak `_date`, ale brak wartości to `None`."""
    return _date(v) if v not in (None, "") else None


def _time(v: Any) -> time | None:
    """Godzina z `HH:MM` albo `HH:MM:SS`; brak wartości to `None`."""
    return time.fromisoformat(str(v)[:5]) if v not in (None, "") else None


def _int(v: Any) -> int | None:
    """Liczba z napisu albo liczby; brak wartości to `None`."""
    return int(v) if v not in (None, "") else None


def _text(v: Any) -> str:
    """Napis bez białych znaków na brzegach; brak wartości to pusty napis."""
    return str(v).strip() if v is not None else ""


class _Adapter:
    """Zamiana rekordów jednego dziecka; trzyma słowniki i licznik pominięć."""

    def __init__(
        self,
        child: str,
        lookups: dict[str, dict[str, str]],
        ignore: frozenset[ContentType],
        today: date,
    ) -> None:
        self.child = child
        self.subjects = lookups.get("subjects", {})
        self.users = lookups.get("users", {})
        self.hw_categories = lookups.get("homework_categories", {})
        self.note_categories = lookups.get("note_categories", {})
        self.class_id = lookups.get("class", {}).get("id")
        self.ignore = ignore
        self.today = today
        self.snap = Snapshot()

    def each(
        self, module: str, records: Iterable[Record], fn: Callable[[Record], T | None]
    ) -> list[T]:
        """Stosuje `fn` do rekordów; błędne rekordy pomija z ostrzeżeniem.

        Args:
            module: Nazwa modułu (do logu).
            records: Rekordy modułu.
            fn: Zamiana jednego rekordu.

        Returns:
            Wyniki dla rekordów, które się udały (bez `None`).
        """
        out: list[T] = []
        for r in records:
            try:
                value = fn(r)
            except (KeyError, ValueError, TypeError, AttributeError) as e:
                self.snap.skipped[module] += 1
                rid = ref_id(r.get("Id") or r.get("id") or r.get("identifier"))
                log.warning("%s: pominięty rekord %s (%s)", module, rid, type(e).__name__)
                continue
            if value is not None:
                out.append(value)
        return out

    def wants(self, content_type: ContentType) -> bool:
        """Czy typ treści nie jest ignorowany."""
        return content_type not in self.ignore

    def subject(self, ref: Any) -> str:
        """Nazwa przedmiotu z odwołania `{"Id"}`."""
        return self.subjects.get(ref_id(ref) or "", "")

    def user(self, ref: Any) -> str:
        """Imię i nazwisko z odwołania `{"Id"}` (do bazy, nigdy do logów)."""
        return self.users.get(ref_id(ref) or "", "")

    # --- plan i zmiany planu ------------------------------------------------

    def lesson(self, r: Record) -> Lesson:
        """Lekcja z `Timetables` (pola `_day`, `_slot` dodaje gateway)."""
        status = (
            LessonStatus.cancelled
            if r.get("IsCanceled")
            else LessonStatus.substitution
            if r.get("IsSubstitutionClass")
            else LessonStatus.normal
        )
        return Lesson(
            child=self.child,
            day=_date(r["_day"]),
            number=int(r["LessonNo"]),
            start=_time(r.get("HourFrom")),
            end=_time(r.get("HourTo")),
            subject=self.subject(r.get("Subject")),
            teacher=self.user(r.get("Teacher")),
            status=status,
            note=_text(r.get("SubstitutionNote")),
            key=ref_id(r.get("Lesson")) or str(r["_slot"]),
        )

    def plan_change(self, r: Record) -> PlanChange:
        """Zmiana z `Calendars/Substitutions`."""
        kind = (
            PlanChangeKind.cancelled
            if r.get("IsCancelled")
            else PlanChangeKind.shifted
            if r.get("IsShifted")
            else PlanChangeKind.substitution
        )
        return PlanChange(
            child=self.child,
            key=str(r["Id"]),
            kind=kind,
            org_day=_date(r["OrgDate"]),
            org_lesson_no=_int(r.get("OrgLessonNo")),
            subject=self.subject(r.get("OrgSubject")),
            teacher=self.user(r.get("OrgTeacher")),
            new_day=_opt_date(r.get("Date")),
            new_lesson_no=_int(r.get("LessonNo")),
            new_subject=self.subject(r.get("Subject")),
            new_teacher=self.user(r.get("Teacher")),
        )

    def free_day(self, scope: str) -> Callable[[Record], FreeDay | None]:
        """Zamiana dnia wolnego szkoły albo klasy (klasy tylko dla klasy dziecka)."""

        def convert(r: Record) -> FreeDay | None:
            if scope == "class" and ref_id(r.get("Class")) != self.class_id:
                return None
            return FreeDay(
                child=self.child,
                key=f"{scope}:{r['Id']}",
                scope=scope,
                name=_text(r.get("Name")),
                date_from=_date(r["DateFrom"]),
                date_to=_date(r["DateTo"]),
                lesson_from=_int(r.get("LessonNoFrom")),
                lesson_to=_int(r.get("LessonNoTo")),
            )

        return convert

    # --- wpisy z datą i ich teksty --------------------------------------------

    def agenda(self, r: Record) -> tuple[Entry, TextItem | None]:
        """Wpis terminarza (`HomeWorks`) i jego treść."""
        category = self.hw_categories.get(ref_id(r.get("Category")) or "", "")
        day = _date(r["Date"])
        teacher = self.user(r.get("CreatedBy"))
        body = _text(r.get("Content"))
        entry = Entry(
            child=self.child,
            source=ContentType.agenda,
            key=str(r["Id"]),
            day=day,
            title=category,
            subject=self.subject(r.get("Subject")),
            detail=body,
            teacher=teacher,
            at=_time(r.get("TimeFrom")),
            category=category,
        )
        text = TextItem(
            child=self.child,
            source=ContentType.agenda,
            key=entry.key,
            sent=_opt_date(r.get("AddDate")) or day,
            author=teacher,
            title=" · ".join(p for p in (category, entry.subject) if p),
            body=body,
            valid_from=day,
            valid_to=day,
        )
        return entry, text if body else None

    def homework(self, r: Record) -> tuple[Entry, TextItem | None]:
        """Zadanie domowe (`HomeWorkAssignments`) i jego treść."""
        due = _date(r["DueDate"])
        teacher = self.user(r.get("Teacher"))
        topic, body = _text(r.get("Topic")), _text(r.get("Text"))
        entry = Entry(
            child=self.child,
            source=ContentType.homework,
            key=str(r["Id"]),
            day=due,
            title=topic,
            detail=body,
            teacher=teacher,
        )
        text = TextItem(
            child=self.child,
            source=ContentType.homework,
            key=entry.key,
            sent=_opt_date(r.get("Date")) or self.today,
            author=teacher,
            title=topic,
            body=body,
            valid_to=due,
        )
        return entry, text if (topic or body) else None

    def conference(self, r: Record) -> tuple[Entry, TextItem | None]:
        """Zebranie z rodzicami i jego temat."""
        day = _date(r["Date"])
        name, topic = _text(r.get("Name")), _text(r.get("Topic"))
        teacher = self.user(r.get("Teacher"))
        entry = Entry(
            child=self.child,
            source=ContentType.conference,
            key=str(r["Id"]),
            day=day,
            title=name,
            detail=topic,
            teacher=teacher,
            at=_time(r.get("Time")),
        )
        text = TextItem(
            child=self.child,
            source=ContentType.conference,
            key=entry.key,
            sent=day,
            author=teacher,
            title=name,
            body=topic,
            valid_to=day,
        )
        return entry, text if topic else None

    def trip(self, r: Record) -> tuple[Entry, TextItem | None]:
        """Wycieczka szkolna (pola małymi literami)."""
        start = _date(r["termFrom"])
        end = _opt_date(r.get("termTo")) or start
        destination = _text(r.get("destination"))
        details = [_text(r.get(k)) for k in ("route", "locomotion")]
        body = " · ".join(p for p in (destination, *details) if p)
        entry = Entry(
            child=self.child,
            source=ContentType.trip,
            key=str(r["id"]),
            day=start,
            title=destination,
            detail=" · ".join(p for p in details if p),
        )
        text = TextItem(
            child=self.child,
            source=ContentType.trip,
            key=entry.key,
            sent=start,
            author="",
            title=destination,
            body=body,
            valid_from=start,
            valid_to=end,
        )
        return entry, text if body else None

    def activity(self, r: Record) -> Entry:
        """Zajęcia dodatkowe; odwołane mają `category=CANCELED`."""
        return Entry(
            child=self.child,
            source=ContentType.activity,
            key=str(r["identifier"]),
            day=_date(r["date"]),
            title=_text(r.get("title")),
            teacher=_text(r.get("teacherName")),
            at=_time(r.get("startTime")),
            category=_text(r.get("status")),
        )

    def grade(self, r: Record) -> Entry:
        """Nowa ocena (tylko gdy `grade` nie jest ignorowane)."""
        return Entry(
            child=self.child,
            source=ContentType.grade,
            key=str(r["Id"]),
            day=_date(r["Date"]),
            title=_text(r.get("Grade")),
            subject=self.subject(r.get("Subject")),
            teacher=self.user(r.get("AddedBy")),
        )

    # --- same teksty ------------------------------------------------------------

    def notice(self, r: Record) -> TextItem | None:
        """Ogłoszenie szkoły."""
        body = _text(r.get("Content"))
        if not body:
            return None
        return TextItem(
            child=self.child,
            source=ContentType.notice,
            key=str(r["Id"]),
            sent=_opt_date(r.get("CreationDate")) or _date(r["StartDate"]),
            author=self.user(r.get("AddedBy")),
            title=_text(r.get("Subject")),
            body=body,
            valid_from=_opt_date(r.get("StartDate")),
            valid_to=_opt_date(r.get("EndDate")),
        )

    def note(self, r: Record) -> TextItem | None:
        """Uwaga."""
        body = _text(r.get("Text"))
        if not body:
            return None
        return TextItem(
            child=self.child,
            source=ContentType.note,
            key=str(r["Id"]),
            sent=_date(r["Date"]),
            author=self.user(r.get("Teacher")),
            title=self.note_categories.get(ref_id(r.get("Category")) or "", ""),
            body=body,
        )

    def grade_comment(self, grades: dict[str, Record]) -> Callable[[Record], TextItem | None]:
        """Komentarz do oceny; przedmiot i data z oceny, jeśli jest w module ocen."""

        def convert(r: Record) -> TextItem | None:
            body = _text(r.get("Text"))
            if not body:
                return None
            grade = grades.get(ref_id(r.get("Grade")) or "", {})
            return TextItem(
                child=self.child,
                source=ContentType.grade_comment,
                key=str(r["Id"]),
                sent=_opt_date(grade.get("AddDate")) or _opt_date(grade.get("Date")) or self.today,
                author=self.user(r.get("AddedBy")),
                title=self.subject(grade.get("Subject")),
                body=body,
            )

        return convert

    def lesson_topic(self, r: Record) -> TextItem | None:
        """Temat zrealizowanej lekcji (gdy szkoła go podaje)."""
        topic = _text(r.get("Topic"))
        if not topic:
            return None
        return TextItem(
            child=self.child,
            source=ContentType.lesson_topic,
            key=str(r["Id"]),
            sent=_date(r["Date"]),
            author=self.user(r.get("AddedBy")),
            title="",
            body=topic,
        )


def _with_text(snap: Snapshot, pairs: list[tuple[Entry, TextItem | None]], keep_text: bool) -> None:
    """Dokłada wpisy i ich teksty do zrzutu."""
    for entry, text in pairs:
        snap.entries.append(entry)
        if keep_text and text is not None:
            snap.texts.append(text)


def adapt(
    child: str,
    data: GatewayData,
    lookups: dict[str, dict[str, str]],
    *,
    ignore: frozenset[ContentType],
    today: date,
    timetable_days: Iterable[date] = (),
) -> Snapshot:
    """Zamienia rekordy z jednego cyklu na modele domenowe.

    Args:
        child: Id dziecka.
        data: Wynik `gateway.fetch_all`.
        lookups: Słowniki (z tego cyklu albo z bazy, `lookups_from`).
        ignore: Typy treści pomijane w całości.
        today: Bieżąca data (zapasowa data tekstów bez własnej).
        timetable_days: Wszystkie dni pobranych tygodni planu; dzień bez lekcji
            w API dostaje pustą listę.

    Returns:
        Zrzut danych dziecka.
    """
    a = _Adapter(child, lookups, ignore, today)
    snap = a.snap
    rec = data.records

    if "Timetables" in rec:
        lessons: dict[date, list[Lesson]] = {d: [] for d in timetable_days}
        for ls in a.each("Timetables", rec["Timetables"], a.lesson):
            lessons.setdefault(ls.day, []).append(ls)
        snap.lessons = lessons
    if a.wants(ContentType.plan_change):
        snap.plan_changes = a.each("Substitutions", rec.get("Substitutions", []), a.plan_change)
    if a.wants(ContentType.free_day):
        if a.class_id is None and rec.get("ClassFreeDays"):
            log.warning("ClassFreeDays: nieznana klasa dziecka, dni wolne klasy pominięte")
        snap.free_days = a.each(
            "SchoolFreeDays", rec.get("SchoolFreeDays", []), a.free_day("school")
        ) + a.each("ClassFreeDays", rec.get("ClassFreeDays", []), a.free_day("class"))

    for content_type, module, fn in (
        (ContentType.agenda, "HomeWorks", a.agenda),
        (ContentType.homework, "HomeWorkAssignments", a.homework),
        (ContentType.conference, "ParentTeacherConferences", a.conference),
        (ContentType.trip, "SchoolTrips", a.trip),
    ):
        if a.wants(content_type):
            _with_text(snap, a.each(module, rec.get(module, []), fn), keep_text=True)
    if a.wants(ContentType.activity):
        snap.entries += a.each("OtherActivities", rec.get("OtherActivities", []), a.activity)
    if a.wants(ContentType.grade):
        snap.entries += a.each("Grades", rec.get("Grades", []), a.grade)

    if a.wants(ContentType.notice):
        snap.texts += a.each("SchoolNotices", rec.get("SchoolNotices", []), a.notice)
    if a.wants(ContentType.note):
        snap.texts += a.each("Notes", rec.get("Notes", []), a.note)
    if a.wants(ContentType.grade_comment):
        grades = {ref_id(g.get("Id")) or "": g for g in rec.get("Grades", [])}
        snap.texts += a.each("GradeComments", rec.get("GradeComments", []), a.grade_comment(grades))
    if a.wants(ContentType.lesson_topic):
        snap.texts += a.each("Realizations", rec.get("Realizations", []), a.lesson_topic)
    return snap
