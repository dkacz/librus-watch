"""Pomiar sita (E5a) na zapisanych odpowiedziach API ze spike'ów (`local/spike`).

Bez logowania i bez modelu. Liczy teksty z każdego konta: ile przeszłoby sito
przy pierwszej synchronizacji (cała historia) i w zwykły dzień (teksty dodane
w ostatniej dobie). Wypisuje wyłącznie liczby według rodzaju źródła.

Użycie:
    uv run python scripts/measure_sieve.py 2026-11-06
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from librus_watch.config import ContentType, LLMConfig
from librus_watch.librus.adapter import adapt, lookups_from
from librus_watch.librus.gateway import MODULES, GatewayData, _records
from librus_watch.reading import plan_reading
from librus_watch.store import Store


def gateway_data(raw: dict[str, object]) -> GatewayData:
    """Odtwarza `GatewayData` z zapisanych odpowiedzi spike'u części 2."""
    by_path = {m.name: m for m in MODULES}
    alias = {
        "Calendars/Substitutions": "Substitutions",
        "HomeWorks/Categories": "HomeWorkCategories",
        "Notes/Categories": "NoteCategories",
        "Grades/Comments": "GradeComments",
        "Timetables/OtherActivitiesRegister": "OtherActivities",
    }
    data = GatewayData()
    for path, value in raw.items():
        if not isinstance(value, dict) or value.get("status") != 200:
            continue
        name = alias.get(path.split("?")[0], path.split("?")[0])
        module = by_path.get(name)
        if module is None:
            continue
        data.records.setdefault(name, []).extend(_records(module, path, value["json"]))
    return data


def main(today: date) -> None:
    """Wypisuje liczby tekstów przed i po sicie dla każdego zapisanego konta."""
    cfg = LLMConfig()
    for path in sorted(Path("local/spike").glob("raw-gateway-*.json")):
        account = path.name.split("-")[2]
        data = gateway_data(json.loads(path.read_text()))
        snap = adapt(
            account, data, lookups_from(data), ignore=frozenset({ContentType.grade}), today=today
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            now = datetime.combine(today, datetime.min.time())
            first = plan_reading(snap.texts, store, cfg, today, now, dry_run=True)
            recent = [t for t in snap.texts if t.sent >= today - timedelta(days=1)]
            day = plan_reading(recent, store, cfg, today, now, dry_run=True)
            store.close()
        total = Counter(t.source.value for t in snap.texts)
        passed = Counter(q.item.source.value for q in first.queue)
        print(f"konto {account[:2]}…: teksty z API {sum(total.values())}")
        for source in sorted(total):
            print(f"  {source:14} {total[source]:4} -> po sicie {passed[source]:4}")
        print(
            f"  pierwsza synchronizacja: do modelu {len(first.queue)}, "
            f"sito odrzuciło {dict(first.dropped)}"
        )
        print(
            f"  zwykły dzień (dodane w ostatniej dobie): {len(recent)} -> do modelu "
            f"{len(day.queue)}"
        )


if __name__ == "__main__":
    main(date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today())
