"""Trwały stan programu w SQLite.

Jedna baza w `data_dir/librus-watch.db` (uprawnienia 0600). Tryb WAL
i transakcje zapewniają, że przerwany zapis nie zostawi uszkodzonego stanu.
Schemat jest wersjonowany przez `PRAGMA user_version`.

Tabele:

* `meta`: znaczniki (np. data ostatniego wieczornego podsumowania),
* `fetch_state`: licznik porażek, odstęp i blokada pobierania per dziecko,
* `lessons`: ostatnio pobrany plan lekcji per dzień,
* `day_profile`: planowany początek i koniec dnia (historia do wyliczania normy),
* `entries`: terminarz i prace domowe z datą pierwszego i ostatniego widzenia,
* `messages`: wiadomości i ogłoszenia z treścią (pobieraną raz) i stanem analizy,
* `findings`: wykryte odstępstwa (historia wersji),
* `sent`: co i w jakiej wersji zostało już wysłane.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

from librus_watch.models import (
    Entry,
    EntrySource,
    Finding,
    Lesson,
    LessonStatus,
    MessageRef,
    MessageSource,
)

SCHEMA_VERSION = 1

_SCHEMA = """
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


def _t(v: time | None) -> str | None:
    """Godzina jako tekst HH:MM albo `None`."""
    return v.strftime("%H:%M") if v else None


def _pt(v: str | None) -> time | None:
    """Tekst HH:MM jako godzina albo `None`."""
    return time.fromisoformat(v) if v else None


@dataclass(frozen=True)
class FetchState:
    """Stan pobierania jednego dziecka.

    Attributes:
        day: Doba, której dotyczy licznik porażek.
        failures: Liczba porażek w tej dobie.
        next_attempt: Najwcześniejszy moment kolejnej próby.
        blocked_until: Do kiedy próby są wstrzymane (np. po błędzie logowania).
        last_success: Moment ostatniego udanego pobrania.
        last_error: Klasa ostatniego błędu (bez treści, która mogłaby zawierać dane).
    """

    day: date
    failures: int = 0
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
        os.chmod(data_dir, 0o700)
        self.path = data_dir / "librus-watch.db"
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def _migrate(self) -> None:
        """Tworzy schemat i podbija wersję (kolejne migracje dopisujemy tutaj).

        `executescript` zatwierdza otwartą transakcję przed startem, dlatego
        transakcja jest częścią samego skryptu, a nie `tx()`.
        """
        version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        if version < SCHEMA_VERSION:
            try:
                self.db.executescript(
                    f"BEGIN IMMEDIATE;{_SCHEMA}PRAGMA user_version={SCHEMA_VERSION};COMMIT;"
                )
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Transakcja: wszystko albo nic.

        Yields:
            Połączenie z otwartą transakcją.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
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
            "INSERT INTO fetch_state(child, day, failures, next_attempt, blocked_until, "
            "last_success, last_error) VALUES(?,?,?,?,?,?,?) ON CONFLICT(child) DO UPDATE SET "
            "day=excluded.day, failures=excluded.failures, next_attempt=excluded.next_attempt, "
            "blocked_until=excluded.blocked_until, last_success=excluded.last_success, "
            "last_error=excluded.last_error",
            (
                child,
                st.day.isoformat(),
                st.failures,
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
                        "INSERT INTO lessons VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            child,
                            day.isoformat(),
                            ls.number,
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
            "SELECT * FROM lessons WHERE child=? AND day=? ORDER BY number",
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

    # --- terminarz i prace domowe --------------------------------------------

    def entries(self, child: str, source: EntrySource, *, active_only: bool = True) -> list[Entry]:
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
                source=EntrySource(r["source"]),
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

    def upsert_entry(self, e: Entry, now: datetime) -> None:
        """Zapisuje nową lub zmienioną wersję wpisu (i cofa ewentualne usunięcie).

        Args:
            e: Wpis.
            now: Moment pobrania.
        """
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

    # --- wiadomości -----------------------------------------------------------

    def known_message(self, child: str, source: MessageSource, key: str) -> bool:
        """Czy komunikat jest już w bazie.

        Args:
            child: Id dziecka.
            source: Wiadomość albo ogłoszenie.
            key: Klucz komunikatu.

        Returns:
            `True`, gdy komunikat był już widziany.
        """
        row = self.db.execute(
            "SELECT 1 FROM messages WHERE child=? AND source=? AND key=?",
            (child, source.value, key),
        ).fetchone()
        return row is not None

    def add_message(self, m: MessageRef, now: datetime) -> None:
        """Dodaje komunikat (bez nadpisywania istniejącego).

        Args:
            m: Komunikat; treść może być pusta, jeśli jeszcze jej nie pobrano.
            now: Moment pobrania.
        """
        self.db.execute(
            "INSERT INTO messages(child, source, key, sent, author, title, body, first_seen) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (
                m.child,
                m.source.value,
                m.key,
                m.sent.isoformat(),
                m.author,
                m.title,
                m.body,
                now.isoformat(),
            ),
        )

    def messages_without_body(self, child: str, max_attempts: int) -> list[MessageRef]:
        """Wiadomości, których treści jeszcze nie udało się pobrać.

        Args:
            child: Id dziecka.
            max_attempts: Pomija wiadomości z taką lub większą liczbą nieudanych prób.

        Returns:
            Wiadomości bez treści.
        """
        rows = self.db.execute(
            "SELECT * FROM messages WHERE child=? AND body IS NULL AND body_attempts<?",
            (child, max_attempts),
        ).fetchall()
        return [self._msg(r) for r in rows]

    def set_body(self, m: MessageRef, body: str | None) -> None:
        """Zapisuje pobraną treść albo odnotowuje nieudaną próbę.

        Args:
            m: Komunikat.
            body: Treść albo `None` przy porażce.
        """
        if body is None:
            self.db.execute(
                "UPDATE messages SET body_attempts=body_attempts+1 "
                "WHERE child=? AND source=? AND key=?",
                (m.child, m.source.value, m.key),
            )
        else:
            self.db.execute(
                "UPDATE messages SET body=? WHERE child=? AND source=? AND key=?",
                (body, m.child, m.source.value, m.key),
            )

    def messages_to_extract(self, max_attempts: int) -> list[MessageRef]:
        """Komunikaty z treścią, które czekają na analizę modelem.

        Args:
            max_attempts: Pomija komunikaty z taką lub większą liczbą nieudanych analiz.

        Returns:
            Komunikaty od najstarszego.
        """
        rows = self.db.execute(
            "SELECT * FROM messages WHERE body IS NOT NULL AND extracted_at IS NULL "
            "AND extract_attempts<? ORDER BY sent",
            (max_attempts,),
        ).fetchall()
        return [self._msg(r) for r in rows]

    def mark_extracted(self, m: MessageRef, now: datetime | None) -> None:
        """Odnotowuje wynik analizy.

        Args:
            m: Komunikat.
            now: Moment udanej analizy albo `None` przy porażce (zwiększa licznik prób).
        """
        if now is None:
            self.db.execute(
                "UPDATE messages SET extract_attempts=extract_attempts+1 "
                "WHERE child=? AND source=? AND key=?",
                (m.child, m.source.value, m.key),
            )
        else:
            self.db.execute(
                "UPDATE messages SET extracted_at=? WHERE child=? AND source=? AND key=?",
                (now.isoformat(), m.child, m.source.value, m.key),
            )

    @staticmethod
    def _msg(r: sqlite3.Row) -> MessageRef:
        """Wiersz tabeli `messages` jako `MessageRef`."""
        return MessageRef(
            child=r["child"],
            source=MessageSource(r["source"]),
            key=r["key"],
            sent=date.fromisoformat(r["sent"]),
            author=r["author"],
            title=r["title"],
            body=r["body"],
        )

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
        with self.tx() as db:
            db.execute("DELETE FROM lessons WHERE day<?", (d,))
            db.execute("DELETE FROM day_profile WHERE day<?", (d,))
            db.execute("DELETE FROM entries WHERE day<?", (d,))
            db.execute("DELETE FROM messages WHERE sent<?", (d,))
            db.execute("DELETE FROM findings WHERE day<?", (d,))
