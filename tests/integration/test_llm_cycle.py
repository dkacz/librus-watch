"""E5b na pełnym cyklu: model na atrapie, pozycje w bazie, cache, awaria modelu."""

import json
from datetime import date, datetime
from pathlib import Path

import pytest
from fixtures.librus_http import FakeLibrus

from librus_watch.config import Kind, child_credentials, load_config
from librus_watch.extract import ModelUnavailable
from librus_watch.models import Origin
from librus_watch.store import Store
from librus_watch.sync import login_child, sync_child

NOW = datetime(2026, 11, 6, 12, 0)
LATER = datetime(2026, 11, 6, 15, 0)


class ScriptedChat:
    """Atrapa modelu: dla tekstu ze „Zbiórka 7:30” zwraca zbiórkę, dla reszty nic."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def complete(
        self, messages: list[dict[str, str]], mode: str, timeout: float
    ) -> tuple[str, int]:
        self.calls += 1
        if self.fail:
            raise ModelUnavailable("APIConnectionError")
        text = messages[-1]["content"]
        items = []
        if "Zbiórka 7:30 pod szkołą" in text:
            items.append(
                {
                    "kind": "early_start",
                    "day": "2026-11-06",
                    "at": "07:30",
                    "deadline": None,
                    "what": "zbiórka 7:30",
                    "quote": "Zbiórka 7:30 pod szkołą",
                    "confidence": 0.9,
                }
            )
        return json.dumps({"items": items}), 50


def run(config_file: Path, chat: ScriptedChat, now: datetime):  # type: ignore[no-untyped-def]
    cfg = load_config(config_file).model_copy(
        update={
            "llm": load_config(config_file).llm.model_copy(
                update={"enabled": True, "json_mode": "json_schema"}
            )
        }
    )
    store = Store(cfg.data_dir)
    session = login_child(cfg, store, "ania", child_credentials(cfg.children[0]), now)
    report = sync_child(cfg, "ania", session, store, now=now, llm_client=chat)
    return report, store


def test_model_findings_are_stored_and_second_cycle_uses_cache(
    fake_librus: FakeLibrus, config_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    chat = ScriptedChat()
    first, store = run(config_file, chat, NOW)
    assert first.llm is not None and first.llm.read > 0 and first.llm.failed == 0
    llm = [f for f in first.findings if f.origin is Origin.llm]
    assert [(f.day, f.kind) for f in llm] == [(date(2026, 11, 6), Kind.early_start)]
    assert llm[0].source_key == "message:123"
    calls = chat.calls
    store.close()
    second, store = run(config_file, chat, LATER)
    assert chat.calls == calls  # nic nowego do czytania
    assert second.llm is None or second.llm.read == 0
    assert "Zbiórka" not in caplog.text and "pod szkołą" not in caplog.text
    store.close()


def test_model_outage_keeps_computed_deviations(fake_librus: FakeLibrus, config_file: Path) -> None:
    report, store = run(config_file, ScriptedChat(fail=True), NOW)
    assert report.llm is not None and "błędy połączenia" in report.llm.stopped
    assert {f.origin for f in report.findings} >= {Origin.timetable, Origin.plan_change}
    assert store.texts_to_extract(3)  # teksty czekają na kolejny cykl
    store.close()
