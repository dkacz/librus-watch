"""Modele domenowe niezależne od biblioteki scrapującej Librusa.

Adapter (`librus.py`) zamienia obiekty `librus-apix` na te klasy. Reszta
programu zna wyłącznie modele z tego pliku, więc zmiana scrapera dotyczy
jednego modułu.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, time
from enum import StrEnum

from librus_watch.config import ContentType, Kind


def fingerprint(*parts: object) -> str:
    """Stabilny skrót z dowolnych wartości (do kluczy i wykrywania zmian).

    Args:
        *parts: Wartości serializowalne do JSON (daty i godziny jako tekst).

    Returns:
        16 znaków szesnastkowych SHA-256.
    """
    raw = json.dumps(parts, ensure_ascii=False, default=str, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class LessonStatus(StrEnum):
    """Stan lekcji w planie."""

    normal = "normal"
    substitution = "substitution"
    cancelled = "cancelled"
    other = "other"
    """Inna adnotacja Librusa (np. przesunięcie); treść w `Lesson.note`."""


@dataclass(frozen=True)
class Lesson:
    """Jedna lekcja z planu lekcji.

    Attributes:
        child: Id dziecka.
        day: Data lekcji.
        number: Numer lekcji w dniu.
        start: Godzina rozpoczęcia.
        end: Godzina zakończenia.
        subject: Przedmiot (pusty dla okienka).
        teacher: Nauczyciel (i sala, jeśli źródło ją podaje).
        status: Stan lekcji.
        note: Adnotacja Librusa (np. uwaga do zastępstwa).
        key: Rozróżnia lekcje w jednym slocie (klasa podzielona na grupy);
            id lekcji z API.
    """

    child: str
    day: date
    number: int
    start: time | None
    end: time | None
    subject: str
    teacher: str
    status: LessonStatus = LessonStatus.normal
    note: str = ""
    key: str = ""

    @property
    def held(self) -> bool:
        """Czy lekcja się odbywa (ma przedmiot i nie jest odwołana)."""
        return bool(self.subject) and self.status is not LessonStatus.cancelled


@dataclass(frozen=True)
class Entry:
    """Wpis z datą: terminarz, zadanie domowe, zebranie, wycieczka, zajęcia, ocena.

    Służy do wyliczania zmian terminów bez modelu. Wolny tekst tych samych
    wpisów trafia osobno do `TextItem`.

    Attributes:
        child: Id dziecka.
        source: Typ treści.
        key: Stabilny klucz wpisu (id z Librusa albo skrót pól identyfikujących).
        day: Data wydarzenia lub termin oddania.
        title: Tytuł lub kategoria wpisu.
        subject: Przedmiot.
        detail: Opis.
        teacher: Nauczyciel.
        at: Godzina, jeśli znana.
        category: Kategoria Librusa (np. „sprawdzian”, „wycieczka”).
    """

    child: str
    source: ContentType
    key: str
    day: date
    title: str
    subject: str = ""
    detail: str = ""
    teacher: str = ""
    at: time | None = None
    category: str = ""

    @property
    def fp(self) -> str:
        """Odcisk treści; zmiana oznacza, że wpis został zmieniony."""
        return fingerprint(self.day, self.at, self.title, self.subject, self.detail, self.category)

    def describe(self) -> str:
        """Krótki opis do powiadomienia, np. „sprawdzian · matematyka · 10:00”."""
        parts = [self.title or self.category, self.subject]
        if self.at:
            parts.append(self.at.strftime("%H:%M"))
        return " · ".join(p for p in parts if p)


@dataclass(frozen=True)
class TextItem:
    """Wolny tekst z dowolnego modułu, kandydat do odczytu przez model (ADR-012).

    Attributes:
        child: Id dziecka (konto, na którym tekst jest widoczny).
        source: Typ treści.
        key: Stabilny klucz w obrębie typu (id z Librusa).
        sent: Data wysłania lub dodania.
        author: Autor (w bazie, nigdy w logach).
        title: Temat lub krótki opis.
        body: Treść; `None`, gdy jeszcze nie pobrano (wiadomości).
        valid_from: Od kiedy tekst dotyczy (np. początek ogłoszenia, data wpisu).
        valid_to: Do kiedy tekst dotyczy (np. koniec ogłoszenia, termin zadania).
    """

    child: str
    source: ContentType
    key: str
    sent: date
    author: str
    title: str
    body: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None

    @property
    def fp(self) -> str:
        """Odcisk treści wpisu; zmiana oznacza, że tekst trzeba przeczytać ponownie."""
        return fingerprint(self.title, self.body, self.valid_from, self.valid_to)


@dataclass(frozen=True)
class StandingLesson:
    """Lekcja planu stałego (`TimetableEntries`): podstawa normy dnia.

    Attributes:
        child: Id dziecka.
        key: Id wpisu planu z Librusa.
        weekday: Dzień tygodnia, 1 = poniedziałek (jak `date.isoweekday`).
        lesson_no: Numer lekcji.
        date_from: Od kiedy wpis obowiązuje.
        date_to: Do kiedy wpis obowiązuje.
    """

    child: str
    key: str
    weekday: int
    lesson_no: int
    date_from: date
    date_to: date

    def valid_on(self, day: date) -> bool:
        """Czy wpis obowiązuje w danym dniu (dzień tygodnia i zakres dat)."""
        return day.isoweekday() == self.weekday and self.date_from <= day <= self.date_to


class PlanChangeKind(StrEnum):
    """Rodzaj zmiany w planie lekcji."""

    cancelled = "cancelled"
    substitution = "substitution"
    shifted = "shifted"


@dataclass(frozen=True)
class PlanChange:
    """Zmiana w planie lekcji (`Calendars/Substitutions`).

    Attributes:
        child: Id dziecka.
        key: Id zmiany z Librusa.
        kind: Odwołanie, zastępstwo albo przesunięcie.
        org_day: Dzień lekcji, której dotyczy zmiana.
        org_lesson_no: Numer tej lekcji.
        subject: Przedmiot lekcji, której dotyczy zmiana.
        teacher: Nauczyciel lekcji, której dotyczy zmiana.
        new_day: Dzień lekcji zastępującej (zastępstwo, przesunięcie).
        new_lesson_no: Numer lekcji zastępującej.
        new_subject: Przedmiot lekcji zastępującej.
        new_teacher: Nauczyciel lekcji zastępującej.
    """

    child: str
    key: str
    kind: PlanChangeKind
    org_day: date
    org_lesson_no: int | None
    subject: str = ""
    teacher: str = ""
    new_day: date | None = None
    new_lesson_no: int | None = None
    new_subject: str = ""
    new_teacher: str = ""

    @property
    def fp(self) -> str:
        """Odcisk zmiany; inny przy tym samym kluczu oznacza poprawioną zmianę."""
        return fingerprint(
            self.kind,
            self.org_day,
            self.org_lesson_no,
            self.subject,
            self.new_day,
            self.new_lesson_no,
            self.new_subject,
        )


@dataclass(frozen=True)
class FreeDay:
    """Dzień wolny szkoły albo klasy, cały lub na część lekcji.

    Attributes:
        child: Id dziecka.
        key: Id z Librusa (z prefiksem zakresu, bo id szkoły i klasy mogą się powtarzać).
        scope: `school` albo `class`.
        name: Nazwa (dla dni klasy może być pusta).
        date_from: Pierwszy dzień.
        date_to: Ostatni dzień.
        lesson_from: Pierwsza wolna lekcja, gdy wolna jest tylko część dnia.
        lesson_to: Ostatnia wolna lekcja.
    """

    child: str
    key: str
    scope: str
    name: str
    date_from: date
    date_to: date
    lesson_from: int | None = None
    lesson_to: int | None = None

    @property
    def fp(self) -> str:
        """Odcisk dnia wolnego."""
        return fingerprint(
            self.name, self.date_from, self.date_to, self.lesson_from, self.lesson_to
        )

    @property
    def whole_day(self) -> bool:
        """Czy wolny jest cały dzień (bez zakresu lekcji)."""
        return self.lesson_from is None and self.lesson_to is None


class Origin(StrEnum):
    """Skąd program wie o odstępstwie."""

    llm = "llm"
    """Z treści wiadomości lub ogłoszenia (model językowy)."""
    timetable = "timetable"
    """Z planu lekcji porównanego z normą."""
    schedule = "schedule"
    """Ze zmiany w terminarzu."""
    homework = "homework"
    """Z prac domowych."""
    plan_change = "plan_change"
    """Ze zmiany planu (odwołanie, zastępstwo) z historii zmian Librusa."""


@dataclass(frozen=True)
class Finding:
    """Wykryte odstępstwo od normalnego dnia, kandydat do powiadomienia.

    Attributes:
        child: Id dziecka.
        day: Dzień, którego dotyczy.
        kind: Rodzaj odstępstwa.
        what: Opis po polsku (krótki, do powiadomienia).
        origin: Źródło wiedzy.
        source_key: Klucz źródła (wiadomości, wpisu lub dnia planu).
        at: Godzina, jeśli znana.
        quote: Dosłowny cytat ze źródła (dla pozycji z modelu zawsze niepusty).
        before: Poprzedni stan przy zmianach („było”).
        confidence: Pewność (1.0 dla pozycji wyliczonych deterministycznie).
        deadline: Termin działania, jeśli inny niż `day` (np. oddanie zgody).
        extra: Dodatkowe dane do szablonu.
    """

    child: str
    day: date
    kind: Kind
    what: str
    origin: Origin
    source_key: str
    at: time | None = None
    quote: str = ""
    before: str = ""
    confidence: float = 1.0
    deadline: date | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def action_day(self) -> date:
        """Dzień, do którego trzeba coś zrobić (termin albo dzień zdarzenia)."""
        return self.deadline or self.day

    @property
    def identity(self) -> str:
        """Tożsamość rzeczy, o której mowa, niezależna od szczegółów.

        Dwie pozycje o tej samej tożsamości i różnej treści to ZMIANA (np. zbiórka
        przesunięta z 7:30 na 7:15). Dla rzeczy do przyniesienia, zgód
        i płatności w tożsamości jest opis, bo jednego dnia może ich być kilka.
        Pozycje z wpisów (terminarz, zadania) i ze zmian planu mają tożsamość po
        kluczu źródła, więc przesunięcie terminu to ZMIANA, a nie nowa pozycja.
        """
        if self.origin in (Origin.schedule, Origin.homework, Origin.plan_change):
            return fingerprint(self.child, self.origin, self.source_key)
        what_key = " ".join(self.what.lower().split())[:60]
        if self.kind in (Kind.bring, Kind.consent, Kind.payment, Kind.other, Kind.homework):
            return fingerprint(self.child, self.day, self.kind, what_key)
        return fingerprint(self.child, self.day, self.kind, self.origin)

    @property
    def content_fp(self) -> str:
        """Odcisk treści; zmiana przy tej samej tożsamości oznacza ZMIANĘ."""
        return fingerprint(self.day, self.at, self.kind, self.what, self.deadline)

    def to_json(self) -> str:
        """Serializuje pozycję do JSON (do zapisu w bazie).

        Returns:
            JSON z wszystkimi polami; daty jako ISO 8601.
        """
        return json.dumps(asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, raw: str) -> Finding:
        """Odtwarza pozycję zapisaną przez `to_json`.

        Args:
            raw: JSON z bazy.

        Returns:
            Odtworzona pozycja.
        """
        d = json.loads(raw)
        return cls(
            child=d["child"],
            day=date.fromisoformat(d["day"]),
            kind=Kind(d["kind"]),
            what=d["what"],
            origin=Origin(d["origin"]),
            source_key=d["source_key"],
            at=time.fromisoformat(d["at"]) if d.get("at") else None,
            quote=d.get("quote", ""),
            before=d.get("before", ""),
            confidence=float(d.get("confidence", 1.0)),
            deadline=date.fromisoformat(d["deadline"]) if d.get("deadline") else None,
            extra=dict(d.get("extra") or {}),
        )
