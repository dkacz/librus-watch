import json
import stat
from datetime import date, datetime, time
from pathlib import Path

import pytest

from librus_watch import stats
from librus_watch.config import Config, ContentType, Kind
from librus_watch.models import Finding, Origin, TextItem
from librus_watch.store import Store

NOW = datetime(2026, 11, 6, 18, 0)
SECRET_WORDS = ("Testowa", "zbiórka pod szkołą", "zbiórka 7:30", "Ania", "notatka rodzica")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(children=[{"id": "ania", "name": "Ania"}], data_dir=tmp_path / "data")


@pytest.fixture
def store(cfg: Config) -> Store:
    s = Store(cfg.data_dir)
    yield s  # type: ignore[misc]
    s.close()


def fill(store: Store) -> None:
    t = TextItem(
        child="ania", source=ContentType.message, key="m1", sent=date(2026, 11, 6),
        author="T. Testowa", title="Zbiórka", body="zbiórka 7:30",
    )  # fmt: skip
    store.upsert_text(t, NOW)
    store.set_sieve(t, "signal", "fp1")
    f = Finding(
        child="ania", day=date(2026, 11, 9), kind=Kind.early_start, what="zbiórka pod szkołą",
        origin=Origin.llm, source_key="m1", at=time(7, 30), quote="zbiórka 7:30",
    )  # fmt: skip
    store.add_finding(f, NOW)
    store.mark_sent(f, "evening", NOW)
    store.add_feedback(f.identity, "wrong", NOW, reason="not_deviation", note="notatka rodzica")
    store.add_feedback(f.identity, "confirmed", NOW)
    store.add_llm_usage(NOW.date(), 3, 1200)
    store.set_meta("module_errors:2026-11-06", json.dumps({"Notes": 2}))
    store.mark_module("ania", "Notes", NOW)
    store.set_meta("last_cycle", NOW.isoformat())
    stats.note_cycle(store, NOW, 2.0)
    stats.note_cycle(store, NOW, 4.0)


def test_daily_record_counts(store: Store) -> None:
    fill(store)
    r = stats.daily_record(store, NOW.date())
    assert r["day"] == "2026-11-06"
    assert r["findings"] == {"early_start": 1}
    assert r["sent"] == {"evening": 1}
    assert r["ratings"] == {"wrong": 1, "confirmed": 1}
    assert r["wrong_reasons"] == {"not_deviation": 1}
    assert r["sieve"] == {"signal": 1}
    assert r["new_texts"] == {"message": 1}
    assert r["llm"] == {"requests": 3, "tokens": 1200}
    assert r["module_errors"] == {"Notes": 2}
    assert r["cycles"] == {"n": 2, "avg_seconds": 3.0, "max_seconds": 4.0}
    assert r["findings_retracted"] == 0


def test_daily_record_empty_day(store: Store) -> None:
    r = stats.daily_record(store, date(2026, 1, 1))
    assert r["findings"] == {} and r["cycles"]["n"] == 0 and r["cycles"]["avg_seconds"] == 0.0


def test_retracted_findings_counted(store: Store) -> None:
    fill(store)
    store.retract_identity(
        Finding(
            child="ania", day=date(2026, 11, 9), kind=Kind.early_start, what="x",
            origin=Origin.llm, source_key="m1",
        ).identity,
        NOW,
    )  # fmt: skip
    assert stats.daily_record(store, NOW.date())["findings_retracted"] == 1


def test_now_record_has_children_by_id_only(cfg: Config, store: Store) -> None:
    fill(store)
    r = stats.now_record(cfg, store, NOW)
    assert r["schema_version"] == stats.STATS_SCHEMA
    assert [c["id"] for c in r["children"]] == ["ania"]
    assert r["modules"] == {"ania": {"Notes": NOW.isoformat()}}
    assert r["llm_today"] == {"requests": 3, "tokens": 1200}
    assert r["last_cycle"] == NOW.isoformat() and r["audit_open"] == 0


def test_write_stats_files_hold_no_content(cfg: Config, store: Store) -> None:
    fill(store)
    stats.write_stats(cfg, store, NOW)
    out = cfg.data_dir / "stats"
    blob = (out / "now.json").read_text() + (out / "daily.jsonl").read_text()
    for word in SECRET_WORDS:
        assert word not in blob
    for name in ("now.json", "daily.jsonl"):
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert not list(out.glob("*.tmp"))


def test_write_stats_replaces_today_and_keeps_history(cfg: Config, store: Store) -> None:
    out = cfg.data_dir / "stats"
    out.mkdir(parents=True)
    old = {"day": "2026-11-05", "findings": {}}
    (out / "daily.jsonl").write_text(
        json.dumps(old) + "\nnie-json\n" + json.dumps({"day": "2026-11-06", "stare": 1}) + "\n[1]\n"
    )
    stats.write_stats(cfg, store, NOW)
    stats.write_stats(cfg, store, NOW)
    rows = [json.loads(x) for x in (out / "daily.jsonl").read_text().splitlines()]
    assert [r["day"] for r in rows] == ["2026-11-05", "2026-11-06"]
    assert "stare" not in rows[1]


def test_write_stats_trims_to_keep_days(
    cfg: Config, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stats, "KEEP_DAYS", 2)
    out = cfg.data_dir / "stats"
    out.mkdir(parents=True)
    (out / "daily.jsonl").write_text(
        "".join(json.dumps({"day": f"2026-11-0{i}"}) + "\n" for i in (1, 2, 3))
    )
    stats.write_stats(cfg, store, NOW)
    rows = [json.loads(x)["day"] for x in (out / "daily.jsonl").read_text().splitlines()]
    assert rows == ["2026-11-03", "2026-11-06"]


def test_custom_dir_and_disabled(cfg: Config, store: Store, tmp_path: Path) -> None:
    custom = tmp_path / "elsewhere"
    on = cfg.model_copy(update={"stats": stats_cfg(dir=custom)})
    stats.write_stats(on, store, NOW)
    assert (custom / "now.json").exists()
    off = cfg.model_copy(update={"stats": stats_cfg(enabled=False)})
    stats.write_stats(off, store, NOW)
    assert not (cfg.data_dir / "stats").exists()


def stats_cfg(**kw: object) -> object:
    from librus_watch.config import StatsConfig

    return StatsConfig(**kw)  # type: ignore[arg-type]


def test_write_failure_is_warning_without_details(
    cfg: Config, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = cfg.data_dir / "stats"
    blocker.write_text("plik zamiast katalogu")
    caplog.set_level("WARNING")
    stats.write_stats(cfg, store, NOW)
    assert "statystyki: zapis nieudany (" in caplog.text
    assert str(blocker) not in caplog.text
