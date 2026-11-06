"""Spike: co `librus-apix` zwraca dla jednego konta (tylko odczyt, jedno logowanie).

Nie otwiera treści wiadomości (Librus oznaczyłby je jako przeczytane) ani nie
wywołuje funkcji wysyłania. Surowe dane trafiają do `local/spike/` (ignorowane
przez gita, pliki 0600). Na ekran idzie tylko struktura: liczby, nazwy pól
i próbki z zamaskowanymi wyrazami od wielkiej litery (nazwiska, imiona).

Użycie:
    LIBRUS_X_LOGIN_FILE=secrets/librus_x_login \
    LIBRUS_X_PASSWORD_FILE=secrets/librus_x_password \
    uv run python scripts/spike_librus.py x
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from librus_apix import announcements, homework, messages, schedule, timetable
from librus_apix.client import Client, Token
from requests.cookies import RequestsCookieJar

from librus_watch.secret_env import get_secret, register_all, setup_logging

TIMEOUT = 30
_CAPWORD = re.compile(r"\b[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż.-]*")


def mask(value: Any) -> Any:
    """Ukrywa wyrazy od wielkiej litery, zostawia cyfry, interpunkcję i małe litery.

    Args:
        value: Dowolna wartość z danych Librusa.

    Returns:
        Ta sama struktura z zamaskowanymi napisami.
    """
    if isinstance(value, str):
        return _CAPWORD.sub(lambda m: "X" + "x" * (len(m.group()) - 1), value)
    if isinstance(value, dict):
        # klucze też: parser terminarza wkłada linie opisu do kluczy `data`
        return {mask(k): mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value


def plain(obj: Any) -> Any:
    """Zamienia dataclassy i kolekcje z `librus-apix` na typy JSON.

    Args:
        obj: Wynik funkcji biblioteki.

    Returns:
        Struktura z dict/list/str/int.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: plain(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): plain(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [plain(v) for v in obj]
    return obj


def summarize(name: str, items: list[Any]) -> None:
    """Wypisuje liczbę, pola i do trzech zamaskowanych próbek sekcji.

    Args:
        name: Nazwa sekcji.
        items: Lista rekordów (już jako typy JSON).
    """
    keys = sorted({k for it in items if isinstance(it, dict) for k in it})
    print(f"\n## {name}: {len(items)} rekordów, pola: {keys}")
    for it in items[:3]:
        print("  ", json.dumps(mask(it), ensure_ascii=False)[:250])


def login(child_id: str) -> Client:
    """Loguje się raz kontem dziecka i rejestruje ciasteczka sesji do redakcji logów.

    Każdy klient dostaje własny pojemnik na ciasteczka (domyślny w
    `librus-apix` jest współdzielony), a każde żądanie ma limit czasu.

    Args:
        child_id: Id dziecka; sekrety `LIBRUS_<ID>_LOGIN` i `LIBRUS_<ID>_PASSWORD`.

    Returns:
        Zalogowany klient.
    """
    prefix = f"LIBRUS_{child_id.upper()}"
    user = get_secret(f"{prefix}_LOGIN")
    password = get_secret(f"{prefix}_PASSWORD")
    if user is None or password is None:  # pragma: no cover - get_secret już zgłasza błąd
        raise SystemExit("brak loginu lub hasła")
    client = Client(Token(), extra_cookies=RequestsCookieJar())
    client._session.request = partial(client._session.request, timeout=TIMEOUT)
    token = client.get_token(user.get_secret_value(), password.get_secret_value())
    register_all(token.API_Key)
    print("logowanie: OK")
    return client


def run(child_id: str) -> None:
    """Loguje się raz i pobiera sekcje potrzebne programowi.

    Args:
        child_id: Id dziecka; sekrety `LIBRUS_<ID>_LOGIN` i `LIBRUS_<ID>_PASSWORD`.
    """
    client = login(child_id)

    today = date.today()
    monday = datetime.combine(today - timedelta(days=today.weekday()), datetime.min.time())
    out: dict[str, Any] = {"fetched_at": datetime.now().isoformat(), "errors": {}}

    def section(name: str, fn: Any) -> None:
        try:
            out[name] = plain(fn())
        except Exception as e:  # spike: zbieramy typ błędu i idziemy dalej
            out["errors"][name] = type(e).__name__
            print(f"\n## {name}: BŁĄD {type(e).__name__}")

    for w in (0, 1):
        section(
            f"timetable_week{w}",
            partial(timetable.get_timetable, client, monday + timedelta(weeks=w)),
        )
    for m in (0, 1):
        d = (today.replace(day=1) + timedelta(days=32 * m)).replace(day=1)
        section(
            f"schedule_{d:%Y-%m}",
            partial(schedule.get_schedule, client, f"{d.month:02d}", str(d.year)),
        )
    section(
        "homework",
        partial(
            homework.get_homework,
            client,
            f"{today - timedelta(days=7)}",
            f"{today + timedelta(days=21)}",
        ),
    )
    section("announcements", partial(announcements.get_announcements, client))
    section("messages_max_page", partial(messages.get_max_page_number, client))
    section("messages_page0", partial(messages.get_received, client, 0))

    for w in (0, 1):
        days = out.get(f"timetable_week{w}") or []
        periods = [p for day in days for p in day]
        summarize(f"timetable_week{w} (dni: {len(days)})", periods)
        infos = sorted(
            {json.dumps(mask(p["info"]), ensure_ascii=False) for p in periods if p.get("info")}
        )
        print("   info (różne):", infos[:15])
    for key in [k for k in out if k.startswith("schedule_")]:
        events = [e for evs in (out[key] or {}).values() for e in evs]
        summarize(key, events)
        keys = {k for e in events for k in (e.get("data") or {})}
        print("   data.keys:", sorted(mask(k)[:40] for k in keys))
    for key in ("homework", "announcements", "messages_page0"):
        summarize(key, out.get(key) or [])
    print("\nmessages_max_page:", out.get("messages_max_page"))
    unread = [m for m in out.get("messages_page0") or [] if m.get("unread")]
    print("wiadomości nieprzeczytane na 1. stronie:", len(unread))
    print("błędy:", out["errors"] or "brak")

    target = Path("local/spike")
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = target / f"raw-{child_id}-{datetime.now():%Y%m%d-%H%M%S}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\nsurowe dane:", path)


if __name__ == "__main__":
    setup_logging("WARNING")
    run(sys.argv[1])
