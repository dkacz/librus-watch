"""Lokalna pętla zwrotna (E7a, ADR-016): oceny, audyt w cieniu, zestaw oceny.

Wszystko zostaje w katalogu danych. W logach są wyłącznie liczby i klucze.

* `/brak` jest wiązany z tekstem z ostatnich dni po wspólnych słowach;
* **audyt w cieniu**: codziennie kilka tekstów odrzuconych przez sito albo
  przesiew czyta model bez wysyłania powiadomień; znalezisko to kandydat do
  przeglądu (możliwe pominięcie);
* **kandydaci z rozjazdu**: model widzi zmianę godzin, której nie widać
  w pobranym planie; tekst ze słowami alarmowymi, w którym model nic nie znalazł;
* **raport tygodniowy** i pytania o kandydatów (przyciski w bocie);
* **lokalny zestaw oceny** z ocenionych przypadków i bramka zmian;
* **przykłady do promptu** z potwierdzonych odczytów;
* **metryki** dla `node_exporter` (plik tekstowy).
"""

from __future__ import annotations

import logging
import random
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from librus_watch.config import Config, ContentType, Kind
from librus_watch.extract import BadAnswer, ChatClient, ModelUnavailable, extract_one
from librus_watch.models import Origin, TextItem
from librus_watch.prefilter import content_fp, model_text
from librus_watch.reading import PROMPT_VERSION, SCHEMA_VERSION
from librus_watch.store import Store

log = logging.getLogger(__name__)

ALARM_WORDS = (
    "jutro",
    "zbiórk",
    "zbiork",
    "zgod",
    "przynie",
    "odwoł",
    "odwol",
    "wcześniej",
    "wczesniej",
    "strój",
    "stroj",
)
TIME_KINDS = (Kind.early_start, Kind.late_start, Kind.early_end, Kind.late_end, Kind.day_off)
LINK_DAYS = 7
_WORD = re.compile(r"\w{4,}", re.UNICODE)


def text_ref(t: TextItem) -> str:
    """Odnośnik do tekstu w ocenach i audycie."""
    return f"{t.child}|{t.source.value}|{t.key}"


def recent_texts(cfg: Config, store: Store, now: datetime, days: int) -> list[TextItem]:
    """Teksty wszystkich dzieci z treścią, wysłane w ostatnich dniach."""
    since = now.date() - timedelta(days=days)
    return [t for c in cfg.children for t in store.texts(c.id) if t.body and t.sent >= since]


def link_missed(cfg: Config, store: Store, note: str, now: datetime) -> str | None:
    """Tekst z ostatnich dni najbardziej pasujący do zgłoszenia `/brak` (min. 2 wspólne słowa)."""
    words = {w.lower() for w in _WORD.findall(note)}
    best: tuple[int, str] | None = None
    for t in recent_texts(cfg, store, now, LINK_DAYS):
        common = len(words & {w.lower() for w in _WORD.findall(f"{t.title} {t.body}")})
        if common >= 2 and (best is None or common > best[0]):
            best = (common, text_ref(t))
    return best[1] if best else None


def shadow_audit(cfg: Config, store: Store, client: ChatClient, now: datetime) -> int:
    """Czyta w cieniu próbkę tekstów odrzuconych przez sito lub przesiew.

    Raz dziennie, najwyżej `feedback.audit_daily` tekstów, z budżetu modelu.
    Znalezisko trafia do kandydatów (bez powiadomienia).

    Returns:
        Liczba nowych kandydatów.
    """
    limit = cfg.feedback.audit_daily
    key = f"shadow_audit:{now.date()}"
    if not limit or store.get_meta(key):
        return 0
    pool = []
    for t in recent_texts(cfg, store, now, LINK_DAYS):
        reason, _fp = store.sieve_of(t)
        if reason in ("no_signal", "old", "triage_out"):
            pool.append((t, "shadow_triage" if reason == "triage_out" else "shadow_sieve"))
    rng = random.Random(now.date().toordinal())  # noqa: S311 - losowanie próbki, nie kryptografia
    found = 0
    for t, why in rng.sample(pool, min(limit, len(pool))):
        try:
            result = extract_one(
                client, t, cfg.llm, "json_schema", cfg.llm.cold_start_timeout_seconds
            )
        except (ModelUnavailable, BadAnswer):
            continue
        store.add_llm_usage(now.date(), result.requests, result.tokens)
        if result.items and store.add_audit(
            t.child, t.source.value, t.key, why, [str(i["kind"]) for i in result.items], now
        ):
            found += 1
    store.set_meta(key, now.isoformat())
    log.info(
        "audyt w cieniu: %d tekstów z puli %d, nowych kandydatów %d",
        min(limit, len(pool)),
        len(pool),
        found,
    )
    return found


def find_candidates(cfg: Config, store: Store, now: datetime) -> int:
    """Kandydaci z rozjazdu źródeł i ze słów alarmowych (bez modelu).

    Returns:
        Liczba nowych kandydatów.
    """
    today = now.date()
    new = 0
    timetable = {
        (s.finding.child, s.finding.day)
        for s in store.current_findings(today, today + timedelta(days=14))
        if s.finding.origin is Origin.timetable
    }
    for s in store.current_findings(today, today + timedelta(days=14)):
        f = s.finding
        if f.origin is not Origin.llm or f.kind not in TIME_KINDS:
            continue
        if store.lessons(f.child, f.day) and (f.child, f.day) not in timetable:
            source, _, key = f.source_key.partition(":")
            new += store.add_audit(f.child, source, key, "disagreement", [f.kind.value], now)
    for t in recent_texts(cfg, store, now, 2):
        reason, fp = store.sieve_of(t)
        if reason not in ("ok", "signal") or fp is None:
            continue
        result = store.cache_get((fp, PROMPT_VERSION, cfg.llm.model, SCHEMA_VERSION))
        body = f"{t.title} {t.body}".lower()
        if result == [] and any(w in body for w in ALARM_WORDS):
            new += store.add_audit(t.child, t.source.value, t.key, "alarm_words", [], now)
    return new


def weekly_report(cfg: Config, store: Store, now: datetime) -> str:
    """Raport tygodniowy: oceny z ostatnich 7 dni i liczba kandydatów (bez treści)."""
    since = (now - timedelta(days=7)).isoformat()
    ratings = Counter(r["rating"] for r in store.feedback() if str(r["created_at"]) >= since)
    sent = store.db.execute("SELECT COUNT(*) FROM sent WHERE sent_at>=?", (since,)).fetchone()[0]
    open_audits = len(store.audits())
    return (
        f"librus-watch, tydzień do {now:%d.%m}:\n"
        f"wysłanych pozycji {sent}; trafne {ratings['confirmed']}, błędne {ratings['wrong']}, "
        f"przegapione {ratings['missed']}.\n"
        f"Do sprawdzenia: {open_audits}."
    )


def audit_questions(cfg: Config, store: Store, limit: int = 3) -> list[tuple[int, str]]:
    """Pytania o kandydatów: (id, treść pytania z fragmentem tekstu) do bota."""
    names = {c.id: c.name for c in cfg.children}
    out = []
    for a in store.audits()[:limit]:
        texts = [
            t
            for t in store.texts(str(a["child"]), ContentType(str(a["source"])))
            if t.key == a["key"]
        ]
        if not texts:
            continue
        t = texts[0]
        snippet = " ".join((t.body or "").split())[:160]
        label = {
            "shadow_sieve": "odrzucone przez sito",
            "shadow_triage": "odrzucone przez przesiew",
            "disagreement": "model widzi zmianę godzin, której nie ma w planie",
            "alarm_words": "słowa alarmowe, a model nic nie znalazł",
        }.get(str(a["reason"]), "")
        out.append(
            (
                int(str(a["id"])),
                f"{names.get(t.child, t.child)}, {t.source.value} z {t.sent:%d.%m} ({label}):\n"
                f"„{snippet}”\nCzy to powinno przyjść jako powiadomienie?",
            )
        )
    return out


def examples_for(cfg: Config, store: Store, source: ContentType) -> list[str]:
    """Przykłady do promptu: teksty tego rodzaju z potwierdzonymi pozycjami modelu."""
    if not cfg.feedback.examples:
        return []
    confirmed = {r["identity"] for r in store.feedback() if r["rating"] == "confirmed"}
    out: list[str] = []
    for s in store.current_findings(date.min, date.max):
        f = s.finding
        if f.identity not in confirmed or f.origin is not Origin.llm:
            continue
        src, _, key = f.source_key.partition(":")
        if src != source.value:
            continue
        match = [t for t in store.texts(f.child, source) if t.key == key and t.body]
        if match:
            item = {"kind": f.kind.value, "day": f.day.isoformat(), "quote": f.quote}
            out.append(f"{model_text(match[0], 600)}\n→ {item}")
        if len(out) >= cfg.feedback.examples:
            break
    return out


@dataclass
class EvalScore:
    """Wynik na lokalnym zestawie oceny.

    Attributes:
        cases: Liczba przypadków.
        kept: Potwierdzone pozycje odtworzone przez model.
        confirmed: Wszystkie potwierdzone pozycje.
        false: Pozycje ocenione jako błędne, które model znów zwrócił.
        missed_found: Zgłoszone pominięcia, w których model teraz coś znalazł.
        missed: Wszystkie zgłoszone pominięcia z tekstem.
    """

    cases: int = 0
    kept: int = 0
    confirmed: int = 0
    false: int = 0
    missed_found: int = 0
    missed: int = 0

    @property
    def score(self) -> float:
        """Jedna liczba do bramki: trafienia i odnalezione pominięcia minus powtórzone błędy."""
        good = self.kept + self.missed_found
        total = max(self.confirmed + self.missed, 1)
        return (good - self.false) / total


def local_cases(cfg: Config, store: Store) -> list[tuple[TextItem, set[tuple[str, str]], str]]:
    """Przypadki z ocen: (tekst, (rodzaj, dzień) ocenione pozycje, ocena)."""
    by_identity = {
        s.finding.identity: s.finding for s in store.current_findings(date.min, date.max)
    }
    cases: dict[str, tuple[TextItem, set[tuple[str, str]], str]] = {}
    for r in store.feedback():
        f = by_identity.get(str(r["identity"])) if r["identity"] else None
        if f is not None and f.origin is Origin.llm:
            src, _, key = f.source_key.partition(":")
            ref = f"{f.child}|{src}|{key}"
            pair = {(f.kind.value, f.day.isoformat())}
        elif r["rating"] == "missed" and r["text_ref"]:
            ref, pair = str(r["text_ref"]), set()
        else:
            continue
        child, src, key = ref.split("|")
        texts = [t for t in store.texts(child, ContentType(src)) if t.key == key and t.body]
        if texts:
            cases[f"{ref}|{r['rating']}|{pair}"] = (texts[0], pair, str(r["rating"]))
    return list(cases.values())


def local_eval(cfg: Config, store: Store, client: ChatClient) -> EvalScore:
    """Uruchamia lokalny zestaw oceny na bieżącym modelu i prompcie."""
    score = EvalScore()
    for text, pairs, rating in local_cases(cfg, store):
        score.cases += 1
        try:
            result = extract_one(client, text, cfg.llm, "json_schema", cfg.llm.timeout_seconds)
        except (ModelUnavailable, BadAnswer):
            got: set[tuple[str, str]] = set()
        else:
            got = {(str(i["kind"]), str(i["day"])) for i in result.items}
        if rating == "confirmed":
            score.confirmed += len(pairs)
            score.kept += len(pairs & got)
        elif rating == "wrong":
            score.false += len(pairs & got)
        elif rating == "missed":
            score.missed += 1
            score.missed_found += bool(got)
    return score


def gate(cfg: Config, store: Store, score: EvalScore, set_baseline: bool = False) -> bool:
    """Bramka zmian: wynik nie może być gorszy niż zapisany punkt odniesienia.

    Args:
        cfg: Konfiguracja (model).
        store: Baza (punkt odniesienia w `meta`).
        score: Wynik bieżącej konfiguracji.
        set_baseline: Zapisz wynik jako nowy punkt odniesienia (gdy bramka przepuszcza).

    Returns:
        `True`, gdy zmiana może być włączona.
    """
    key = "eval_baseline"
    raw = store.get_meta(key)
    ok = raw is None or score.score >= float(raw.split("@")[0])
    if ok and set_baseline:
        store.set_meta(key, f"{score.score:.4f}@{cfg.llm.model}@{PROMPT_VERSION}")
    if not ok:
        log.warning(
            "bramka: wynik %.3f gorszy niż punkt odniesienia, zmiana odrzucona", score.score
        )
    return ok


def write_metrics(cfg: Config, store: Store, now: datetime, extra: dict[str, float]) -> None:
    """Zapisuje metryki (format Prometheus) dla `node_exporter`; bez treści i bez imion."""
    path = cfg.feedback.metrics_textfile
    if path is None:
        return
    ratings = Counter(r["rating"] for r in store.feedback())
    lines = [f'librus_watch_feedback_total{{rating="{k}"}} {v}' for k, v in sorted(ratings.items())]
    lines.append(f"librus_watch_audit_open {len(store.audits())}")
    lines += [f"librus_watch_{k} {v}" for k, v in sorted(extra.items())]
    lines.append(f"librus_watch_last_run_timestamp {int(now.timestamp())}")
    tmp = Path(str(path) + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)


def mark_triaged(store: Store, items: list[Any], cfg: Config) -> None:
    """Oznacza teksty odrzucone przez przesiew (pula audytu w cieniu)."""
    for item in items:
        store.set_sieve(item, "triage_out", content_fp(item, cfg.llm.max_input_chars))
