"""Migawka statystyk usługi dla panelu admina (same liczby, bez treści).

Po każdym cyklu usługa zapisuje w katalogu statystyk dwa pliki:

* `now.json`: stan bieżący (wersje, ostatni cykl, stan kont dzieci, moduły, budżet modelu);
* `daily.jsonl`: jedna linia na dobę (pozycje, powiadomienia, oceny, sito, model, błędy).

Panel czyta wyłącznie te pliki i nigdy bazy, więc nie może pokazać treści wiadomości,
nazwisk ani notatek rodziców (ADR-006, ADR-011). Dziecko to `id` z konfiguracji.
Zapis jest atomowy (plik tymczasowy i `rename`).
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from librus_watch import __version__
from librus_watch.config import Config
from librus_watch.reading import PROMPT_VERSION, SCHEMA_VERSION
from librus_watch.store import Store

log = logging.getLogger(__name__)

STATS_SCHEMA = 1
KEEP_DAYS = 400
NOW_FILE = "now.json"
DAILY_FILE = "daily.jsonl"


def stats_dir(cfg: Config) -> Path:
    """Katalog statystyk: `stats.dir` z konfiguracji albo `data_dir/stats`."""
    return cfg.stats.dir or cfg.data_dir / "stats"


def note_cycle(store: Store, now: datetime, seconds: float) -> None:
    """Dolicza czas jednego cyklu do dziennych liczników (liczba, suma, maksimum)."""
    key = f"cycle_stats:{now.date()}"
    cur = json.loads(store.get_meta(key) or "{}")
    n = int(cur.get("n", 0)) + 1
    total = float(cur.get("sum", 0.0)) + seconds
    top = max(float(cur.get("max", 0.0)), seconds)
    store.set_meta(key, json.dumps({"n": n, "sum": round(total, 3), "max": round(top, 3)}))


def _count(store: Store, sql: str, day: str) -> dict[str, int]:
    """Zlicza wiersze zapytania `SELECT klucz ... WHERE ... =?` w podziale na klucz."""
    return dict(Counter(str(r[0]) for r in store.db.execute(sql, (day,))))


def daily_record(store: Store, day: date) -> dict[str, Any]:
    """Liczby z jednej doby (bez treści)."""
    d = day.isoformat()
    requests, tokens = store.llm_usage(day)
    cycles = json.loads(store.get_meta(f"cycle_stats:{d}") or "{}")
    n = int(cycles.get("n", 0))
    return {
        "day": d,
        "findings": _count(store, "SELECT kind FROM findings WHERE substr(created_at,1,10)=?", d),
        "findings_retracted": int(
            store.db.execute(
                "SELECT COUNT(*) FROM findings WHERE substr(retracted_at,1,10)=?", (d,)
            ).fetchone()[0]
        ),
        "sent": _count(store, "SELECT alert_type FROM sent WHERE substr(sent_at,1,10)=?", d),
        "ratings": _count(store, "SELECT rating FROM feedback WHERE substr(created_at,1,10)=?", d),
        "wrong_reasons": _count(
            store,
            "SELECT reason FROM feedback WHERE rating='wrong' AND reason<>'' "
            "AND substr(created_at,1,10)=?",
            d,
        ),
        "sieve": _count(
            store,
            "SELECT sieve FROM texts WHERE sieve IS NOT NULL AND substr(first_seen,1,10)=?",
            d,
        ),
        "new_texts": _count(store, "SELECT source FROM texts WHERE substr(first_seen,1,10)=?", d),
        "llm": {"requests": requests, "tokens": tokens},
        "module_errors": json.loads(store.get_meta(f"module_errors:{d}") or "{}"),
        "cycles": {
            "n": n,
            "avg_seconds": round(float(cycles.get("sum", 0.0)) / n, 2) if n else 0.0,
            "max_seconds": float(cycles.get("max", 0.0)),
        },
    }


def now_record(cfg: Config, store: Store, now: datetime) -> dict[str, Any]:
    """Stan bieżący usługi: wersje, ostatni cykl, dzieci (po `id`), moduły, budżet modelu."""
    children = []
    modules: dict[str, dict[str, str]] = {}
    for child in cfg.children:
        st = store.fetch_state(child.id, now.date())
        children.append(
            {
                "id": child.id,
                "last_success": st.last_success.isoformat() if st.last_success else None,
                "failures_today": st.failures,
                "logins_today": st.logins,
                "next_attempt": st.next_attempt.isoformat() if st.next_attempt else None,
                "blocked_until": st.blocked_until.isoformat() if st.blocked_until else None,
                "last_error": st.last_error,
            }
        )
        modules[child.id] = {
            str(r[0]): str(r[1])
            for r in store.db.execute(
                "SELECT module, fetched_at FROM module_state WHERE child=? ORDER BY module",
                (child.id,),
            )
        }
    requests, tokens = store.llm_usage(now.date())
    return {
        "schema_version": STATS_SCHEMA,
        "generated_at": now.isoformat(),
        "app_version": __version__,
        "prompt_version": PROMPT_VERSION,
        "answer_schema": SCHEMA_VERSION,
        "service_started": store.get_meta("service_started"),
        "last_cycle": store.get_meta("last_cycle"),
        "fetch_interval_minutes": cfg.fetch.interval_minutes,
        "children": children,
        "modules": modules,
        "llm_today": {"requests": requests, "tokens": tokens},
        "audit_open": len(store.audits()),
    }


def _atomic_write(path: Path, text: str) -> None:
    """Zapisuje plik atomowo z uprawnieniami 0640."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _read_daily(path: Path) -> list[dict[str, Any]]:
    """Wczytuje `daily.jsonl`, pomijając uszkodzone linie."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("day"), str):
            rows.append(row)
    return rows


def write_stats(cfg: Config, store: Store, now: datetime) -> None:
    """Zapisuje `now.json` i aktualizuje dzisiejszą linię `daily.jsonl`.

    Nigdy nie zatrzymuje usługi: błąd zapisu to ostrzeżenie z samą klasą wyjątku.
    """
    if not cfg.stats.enabled:
        return
    out = stats_dir(cfg)
    try:
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(out, 0o700)
        _atomic_write(out / NOW_FILE, json.dumps(now_record(cfg, store, now), sort_keys=True))
        today = daily_record(store, now.date())
        rows = [r for r in _read_daily(out / DAILY_FILE) if r["day"] != today["day"]]
        rows = sorted([*rows, today], key=lambda r: r["day"])[-KEEP_DAYS:]
        _atomic_write(out / DAILY_FILE, "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    except Exception as e:  # statystyki nie mogą zatrzymać usługi
        log.warning("statystyki: zapis nieudany (%s)", type(e).__name__)
        return
    log.debug("statystyki: zapisane")
