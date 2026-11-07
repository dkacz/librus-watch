"""Strumień rekordów po analizie modelu i skrzynka ocen z panelu (ADR-026).

Włączane przez `stats.records` (domyślnie wyłączone). Usługa zapisuje w katalogu statystyk
plik `records.jsonl`: ostatnie teksty przeczytane przez model razem z PEŁNĄ TREŚCIĄ, ich
pozycjami, statusem alertu i dotychczasową oceną. Panel pokazuje ten plik i zapisuje wybory
rodzica jako pliki w skrzynce (`inbox_dir`); usługa je waliduje i zapisuje jako oceny tym
samym kodem co bot (`Store.add_feedback`). Panel nie ma dostępu do bazy.

Do logów trafiają wyłącznie liczby (ADR-011).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from librus_watch.config import Config
from librus_watch.models import Finding
from librus_watch.stats import stats_dir
from librus_watch.store import Store
from librus_watch.telegram import REASONS

log = logging.getLogger(__name__)

RECORDS_FILE = "records.jsonl"
MAX_RECORDS = 300
MAX_INBOX_BYTES = 4096
MAX_INBOX_BATCH = 50
ACTIONS = ("confirmed", "wrong", "missed")


def inbox_dir(cfg: Config) -> Path:
    """Katalog skrzynki ocen: `stats.inbox_dir` albo `data_dir/inbox`."""
    return cfg.stats.inbox_dir or cfg.data_dir / "inbox"


def _latest_ratings(store: Store) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Ostatnia ocena pozycji (po tożsamości) i ostatnie zgłoszenie „brak” tekstu (po odnośniku)."""
    items: dict[str, dict[str, str]] = {}
    missed: dict[str, str] = {}
    for r in store.feedback():
        ident, rating = r["identity"], str(r["rating"])
        if ident and rating in ("confirmed", "wrong"):
            items[str(ident)] = {"rating": rating, "reason": str(r["reason"] or "")}
        elif rating == "missed" and r["text_ref"]:
            missed[str(r["text_ref"])] = str(r["created_at"])
    return items, missed


def _findings_by_text(store: Store, since: str) -> dict[str, list[dict[str, Any]]]:
    """Najnowsza wersja każdej pozycji z modelu, pogrupowana po odnośniku do tekstu."""
    sent = {
        str(r["identity"]): (str(r["content_fp"]), str(r["alert_type"]), str(r["sent_at"]))
        for r in store.db.execute("SELECT identity, content_fp, alert_type, sent_at FROM sent")
    }
    latest: dict[str, tuple[str, str, str | None]] = {}
    for r in store.db.execute(
        "SELECT identity, content_fp, data, created_at, retracted_at FROM findings "
        "WHERE created_at>=? ORDER BY created_at",
        (since,),
    ):
        latest[str(r["identity"])] = (str(r["data"]), str(r["content_fp"]), r["retracted_at"])
    out: dict[str, list[dict[str, Any]]] = {}
    for ident, (data, fp, retracted) in latest.items():
        f = Finding.from_json(data)
        src, sep, key = f.source_key.partition(":")
        if not sep:
            continue
        s = sent.get(ident)
        out.setdefault(f"{f.child}|{src}|{key}", []).append(
            {
                "identity": ident,
                "kind": f.kind.value,
                "day": f.day.isoformat(),
                "at": f.at.strftime("%H:%M") if f.at else None,
                "what": f.what,
                "quote": f.quote,
                "confidence": f.confidence,
                "retracted": retracted is not None,
                "alert": {"type": s[1], "sent_at": s[2]} if s and s[0] == fp else None,
            }
        )
    return out


def build_records(cfg: Config, store: Store, now: datetime) -> list[dict[str, Any]]:
    """Ostatnie teksty przeczytane przez model (od najnowszej analizy) z pozycjami i ocenami."""
    since = (now - timedelta(days=cfg.stats.records_days)).isoformat()
    ratings, missed = _latest_ratings(store)
    by_text = _findings_by_text(store, since)
    rows = store.db.execute(
        "SELECT child, source, key, sent, author, title, body, sieve, extracted_at FROM texts "
        "WHERE extracted_at IS NOT NULL AND extracted_at>=? ORDER BY extracted_at DESC LIMIT ?",
        (since, MAX_RECORDS),
    ).fetchall()
    out = []
    for r in rows:
        ref = f"{r['child']}|{r['source']}|{r['key']}"
        items = by_text.get(ref, [])
        for it in items:
            it["rating"] = ratings.get(it["identity"])
        out.append(
            {
                "ref": ref,
                "child": r["child"],
                "source": r["source"],
                "sent": r["sent"],
                "author": r["author"],
                "title": r["title"],
                "body": r["body"] or "",
                "sieve": r["sieve"],
                "analyzed_at": r["extracted_at"],
                "findings": items,
                "missed_at": missed.get(ref),
            }
        )
    return out


def write_records(cfg: Config, store: Store, now: datetime) -> None:
    """Zapisuje `records.jsonl` (atomowo, 0600); błąd to ostrzeżenie, nie awaria usługi."""
    if not (cfg.stats.enabled and cfg.stats.records):
        return
    out = stats_dir(cfg)
    try:
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        rows = build_records(cfg, store, now)
        tmp = out / (RECORDS_FILE + ".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        os.chmod(tmp, 0o600)
        tmp.replace(out / RECORDS_FILE)
    except Exception as e:  # strumień nie może zatrzymać usługi
        log.warning("rekordy: zapis nieudany (%s)", type(e).__name__)


def _apply(store: Store, req: dict[str, Any], now: datetime) -> bool:
    """Waliduje jedno żądanie i zapisuje ocenę; `False`, gdy jest nieprawidłowe."""
    action = req.get("action")
    if action not in ACTIONS:
        return False
    if action == "missed":
        ref = req.get("ref")
        parts = ref.split("|") if isinstance(ref, str) and len(ref) <= 300 else []
        if len(parts) != 3:
            return False
        found = store.db.execute(
            "SELECT 1 FROM texts WHERE child=? AND source=? AND key=?", parts
        ).fetchone()
        if found is None:
            return False
        store.add_feedback(None, "missed", now, note="panel", text_ref=str(ref))
        return True
    ident = req.get("identity")
    if not isinstance(ident, str) or len(ident) > 100:
        return False
    if store.db.execute("SELECT 1 FROM findings WHERE identity=?", (ident,)).fetchone() is None:
        return False
    reason = req.get("reason") or ""
    if action == "wrong" and reason and reason not in REASONS:
        return False
    store.add_feedback(ident, str(action), now, reason=str(reason) if action == "wrong" else "")
    return True


def process_inbox(cfg: Config, store: Store, now: datetime) -> int:
    """Zapisuje oceny ze skrzynki panelu; zwraca liczbę przyjętych. Pliki są usuwane.

    Ignoruje dowiązania i pliki spoza wzorca `*.json`, odrzuca za duże i nieprawidłowe.
    Po przyjęciu ocen odświeża `records.jsonl`.
    """
    if not (cfg.stats.enabled and cfg.stats.records):
        return 0
    box = inbox_dir(cfg)
    if not box.is_dir():
        return 0
    accepted = rejected = 0
    for path in sorted(box.glob("*.json"))[:MAX_INBOX_BATCH]:
        try:
            if path.is_symlink() or path.stat().st_size > MAX_INBOX_BYTES:
                ok = False
            else:
                req = json.loads(path.read_text())
                ok = isinstance(req, dict) and _apply(store, req, now)
        except (OSError, ValueError):
            ok = False
        try:
            path.unlink()
        except OSError:
            log.warning("skrzynka: nie udało się usunąć pliku")
        accepted += ok
        rejected += not ok
    if accepted or rejected:
        log.info("skrzynka panelu: przyjęto %d, odrzucono %d", accepted, rejected)
    if accepted:
        write_records(cfg, store, now)
    return accepted
