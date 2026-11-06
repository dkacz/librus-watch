"""Anonimizacja tekstów do wspólnego korpusu (E11, ADR-017, ADR-018).

Kolejno: słowniki Librusa (nauczyciele, dzieci, klasa) na znaczniki, dane
kontaktowe i identyfikatory (telefon, e-mail, numer konta, PESEL, adres,
kod pocztowy, adres WWW) na znaczniki, daty bezwzględne na określenia
względne od daty wysłania. Na końcu zasada „w razie wątpliwości odrzuć”:
tekst z niewyjaśnionym słowem od wielkiej litery (poza początkiem zdania
i listą słów zwykłych) albo z długą liczbą nie trafia do korpusu.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("[EMAIL]", re.compile(r"\b[\w.+-]+@[\w-]+(\.[\w-]+)+\b")),
    ("[WWW]", re.compile(r"\b(https?://|www\.)\S+", re.IGNORECASE)),
    ("[KONTO]", re.compile(r"\b(PL)?\d{2}( ?\d{4}){6}\b")),
    ("[PESEL]", re.compile(r"\b\d{11}\b")),
    ("[TELEFON]", re.compile(r"(\+48[ -]?)?\b\d{3}[ -]?\d{3}[ -]?\d{3}\b")),
    ("[KOD]", re.compile(r"\b\d{2}-\d{3}\b")),
    (
        "[ADRES]",
        re.compile(
            r"\b(ul\.|ulica|al\.|aleja|os\.|osiedle|pl\.|plac)\s+[\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ.-]+(\s+\d+\w?)?",
            re.IGNORECASE,
        ),
    ),
)
_DATE_NUM = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")
_MONTHS = (
    "stycz",
    "lut",
    "mar",
    "kwie",
    "maj",
    "czerw",
    "lip",
    "sierp",
    "wrze",
    "paźdz",
    "listop",
    "grud",
)
_DATE_WORD = re.compile(r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\w*", re.IGNORECASE)
_CAPITAL = re.compile(r"\b([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]{2,})\b")
_SENTENCE_START = re.compile(r"(^|[.!?:;]\s+|\n\s*|[-•*]\s+|„)$")
_LONG_NUMBER = re.compile(r"\d{5,}")

COMMON = frozenset(
    [
        "Dzień",
        "Dobry",
        "Szanowni",
        "Państwo",
        "Drodzy",
        "Rodzice",
        "Rodzica",
        "Uwaga",
        "Pani",
        "Pan",
        "Proszę",
        "Prosimy",
        "Przypominam",
        "Przypominamy",
        "Informuję",
        "Informujemy",
        "Dziękuję",
        "Dziękujemy",
        "Pozdrawiam",
        "Pozdrawiamy",
        "Wychowawca",
        "Wychowawczyni",
        "Dyrekcja",
        "Dyrektor",
        "Sekretariat",
        "Szkoła",
        "Szkoły",
        "Klasa",
        "Klasy",
        "Uczniowie",
        "Dzieci",
        "Poniedziałek",
        "Wtorek",
        "Środa",
        "Czwartek",
        "Piątek",
        "Sobota",
        "Niedziela",
        "Matematyka",
        "Polski",
        "Angielski",
        "Język",
        "Historia",
        "Przyroda",
        "Biologia",
        "Geografia",
        "Fizyka",
        "Chemia",
        "Muzyka",
        "Plastyka",
        "Technika",
        "Informatyka",
        "Religia",
        "Etyka",
        "Kartkówka",
        "Sprawdzian",
        "Wycieczka",
        "Zebranie",
        "Zbiórka",
        "Zgoda",
        "Wiadomość",
        "Ogłoszenie",
        "Termin",
        "Jutro",
        "Dziś",
        "Dzisiaj",
        "Wszystkich",
        "Wszystkie",
        "Bardzo",
        "Także",
        "Również",
        "Natomiast",
        "Jednak",
        "Ponadto",
        "Oraz",
        "Lekcje",
        "Lekcja",
        "Zajęcia",
        "Świetlica",
        "Biblioteka",
        "Basen",
        "Teatr",
        "Kino",
        "Muzeum",
        "Strój",
        "Rada",
        "Pedagogiczna",
        "Ferie",
        "Dear",
        "Parents",
        "Please",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "English",
    ]
)


@dataclass(frozen=True)
class Anonymized:
    """Wynik anonimizacji.

    Attributes:
        text: Tekst ze znacznikami albo pusty, gdy odrzucony.
        rejected: Powód odrzucenia (`capital`, `number`) albo pusty napis.
    """

    text: str
    rejected: str = ""


def relative(day: date, sent: date) -> str:
    """Data względna od daty wysłania, np. `[D+1]`."""
    delta = (day - sent).days
    return f"[D{delta:+d}]"


def _dates(text: str, sent: date) -> str:
    """Daty bezwzględne (12.11, 12.11.2026, 12 listopada) na `[D+n]`."""

    def num(m: re.Match[str]) -> str:
        d, mth = int(m.group(1)), int(m.group(2))
        if not (1 <= d <= 31 and 1 <= mth <= 12):
            return m.group(0)
        year = int(m.group(3)) if m.group(3) else sent.year
        year = year + 2000 if year < 100 else year
        try:
            return relative(date(year, mth, d), sent)
        except ValueError:
            return m.group(0)

    def word(m: re.Match[str]) -> str:
        month = next(i for i, p in enumerate(_MONTHS, 1) if m.group(2).lower().startswith(p))
        try:
            return relative(date(sent.year, month, int(m.group(1))), sent)
        except ValueError:
            return m.group(0)

    return _DATE_WORD.sub(word, _DATE_NUM.sub(num, text))


def anonymize(
    text: str, sent: date, names: list[str], extra_allowed: set[str] | None = None
) -> Anonymized:
    """Anonimizuje tekst; przy wątpliwości odrzuca.

    Args:
        text: Tekst (po normalizacji).
        sent: Data wysłania (podstawa dat względnych).
        names: Imiona, nazwiska, nazwy klas i szkoły ze słowników (na znaczniki).
        extra_allowed: Dodatkowe słowa od wielkiej litery uznane za zwykłe (np. przedmioty).

    Returns:
        Tekst ze znacznikami albo odrzucenie z powodem.
    """
    out = text
    parts = sorted(
        {p for n in names for p in [n, *n.split()] if len(p) >= 3 or any(c.isdigit() for c in p)},
        key=len,
        reverse=True,
    )
    for i, name in enumerate(parts):
        out = re.sub(rf"\b{re.escape(name)}\w*", f"[OSOBA_{i + 1}]", out)
    for tag, pattern in PATTERNS:
        out = pattern.sub(tag, out)
    out = _dates(out, sent)
    if _LONG_NUMBER.search(out):
        return Anonymized("", "number")
    allowed = COMMON | (extra_allowed or set())
    for m in _CAPITAL.finditer(out):
        if m.group(1) in allowed or _SENTENCE_START.search(out[: m.start()]):
            continue  # słowo zwykłe albo początek zdania lub linii
        return Anonymized("", "capital")
    return Anonymized(out)
