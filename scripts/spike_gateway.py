"""Spike, część 2: które moduły JSON-owego API Synergii działają na koncie rodzica.

Jedno logowanie, tylko żądania GET. Na ekran idą wyłącznie: kod HTTP, klucze
główne, liczba rekordów i nazwy pól pierwszego rekordu (bez żadnych wartości).
Surowe odpowiedzi trafiają do `local/spike/` (ignorowane przez gita, pliki 0600).

Użycie jak `spike_librus.py`:
    uv run python scripts/spike_gateway.py <id_dziecka>
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from spike_librus import login

from librus_watch.secret_env import register_all, setup_logging

API = "https://synergia.librus.pl/gateway/api/2.0/"


def endpoints(today: date) -> list[str]:
    """Lista modułów do sprawdzenia (ADR-012).

    Args:
        today: Bieżąca data (do parametrów tygodnia i zakresu dat).

    Returns:
        Ścieżki względem `API`.
    """
    monday = today - timedelta(days=today.weekday())
    d_from, d_to = today - timedelta(days=14), today + timedelta(days=45)
    return [
        "Me",
        "Classes",
        f"Timetables?weekStart={monday}",
        f"Timetables?weekStart={monday + timedelta(weeks=1)}",
        "Calendars/Substitutions",
        "ClassFreeDays",
        "SchoolFreeDays",
        "HomeWorks",
        "HomeWorks/Categories",
        "HomeWorkAssignments",
        "SchoolNotices",
        "Notes",
        "Notes/Categories",
        "Grades",
        "Grades/Comments",
        "Realizations",
        "ParentTeacherConferences",
        "SchoolTrips",
        f"Timetables/OtherActivitiesRegister?dateFrom={d_from}&dateTo={d_to}"
        "&hideOutdatedEntries=false",
        "Subjects",
        "Users",
    ]


def shape(payload: Any) -> tuple[list[str], int | None, list[str]]:
    """Opisuje kształt odpowiedzi bez wartości.

    Args:
        payload: Odpowiedź JSON.

    Returns:
        Klucze główne (bez `Resources`/`Url`), liczba rekordów pierwszej listy
        (albo dni, gdy to słownik dat) i nazwy pól pierwszego rekordu.
    """
    if not isinstance(payload, dict):
        return [], None, []
    roots = [k for k in payload if k not in ("Resources", "Url")]
    for k in roots:
        v = payload[k]
        if isinstance(v, list):
            first = next((x for x in v if isinstance(x, dict)), {})
            return roots, len(v), sorted(first)
        if isinstance(v, dict):
            if v and all(isinstance(x, list) for x in v.values()):  # Timetables: data -> sloty
                lessons = [ls for slots in v.values() for slot in slots for ls in slot]
                return roots, len(lessons), sorted(lessons[0]) if lessons else []
            return roots, None, sorted(v)
    return roots, None, []


def run(child_id: str) -> None:
    """Loguje się, odświeża token API i sprawdza moduły.

    Args:
        child_id: Id dziecka (jak w `spike_librus.py`).
    """
    client = login(child_id)
    register_all(client.refresh_oauth())
    raw: dict[str, Any] = {"fetched_at": datetime.now().isoformat()}
    print(f"{'moduł':58} {'HTTP':>4} {'rek.':>5}  pola")
    for ep in endpoints(date.today()):
        try:
            r = client.get(API + ep)
        except Exception as e:  # spike: typ błędu i dalej
            print(f"{ep:58} {'ERR':>4} {'':>5}  {type(e).__name__}")
            continue
        try:
            payload = r.json()
        except ValueError:
            payload = None
        raw[ep] = {"status": r.status_code, "json": payload}
        roots, count, fields = shape(payload)
        label = ",".join(fields) if fields else ",".join(roots)
        print(f"{ep[:58]:58} {r.status_code:>4} {'' if count is None else count:>5}  {label[:160]}")

    target = Path("local/spike")
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = target / f"raw-gateway-{child_id}-{datetime.now():%Y%m%d-%H%M%S}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)
    print("\nsurowe dane:", path)


if __name__ == "__main__":
    setup_logging("WARNING")
    run(sys.argv[1])
