"""E5a na pełnym cyklu: sito, kolejka, cache wspólny dla dzieci, raport polecenia."""

from datetime import datetime
from pathlib import Path

import pytest
from fixtures.librus_http import FakeLibrus

from librus_watch.cli import main
from librus_watch.config import ContentType, child_credentials, load_config
from librus_watch.store import Store
from librus_watch.sync import login_child, sync_child

NOW = datetime(2026, 11, 6, 12, 0)
LATER = datetime(2026, 11, 6, 15, 0)


def test_queue_then_cache_hits_after_model_answers(
    fake_librus: FakeLibrus, config_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = load_config(config_file)
    store = Store(cfg.data_dir)
    creds = child_credentials(cfg.children[0])
    first = sync_child(cfg, "ania", login_child(cfg, store, "ania", creds, NOW), store, now=NOW)
    plan = first.reading
    assert plan is not None and plan.queue and plan.cached == []
    sources = {q.item.source for q in plan.queue}
    assert {ContentType.message, ContentType.notice, ContentType.grade_comment} <= sources
    assert plan.queue[0].urgent  # świeże wiadomości i ogłoszenia na początku

    for q in plan.queue:  # model (E5b) odpowiada „nic do zgłoszenia”
        store.cache_put(q.key, [], NOW)
    second = sync_child(
        cfg, "ania", login_child(cfg, store, "ania", creds, LATER), store, now=LATER
    )
    assert second.reading is not None
    assert second.reading.queue == [] and len(second.reading.cached) == len(plan.queue)
    assert store.texts_to_extract(cfg.llm.max_attempts_per_message) == []
    assert "Zbiórka" not in caplog.text and "apel" not in caplog.text.lower()
    store.close()


def test_fetch_prints_reading_summary(
    fake_librus: FakeLibrus, config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config", str(config_file), "fetch", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert (
        "do odczytu przez model:" in out and "bez treści 2" in out
    )  # dry-run nie otwiera wiadomości
    assert main(["--config", str(config_file), "fetch"]) == 0
    out = capsys.readouterr().out
    assert "do odczytu przez model:" in out and "bez treści 0" in out
