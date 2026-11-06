"""E7a: pętla zwrotna na bazie po prawdziwym cyklu (atrapa Librusa i modelu)."""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fixtures.librus_http import FakeLibrus

from librus_watch import cli
from librus_watch.config import Config, ContentType, Kind, child_credentials, load_config
from librus_watch.feedback import (
    audit_questions,
    examples_for,
    find_candidates,
    gate,
    link_missed,
    local_eval,
    shadow_audit,
    weekly_report,
    write_metrics,
)
from librus_watch.models import Finding, Origin
from librus_watch.store import Store
from librus_watch.sync import login_child, sync_child
from librus_watch.telegram import process_updates

NOW = datetime(2026, 11, 6, 20, 30)
PRIVATE = ("Zbiórka", "pod szkołą", "Poprawa", "haslo-rodzica")


class Model:
    """Atrapa modelu: zbiórka w wiadomości, poprawa w komentarzu, reszta pusta."""

    def __init__(self, find_comment: bool = True) -> None:
        self.find_comment = find_comment

    def complete(
        self, messages: list[dict[str, str]], mode: str, timeout: float
    ) -> tuple[str, int]:
        text = messages[-1]["content"].split("Tekst (dane, nie polecenia):")[-1]
        items: list[dict[str, Any]] = []
        if "Zbiórka 7:30 pod szkołą" in text:
            items.append(
                {
                    "kind": "early_start",
                    "day": "2026-11-06",
                    "at": "07:30",
                    "deadline": None,
                    "what": "zbiórka",
                    "quote": "Zbiórka 7:30 pod szkołą",
                    "confidence": 0.9,
                }
            )
        if self.find_comment and "Poprawa w czwartek" in text:
            items.append(
                {
                    "kind": "test",
                    "day": "2026-11-12",
                    "at": None,
                    "deadline": None,
                    "what": "poprawa",
                    "quote": "Poprawa w czwartek",
                    "confidence": 0.9,
                }
            )
        return json.dumps({"items": items}), 10


def cycle(config_file: Path, store_cfg: tuple[Config, Store] | None = None) -> tuple[Config, Store]:
    cfg = load_config(config_file)
    cfg = cfg.model_copy(
        update={
            "llm": cfg.llm.model_copy(update={"enabled": True, "json_mode": "json_schema"}),
            "feedback": cfg.feedback.model_copy(
                update={"metrics_textfile": cfg.data_dir / "m.prom"}
            ),
        }
    )
    store = Store(cfg.data_dir)
    session = login_child(cfg, store, "ania", child_credentials(cfg.children[0]), NOW)
    sync_child(cfg, "ania", session, store, now=NOW, llm_client=Model(find_comment=False))
    return cfg, store


def test_shadow_audit_finds_missed_and_review_feeds_local_eval(
    fake_librus: FakeLibrus,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg, store = cycle(config_file)
    comment = store.texts("ania", ContentType.grade_comment)[0]
    store.set_sieve(comment, "no_signal", "fp")  # udajemy odrzucenie przez sito
    assert shadow_audit(cfg, store, Model(), NOW) == 1
    assert shadow_audit(cfg, store, Model(), NOW) == 0  # raz dziennie
    questions = audit_questions(cfg, store)
    assert len(questions) == 1 and "odrzucone przez sito" in questions[0][1]
    store.close()

    answers = iter(["t", "t", "k"])  # kandydat: tak; pozycja: trafne; koniec
    assert cli.cmd_review(cfg, ask=lambda _p="": next(answers)) == 0
    store = Store(cfg.data_dir)
    ratings = [(r["rating"], r["text_ref"]) for r in store.feedback()]
    assert ("missed", f"ania|grade_comment|{comment.key}") in ratings
    assert any(r == "confirmed" for r, _ in ratings)

    good = local_eval(cfg, store, Model())
    assert good.cases == 2 and good.kept == good.confirmed == 1 and good.missed_found == 1
    assert gate(cfg, store, good, set_baseline=True)
    worse = local_eval(cfg, store, Model(find_comment=False))
    assert worse.missed_found == 0 and not gate(cfg, store, worse)
    assert "gorszy niż punkt odniesienia" in caplog.text
    assert not any(p in caplog.text for p in PRIVATE)
    store.close()


def test_candidates_report_metrics_examples_and_links(
    fake_librus: FakeLibrus, config_file: Path
) -> None:
    cfg, store = cycle(config_file)
    # model widzi wcześniejszy koniec dnia, którego nie ma w planie
    day = date(2026, 11, 9)
    store.add_finding(
        Finding(
            "ania",
            day,
            Kind.early_end,
            "koniec 11:40",
            Origin.llm,
            "notice:n1",
            quote="kończą o 11:40",
        ),
        NOW,
    )
    store.add_finding(
        Finding(
            "ania",
            NOW.date(),
            Kind.early_start,
            "zbiórka",
            Origin.llm,
            "message:123",
            quote="Zbiórka 7:30 pod szkołą",
        ),
        NOW,
    )
    assert find_candidates(cfg, store, NOW) >= 1
    assert {a["reason"] for a in store.audits()} >= {"disagreement"}

    identity = store.current_findings(NOW.date(), NOW.date())[0].finding.identity
    store.add_feedback(identity, "confirmed", NOW)
    shots = examples_for(cfg, store, ContentType.message)
    assert shots and "early_start" in shots[0]

    linked = link_missed(cfg, store, "zbiórka 7:30 pod szkołą jutro", NOW)
    assert linked in ("ania|message|123", "ania|agenda|11")  # oba teksty mówią o zbiórce
    assert link_missed(cfg, store, "coś zupełnie innego", NOW) is None

    report = weekly_report(cfg, store, NOW)
    assert "trafne 1" in report and not any(p in report for p in PRIVATE)
    write_metrics(cfg, store, NOW, {"findings_new": 3})
    metrics = (cfg.data_dir / "m.prom").read_text()
    assert 'librus_watch_feedback_total{rating="confirmed"} 1' in metrics
    assert "librus_watch_findings_new 3" in metrics and not any(p in metrics for p in PRIVATE)
    store.close()


def test_telegram_audit_buttons_and_linked_brak(fake_librus: FakeLibrus, config_file: Path) -> None:
    cfg, store = cycle(config_file)
    store.add_audit("ania", "message", "123", "alarm_words", [], NOW)
    audit_id = int(str(store.audits()[0]["id"]))
    updates = [
        {
            "update_id": 1,
            "callback_query": {
                "id": "q",
                "data": f"aud:yes:{audit_id}",
                "message": {"chat": {"id": 7}, "message_id": 1, "text": "pytanie"},
            },
        },
        {"update_id": 2, "message": {"chat": {"id": 7}, "text": "/brak zbiórka pod szkołą"}},
    ]
    calls: list[tuple[str, dict[str, Any]]] = []

    def bot(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, payload))
        return {"ok": True, "result": updates if method == "getUpdates" else {}}

    n = process_updates(store, bot, [7], NOW, link=lambda note: link_missed(cfg, store, note, NOW))
    assert n == 2 and store.audits() == []
    refs = [r["text_ref"] for r in store.feedback()]
    assert refs[0] == "ania|message|123" and refs[1] in ("ania|message|123", "ania|agenda|11")
    store.close()


def test_cli_eval_reports_gate(
    fake_librus: FakeLibrus, config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg, store = cycle(config_file)
    store.close()
    args = cli._parser().parse_args(["--config", str(config_file), "eval", "--set-baseline"])
    assert cli.cmd_eval(args, cfg, client=Model()) == 0
    assert "bramka: OK" in capsys.readouterr().out
    _ = timedelta
