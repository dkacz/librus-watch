import json
import logging
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest

from librus_watch import records
from librus_watch.config import Config, ContentType, Kind, StatsConfig
from librus_watch.models import Finding, Origin, TextItem
from librus_watch.store import Store

NOW = datetime(2026, 11, 6, 18, 0)


def make_cfg(tmp_path: Path, **stats: Any) -> Config:
    return Config(
        children=[{"id": "ania", "name": "Ania"}],
        data_dir=tmp_path / "data",
        stats=StatsConfig(records=True, **stats),
    )


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return make_cfg(tmp_path)


@pytest.fixture
def store(cfg: Config) -> Store:
    s = Store(cfg.data_dir)
    yield s  # type: ignore[misc]
    s.close()


def add_text(store: Store, key: str, analyzed: bool = True, **kw: Any) -> TextItem:
    t = TextItem(
        child="ania", source=ContentType.message, key=key, sent=date(2026, 11, 6),
        author="T. Testowa", title=f"Temat {key}", body=f"Treść pełna {key}",
    )  # fmt: skip
    store.upsert_text(t, NOW)
    store.set_sieve(t, kw.get("sieve", "signal"), "fp")
    if analyzed:
        store.mark_extracted(t, kw.get("at", NOW))
    return t


def add_finding(store: Store, key: str, sent: bool = True, **kw: Any) -> Finding:
    f = Finding(
        child="ania", day=date(2026, 11, 9), kind=Kind.early_start, what="zbiórka",
        origin=Origin.llm, source_key=f"message:{key}", at=time(7, 30), quote="zbiórka 7:30",
        **kw,
    )  # fmt: skip
    store.add_finding(f, NOW)
    if sent:
        store.mark_sent(f, "evening", NOW)
    return f


def test_build_records_statuses(cfg: Config, store: Store) -> None:
    add_text(store, "a")
    f = add_finding(store, "a")
    add_text(store, "b")
    add_text(store, "c", analyzed=False)
    rows = {r["ref"]: r for r in records.build_records(cfg, store, NOW)}
    assert set(rows) == {"ania|message|a", "ania|message|b"}
    a = rows["ania|message|a"]
    assert a["body"] == "Treść pełna a" and a["title"] == "Temat a"
    assert a["findings"][0]["identity"] == f.identity
    assert a["findings"][0]["alert"]["type"] == "evening"
    assert a["findings"][0]["rating"] is None and a["missed_at"] is None
    assert rows["ania|message|b"]["findings"] == []


def test_finding_not_alerted_when_new_version_unsent(cfg: Config, store: Store) -> None:
    add_text(store, "a")
    add_finding(store, "a", sent=True)
    newer = Finding(
        child="ania", day=date(2026, 11, 9), kind=Kind.early_start, what="zbiórka",
        origin=Origin.llm, source_key="message:a", at=time(7, 15), quote="zbiórka 7:15",
    )  # fmt: skip
    store.add_finding(newer, datetime(2026, 11, 6, 19, 0))
    it = records.build_records(cfg, store, NOW)[0]["findings"][0]
    assert it["alert"] is None and it["at"] == "07:15"


def test_retracted_and_foreign_source_keys(cfg: Config, store: Store) -> None:
    add_text(store, "a")
    f = add_finding(store, "a")
    store.retract_identity(f.identity, NOW)
    store.add_finding(
        Finding(child="ania", day=date(2026, 11, 9), kind=Kind.cancel, what="x",
                origin=Origin.schedule, source_key="2026-11-09"),
        NOW,
    )  # fmt: skip
    rec = records.build_records(cfg, store, NOW)[0]
    assert rec["findings"][0]["retracted"] is True and len(rec["findings"]) == 1


def test_window_and_limit(cfg: Config, store: Store) -> None:
    add_text(store, "old", at=datetime(2026, 10, 1, 12, 0))
    add_text(store, "new")
    assert [r["ref"] for r in records.build_records(cfg, store, NOW)] == ["ania|message|new"]


def test_ratings_reflected(cfg: Config, store: Store) -> None:
    add_text(store, "a")
    f = add_finding(store, "a")
    store.add_feedback(f.identity, "wrong", NOW, reason="day")
    store.add_feedback(f.identity, "confirmed", datetime(2026, 11, 6, 18, 5))
    store.add_feedback(None, "missed", NOW, text_ref="ania|message|a")
    store.add_feedback(None, "missed", NOW, note="bez odnośnika")
    rec = records.build_records(cfg, store, NOW)[0]
    assert rec["findings"][0]["rating"] == {"rating": "confirmed", "reason": ""}
    assert rec["missed_at"] == NOW.isoformat()


def test_write_records_atomic_private_and_toggle(cfg: Config, store: Store, tmp_path: Path) -> None:
    add_text(store, "a")
    records.write_records(cfg, store, NOW)
    path = cfg.data_dir / "stats" / "records.jsonl"
    assert json.loads(path.read_text().splitlines()[0])["body"] == "Treść pełna a"
    assert path.stat().st_mode & 0o777 == 0o600 and not list(path.parent.glob("*.tmp"))
    off = Config(children=[{"id": "ania", "name": "Ania"}], data_dir=tmp_path / "other")
    records.write_records(off, store, NOW)
    assert not (tmp_path / "other").exists()


def test_write_records_failure_is_warning(
    cfg: Config, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    (cfg.data_dir / "stats").write_text("plik")
    caplog.set_level(logging.WARNING)
    records.write_records(cfg, store, NOW)
    assert "rekordy: zapis nieudany (" in caplog.text and "Treść" not in caplog.text


def drop(cfg: Config, name: str, payload: Any) -> Path:
    box = records.inbox_dir(cfg)
    box.mkdir(parents=True, exist_ok=True)
    p = box / name
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return p


def test_inbox_applies_valid_actions(cfg: Config, store: Store) -> None:
    add_text(store, "a")
    f = add_finding(store, "a")
    drop(cfg, "1.json", {"action": "confirmed", "identity": f.identity})
    drop(cfg, "2.json", {"action": "wrong", "identity": f.identity, "reason": "day"})
    drop(cfg, "3.json", {"action": "missed", "ref": "ania|message|a"})
    assert records.process_inbox(cfg, store, NOW) == 3
    assert not list(records.inbox_dir(cfg).glob("*.json"))
    ratings = [(r["rating"], r["reason"], r["text_ref"]) for r in store.feedback()]
    assert ("confirmed", "", None) in ratings and ("wrong", "day", None) in ratings
    assert ("missed", "", "ania|message|a") in ratings
    rec = json.loads((cfg.data_dir / "stats" / "records.jsonl").read_text().splitlines()[0])
    assert rec["missed_at"] and rec["findings"][0]["rating"]["rating"] == "wrong"


@pytest.mark.parametrize(
    "payload",
    [
        "nie json",
        "[1]",
        {"action": "delete", "identity": "x"},
        {"action": "confirmed", "identity": "brak-takiej"},
        {"action": "confirmed", "identity": 5},
        {"action": "confirmed", "identity": "x" * 200},
        {"action": "missed", "ref": "a|b"},
        {"action": "missed", "ref": "ania|message|nie-ma"},
        {"action": "missed", "ref": 7},
        {"action": "missed", "ref": "a|" * 200},
    ],
)
def test_inbox_rejects_invalid(cfg: Config, store: Store, payload: Any) -> None:
    add_text(store, "a")
    add_finding(store, "a")
    p = drop(cfg, "x.json", payload)
    assert records.process_inbox(cfg, store, NOW) == 0
    assert not p.exists() and store.feedback() == []


def test_inbox_rejects_bad_reason_oversize_and_symlink(cfg: Config, store: Store) -> None:
    add_text(store, "a")
    f = add_finding(store, "a")
    drop(cfg, "r.json", {"action": "wrong", "identity": f.identity, "reason": "zmyslone"})
    drop(cfg, "big.json", " " * (records.MAX_INBOX_BYTES + 1))
    outside = cfg.data_dir / "poza.json"
    outside.write_text(json.dumps({"action": "confirmed", "identity": f.identity}))
    (records.inbox_dir(cfg) / "link.json").symlink_to(outside)
    drop(cfg, "ignorowany.txt", "x")
    assert records.process_inbox(cfg, store, NOW) == 0
    assert store.feedback() == [] and outside.exists()
    assert (records.inbox_dir(cfg) / "ignorowany.txt").exists()


def test_inbox_disabled_missing_and_logs_counts_only(
    cfg: Config, store: Store, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert records.process_inbox(cfg, store, NOW) == 0  # brak katalogu
    off = Config(children=[{"id": "ania", "name": "Ania"}], data_dir=tmp_path / "data")
    assert records.process_inbox(off, store, NOW) == 0
    add_text(store, "a")
    drop(cfg, "x.json", {"action": "missed", "ref": "ania|message|a"})
    caplog.set_level(logging.INFO)
    records.process_inbox(cfg, store, NOW)
    assert "przyjęto 1, odrzucono 0" in caplog.text and "ania|message" not in caplog.text


def test_inbox_unlink_failure_is_warning(
    cfg: Config, store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    drop(cfg, "x.json", "zepsute")

    def boom(self: Path, missing_ok: bool = False) -> None:
        raise OSError("nie")

    monkeypatch.setattr(Path, "unlink", boom)
    caplog.set_level(logging.WARNING)
    records.process_inbox(cfg, store, NOW)
    assert "nie udało się usunąć pliku" in caplog.text
