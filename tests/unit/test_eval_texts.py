"""Spójność zestawu oceny: zestaw jest kontraktem dla sita (E5a) i modelu (E5b)."""

import re
from collections import Counter

import pytest
from fixtures.eval_texts import CASES, TODAY

from librus_watch.config import ContentType, Kind


def _normalized(text: str) -> str:
    """Normalizacja jak przed modelem: bez HTML i encji, jednolite białe znaki."""
    text = re.sub(r"<[^>]+>", " ", text).replace("&nbsp;", " ")
    return " ".join(text.split())


def test_ids_are_unique_and_sources_known() -> None:
    ids = Counter(c["id"] for c in CASES)
    assert [i for i, n in ids.items() if n > 1] == []
    for c in CASES:
        ContentType(c["source"])
        assert c["sieve"] in ("pass", "drop"), c["id"]
        assert c["why"], c["id"]


@pytest.mark.parametrize("c", CASES, ids=lambda c: c["id"])
def test_every_quote_is_in_the_normalized_text(c: dict) -> None:  # type: ignore[type-arg]
    body = _normalized(c["body"])
    for e in c["expect"]:
        Kind(e["kind"])
        assert " ".join(e["quote"].split()) in body, (c["id"], e["quote"])


@pytest.mark.parametrize("c", CASES, ids=lambda c: c["id"])
def test_expected_dates_are_sane(c: dict) -> None:  # type: ignore[type-arg]
    for e in c["expect"]:
        assert (e["day"] - c["sent"]).days >= 0, c["id"]
        assert (e["day"] - c["sent"]).days <= 120, c["id"]
        if e["deadline"]:
            assert e["deadline"] <= e["day"], c["id"]
        if e["at"]:
            assert re.fullmatch(r"\d\d:\d\d", e["at"]), c["id"]


def test_dropped_cases_expect_nothing() -> None:
    for c in CASES:
        if c["sieve"] == "drop":
            assert c["expect"] == [], c["id"]


def test_set_covers_every_channel_and_trap() -> None:
    sources = {c["source"] for c in CASES}
    assert sources >= {
        "message",
        "notice",
        "agenda",
        "homework",
        "grade_comment",
        "note",
        "lesson_topic",
    }
    empty = [c for c in CASES if not c["expect"]]
    assert len(empty) >= 8  # pułapki: cykliczne, warunkowe, przeszłość, inne klasy, ataki
    assert sum(len(c["expect"]) for c in CASES) >= 35
    assert any(c["sent"] < TODAY for c in CASES)
