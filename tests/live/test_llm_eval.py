"""Zestaw oceny na żywym modelu (`pytest -m llm`, ręcznie).

Wymaga zmiennych `LLM_BASE_URL` (np. `http://127.0.0.1:18080/v1`) i `LLM_MODEL`.
Wypisuje trafienia, pominięcia i fałszywe pozycje dla każdego przypadku
z `tests/fixtures/eval_texts.py` oraz sumę zapytań i tokenów.
"""

from __future__ import annotations

import os
from datetime import date

import pytest
from fixtures.eval_texts import CASES

from librus_watch.config import ContentType, LLMConfig
from librus_watch.extract import OpenAIChat, extract_one, parse_answer
from librus_watch.models import TextItem

pytestmark = pytest.mark.llm

TRAPS = {
    "kazdy-wtorek",
    "warunkowe",
    "relacja",
    "prompt-injection",
    "zmyslony-cytat-pulapka",
    "po-feriach",
    "inna-klasa",
}


@pytest.fixture(scope="module")
def chat() -> tuple[OpenAIChat, LLMConfig]:
    url, model = os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL")
    if not url or not model:
        pytest.skip("brak LLM_BASE_URL / LLM_MODEL")
    cfg = LLMConfig(base_url=url, model=model, json_mode="json_schema")
    return OpenAIChat(cfg, None), cfg


def test_evaluation_set(chat: tuple[OpenAIChat, LLMConfig]) -> None:
    client, cfg = chat
    assert parse_answer('{"items": []}').items == []
    hits = misses = false = requests = tokens = 0
    trap_hits: list[str] = []
    for c in (c for c in CASES if c["sieve"] == "pass"):
        text = TextItem(
            "x", ContentType(c["source"]), c["id"], c["sent"], "", c["title"], c["body"]
        )
        result = extract_one(client, text, cfg, "json_schema", cfg.cold_start_timeout_seconds, "3b")
        requests += result.requests
        tokens += result.tokens
        got = {(r["kind"], date.fromisoformat(r["day"])) for r in result.items}
        want = {(e["kind"], e["day"]) for e in c["expect"]}
        hits += len(got & want)
        misses += len(want - got)
        false += len(got - want)
        if c["id"] in TRAPS and got:
            trap_hits.append(c["id"])
        print(
            f"{c['id']:24} ok={len(got & want)} brak={sorted(want - got)} "
            f"nadmiar={sorted(got - want)} odrzucone={result.rejected}"
        )
    total = hits + misses
    print(
        f"\nRAZEM: trafienia {hits}/{total} ({hits / max(total, 1):.0%}), "
        f"fałszywe {false}, zapytania {requests}, tokeny {tokens}, pułapki {trap_hits}"
    )
    assert "kazdy-wtorek" not in trap_hits
