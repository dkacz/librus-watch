from datetime import date

import pytest

from librus_watch.anonymize import anonymize, relative

SENT = date(2026, 11, 5)
NAMES = ["Anna Testowa", "Iwo", "3b"]


@pytest.mark.parametrize(
    ("text", "leaks", "tags"),
    [
        (
            "Anna Testowa prosi o kontakt: 600 123 456 albo +48 600-123-456.",
            ["Anna", "Testowa", "600"],
            ["[OSOBA_", "[TELEFON]"],
        ),
        (
            "Pisać na anna.testowa@szkola.edu.pl, strona www.szkola.edu.pl/plan.",
            ["anna.testowa", "szkola.edu"],
            ["[EMAIL]", "[WWW]"],
        ),
        (
            "Zbiórka na ul. Polnej 5, 00-950, wpłaty na PL61 1090 1014 0000 0712 1981 2874.",
            ["Polnej", "00-950", "1090"],
            ["[ADRES]", "[KOD]", "[KONTO]"],
        ),
        (
            "Sprawdzian 12.11, poprawa 19 listopada, zebranie 4.11.2026.",
            ["12.11", "19 listopada", "4.11"],
            ["[D+7]", "[D+14]", "[D-1]"],
        ),
        ("Iwo i Iwona z 3b jadą jutro.", ["Iwo ", "Iwona", "3b"], ["[OSOBA_"]),
        ("PESEL dziecka 12345678901 proszę podać.", ["12345678901"], ["[PESEL]"]),
    ],
)
def test_no_leaks(text: str, leaks: list[str], tags: list[str]) -> None:
    result = anonymize(text, SENT, NAMES)
    assert result.rejected == "" and result.text
    assert not any(leak in result.text for leak in leaks), result.text
    assert all(tag in result.text for tag in tags), result.text


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Gratulujemy Zosi Kowalskiej zwycięstwa.", "capital"),  # osoba spoza słowników
        ("Wyjazd do Zakopanego w czwartek.", "capital"),  # rzadkie, rozpoznawalne miejsce
        ("Numer legitymacji 123456.", "number"),
    ],
)
def test_doubtful_texts_are_rejected(text: str, reason: str) -> None:
    result = anonymize(text, SENT, NAMES)
    assert result.rejected == reason and result.text == ""


def test_plain_school_text_passes_unchanged() -> None:
    text = "Jutro zbiórka o 7:30 pod szkołą. Proszę o strój galowy. Dziękuję, Wychowawczyni."
    assert anonymize(text, SENT, NAMES).text == text
    assert relative(date(2026, 11, 4), SENT) == "[D-1]"
