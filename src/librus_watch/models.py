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

from librus_watch.config import Kind


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
        teacher: Nauczyciel i sala w formacie Librusa.
        status: Stan lekcji.
        note: Adnotacja Librusa (np. zastępujący nauczyciel).
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

    @property
    def held(self) -> bool:
        """Czy lekcja się odbywa (ma przedmiot i nie jest odwołana)."""
        return bool(self.subject) and self.status is not LessonStatus.cancelled


class EntrySource(StrEnum):
    """Skąd pochodzi wpis."""

    schedule = "schedule"
    """Terminarz (sprawdziany, wycieczki, wydarzenia)."""
    homework = "homework"
    """Prace domowe."""


@dataclass(frozen=True)
class Entry:
    """Wpis z terminarza albo praca domowa.

    Attributes:
        child: Id dziecka.
        source: Terminarz albo praca domowa.
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
    source: EntrySource
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


class MessageSource(StrEnum):
    """Rodzaj komunikatu."""

    message = "message"
    announcement = "announcement"


@dataclass(frozen=True)
class MessageRef:
    """Wiadomość albo ogłoszenie.

    Attributes:
        child: Id dziecka (konto, na którym komunikat jest widoczny).
        source: Wiadomość albo ogłoszenie.
        key: Stabilny klucz (id wiadomości z Librusa albo skrót ogłoszenia).
        sent: Data wysłania.
        author: Autor.
        title: Temat.
        body: Treść; `None`, gdy jeszcze nie pobrano.
    """

    child: str
    source: MessageSource
    key: str
    sent: date
    author: str
    title: str
    body: str | None = None


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
        Wpisy terminarza i prace domowe mają tożsamość po kluczu wpisu.
        """
        what_key = " ".join(self.what.lower().split())[:60]
        if self.kind in (Kind.bring, Kind.consent, Kind.payment, Kind.other, Kind.homework):
            return fingerprint(self.child, self.day, self.kind, what_key)
        if self.origin in (Origin.schedule, Origin.homework):
            return fingerprint(self.child, self.origin, self.source_key)
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
