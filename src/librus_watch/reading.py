"""Plan odczytu tekstów przez model: sito, cache, kolejka i budżet (E5a, ADR-013).

Dla każdego tekstu czekającego na odczyt: decyzja sita (odrzucone wychodzą
z kolejki od razu), potem cache wyników (trafienie = gotowe pozycje bez
modelu, także pusty wynik), a reszta trafia do kolejki. Kolejka jest
uporządkowana po najbliższym terminie i rodzaju źródła, a dzienny budżet ma
rezerwę na teksty pilne (dziś i jutro). Model (E5b) czyta wyłącznie kolejkę.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from librus_watch.config import ContentType, LLMConfig
from librus_watch.models import TextItem
from librus_watch.prefilter import content_fp, model_text, sieve
from librus_watch.store import Store

log = logging.getLogger(__name__)

PROMPT_VERSION = "1"
"""Wersja promptu ekstrakcji; część klucza cache (zmiana unieważnia wyniki)."""
SCHEMA_VERSION = "1"
"""Wersja schematu odpowiedzi modelu; część klucza cache."""
PROMPT_OVERHEAD_TOKENS = 1500
"""Szacowany rozmiar stałej części promptu (instrukcja, schemat, przykłady)."""

SOURCE_ORDER: tuple[ContentType, ...] = (
    ContentType.message,
    ContentType.notice,
    ContentType.homework,
    ContentType.agenda,
    ContentType.note,
    ContentType.grade_comment,
    ContentType.lesson_topic,
    ContentType.conference,
    ContentType.trip,
)
"""Kolejność rodzajów źródeł przy tym samym terminie (ważniejsze najpierw)."""


def estimate_tokens(text: str) -> int:
    """Szacunek tokenów zapytania: stały prefiks i ok. 3 znaki na token treści.

    Args:
        text: Tekst dla modelu.

    Returns:
        Szacowana liczba tokenów.
    """
    return PROMPT_OVERHEAD_TOKENS + len(text) // 3 + 1


def due_date(item: TextItem, today: date) -> date | None:
    """Najbliższy termin, którego tekst dotyczy (do kolejności w kolejce).

    Args:
        item: Tekst.
        today: Bieżąca data.

    Returns:
        Najwcześniejsza z dat `valid_from`/`valid_to` od dziś; gdy ich brak,
        dzień po wysłaniu, o ile nie minął (świeże teksty mówią zwykle
        o najbliższych dniach); w pozostałych przypadkach `None`.
    """
    upcoming = [d for d in (item.valid_from, item.valid_to) if d is not None and d >= today]
    if upcoming:
        return min(upcoming)
    next_day = item.sent + timedelta(days=1)
    return next_day if next_day >= today else None


def is_urgent(item: TextItem, today: date) -> bool:
    """Czy tekst może dotyczyć dziś lub jutra (korzysta z rezerwy budżetu).

    Args:
        item: Tekst.
        today: Bieżąca data.

    Returns:
        `True` dla terminu do jutra albo świeżej wiadomości lub ogłoszenia.
    """
    fresh = item.source in (ContentType.message, ContentType.notice) and (
        item.sent >= today - timedelta(days=1)
    )
    due = due_date(item, today)
    return fresh or (due is not None and due <= today + timedelta(days=1))


@dataclass(frozen=True)
class Queued:
    """Tekst czekający na model.

    Attributes:
        item: Tekst.
        key: Klucz cache (odcisk treści, wersja promptu, model, wersja schematu).
        due: Najbliższy termin (`None`: stary tekst bez terminu, na koniec kolejki).
        urgent: Czy pilny.
        tokens: Szacowana liczba tokenów zapytania.
    """

    item: TextItem
    key: tuple[str, str, str, str]
    due: date | None
    urgent: bool
    tokens: int


@dataclass
class Budget:
    """Dzienny budżet modelu z rezerwą na pilne.

    Attributes:
        cfg: Konfiguracja modelu (limity i rezerwa).
        requests: Zapytania już zużyte dziś.
        tokens: Tokeny już zużyte dziś.
    """

    cfg: LLMConfig
    requests: int = 0
    tokens: int = 0

    def take(self, tokens: int, urgent: bool) -> bool:
        """Rezerwuje miejsce na zapytanie, jeśli mieści się w limicie.

        Teksty niepilne mogą zużyć tylko część budżetu bez rezerwy.

        Args:
            tokens: Szacowane tokeny.
            urgent: Czy tekst jest pilny.

        Returns:
            `True`, gdy zapytanie się mieści (i zostało doliczone).
        """
        share = 1.0 if urgent else 1.0 - self.cfg.urgent_reserve
        if self.requests + 1 > int(self.cfg.daily_requests * share):
            return False
        if self.tokens + tokens > int(self.cfg.daily_tokens * share):
            return False
        self.requests += 1
        self.tokens += tokens
        return True


@dataclass
class ReadingPlan:
    """Wynik planowania odczytu (bez treści, do raportu).

    Attributes:
        dropped: Powód sita -> liczba odrzuconych tekstów.
        cached: Teksty z gotowym wynikiem w cache i ten wynik.
        queue: Teksty do odczytu w tej kolejności (zmieszczone w budżecie).
        deferred: Teksty, które nie zmieściły się w dzisiejszym budżecie.
        waiting_body: Wiadomości bez pobranej treści (poza planem).
    """

    dropped: Counter[str] = field(default_factory=Counter)
    cached: list[tuple[TextItem, list[dict[str, object]]]] = field(default_factory=list)
    queue: list[Queued] = field(default_factory=list)
    deferred: int = 0
    waiting_body: int = 0

    @property
    def urgent(self) -> int:
        """Ile tekstów w kolejce jest pilnych."""
        return sum(1 for q in self.queue if q.urgent)


def plan_reading(
    items: Iterable[TextItem],
    store: Store,
    cfg: LLMConfig,
    today: date,
    now: datetime,
    *,
    dry_run: bool = False,
) -> ReadingPlan:
    """Przepuszcza teksty przez sito i cache, układa kolejkę w budżecie.

    W zwykłym przebiegu zapisuje decyzję sita i odcisk treści przy tekście,
    a teksty odrzucone i trafienia w cache oznacza jako odczytane (wychodzą
    z kolejki; zmiana treści przywraca je do kolejki). W dry-run nic nie zapisuje.

    Args:
        items: Teksty czekające na odczyt (z bazy albo z bieżącego cyklu).
        store: Baza (cache i zużycie budżetu).
        cfg: Konfiguracja modelu.
        today: Bieżąca data.
        now: Bieżący moment.
        dry_run: Bez zapisu.

    Returns:
        Plan odczytu.
    """
    plan = ReadingPlan()
    candidates: list[Queued] = []
    for item in items:
        if item.body is None:
            plan.waiting_body += 1
            continue
        decision = sieve(item, today, cfg)
        fp = content_fp(item, cfg.max_input_chars)
        if not dry_run:
            store.set_sieve(item, decision.reason, fp)
        if not decision.passed:
            plan.dropped[decision.reason] += 1
            if not dry_run:
                store.mark_extracted(item, now)
            continue
        key = (fp, PROMPT_VERSION, cfg.model, SCHEMA_VERSION)
        hit = store.cache_get(key)
        if hit is not None:
            plan.cached.append((item, hit))
            if not dry_run:
                store.mark_extracted(item, now)
            continue
        text = model_text(item, cfg.max_input_chars)
        candidates.append(
            Queued(item, key, due_date(item, today), is_urgent(item, today), estimate_tokens(text))
        )

    order = {source: i for i, source in enumerate(SOURCE_ORDER)}
    candidates.sort(
        key=lambda q: (
            not q.urgent,
            q.due or date.max,
            order.get(q.item.source, 99),
            q.item.sent,
            q.item.key,
        )
    )
    seen: set[tuple[str, str, str, str]] = set()
    used_requests, used_tokens = store.llm_usage(today)
    budget = Budget(cfg, used_requests, used_tokens)
    for q in candidates:
        if q.key in seen:  # ten sam tekst u drugiego dziecka: model czyta raz
            plan.queue.append(q)
            continue
        if budget.take(q.tokens, q.urgent):
            seen.add(q.key)
            plan.queue.append(q)
        else:
            plan.deferred += 1
    if plan.deferred:
        log.warning("budżet modelu: %d tekstów odłożonych na kolejny dzień", plan.deferred)
    log.debug(
        "plan odczytu: kolejka %d (pilne %d), cache %d, sito %d, odłożone %d",
        len(plan.queue),
        plan.urgent,
        len(plan.cached),
        sum(plan.dropped.values()),
        plan.deferred,
    )
    return plan
