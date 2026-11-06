import json
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from librus_watch.config import ContentType, Kind, LLMConfig
from librus_watch.extract import (
    SYSTEM_PROMPT,
    BadAnswer,
    ModelUnavailable,
    check_item,
    extract_one,
    parse_answer,
    probe_mode,
    read_queue,
    user_message,
)
from librus_watch.models import Origin, TextItem
from librus_watch.reading import plan_reading
from librus_watch.store import Store

SENT = date(2026, 11, 5)
NOW = datetime(2026, 11, 5, 18, 0)
CFG = LLMConfig(json_mode="json_schema")
TEXT = TextItem(
    "ania",
    ContentType.message,
    "m1",
    SENT,
    "A",
    "Wycieczka",
    "Jutro zbiórka o 7:30 pod szkołą. Proszę o strój galowy.",
)


def item(**kw: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "early_start",
        "day": "2026-11-06",
        "at": "07:30",
        "deadline": None,
        "what": "zbiórka 7:30",
        "quote": "Jutro zbiórka o 7:30 pod szkołą",
        "confidence": 0.9,
    }
    base.update(kw)
    return base


def answer(*items: dict[str, Any]) -> str:
    return json.dumps({"items": list(items)})


class FakeChat:
    def __init__(self, *replies: str | BaseException) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[dict[str, str]], str, float]] = []

    def complete(
        self, messages: list[dict[str, str]], mode: str, timeout: float
    ) -> tuple[str, int]:
        self.calls.append((messages, mode, timeout))
        reply = self.replies.pop(0) if self.replies else answer()
        if isinstance(reply, BaseException):
            raise reply
        return reply, 100


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path)
    yield s
    s.close()


# --- prompt i parsowanie ---------------------------------------------------------


def test_prompt_prefix_is_static_and_text_is_fenced() -> None:
    msg = user_message(TEXT, 1000, "3b")
    assert msg.startswith("Data wysłania: 2026-11-05 (czwartek)\nKalendarz: 2026-11-06 piątek, ")
    assert "2026-11-12 czwartek" in msg and "\nKlasa dziecka: 3b\n" in msg
    assert "<<<\n[wiadomość] Wycieczka\nJutro zbiórka" in msg and msg.endswith(">>>")
    assert "Jutro zbiórka" not in SYSTEM_PROMPT and "items" in SYSTEM_PROMPT


@pytest.mark.parametrize(
    "raw", [answer(item()), "```json\n" + answer(item()) + "\n```", json.dumps([item()])]
)
def test_parse_answer_variants(raw: str) -> None:
    assert parse_answer(raw).items[0].kind is Kind.early_start


@pytest.mark.parametrize(
    "raw",
    [
        "nie wiem",
        '{"items": [{"kind": "zbiorka"}]}',
        answer(item(kind="substitution")),
        answer(item(day="jutro")),
    ],
)
def test_bad_answers(raw: str) -> None:
    with pytest.raises(BadAnswer):
        parse_answer(raw)


# --- kontrola pozycji --------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({}, None),
        ({"quote": "JUTRO zbiórka o 7:30  pod szkołą"}, None),  # wielkość liter i spacje
        ({"quote": "Wycieczka"}, None),  # cytat z tytułu
        ({"quote": "zbiórka o 6:30"}, "quote"),  # zmyślony
        ({"day": "2026-11-03"}, "date"),
        ({"day": "2027-06-01"}, "date"),
        ({"deadline": "2026-01-01"}, "date"),
        ({"confidence": 0.2}, "confidence"),
    ],
)
def test_check_item(kw: dict[str, Any], reason: str | None) -> None:
    assert check_item(parse_answer(answer(item(**kw))).items[0], TEXT, CFG) == reason


def test_extract_keeps_good_and_logs_rejected_without_quote(
    caplog: pytest.LogCaptureFixture,
) -> None:
    chat = FakeChat(
        answer(
            item(),
            item(kind="bring", quote="Proszę o strój galowy", at=None),
            item(quote="Pojutrze basen z rodzicami"),
        )
    )
    result = extract_one(chat, TEXT, CFG, "json_schema", 30)
    assert [r["kind"] for r in result.items] == ["early_start", "bring"]
    assert result.rejected == {"quote": 1} and result.requests == 1
    assert "odrzucona pozycja early_start (quote)" in caplog.text
    assert "Pojutrze" not in caplog.text and "zbiórka" not in caplog.text


def test_one_repair_then_failure() -> None:
    chat = FakeChat("to nie JSON", answer(item()))
    result = extract_one(chat, TEXT, CFG, "json_schema", 30)
    assert result.requests == 2 and len(result.items) == 1
    assert (
        chat.calls[1][0][-1]["role"] == "user" and "niepoprawna" in chat.calls[1][0][-1]["content"]
    )
    with pytest.raises(BadAnswer):
        extract_one(FakeChat("zły", "dalej zły"), TEXT, CFG, "json_schema", 30)


# --- sonda trybu JSON ----------------------------------------------------------------


def test_probe_picks_best_mode_and_caches_for_a_day(store: Store) -> None:
    cfg = LLMConfig()
    chat = FakeChat(BadAnswer("HTTP 400"), answer())
    assert probe_mode(chat, cfg, store, NOW, 180) == "json_object"
    assert [c[1] for c in chat.calls] == ["json_schema", "json_object"] and chat.calls[0][2] == 180
    assert probe_mode(FakeChat(), cfg, store, NOW, 180) == "json_object"  # z pamięci
    later = datetime(2026, 11, 7, 18, 0)
    assert probe_mode(FakeChat("bez json", "bez json"), cfg, store, later, 180) == "none"
    assert (
        probe_mode(FakeChat(), LLMConfig(json_mode="json_object"), store, NOW, 1) == "json_object"
    )


# --- kolejka ---------------------------------------------------------------------


def queued(store: Store, *texts: TextItem) -> Any:
    for t in texts:
        store.upsert_text(t, NOW)
    return plan_reading(store.texts_to_extract(3), store, CFG, SENT, NOW)


def test_read_queue_caches_results_and_shares_between_children(store: Store) -> None:
    twin = TextItem("kuba", ContentType.message, "m9", SENT, "A", "Wycieczka", TEXT.body)
    plan = queued(store, TEXT, twin)
    chat = FakeChat(answer(item()))
    report = read_queue(plan, store, CFG, NOW, chat)
    assert report.read == 1 and report.from_cache == 1 and len(chat.calls) == 1
    assert {(f.child, f.kind, f.origin) for f in report.findings} == {
        ("ania", Kind.early_start, Origin.llm),
        ("kuba", Kind.early_start, Origin.llm),
    }
    assert report.findings[0].source_key == "message:m1" and report.findings[0].quote
    assert store.texts_to_extract(3) == []
    assert store.llm_usage(SENT) == (1, 100)
    again = plan_reading([TEXT], store, CFG, SENT, NOW)
    assert again.queue == [] and len(read_queue(again, store, CFG, NOW, FakeChat()).findings) == 1


def test_bad_answer_counts_attempt_and_breaker_stops(store: Store) -> None:
    texts = [
        TextItem("ania", ContentType.message, f"m{i}", SENT, "A", "T", f"Jutro {i}.")
        for i in range(5)
    ]
    plan = queued(store, *texts)
    chat = FakeChat("zły", "zły", *(ModelUnavailable("timeout") for _ in range(5)))
    report = read_queue(plan, store, CFG, NOW, chat)
    assert report.failed == 1 and report.stopped.startswith("3 kolejne błędy")
    # zła odpowiedź zużyła próbę, błędy połączenia nie
    pending = {t.key: t for t in store.texts_to_extract(3)}
    assert set(pending) == {"m0", "m1", "m2", "m3", "m4"}
    assert set(t.key for t in store.texts_to_extract(1)) == {"m1", "m2", "m3", "m4"}


def test_unavailable_model_at_probe_stops_cycle(store: Store) -> None:
    plan = queued(store, TEXT)
    report = read_queue(plan, store, LLMConfig(), NOW, FakeChat(ModelUnavailable("conn")))
    assert report.stopped.startswith("model niedostępny") and report.read == 0
    assert len(store.texts_to_extract(3)) == 1


def test_schema_avoids_constructs_llama_cpp_cannot_parse() -> None:
    from librus_watch.extract import SCHEMA

    raw = json.dumps(SCHEMA)
    assert "\\\\d" not in raw and '["string", "null"]' not in raw
