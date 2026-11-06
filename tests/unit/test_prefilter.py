from datetime import date

import pytest
from fixtures.eval_texts import CASES, TODAY

from librus_watch.config import ContentType, LLMConfig
from librus_watch.models import TextItem
from librus_watch.prefilter import content_fp, has_signal, model_text, normalize, sieve

CFG = LLMConfig()
C = ContentType


def item(body: str, source: ContentType = C.message, **kw: object) -> TextItem:
    base: dict[str, object] = {
        "child": "ania",
        "source": source,
        "key": "1",
        "sent": TODAY,
        "author": "A",
        "title": "",
        "body": body,
    }
    base.update(kw)
    return TextItem(**base)  # type: ignore[arg-type]


# --- normalizacja ------------------------------------------------------------------


def test_html_entities_and_whitespace() -> None:
    raw = "<p>Szanowni Państwo,</p><p><b>w piątek</b>   zbiórka o&nbsp;7:45.</p>"
    assert normalize(raw, 1000) == "Szanowni Państwo,\nw piątek zbiórka o 7:45."


def test_quoted_reply_signature_and_quote_lines_are_cut() -> None:
    raw = (
        "Przypominam o jutrzejszej kartkówce.\n> stary cytat\n\n"
        "----- Wiadomość oryginalna -----\nKartkówka 30.10."
    )
    assert normalize(raw, 1000) == "Przypominam o jutrzejszej kartkówce."
    assert (
        normalize("Jutro koniec 12:35.\n--\nAnna, tel. 600 000 000", 1000) == "Jutro koniec 12:35."
    )
    assert normalize("Ok.\nW dniu 4.11.2026 Jan Testowy napisał:\nstare", 1000) == "Ok."


def test_limit_and_model_text_label() -> None:
    assert normalize("a" * 50, 10) == "a" * 10
    text = model_text(item("  W piątek   apel. ", C.notice, title=" Strój  galowy "), 100)
    assert text == "[ogłoszenie] Strój galowy\nW piątek apel."


def test_content_fp_ignores_child_key_and_formatting() -> None:
    a = item("W piątek apel.", C.notice, title="Apel")
    b = item("<p>W  piątek apel.</p>", C.notice, title="Apel", child="kuba", key="LID-2")
    assert content_fp(a, 100) == content_fp(b, 100)
    assert content_fp(a, 100) != content_fp(item("W piątek apel.", C.message, title="Apel"), 100)


# --- sito ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"body": "   "}, "empty"),
        ({"body": "ok."}, "empty"),
        ({"body": "Wycieczka 2.10.", "source": C.notice, "valid_to": date(2026, 10, 2)}, "expired"),
        ({"body": "Jutro zbiórka.", "sent": date(2026, 10, 1)}, "old"),
        ({"body": "Jutro zbiórka.", "sent": TODAY}, "ok"),
        # stare, ale wciąż aktualne (termin w przyszłości) przechodzi
        (
            {
                "body": "Apel 20.11.",
                "source": C.notice,
                "sent": date(2026, 9, 1),
                "valid_to": date(2026, 11, 20),
            },
            "ok",
        ),
        ({"body": "Bardzo staranna praca!", "source": C.grade_comment}, "no_signal"),
        ({"body": "Poprawa w czw.", "source": C.grade_comment}, "signal"),
        ({"body": "Brak stroju.", "source": C.note}, "signal"),  # słowo-klucz „stroj”
        ({"body": "Ułamki zwykłe.", "source": C.lesson_topic}, "no_signal"),
        ({"body": "Ułamki, sprawdzian 12.11.", "source": C.lesson_topic}, "signal"),
    ],
)
def test_sieve_rules(kw: dict, reason: str) -> None:  # type: ignore[type-arg]
    decision = sieve(item(**kw), TODAY, CFG)
    assert decision.reason == reason
    assert decision.passed is (reason in ("ok", "signal"))


@pytest.mark.parametrize(
    "text",
    [
        "12.11",
        "7:30",
        "12 listopada",
        "12 XI",
        "w pt.",
        "w czw",
        "poniedziałek",
        "jutro",
        "za tydzień",
        "dzisiaj",
    ],
)
def test_signal_patterns(text: str) -> None:
    assert has_signal(f"Coś {text} coś", [])


def test_no_signal_in_plain_praise_and_custom_keywords() -> None:
    assert not has_signal("Brawo, piękna praca", [])
    assert has_signal("Brawo, piękna praca", ["brawo"])


@pytest.mark.parametrize("c", CASES, ids=lambda c: c["id"])
def test_sieve_matches_evaluation_set(c: dict) -> None:  # type: ignore[type-arg]
    t = item(c["body"], C(c["source"]), title=c["title"], sent=c["sent"])
    assert sieve(t, TODAY, CFG).passed is (c["sieve"] == "pass"), c["why"]


def test_evaluation_quotes_survive_normalization() -> None:
    for c in CASES:
        body = " ".join(normalize(c["body"], CFG.max_input_chars).split())
        for e in c["expect"]:
            assert " ".join(e["quote"].split()) in body, (c["id"], e["quote"])
