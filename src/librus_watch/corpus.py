"""Wspólny korpus przykładów (E11, ADR-018: opt-out, wysyłka automatyczna).

Z lokalnych ocen (potwierdzone i błędne odczyty modelu, zgłoszone pominięcia
powiązane z tekstem) powstają przykłady po anonimizacji (`anonymize`). Tekst
budzący wątpliwość nie jest wysyłany. Każdy przykład wychodzi raz; dokładnie to,
co wyszło, jest dopisywane do lokalnego dziennika `corpus_sent.jsonl`.
Wysyłka idzie raz dziennie na `feedback.corpus_endpoint` tym samym kanałem co
telemetria (POST JSON). Bez informacji przy pierwszym uruchomieniu nic nie wychodzi.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from librus_watch import __version__
from librus_watch.anonymize import anonymize, relative
from librus_watch.config import Config, ContentType, Kind
from librus_watch.feedback import local_cases
from librus_watch.models import fingerprint
from librus_watch.prefilter import normalize
from librus_watch.reading import PROMPT_VERSION
from librus_watch.store import Store
from librus_watch.telemetry import endpoint_allowed

log = logging.getLogger(__name__)
MAX_TEXT = 1500
LOG_FILE = "corpus_sent.jsonl"


class CorpusItem(BaseModel):
    """Oczekiwana pozycja w przykładzie (po anonimizacji)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    kind: Kind
    day: str = Field(pattern=r"^\[D[+-]\d{1,3}\]$")
    quote: str = Field(max_length=300)


class CorpusExample(BaseModel):
    """Jeden przykład korpusu."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    source: ContentType
    rating: Literal["confirmed", "wrong", "missed"]
    text: str = Field(min_length=5, max_length=MAX_TEXT)
    items: list[CorpusItem] = Field(max_length=10)


class CorpusPacket(BaseModel):
    """Paczka przykładów (odbiornik odrzuca wszystko inne)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1]
    app_version: str = Field(max_length=20)
    prompt_version: str = Field(max_length=10)
    examples: list[CorpusExample] = Field(min_length=1, max_length=50)


def build_examples(cfg: Config, store: Store) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Przykłady gotowe do wysyłki (jeszcze niewysłane) i liczniki odrzuceń.

    Returns:
        (przykłady z kluczem `_fp` do dziennika, powód -> liczba odrzuconych).
    """
    names = [n for c in cfg.children for n in (c.name,)]
    for c in cfg.children:
        names += list(store.lookups(c.id, "users").values())
    rejected: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for text, pairs, rating in local_cases(cfg, store):
        if rating not in ("confirmed", "wrong", "missed"):
            continue
        body = normalize(f"{text.title}\n{text.body or ''}", MAX_TEXT)
        anon = anonymize(body, text.sent, names)
        if anon.rejected:
            rejected[anon.rejected] = rejected.get(anon.rejected, 0) + 1
            continue
        items = []
        for kind, day in sorted(pairs):
            items.append(
                {"kind": kind, "day": relative(date.fromisoformat(day), text.sent), "quote": ""}
            )
        fp = fingerprint("corpus", text.source.value, anon.text, rating, str(sorted(pairs)))
        if store.get_meta(f"corpus_sent:{fp}"):
            continue
        example = {
            "source": text.source.value,
            "rating": rating,
            "text": anon.text,
            "items": items,
            "_fp": fp,
        }
        CorpusExample.model_validate({k: v for k, v in example.items() if k != "_fp"})
        out.append(example)
    return out, rejected


def send_corpus(cfg: Config, store: Store, now: datetime, post: Any = None) -> int:
    """Wysyła nowe przykłady raz dziennie (opt-out). Zwraca liczbę wysłanych przykładów."""
    f = cfg.feedback
    if not f.share_corpus or not f.corpus_endpoint or not store.get_meta("corpus_notice"):
        return 0
    key = f"corpus_day:{now.date()}"
    if store.get_meta(key):
        return 0
    if not endpoint_allowed(f.corpus_endpoint):
        log.warning("korpus: niedozwolony adres (wymagany HTTPS), wysyłka pominięta")
        return 0
    examples, rejected = build_examples(cfg, store)
    store.set_meta(key, now.isoformat())
    if rejected:
        log.info("korpus: odrzucone przy anonimizacji %s", dict(sorted(rejected.items())))
    if not examples:
        return 0
    packet = {
        "schema_version": 1,
        "app_version": __version__,
        "prompt_version": PROMPT_VERSION,
        "examples": [{k: v for k, v in e.items() if k != "_fp"} for e in examples[:50]],
    }
    if post is None:
        import requests

        def post(url: str, data: dict[str, Any]) -> int:
            return requests.post(url, json=data, timeout=15).status_code

    try:
        status = post(f.corpus_endpoint, packet)
    except Exception as e:  # korpus nigdy nie zatrzymuje usługi
        log.warning("korpus: wysyłka nieudana (%s)", type(e).__name__)
        return 0
    if status >= 300:
        log.warning("korpus: odbiornik odpowiedział %d", status)
        return 0
    with (Path(cfg.data_dir) / LOG_FILE).open("a") as fh:
        fh.write(
            json.dumps({"sent_at": now.isoformat(), "packet": packet}, ensure_ascii=False) + "\n"
        )
    for example in examples[:50]:
        store.set_meta(f"corpus_sent:{example['_fp']}", now.isoformat())
    count = min(len(examples), 50)
    log.info("korpus: wysłano %d przykładów", count)
    return count
