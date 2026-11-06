"""Trwały stan programu w SQLite.

Jedna baza w `data_dir/librus-watch.db` (uprawnienia 0600). Tryb WAL
i transakcje zapewniają, że przerwany zapis nie zostawi uszkodzonego stanu.
Schemat jest wersjonowany przez `PRAGMA user_version`.

Tabele:

* `meta`: znaczniki (np. data ostatniego wieczornego podsumowania),
* `fetch_state`: licznik porażek, odstęp i blokada pobierania per dziecko,
* `lessons`: ostatnio pobrany plan lekcji per dzień,
* `day_profile`: planowany początek i koniec dnia (historia do wyliczania normy),
* `entries`: wpisy z datą (terminarz, zadania, zebrania, wycieczki, zajęcia, oceny)
  z datą pierwszego i ostatniego widzenia,
* `texts`: wolne teksty ze wszystkich modułów z treścią (wiadomości pobierane raz)
  i stanem odczytu przez model,
* `plan_changes`: odwołania i zastępstwa z `Calendars/Substitutions`,
* `free_days`: dni wolne szkoły i klasy,
* `lookups`: słowniki API (przedmioty, użytkownicy, kategorie, klasa) per dziecko,
* `module_state`: kiedy moduł API był ostatnio pobrany (moduły rzadko zmienne),
* `standing_plan`: plan stały z `TimetableEntries` (podstawa normy dnia),
* `findings`: wykryte odstępstwa (historia wersji),
* `sent`: co i w jakiej wersji zostało już wysłane.

Wpisy, teksty, zmiany planu i dni wolne mają odcisk treści (`fp`). Zapis zwraca
`Change`, więc dalsze etapy dostają tylko to, co nowe albo zmienione.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from pathlib import Path

from librus_watch.config import ContentType
from librus_watch.models import (
    Entry,
    Finding,
    FreeDay,
    Lesson,
    LessonStatus,
    Origin,
    PlanChange,
    PlanChangeKind,
    StandingLesson,
    TextItem,
)

SCHEMA_VERSION = 4

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fetch_state (
    child TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0,
    next_attempt TEXT,
    blocked_until TEXT,
    last_success TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS lessons (
    child TEXT NOT NULL, day TEXT NOT NULL, number INTEGER NOT NULL,
    start TEXT, "end" TEXT, subject TEXT NOT NULL, teacher TEXT NOT NULL,
    status TEXT NOT NULL, note TEXT NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (child, day, number)
);
CREATE TABLE IF NOT EXISTS day_profile (
    child TEXT NOT NULL, day TEXT NOT NULL, weekday INTEGER NOT NULL,
    first_start TEXT, last_end TEXT, lessons INTEGER NOT NULL,
    PRIMARY KEY (child, day)
);
CREATE TABLE IF NOT EXISTS entries (
    child TEXT NOT NULL, source TEXT NOT NULL, key TEXT NOT NULL,
    day TEXT NOT NULL, at TEXT, title TEXT NOT NULL, subject TEXT NOT NULL,
    detail TEXT NOT NULL, teacher TEXT NOT NULL, category TEXT NOT NULL,
    fp TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    removed_at TEXT,
    PRIMARY KEY (child, source, key)
);
CREATE TABLE IF NOT EXISTS messages (
    child TEXT NOT NULL, source TEXT NOT NULL, key TEXT NOT NULL,
    sent TEXT NOT NULL, author TEXT NOT NULL, title TEXT NOT NULL,
    body TEXT, body_attempts INTEGER NOT NULL DEFAULT 0,
    extracted_at TEXT, extract_attempts INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (child, source, key)
);
CREATE TABLE IF NOT EXISTS findings (
    identity TEXT NOT NULL, content_fp TEXT NOT NULL,
    child TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,
    data TEXT NOT NULL, created_at TEXT NOT NULL, retracted_at TEXT,
    PRIMARY KEY (identity, content_fp)
);
CREATE INDEX IF NOT EXISTS findings_day ON findings (day);
CREATE TABLE IF NOT EXISTS sent (
    identity TEXT PRIMARY KEY, content_fp TEXT NOT NULL,
    alert_type TEXT NOT NULL, sent_at TEXT NOT NULL
);
"""

_V1_TO_V2 = """
CREATE TABLE texts (
    child TEXT NOT NULL, source TEXT NOT NULL, key TEXT NOT NULL,
    sent TEXT NOT NULL, author TEXT NOT NULL, title TEXT NOT NULL, body TEXT,
    valid_from TEXT, valid_to TEXT, fp TEXT NOT NULL,
    body_attempts INTEGER NOT NULL DEFAULT 0,
    extracted_at TEXT, extract_attempts INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    PRIMARY KEY (child, source, key)
);
INSERT INTO texts (child, source, key, sent, author, title, body, fp, body_attempts,
                   extracted_at, extract_attempts, first_seen, last_seen)
SELECT child, CASE source WHEN 'announcement' THEN 'notice' ELSE source END, key, sent,
       author, title, body, '', body_attempts, extracted_at, extract_attempts,
       first_seen, first_seen
FROM messages;
DROP TABLE messages;
CREATE INDEX texts_queue ON texts (extracted_at, sent);

ALTER TABLE lessons RENAME TO lessons_v1;
CREATE TABLE lessons (
    child TEXT NOT NULL, day TEXT NOT NULL, number INTEGER NOT NULL,
    key TEXT NOT NULL DEFAULT '',
    start TEXT, "end" TEXT, subject TEXT NOT NULL, teacher TEXT NOT NULL,
    status TEXT NOT NULL, note TEXT NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (child, day, number, key)
);
INSERT INTO lessons (child, day, number, key, start, "end", subject, teacher, status, note,
                     fetched_at)
SELECT child, day, number, '', start, "end", subject, teacher, status, note, fetched_at
FROM lessons_v1;
DROP TABLE lessons_v1;

UPDATE entries SET source='agenda' WHERE source='schedule';

CREATE TABLE plan_changes (
    child TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL,
    org_day TEXT NOT NULL, org_lesson_no INTEGER, subject TEXT NOT NULL,
    teacher TEXT NOT NULL, new_day TEXT, new_lesson_no INTEGER,
    new_subject TEXT NOT NULL, new_teacher TEXT NOT NULL,
    fp TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    PRIMARY KEY (child, key)
);
CREATE TABLE free_days (
    child TEXT NOT NULL, key TEXT NOT NULL, scope TEXT NOT NULL, name TEXT NOT NULL,
    date_from TEXT NOT NULL, date_to TEXT NOT NULL, lesson_from INTEGER, lesson_to INTEGER,
    fp TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    PRIMARY KEY (child, key)
);
CREATE TABLE lookups (
    child TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY (child, kind, key)
);
CREATE TABLE module_state (
    child TEXT NOT NULL, module TEXT NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (child, module)
);
"""

_V2_TO_V3 = """
ALTER TABLE fetch_state ADD COLUMN logins INTEGER NOT NULL DEFAULT 0;
"""

_V3_TO_V4 = """
CREATE TABLE standing_plan (
    child TEXT NOT NULL, key TEXT NOT NULL, weekday INTEGER NOT NULL,
    lesson_no INTEGER NOT NULL, date_from TEXT NOT NULL, date_to TEXT NOT NULL,
    PRIMARY KEY (child, key)
);
"""

_MIGRATIONS: dict[int, str] = {1: _SCHEMA_V1, 2: _V1_TO_V2, 3: _V2_TO_V3, 4: _V3_TO_V4}
"""Skrypt podnoszący schemat do danej wersji; nowa baza przechodzi przez wszystkie po kolei."""


_FP_TABLES = frozenset({"plan_changes", "free_days"})
"""Tabele obsługiwane przez `Store._upsert_fp` (nazwy wstawiane do SQL tylko z tej listy)."""


class Change(StrEnum):
    """Wynik zapisu rekordu z odciskiem treści."""

    new = "new"
    changed = "changed"
    same = "same"


log = logging.getLogger(__name__)


def _tighten(path: Path, mode: int) -> None:
    """Ustawia uprawnienia i ostrzega, gdy wcześniej były luźniejsze.

    Args:
        path: Plik albo katalog.
        mode: Docelowe uprawnienia (np. 0o700).
    """
    current = stat.S_IMODE(path.stat().st_mode)
    if current != mode:
        log.warning("uprawnienia %s: %o -> %o", path.name, current, mode)
        os.chmod(path, mode)


def _t(v: time | None) -> str | None:
    """Godzina jako tekst HH:MM albo `None`."""
    return v.strftime("%H:%M") if v else None


def _pt(v: str | None) -> time | None:
    """Tekst HH:MM jako godzina albo `None`."""
    return time.fromisoformat(v) if v else None


def _d(v: date | None) -> str | None:
    """Data jako tekst ISO albo `None`."""
    return v.isoformat() if v else None


def _pd(v: str | None) -> date | None:
    """Tekst ISO jako data albo `None`."""
    return date.fromisoformat(v) if v else None


@dataclass(frozen=True)
class FetchState:
    """Stan pobierania jednego dziecka.

    Attributes:
        day: Doba, której dotyczy licznik porażek.
        failures: Liczba porażek w tej dobie.
        logins: Liczba logowań w tej dobie (limit `fetch.max_logins_per_day`).
        next_attempt: Najwcześniejszy moment kolejnej próby.
        blocked_until: Do kiedy próby są wstrzymane (np. po błędzie logowania).
        last_success: Moment ostatniego udanego pobrania.
        last_error: Klasa ostatniego błędu (bez treści, która mogłaby zawierać dane).
    """

    day: date
    failures: int = 0
    logins: int = 0
    next_attempt: datetime | None = None
    blocked_until: datetime | None = None
    last_success: datetime | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class StoredFinding:
    """Wersja odstępstwa zapisana w bazie.

    Attributes:
        finding: Pozycja.
        created_at: Kiedy ta wersja została zapisana.
    """

    finding: Finding
    created_at: datetime


class Store:
    """Dostęp do bazy SQLite programu."""

    def __init__(self, data_dir: Path) -> None:
        """Otwiera (lub tworzy) bazę w katalogu danych z bezpiecznymi uprawnieniami.

        Uprawnienia są ustawiane przy każdym otwarciu, także dla istniejącego
        katalogu i bazy, bo wolumen mógł zostać utworzony z luźniejszymi.
        Pliki `-wal` i `-shm` SQLite tworzy z uprawnieniami pliku bazy.

        Args:
            data_dir: Katalog danych (uprawnienia 0700, baza 0600).
        """
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        _tighten(data_dir, 0o700)
        self.path = data_dir / "librus-watch.db"
        existed = self.path.exists()
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        if existed:
            _tighten(self.path, 0o600)
        else:
            os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def _migrate(self) -> None:
        """Podnosi schemat migracja po migracji, każda w osobnej transakcji.

        `executescript` zatwierdza otwartą transakcję przed startem, dlatego
        transakcja jest częścią samego skryptu, a nie `tx()`. Przerwana
        migracja zostawia bazę w poprzedniej, spójnej wersji.
        """
        version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        for target in range(version + 1, SCHEMA_VERSION + 1):
            try:
                self.db.executescript(
                    f"BEGIN IMMEDIATE;{_MIGRATIONS[target]}PRAGMA user_version={target};COMMIT;"
                )
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
            log.info("schemat bazy: wersja %d -> %d", target - 1, target)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Transakcja: wszystko albo nic.

        SQLite przy niektórych błędach (np. pełny dysk) sam wycofuje transakcję,
        dlatego `ROLLBACK` jest wywoływany tylko wtedy, gdy transakcja trwa:
        inaczej błąd „cannot rollback” przykryłby prawdziwą przyczynę.

        Yields:
            Połączenie z otwartą transakcją.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def close(self) -> None:
        """Zamyka połączenie z bazą."""
        self.db.close()

    # --- meta -----------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        """Odczytuje znacznik.

        Args:
            key: Nazwa znacznika.

        Returns:
            Wartość albo `None`.
        """
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Zapisuje znacznik.

        Args:
            key: Nazwa znacznika.
            value: Wartość.
        """
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # --- fetch_state ----------------------------------------------------------

    def fetch_state(self, child: str, today: date) -> FetchState:
        """Zwraca stan pobierania dziecka; licznik porażek zeruje się z nową dobą.

        Args:
            child: Id dziecka.
            today: Bieżąca data.

        Returns:
            Stan pobierania.
        """
        row = self.db.execute("SELECT * FROM fetch_state WHERE child=?", (child,)).fetchone()
        if row is None:
            return FetchState(day=today)

        def dt(v: str | None) -> datetime | None:
            return datetime.fromisoformat(v) if v else None

        same_day = row["day"] == today.isoformat()
        return FetchState(
            day=today,
            failures=row["failures"] if same_day else 0,
            logins=row["logins"] if same_day else 0,
            next_attempt=dt(row["next_attempt"]),
            blocked_until=dt(row["blocked_until"]),
            last_success=dt(row["last_success"]),
            last_error=row["last_error"],
        )

    def save_fetch_state(self, child: str, st: FetchState) -> None:
        """Zapisuje stan pobierania dziecka.

        Args:
            child: Id dziecka.
            st: Nowy stan.
        """

        def s(v: datetime | None) -> str | None:
            return v.isoformat() if v else None

        self.db.execute(
            "INSERT INTO fetch_state(child, day, failures, logins, next_attempt, blocked_until, "
            "last_success, last_error) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(child) DO UPDATE SET "
            "day=excluded.day, failures=excluded.failures, logins=excluded.logins, "
            "next_attempt=excluded.next_attempt, "
            "blocked_until=excluded.blocked_until, last_success=excluded.last_success, "
            "last_error=excluded.last_error",
            (
                child,
                st.day.isoformat(),
                st.failures,
                st.logins,
                s(st.next_attempt),
                s(st.blocked_until),
                s(st.last_success),
                st.last_error,
            ),
        )

    # --- plan lekcji ----------------------------------------------------------

    def save_lessons(self, child: str, days: dict[date, list[Lesson]], now: datetime) -> None:
        """Zastępuje plan lekcji dla pobranych dni i zapisuje profil dnia.

        Profil (`day_profile`) to planowany początek i koniec dnia liczony ze
        wszystkich lekcji z przedmiotem, także odwołanych, bo norma opisuje
        plan, a nie odstępstwa.

        Args:
            child: Id dziecka.
            days: Lekcje pogrupowane po dniach (dzień bez lekcji = pusta lista).
            now: Moment pobrania.
        """
        with self.tx() as db:
            for day, lessons in days.items():
                db.execute("DELETE FROM lessons WHERE child=? AND day=?", (child, day.isoformat()))
                for ls in lessons:
                    db.execute(
                        'INSERT OR REPLACE INTO lessons(child, day, number, key, start, "end", '
                        "subject, teacher, status, note, fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            child,
                            day.isoformat(),
                            ls.number,
                            ls.key,
                            _t(ls.start),
                            _t(ls.end),
                            ls.subject,
                            ls.teacher,
                            ls.status.value,
                            ls.note,
                            now.isoformat(),
                        ),
                    )
                planned = [ls for ls in lessons if ls.subject and ls.start and ls.end]
                starts = [ls.start for ls in planned if ls.start]
                ends = [ls.end for ls in planned if ls.end]
                db.execute(
                    "INSERT INTO day_profile VALUES(?,?,?,?,?,?) ON CONFLICT(child, day) DO UPDATE "
                    "SET first_start=excluded.first_start, last_end=excluded.last_end, "
                    "lessons=excluded.lessons",
                    (
                        child,
                        day.isoformat(),
                        day.weekday(),
                        _t(min(starts)) if starts else None,
                        _t(max(ends)) if ends else None,
                        len(planned),
                    ),
                )

    def lessons(self, child: str, day: date) -> list[Lesson]:
        """Plan lekcji dziecka na dany dzień.

        Args:
            child: Id dziecka.
            day: Dzień.

        Returns:
            Lekcje posortowane po numerze (pusta lista, gdy brak danych).
        """
        rows = self.db.execute(
            "SELECT * FROM lessons WHERE child=? AND day=? ORDER BY number, key",
            (child, day.isoformat()),
        ).fetchall()
        return [
            Lesson(
                child=r["child"],
                day=day,
                number=r["number"],
                start=_pt(r["start"]),
                end=_pt(r["end"]),
                subject=r["subject"],
                teacher=r["teacher"],
                status=LessonStatus(r["status"]),
                note=r["note"],
                key=r["key"],
            )
            for r in rows
        ]

    def has_timetable(self, child: str, day: date) -> bool:
        """Czy plan lekcji na ten dzień był kiedykolwiek pobrany.

        Args:
            child: Id dziecka.
            day: Dzień.

        Returns:
            `True`, gdy istnieje profil dnia.
        """
        row = self.db.execute(
            "SELECT 1 FROM day_profile WHERE child=? AND day=?", (child, day.isoformat())
        ).fetchone()
        return row is not None

    def profiles(
        self, child: str, weekday: int, before: date, limit: int
    ) -> list[tuple[time | None, time | None, int]]:
        """Historia profili danego dnia tygodnia (do wyliczania normy).

        Args:
            child: Id dziecka.
            weekday: Dzień tygodnia (0 = poniedziałek).
            before: Tylko dni wcześniejsze niż ta data.
            limit: Maksymalna liczba ostatnich dni.

        Returns:
            Krotki (pierwsza lekcja, ostatnia lekcja, liczba lekcji), od najnowszych.
        """
        rows = self.db.execute(
            "SELECT first_start, last_end, lessons FROM day_profile "
            "WHERE child=? AND weekday=? AND day<? ORDER BY day DESC LIMIT ?",
            (child, weekday, before.isoformat(), limit),
        ).fetchall()
        return [(_pt(r["first_start"]), _pt(r["last_end"]), int(r["lessons"])) for r in rows]

    def save_standing(self, child: str, items: list[StandingLesson]) -> None:
        """Zastępuje plan stały dziecka nową wersją.

        Args:
            child: Id dziecka.
            items: Wpisy planu stałego.
        """
        with self.tx() as db:
            db.execute("DELETE FROM standing_plan WHERE child=?", (child,))
            db.executemany(
                "INSERT INTO standing_plan VALUES(?,?,?,?,?,?)",
                [
                    (
                        child,
                        i.key,
                        i.weekday,
                        i.lesson_no,
                        i.date_from.isoformat(),
                        i.date_to.isoformat(),
                    )
                    for i in items
                ],
            )

    def standing(self, child: str) -> list[StandingLesson]:
        """Plan stały dziecka.

        Args:
            child: Id dziecka.

        Returns:
            Wpisy planu stałego (pusta lista, gdy brak).
        """
        rows = self.db.execute(
            "SELECT * FROM standing_plan WHERE child=? ORDER BY weekday, lesson_no, key",
            (child,),
        ).fetchall()
        return [
            StandingLesson(
                child=r["child"],
                key=r["key"],
                weekday=r["weekday"],
                lesson_no=r["lesson_no"],
                date_from=date.fromisoformat(r["date_from"]),
                date_to=date.fromisoformat(r["date_to"]),
            )
            for r in rows
        ]

    def bell_times(self, child: str) -> dict[int, tuple[time, time]]:
        """Godziny lekcji (dzwonki): najczęstsza para początek-koniec dla numeru lekcji.

        Args:
            child: Id dziecka.

        Returns:
            Numer lekcji -> (początek, koniec), z zapisanych planów lekcji.
        """
        rows = self.db.execute(
            'SELECT number, start, "end", COUNT(*) AS n FROM lessons '
            'WHERE child=? AND start IS NOT NULL AND "end" IS NOT NULL '
            'GROUP BY number, start, "end" ORDER BY number, n DESC',
            (child,),
        ).fetchall()
        out: dict[int, tuple[time, time]] = {}
        for r in rows:
            start, end = _pt(r["start"]), _pt(r["end"])
            if start and end:
                out.setdefault(int(r["number"]), (start, end))
        return out

    # --- terminarz i prace domowe --------------------------------------------

    def entries(self, child: str, source: ContentType, *, active_only: bool = True) -> list[Entry]:
        """Zapisane wpisy danego źródła.

        Args:
            child: Id dziecka.
            source: Terminarz albo prace domowe.
            active_only: Pomija wpisy oznaczone jako usunięte.

        Returns:
            Lista wpisów.
        """
        sql = "SELECT * FROM entries WHERE child=? AND source=?"
        if active_only:
            sql += " AND removed_at IS NULL"
        rows = self.db.execute(sql, (child, source.value)).fetchall()
        return [
            Entry(
                child=r["child"],
                source=ContentType(r["source"]),
                key=r["key"],
                day=date.fromisoformat(r["day"]),
                title=r["title"],
                subject=r["subject"],
                detail=r["detail"],
                teacher=r["teacher"],
                at=_pt(r["at"]),
                category=r["category"],
            )
            for r in rows
        ]

    def upsert_entry(self, e: Entry, now: datetime) -> Change:
        """Zapisuje wpis; zmiana treści albo powrót usuniętego wpisu to `changed`.

        Args:
            e: Wpis.
            now: Moment pobrania.

        Returns:
            Czy wpis jest nowy, zmieniony, czy bez zmian.
        """
        row = self.db.execute(
            "SELECT fp, removed_at FROM entries WHERE child=? AND source=? AND key=?",
            (e.child, e.source.value, e.key),
        ).fetchone()
        change = (
            Change.new
            if row is None
            else Change.same
            if row["fp"] == e.fp and row["removed_at"] is None
            else Change.changed
        )
        self.db.execute(
            "INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL) "
            "ON CONFLICT(child, source, key) DO UPDATE SET day=excluded.day, at=excluded.at, "
            "title=excluded.title, subject=excluded.subject, detail=excluded.detail, "
            "teacher=excluded.teacher, category=excluded.category, fp=excluded.fp, "
            "last_seen=excluded.last_seen, removed_at=NULL",
            (
                e.child,
                e.source.value,
                e.key,
                e.day.isoformat(),
                _t(e.at),
                e.title,
                e.subject,
                e.detail,
                e.teacher,
                e.category,
                e.fp,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        return change

    def mark_entry_removed(self, e: Entry, now: datetime) -> None:
        """Oznacza wpis jako usunięty z Librusa (rekord zostaje w historii).

        Args:
            e: Wpis.
            now: Moment wykrycia.
        """
        self.db.execute(
            "UPDATE entries SET removed_at=? WHERE child=? AND source=? AND key=?",
            (now.isoformat(), e.child, e.source.value, e.key),
        )

    # --- wolne teksty ---------------------------------------------------------

    def known_text(self, child: str, source: ContentType, key: str) -> bool:
        """Czy tekst jest już w bazie.

        Args:
            child: Id dziecka.
            source: Typ treści.
            key: Klucz tekstu.

        Returns:
            `True`, gdy tekst był już widziany.
        """
        row = self.db.execute(
            "SELECT 1 FROM texts WHERE child=? AND source=? AND key=?",
            (child, source.value, key),
        ).fetchone()
        return row is not None

    def upsert_text(self, t: TextItem, now: datetime) -> Change:
        """Zapisuje tekst; zmieniony wraca do kolejki modelu.

        Treść `None` (wiadomość z listy, jeszcze bez treści) nie nadpisuje
        pobranej wcześniej. Tekst przeniesiony migracją (pusty odcisk) przyjmuje
        nowy odcisk bez oznaczania zmiany, żeby nie wracał do modelu bez powodu.

        Args:
            t: Tekst.
            now: Moment pobrania.

        Returns:
            Czy tekst jest nowy, zmieniony, czy bez zmian.
        """
        row = self.db.execute(
            "SELECT fp FROM texts WHERE child=? AND source=? AND key=?",
            (t.child, t.source.value, t.key),
        ).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO texts(child, source, key, sent, author, title, body, valid_from, "
                "valid_to, fp, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    t.child,
                    t.source.value,
                    t.key,
                    t.sent.isoformat(),
                    t.author,
                    t.title,
                    t.body,
                    _d(t.valid_from),
                    _d(t.valid_to),
                    t.fp,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            return Change.new
        where = (t.child, t.source.value, t.key)
        if row["fp"] in (t.fp, ""):
            self.db.execute(
                "UPDATE texts SET fp=?, last_seen=? WHERE child=? AND source=? AND key=?",
                (t.fp, now.isoformat(), *where),
            )
            return Change.same
        self.db.execute(
            "UPDATE texts SET sent=?, author=?, title=?, body=COALESCE(?, body), valid_from=?, "
            "valid_to=?, fp=?, last_seen=?, extracted_at=NULL, extract_attempts=0 "
            "WHERE child=? AND source=? AND key=?",
            (
                t.sent.isoformat(),
                t.author,
                t.title,
                t.body,
                _d(t.valid_from),
                _d(t.valid_to),
                t.fp,
                now.isoformat(),
                *where,
            ),
        )
        return Change.changed

    def texts(self, child: str, source: ContentType | None = None) -> list[TextItem]:
        """Teksty dziecka, opcjonalnie jednego typu.

        Args:
            child: Id dziecka.
            source: Typ treści albo `None` dla wszystkich.

        Returns:
            Teksty od najnowszego.
        """
        sql, args = "SELECT * FROM texts WHERE child=?", [child]
        if source is not None:
            sql += " AND source=?"
            args.append(source.value)
        rows = self.db.execute(sql + " ORDER BY sent DESC, key", args).fetchall()
        return [self._text(r) for r in rows]

    def texts_without_body(
        self, child: str, source: ContentType, max_attempts: int
    ) -> list[TextItem]:
        """Teksty, których treści jeszcze nie udało się pobrać (wiadomości).

        Args:
            child: Id dziecka.
            source: Typ treści.
            max_attempts: Pomija teksty z taką lub większą liczbą nieudanych prób.

        Returns:
            Teksty bez treści.
        """
        rows = self.db.execute(
            "SELECT * FROM texts WHERE child=? AND source=? AND body IS NULL AND body_attempts<?",
            (child, source.value, max_attempts),
        ).fetchall()
        return [self._text(r) for r in rows]

    def set_body(self, t: TextItem, body: str | None) -> None:
        """Zapisuje pobraną treść albo odnotowuje nieudaną próbę.

        Args:
            t: Tekst.
            body: Treść albo `None` przy porażce.
        """
        if body is None:
            self.db.execute(
                "UPDATE texts SET body_attempts=body_attempts+1 "
                "WHERE child=? AND source=? AND key=?",
                (t.child, t.source.value, t.key),
            )
        else:
            self.db.execute(
                "UPDATE texts SET body=? WHERE child=? AND source=? AND key=?",
                (body, t.child, t.source.value, t.key),
            )

    def texts_to_extract(self, max_attempts: int) -> list[TextItem]:
        """Teksty z treścią, które czekają na odczyt przez model.

        Args:
            max_attempts: Pomija teksty z taką lub większą liczbą nieudanych odczytów.

        Returns:
            Teksty od najstarszego.
        """
        rows = self.db.execute(
            "SELECT * FROM texts WHERE body IS NOT NULL AND extracted_at IS NULL "
            "AND extract_attempts<? ORDER BY sent, key",
            (max_attempts,),
        ).fetchall()
        return [self._text(r) for r in rows]

    def mark_extracted(self, t: TextItem, now: datetime | None) -> None:
        """Odnotowuje wynik odczytu przez model.

        Args:
            t: Tekst.
            now: Moment udanego odczytu albo `None` przy porażce (zwiększa licznik prób).
        """
        if now is None:
            self.db.execute(
                "UPDATE texts SET extract_attempts=extract_attempts+1 "
                "WHERE child=? AND source=? AND key=?",
                (t.child, t.source.value, t.key),
            )
        else:
            self.db.execute(
                "UPDATE texts SET extracted_at=? WHERE child=? AND source=? AND key=?",
                (now.isoformat(), t.child, t.source.value, t.key),
            )

    @staticmethod
    def _text(r: sqlite3.Row) -> TextItem:
        """Wiersz tabeli `texts` jako `TextItem`."""
        return TextItem(
            child=r["child"],
            source=ContentType(r["source"]),
            key=r["key"],
            sent=date.fromisoformat(r["sent"]),
            author=r["author"],
            title=r["title"],
            body=r["body"],
            valid_from=_pd(r["valid_from"]),
            valid_to=_pd(r["valid_to"]),
        )

    # --- zmiany planu i dni wolne ----------------------------------------------

    def _upsert_fp(
        self, table: str, child: str, key: str, fp: str, values: dict[str, object], now: datetime
    ) -> Change:
        """Wspólny zapis rekordu z odciskiem treści dla tabel z kluczem (child, key).

        Args:
            table: `plan_changes` albo `free_days` (stała w kodzie, nie dane z zewnątrz).
            child: Id dziecka.
            key: Klucz rekordu.
            fp: Odcisk treści.
            values: Pozostałe kolumny.
            now: Moment pobrania.

        Returns:
            Czy rekord jest nowy, zmieniony, czy bez zmian.
        """
        if table not in _FP_TABLES:
            raise ValueError(f"nieznana tabela: {table}")
        row = self.db.execute(
            f"SELECT fp FROM {table} WHERE child=? AND key=?",  # noqa: S608 - tabela z _FP_TABLES
            (child, key),
        ).fetchone()
        change = Change.new if row is None else Change.same if row["fp"] == fp else Change.changed
        cols = ["child", "key", *values, "fp", "first_seen", "last_seen"]
        updates = ", ".join(f"{c}=excluded.{c}" for c in [*values, "fp", "last_seen"])
        self.db.execute(
            f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(child, key) DO UPDATE SET {updates}",
            (child, key, *values.values(), fp, now.isoformat(), now.isoformat()),
        )
        return change

    def upsert_plan_change(self, pc: PlanChange, now: datetime) -> Change:
        """Zapisuje zmianę planu lekcji.

        Args:
            pc: Zmiana.
            now: Moment pobrania.

        Returns:
            Czy zmiana jest nowa, poprawiona, czy bez zmian.
        """
        return self._upsert_fp(
            "plan_changes",
            pc.child,
            pc.key,
            pc.fp,
            {
                "kind": pc.kind.value,
                "org_day": pc.org_day.isoformat(),
                "org_lesson_no": pc.org_lesson_no,
                "subject": pc.subject,
                "teacher": pc.teacher,
                "new_day": _d(pc.new_day),
                "new_lesson_no": pc.new_lesson_no,
                "new_subject": pc.new_subject,
                "new_teacher": pc.new_teacher,
            },
            now,
        )

    def plan_changes(self, child: str) -> list[PlanChange]:
        """Zmiany planu dziecka.

        Args:
            child: Id dziecka.

        Returns:
            Zmiany od najwcześniejszego dnia.
        """
        rows = self.db.execute(
            "SELECT * FROM plan_changes WHERE child=? ORDER BY org_day, org_lesson_no, key",
            (child,),
        ).fetchall()
        return [
            PlanChange(
                child=r["child"],
                key=r["key"],
                kind=PlanChangeKind(r["kind"]),
                org_day=date.fromisoformat(r["org_day"]),
                org_lesson_no=r["org_lesson_no"],
                subject=r["subject"],
                teacher=r["teacher"],
                new_day=_pd(r["new_day"]),
                new_lesson_no=r["new_lesson_no"],
                new_subject=r["new_subject"],
                new_teacher=r["new_teacher"],
            )
            for r in rows
        ]

    def upsert_free_day(self, fd: FreeDay, now: datetime) -> Change:
        """Zapisuje dzień wolny.

        Args:
            fd: Dzień wolny.
            now: Moment pobrania.

        Returns:
            Czy dzień jest nowy, zmieniony, czy bez zmian.
        """
        return self._upsert_fp(
            "free_days",
            fd.child,
            fd.key,
            fd.fp,
            {
                "scope": fd.scope,
                "name": fd.name,
                "date_from": fd.date_from.isoformat(),
                "date_to": fd.date_to.isoformat(),
                "lesson_from": fd.lesson_from,
                "lesson_to": fd.lesson_to,
            },
            now,
        )

    def free_days(self, child: str) -> list[FreeDay]:
        """Dni wolne dziecka.

        Args:
            child: Id dziecka.

        Returns:
            Dni wolne od najwcześniejszego.
        """
        rows = self.db.execute(
            "SELECT * FROM free_days WHERE child=? ORDER BY date_from, key", (child,)
        ).fetchall()
        return [
            FreeDay(
                child=r["child"],
                key=r["key"],
                scope=r["scope"],
                name=r["name"],
                date_from=date.fromisoformat(r["date_from"]),
                date_to=date.fromisoformat(r["date_to"]),
                lesson_from=r["lesson_from"],
                lesson_to=r["lesson_to"],
            )
            for r in rows
        ]

    # --- słowniki i stan modułów -----------------------------------------------

    def save_lookups(self, child: str, kind: str, values: dict[str, str]) -> None:
        """Zastępuje słownik danego rodzaju (np. przedmioty) nową wersją.

        Args:
            child: Id dziecka (słowniki zależą od szkoły).
            kind: Rodzaj słownika, np. `subjects`.
            values: Id -> nazwa.
        """
        with self.tx() as db:
            db.execute("DELETE FROM lookups WHERE child=? AND kind=?", (child, kind))
            db.executemany(
                "INSERT INTO lookups VALUES(?,?,?,?)",
                [(child, kind, k, v) for k, v in values.items()],
            )

    def lookups(self, child: str, kind: str) -> dict[str, str]:
        """Słownik danego rodzaju.

        Args:
            child: Id dziecka.
            kind: Rodzaj słownika.

        Returns:
            Id -> nazwa (pusty, gdy brak).
        """
        rows = self.db.execute(
            "SELECT key, value FROM lookups WHERE child=? AND kind=?", (child, kind)
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def mark_module(self, child: str, module: str, now: datetime) -> None:
        """Odnotowuje udane pobranie modułu API.

        Args:
            child: Id dziecka.
            module: Nazwa modułu.
            now: Moment pobrania.
        """
        self.db.execute(
            "INSERT INTO module_state VALUES(?,?,?) "
            "ON CONFLICT(child, module) DO UPDATE SET fetched_at=excluded.fetched_at",
            (child, module, now.isoformat()),
        )

    def module_fetched_at(self, child: str, module: str) -> datetime | None:
        """Kiedy moduł był ostatnio pobrany.

        Args:
            child: Id dziecka.
            module: Nazwa modułu.

        Returns:
            Moment pobrania albo `None`.
        """
        row = self.db.execute(
            "SELECT fetched_at FROM module_state WHERE child=? AND module=?", (child, module)
        ).fetchone()
        return datetime.fromisoformat(row["fetched_at"]) if row else None

    # --- odstępstwa i wysyłka --------------------------------------------------

    def add_finding(self, f: Finding, now: datetime) -> bool:
        """Zapisuje odstępstwo, jeśli ta wersja jest nowa.

        Args:
            f: Odstępstwo.
            now: Moment wykrycia.

        Returns:
            `True`, gdy zapisano nową wersję.
        """
        cur = self.db.execute(
            "INSERT INTO findings VALUES(?,?,?,?,?,?,?,NULL) ON CONFLICT DO NOTHING",
            (
                f.identity,
                f.content_fp,
                f.child,
                f.day.isoformat(),
                f.kind.value,
                f.to_json(),
                now.isoformat(),
            ),
        )
        return cur.rowcount > 0

    def retract_identity(self, identity: str, now: datetime) -> None:
        """Wycofuje wszystkie wersje odstępstwa (np. lekcja jednak się odbywa).

        Args:
            identity: Tożsamość odstępstwa.
            now: Moment wycofania.
        """
        self.db.execute(
            "UPDATE findings SET retracted_at=? WHERE identity=? AND retracted_at IS NULL",
            (now.isoformat(), identity),
        )

    def current_findings(self, day_from: date, day_to: date) -> list[StoredFinding]:
        """Najnowsza, niewycofana wersja każdego odstępstwa w zakresie dni.

        Pozycja wpada do zakresu, gdy jej dzień albo termin działania leży
        w przedziale `[day_from, day_to]`.

        Args:
            day_from: Początek zakresu (włącznie).
            day_to: Koniec zakresu (włącznie).

        Returns:
            Pozycje posortowane po dniu.
        """
        rows = self.db.execute(
            "SELECT f.data, f.created_at FROM findings f JOIN ("
            "  SELECT identity, MAX(created_at) AS mx FROM findings GROUP BY identity"
            ") last ON f.identity=last.identity AND f.created_at=last.mx "
            "WHERE f.retracted_at IS NULL",
        ).fetchall()
        out = []
        for r in rows:
            f = Finding.from_json(r["data"])
            if day_from <= f.day <= day_to or day_from <= f.action_day <= day_to:
                out.append(StoredFinding(f, datetime.fromisoformat(r["created_at"])))
        return sorted(out, key=lambda s: (s.finding.action_day, s.finding.child))

    def child_findings(
        self, child: str, origins: set[Origin], day_from: date, day_to: date
    ) -> list[Finding]:
        """Bieżące odstępstwa dziecka z danych źródeł w zakresie dni.

        Args:
            child: Id dziecka.
            origins: Źródła, np. plan lekcji i zmiany planu.
            day_from: Początek zakresu (włącznie).
            day_to: Koniec zakresu (włącznie).

        Returns:
            Najnowsze, niewycofane wersje.
        """
        return [
            s.finding
            for s in self.current_findings(day_from, day_to)
            if s.finding.child == child and s.finding.origin in origins
        ]

    def previous_version(self, f: Finding) -> Finding | None:
        """Poprzednia wersja odstępstwa (do komunikatu „było… jest…”).

        Args:
            f: Bieżąca wersja.

        Returns:
            Ostatnia wcześniejsza wersja z inną treścią albo `None`.
        """
        row = self.db.execute(
            "SELECT data FROM findings WHERE identity=? AND content_fp<>? "
            "ORDER BY created_at DESC LIMIT 1",
            (f.identity, f.content_fp),
        ).fetchone()
        return Finding.from_json(row["data"]) if row else None

    def sent_fp(self, identity: str) -> str | None:
        """Wersja odstępstwa, która została wysłana jako ostatnia.

        Args:
            identity: Tożsamość odstępstwa.

        Returns:
            Odcisk treści wysłanej wersji albo `None`, gdy nic nie wysłano.
        """
        row = self.db.execute(
            "SELECT content_fp FROM sent WHERE identity=?", (identity,)
        ).fetchone()
        return str(row["content_fp"]) if row else None

    def mark_sent(self, f: Finding, alert_type: str, now: datetime) -> None:
        """Odnotowuje wysłanie wersji odstępstwa.

        Args:
            f: Wysłana pozycja.
            alert_type: Typ powiadomienia, w którym poszła.
            now: Moment wysłania.
        """
        self.db.execute(
            "INSERT INTO sent VALUES(?,?,?,?) ON CONFLICT(identity) DO UPDATE SET "
            "content_fp=excluded.content_fp, alert_type=excluded.alert_type, "
            "sent_at=excluded.sent_at",
            (f.identity, f.content_fp, alert_type, now.isoformat()),
        )

    # --- porządki -------------------------------------------------------------

    def purge(self, older_than: date) -> None:
        """Usuwa dane starsze niż próg (minimalizacja przechowywanych danych).

        Args:
            older_than: Dni wcześniejsze niż ta data są usuwane.
        """
        d = older_than.isoformat()
        removed = 0
        with self.tx() as db:
            for sql in (
                "DELETE FROM lessons WHERE day<?",
                "DELETE FROM day_profile WHERE day<?",
                "DELETE FROM entries WHERE day<?",
                "DELETE FROM texts WHERE COALESCE(valid_to, sent)<?",
                "DELETE FROM plan_changes WHERE COALESCE(new_day, org_day)<?",
                "DELETE FROM free_days WHERE date_to<?",
                "DELETE FROM findings WHERE day<?",
            ):
                removed += db.execute(sql, (d,)).rowcount
        log.info("porządki: usunięto %d rekordów sprzed %s", removed, d)
