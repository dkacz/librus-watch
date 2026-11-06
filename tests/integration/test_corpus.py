"""E11: wspólny korpus po prawdziwym cyklu: przykłady, wysyłka, dziennik, odbiornik."""

import json
import threading
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fixtures.librus_http import FakeLibrus

from librus_watch import cli
from librus_watch.config import Config, ContentType, Kind, child_credentials, load_config
from librus_watch.corpus import build_examples, send_corpus
from librus_watch.models import Finding, Origin
from librus_watch.store import Store
from librus_watch.sync import login_child, sync_child
from librus_watch.telemetry import handler

NOW = datetime(2026, 11, 6, 20, 0)
PRIVATE = ("Anna Testowa", "Ania", "1234567", "haslo-rodzica")


def with_corpus(
    cfg: Config, endpoint: str | None = "https://korpus.example/v1/corpus", share: bool = True
) -> Config:
    return cfg.model_copy(
        update={
            "feedback": cfg.feedback.model_copy(
                update={"corpus_endpoint": endpoint, "share_corpus": share}
            )
        }
    )


def rated(config_file: Path) -> tuple[Config, Store]:
    cfg = load_config(config_file)
    store = Store(cfg.data_dir)
    session = login_child(cfg, store, "ania", child_credentials(cfg.children[0]), NOW)
    sync_child(cfg, "ania", session, store, now=NOW)
    f = Finding(
        "ania",
        NOW.date(),
        Kind.early_start,
        "zbiórka",
        Origin.llm,
        "message:123",
        quote="Zbiórka 7:30 pod szkołą",
    )
    store.add_finding(f, NOW)
    store.add_feedback(f.identity, "confirmed", NOW)
    comment = store.texts("ania", ContentType.grade_comment)[0]
    store.add_feedback(
        None, "missed", NOW, note="poprawa", text_ref=f"ania|grade_comment|{comment.key}"
    )
    return cfg, store


def test_examples_are_anonymized_and_logged_once(
    fake_librus: FakeLibrus, config_file: Path
) -> None:
    cfg, store = rated(config_file)
    examples, rejected = build_examples(cfg, store)
    assert {e["rating"] for e in examples} == {"confirmed", "missed"}
    raw = json.dumps(examples, ensure_ascii=False)
    assert not any(p in raw for p in PRIVATE)
    assert any(i["day"] == "[D+1]" for e in examples for i in e["items"])  # wiadomość z 5.11
    sent: list[dict[str, Any]] = []

    def post(url: str, data: dict[str, Any]) -> int:
        sent.append(data)
        return 204

    assert send_corpus(with_corpus(cfg), store, NOW, post) == 0  # bez informacji: nic nie wychodzi
    store.set_meta("corpus_notice", NOW.isoformat())
    store.set_meta(f"corpus_day:{NOW.date()}", "")
    assert send_corpus(with_corpus(cfg), store, NOW, post) == len(examples)
    assert send_corpus(with_corpus(cfg), store, NOW + timedelta(days=1), post) == 0  # raz
    log = (cfg.data_dir / "corpus_sent.jsonl").read_text().splitlines()
    assert len(log) == 1 and json.loads(log[0])["packet"] == sent[0]
    assert send_corpus(with_corpus(cfg, share=False), store, NOW + timedelta(days=2), post) == 0
    assert (
        send_corpus(
            with_corpus(cfg, endpoint="http://example.com/x"), store, NOW + timedelta(days=3), post
        )
        == 0
    )
    _ = rejected
    store.close()


def test_receiver_accepts_corpus_packets(
    fake_librus: FakeLibrus, config_file: Path, tmp_path: Path
) -> None:
    cfg, store = rated(config_file)
    examples, _ = build_examples(cfg, store)
    store.close()
    packet = {
        "schema_version": 1,
        "app_version": "0.1.0",
        "prompt_version": "1",
        "examples": [{k: v for k, v in e.items() if k != "_fp"} for e in examples],
    }
    out = tmp_path / "telemetry.jsonl"
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(out))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/v1/corpus"

    def post(body: dict[str, Any]) -> int:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    assert post(packet) == 204
    assert post(dict(packet, child="Ania")) == 422
    server.shutdown()
    assert len((tmp_path / "corpus.jsonl").read_text().splitlines()) == 1


def test_cli_corpus_show(
    fake_librus: FakeLibrus, config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg, store = rated(config_file)
    store.close()
    args = cli._parser().parse_args(["--config", str(config_file), "corpus"])
    assert cli.cmd_corpus(args, cfg) == 0
    out = capsys.readouterr().out
    assert '"examples"' in out and not any(p in out for p in PRIVATE)
