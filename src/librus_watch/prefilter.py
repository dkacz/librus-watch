"""Przygotowanie tekstów dla modelu: normalizacja, odcisk treści i sito (ADR-013).

Stopnie przed modelem, od najtańszego (`docs/llm.md`):

1. **normalizacja**: bez HTML, encji, cytowanej starej korespondencji i stopki,
   jednolite białe znaki, limit znaków;
2. **odcisk treści**: z typu i znormalizowanego tekstu, bez dziecka, więc to
   samo ogłoszenie na dwóch kontach ma ten sam odcisk (wspólny cache);
3. **sito**: odrzuca bez modelu teksty puste, przeterminowane, starą historię
   i teksty z kanałów o słabym sygnale (komentarze, uwagi, tematy lekcji) bez
   dat i słów-kluczy. Wiadomości, ogłoszenia, zadania i wpisy terminarza
   z aktualnym terminem przechodzą zawsze: tam pomyłka kosztuje najwięcej.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, timedelta

from librus_watch.config import ContentType, LLMConfig
from librus_watch.models import TextItem, fingerprint

LABELS: dict[ContentType, str] = {
    ContentType.message: "wiadomość",
    ContentType.notice: "ogłoszenie",
    ContentType.agenda: "terminarz",
    ContentType.homework: "zadanie domowe",
    ContentType.grade_comment: "komentarz do oceny",
    ContentType.note: "uwaga",
    ContentType.lesson_topic: "temat lekcji",
    ContentType.conference: "zebranie",
    ContentType.trip: "wycieczka",
}
"""Etykieta rodzaju tekstu w treści dla modelu."""

WEAK_SOURCES = frozenset({ContentType.grade_comment, ContentType.note, ContentType.lesson_topic})
"""Kanały, w których odstępstwa zdarzają się rzadko: przechodzą tylko z sygnałem."""

MIN_CHARS = 5

_QUOTE_MARKERS = re.compile(
    r"^(-{2,}\s*(wiadomość oryginalna|wiadomosc oryginalna|original message)\s*-{2,}"
    r"|w dniu .{4,80} napisał\(a\)?:|w dniu .{4,80} napisał[a]?:|from:\s.+)",
    re.IGNORECASE,
)
_SIGNATURE = re.compile(r"^--\s*$")
_TAG = re.compile(r"<[^>]+>")
_BLOCK_TAG = re.compile(r"</?(p|br|div|li|tr|h\d)\b[^>]*>", re.IGNORECASE)

_SIGNAL = re.compile(
    r"\b\d{1,2}[./]\d{1,2}\b"  # 12.11, 12/11
    r"|\b\d{1,2}:\d{2}\b"  # 7:30
    r"|\b\d{1,2}\s+(stycz|lut|mar|kwie|maj|czerw|lip|sierp|wrze|paźdz|pazdz|listop|grud)"
    r"|\b\d{1,2}\s+[IVX]{1,4}\b"  # 12 XI
    r"|\b(pon|pn|wt|śr|sr|czw|pt|sob|niedz)\.?(?=\W|$)"
    r"|\b(poniedział|poniedzial|wtor|środ|srod|czwart|piąt|piat|sobot|niedziel)"
    r"|\b(dziś|dzis|dzisiaj|jutr|pojutrz|w przyszł|w przyszl|za tydzień|za tydzien)",
    re.IGNORECASE,
)


def normalize(text: str, max_chars: int) -> str:
    """Normalizuje tekst przed modelem i przed kontrolą cytatu.

    Args:
        text: Surowa treść (może zawierać HTML).
        max_chars: Limit znaków po normalizacji.

    Returns:
        Tekst bez HTML i encji, bez cytowanej starej wiadomości i stopki,
        z pojedynczymi spacjami w liniach i bez pustych linii.
    """
    text = html.unescape(_TAG.sub(" ", _BLOCK_TAG.sub("\n", text)))
    lines: list[str] = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if _SIGNATURE.match(raw.strip()) or _QUOTE_MARKERS.match(line):
            break
        if line.startswith(">"):
            continue
        if line:
            lines.append(line)
    return "\n".join(lines)[:max_chars]


def model_text(item: TextItem, max_chars: int) -> str:
    """Tekst przekazywany modelowi: rodzaj, tytuł i znormalizowana treść.

    Args:
        item: Tekst z bazy (z treścią).
        max_chars: Limit znaków treści.

    Returns:
        Rodzaj i tytuł w pierwszej linii (np. „[ogłoszenie] Strój galowy”),
        treść w kolejnych.
    """
    label = LABELS.get(item.source, item.source.value)
    title = " ".join(item.title.split())
    head = f"[{label}] {title}".rstrip()
    return f"{head}\n{normalize(item.body or '', max_chars)}"


def content_fp(item: TextItem, max_chars: int) -> str:
    """Odcisk treści: typ i tekst dla modelu, bez dziecka i bez identyfikatora wpisu.

    Args:
        item: Tekst.
        max_chars: Limit znaków (część normalizacji).

    Returns:
        Odcisk do klucza cache wyników modelu.
    """
    return fingerprint("text", item.source.value, model_text(item, max_chars))


@dataclass(frozen=True)
class SieveDecision:
    """Decyzja sita.

    Attributes:
        passed: Czy tekst idzie dalej (do cache i kolejki).
        reason: Powód: `ok`, `signal`, `empty`, `expired`, `old`, `no_signal`.
    """

    passed: bool
    reason: str


def has_signal(text: str, keywords: list[str]) -> bool:
    """Czy tekst zawiera datę, godzinę, dzień tygodnia, określenie czasu albo słowo-klucz.

    Args:
        text: Znormalizowany tekst.
        keywords: Rdzenie słów-kluczy (bez rozróżniania wielkości liter).

    Returns:
        `True`, gdy jest sygnał.
    """
    lowered = text.lower()
    return bool(_SIGNAL.search(text)) or any(k.lower() in lowered for k in keywords)


def sieve(item: TextItem, today: date, cfg: LLMConfig) -> SieveDecision:
    """Decyduje, czy tekst wymaga odczytu przez model.

    Args:
        item: Tekst z treścią.
        today: Bieżąca data.
        cfg: Konfiguracja modelu (okno historii, słowa-klucze, limit znaków).

    Returns:
        Decyzja z powodem (zapisywanym w bazie, nie w logach).
    """
    body = normalize(item.body or "", cfg.max_input_chars)
    if len(body) < MIN_CHARS:
        return SieveDecision(False, "empty")
    if item.valid_to is not None and item.valid_to < today:
        return SieveDecision(False, "expired")
    current = item.valid_to is not None and item.valid_to >= today
    if item.sent < today - timedelta(days=cfg.backfill_days) and not current:
        return SieveDecision(False, "old")
    if item.source in WEAK_SOURCES:
        text = f"{item.title}\n{body}"
        if has_signal(text, cfg.keywords):
            return SieveDecision(True, "signal")
        return SieveDecision(False, "no_signal")
    return SieveDecision(True, "ok")
