"""E12: paczka telemetrii po prawdziwym cyklu, wysyłka tygodniowa, odbiornik."""

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
from librus_watch.config import Config, child_credentials, load_config
from librus_watch.store import Store
from librus_watch.sync import login_child, sync_child
from librus_watch.telemetry import (
    Packet,
    build_packet,
    endpoint_allowed,
    handler,
    note_module_errors,
    send_weekly,
)

NOW = datetime(2026, 11, 9, 19, 0)
PRIVATE = ("ania", "Ania", "1234567", "haslo", "Zbiórka", "Matematyka", "local-model")


def with_telemetry(
    cfg: Config,
    endpoint: str | None = "https://telemetria.example/v1/telemetry",
    enabled: bool = True,
) -> Config:
    return cfg.model_copy(
        update={
            "telemetry": cfg.telemetry.model_copy(update={"endpoint": endpoint, "enabled": enabled})
        }
    )


def cycle(config_file: Path) -> tuple[Config, Store]:
    cfg = load_config(config_file)
    store = Store(cfg.data_dir)
    session = login_child(cfg, store, "ania", child_credentials(cfg.children[0]), NOW)
    sync_child(cfg, "ania", session, store, now=NOW)
    note_module_errors(store, NOW, {"Notes": "TransientError", "nieznany": "x"})
    return cfg, store


def test_packet_has_only_blurred_numbers(fake_librus: FakeLibrus, config_file: Path) -> None:
    cfg, store = cycle(config_file)
    packet = build_packet(cfg, store, NOW)
    Packet.model_validate(packet)
    raw = json.dumps(packet, ensure_ascii=False)
    assert not any(p in raw for p in PRIVATE)
    numbers = [
        packet["llm_requests"],
        *packet["findings"].values(),
        *packet["ratings"].values(),
        *packet["sieve"].values(),
        *packet["module_errors"].values(),
    ]
    assert all(n % 5 == 0 and n >= 0 for n in numbers)
    assert set(packet["module_errors"]) == {"Notes"}  # tylko znane moduły
    assert packet["install"] != build_packet(cfg, store, NOW)["install"]  # bez powiązania paczek
    assert len(raw.encode()) < 4096
    store.close()


def test_weekly_send_once_disabled_and_https_only(
    fake_librus: FakeLibrus, config_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg, store = cycle(config_file)
    sent: list[dict[str, Any]] = []

    def post(url: str, data: dict[str, Any]) -> int:
        sent.append(data)
        return 204

    assert send_weekly(with_telemetry(cfg), store, NOW, post)
    assert not send_weekly(with_telemetry(cfg), store, NOW + timedelta(days=1), post)
    assert send_weekly(with_telemetry(cfg), store, NOW + timedelta(days=7), post)
    assert len(sent) == 2
    assert not send_weekly(
        with_telemetry(cfg, enabled=False), store, NOW + timedelta(days=14), post
    )
    assert not send_weekly(
        with_telemetry(cfg, endpoint=None), store, NOW + timedelta(days=14), post
    )
    assert not send_weekly(
        with_telemetry(cfg, endpoint="http://example.com/x"), store, NOW + timedelta(days=14), post
    )
    assert "wymagany HTTPS" in caplog.text
    assert not send_weekly(
        with_telemetry(cfg),
        store,
        NOW + timedelta(days=14),
        lambda u, d: (_ for _ in ()).throw(OSError()),
    )
    assert endpoint_allowed("http://librus-telemetry:8090/v1/telemetry")
    assert endpoint_allowed("http://localhost:8090/v1/telemetry")
    assert not endpoint_allowed("ftp://x")
    store.close()


def test_receiver_accepts_only_valid_packets(
    fake_librus: FakeLibrus, config_file: Path, tmp_path: Path
) -> None:
    cfg, store = cycle(config_file)
    packet = build_packet(cfg, store, NOW)
    store.close()
    out = tmp_path / "telemetry.jsonl"
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(out))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path: str, body: bytes) -> int:
        req = urllib.request.Request(
            url + path, data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    assert post("/v1/telemetry", json.dumps(packet).encode()) == 204
    bad = dict(packet, child_name="Ania")
    assert post("/v1/telemetry", json.dumps(bad).encode()) == 422
    assert post("/v1/telemetry", b"x" * 5000) == 400
    assert post("/inne", json.dumps(packet).encode()) == 400
    server.shutdown()
    lines = out.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["week"] == "2026-W46"


def test_cli_show_and_send(
    fake_librus: FakeLibrus, config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg, store = cycle(config_file)
    store.close()
    args = cli._parser().parse_args(["--config", str(config_file), "telemetry"])
    assert cli.cmd_telemetry(args, cfg) == 0
    assert '"schema_version": 1' in capsys.readouterr().out
    args = cli._parser().parse_args(["--config", str(config_file), "telemetry", "--send"])
    assert cli.cmd_telemetry(args, with_telemetry(cfg), post=lambda u, d: 204) == 0
    assert "wysłane" in capsys.readouterr().out


def test_receiver_limits(fake_librus: FakeLibrus, config_file: Path, tmp_path: Path) -> None:
    from librus_watch.telemetry import Guard, Limits

    cfg, store = cycle(config_file)
    packets = [build_packet(cfg, store, NOW) for _ in range(5)]  # różne identyfikatory instalacji
    store.close()
    clock = {"now": NOW}
    guard = Guard(Limits(per_ip_hour=2, daily=3, max_bytes=10_000), clock=lambda: clock["now"])
    out = tmp_path / "telemetry.jsonl"
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(out, guard=guard))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/v1/telemetry"

    def post(body: dict[str, Any], ip: str) -> int:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "CF-Connecting-IP": ip},
        )
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    assert post(packets[0], "1.1.1.1") == 204
    assert post(packets[0], "2.2.2.2") == 204  # powtórka: przyjęta, niezapisana
    assert post(packets[1], "1.1.1.1") == 204
    assert post(packets[2], "1.1.1.1") == 429  # limit na IP w godzinie
    assert post(packets[3], "3.3.3.3") == 429  # limit dzienny (3)
    clock["now"] = NOW + timedelta(days=1)
    assert post(packets[3], "3.3.3.3") == 204
    assert len(out.read_text().splitlines()) == 3
    out.write_text("x" * 10_000)
    assert post(packets[4], "4.4.4.4") == 507  # pełny dysk
    server.shutdown()
