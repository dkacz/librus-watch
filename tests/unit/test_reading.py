from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from librus_watch.config import ContentType, LLMConfig
from librus_watch.models import TextItem
from librus_watch.reading import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    Budget,
    due_date,
    estimate_tokens,
    is_urgent,
    plan_reading,
)
from librus_watch.store import Store

TODAY = date(2026, 11, 5)
NOW = datetime(2026, 11, 5, 18, 0)
C = ContentType


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path)
    yield s
    s.close()


def text(key: str, source: ContentType = C.message, child: str = "ania", **kw: object) -> TextItem:
    base: dict[str, object] = {
        "child": child,
        "source": source,
        "key": key,
        "sent": TODAY,
        "author": "A",
        "title": "T",
        "body": f"Treść {key} jutro.",
    }
    base.update(kw)
    return TextItem(**base)  # type: ignore[arg-type]


def test_due_date_and_urgency() -> None:
    assert due_date(text("1", valid_to=date(2026, 11, 20)), TODAY) == date(2026, 11, 20)
    assert due_date(
        text("2", valid_from=date(2026, 11, 3), valid_to=date(2026, 11, 9)), TODAY
    ) == date(2026, 11, 9)
    assert due_date(text("3"), TODAY) == date(2026, 11, 6)
    assert due_date(text("4", sent=date(2026, 10, 1)), TODAY) is None
    assert is_urgent(text("5"), TODAY)  # świeża wiadomość
    assert not is_urgent(text("6", C.homework, valid_to=date(2026, 11, 20)), TODAY)
    assert is_urgent(text("7", C.homework, valid_to=date(2026, 11, 6)), TODAY)
    assert estimate_tokens("x" * 300) == 1500 + 100 + 1


def test_budget_keeps_reserve_for_urgent() -> None:
    b = Budget(LLMConfig(daily_requests=4, urgent_reserve=0.5))
    assert b.take(10, urgent=False) and b.take(10, urgent=False)
    assert not b.take(10, urgent=False)  # 2 z 4 dla niepilnych
    assert b.take(10, urgent=True) and b.take(10, urgent=True)
    assert not b.take(10, urgent=True)
    tokens = Budget(LLMConfig(daily_tokens=5_000, urgent_reserve=0))
    assert tokens.take(3_000, urgent=False) and not tokens.take(3_000, urgent=True)


def test_plan_orders_by_urgency_due_date_and_source(store: Store) -> None:
    items = [
        text("hw-far", C.homework, valid_to=date(2026, 11, 25), sent=date(2026, 11, 1)),
        text("note", C.note, body="Brak stroju w pt.", sent=date(2026, 11, 1)),
        text("msg"),
        text("hw-near", C.homework, valid_to=date(2026, 11, 10), sent=date(2026, 11, 1)),
        text("comment", C.grade_comment, body="Ładnie.", sent=date(2026, 11, 1)),
        text("old", sent=date(2026, 9, 1)),
    ]
    for t in items:
        store.upsert_text(t, NOW)
    plan = plan_reading(store.texts_to_extract(3), store, LLMConfig(), TODAY, NOW)
    assert [q.item.key for q in plan.queue] == ["msg", "hw-near", "hw-far", "note"]
    assert plan.urgent == 1 and plan.dropped == {"no_signal": 1, "old": 1}
    # odrzucone wychodzą z kolejki, decyzja i odcisk zapisane przy tekście
    assert {t.key for t in store.texts_to_extract(3)} == {"msg", "note", "hw-near", "hw-far"}
    reason, fp = store.sieve_of(items[5])
    assert reason == "old" and fp


def test_cache_hit_also_for_empty_result_and_other_child(store: Store) -> None:
    ania = text("n1", C.notice, body="Apel w piątek 6.11.", child="ania")
    kuba = text("LID-2", C.notice, body="<p>Apel w piątek 6.11.</p>", child="kuba")
    store.upsert_text(ania, NOW)
    store.upsert_text(kuba, NOW)
    first = plan_reading([ania], store, LLMConfig(), TODAY, NOW)
    store.cache_put(first.queue[0].key, [], NOW)  # model: „nic do zgłoszenia”
    second = plan_reading([kuba], store, LLMConfig(), TODAY, NOW)
    assert second.queue == [] and second.cached == [(kuba, [])]
    assert store.texts_to_extract(3, "kuba") == []
    assert first.queue[0].key[1:] == (PROMPT_VERSION, "local-model", SCHEMA_VERSION)
    # inny model = inny klucz, bez trafienia
    other = plan_reading([kuba], store, LLMConfig(model="inny"), TODAY, NOW)
    assert len(other.queue) == 1


def test_same_text_for_two_children_counts_budget_once(store: Store) -> None:
    a = text("n1", C.notice, body="Apel 6.11.", child="ania")
    b = text("n2", C.notice, body="Apel 6.11.", child="kuba")
    plan = plan_reading(
        [a, b], store, LLMConfig(daily_requests=1, urgent_reserve=0), TODAY, NOW, dry_run=True
    )
    assert len(plan.queue) == 2 and plan.deferred == 0


def test_deferred_when_budget_used_and_dry_run_writes_nothing(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    store.add_llm_usage(TODAY, 2, 1_000)
    store.add_llm_usage(TODAY, 1, 500)
    assert store.llm_usage(TODAY) == (3, 1_500)
    assert store.llm_usage(TODAY + timedelta(days=1)) == (0, 0)
    items = [text(str(i), C.homework, valid_to=date(2026, 11, 20)) for i in range(3)]
    for t in items:
        store.upsert_text(t, NOW)
    plan = plan_reading(
        [*items, text("x", body=None)],
        store,
        LLMConfig(daily_requests=5, urgent_reserve=0.2),
        TODAY,
        NOW,
        dry_run=True,
    )
    assert len(plan.queue) == 1 and plan.deferred == 2 and plan.waiting_body == 1
    assert "2 tekstów odłożonych" in caplog.text
    assert store.sieve_of(items[0]) == (None, None)  # dry-run bez zapisu
    assert len(store.texts_to_extract(3)) == 3


def test_cache_get_ignores_non_list(store: Store) -> None:
    key = ("fp", "1", "m", "1")
    assert store.cache_get(key) is None
    store.db.execute("INSERT INTO llm_cache VALUES(?,?,?,?,?,?)", (*key, '{"a": 1}', "x"))
    assert store.cache_get(key) is None
    store.cache_put(key, [{"kind": "bring", "quote": "cyrkiel"}], NOW)
    assert store.cache_get(key) == [{"kind": "bring", "quote": "cyrkiel"}]
