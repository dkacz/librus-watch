import hashlib
import hmac
import json
import threading
import urllib.request
from collections.abc import Iterator
from datetime import date, datetime, time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from librus_watch.alerts import Alert, AlertLine
from librus_watch.config import AlertType, Config, Kind, Priority
from librus_watch.models import Finding, Origin
from librus_watch.store import Store
from librus_watch.whatsapp import (
    handle_event,
    send_alert,
    send_info,
    verify_signature,
    webhook_handler,
)

NOW = datetime(2026, 11, 5, 19, 0)
MAMA, OBCY = "48600111222", "48999888777"
SECRET, VERIFY, TOKEN = "sekret-aplikacji", "token-weryfikacji", "EAAtokenwhatsapp0123456789"
CFG = Config.model_validate({"children": [{"id": "iwo", "name": "Iwo"}]})
F = Finding(
    "iwo",
    date(2026, 11, 6),
    Kind.early_start,
    "zbiórka",
    Origin.llm,
    "message:1",
    at=time(7, 30),
    quote="jutro zbiórka\no 7:30",
)


class Graph:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(payload)
        return {"messages": [{"id": "wamid.x"}]}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path)
    s.add_finding(F, NOW)
    yield s
    s.close()


def msg(sender: str, **kw: Any) -> dict[str, Any]:
    return {"entry": [{"changes": [{"value": {"messages": [{"from": sender, **kw}]}}]}]}


def test_template_per_item_with_payload_buttons() -> None:
    graph = Graph()
    alert = Alert(AlertType.evening, "Jutro", [AlertLine(F, Priority.high)])
    assert send_alert(alert, CFG, graph, [MAMA], date(2026, 11, 5)) == 1
    t = graph.sent[0]["template"]
    assert t["name"] == "librus_pozycja" and t["language"] == {"code": "pl"}
    params = [p["text"] for p in t["components"][0]["parameters"]]
    assert params[0] == "Iwo, jutro (piątek 6.11)" and "\n" not in params[2]
    payloads = [c["parameters"][0]["payload"] for c in t["components"][1:]]
    assert payloads == [f"ok:{F.identity[:16]}", f"bad:{F.identity[:16]}"]
    assert send_info("Raport\nawarii", CFG, graph, [MAMA]) == 1
    assert graph.sent[-1]["template"]["components"][0]["parameters"][0]["text"] == "Raport awarii"


def test_events_ratings_reasons_brak_and_foreign(store: Store) -> None:
    graph = Graph()
    key = F.identity[:16]
    events = [
        msg(MAMA, type="button", button={"payload": f"ok:{key}"}),
        msg(MAMA, type="button", button={"payload": f"bad:{key}"}),
        msg(MAMA, type="interactive", interactive={"list_reply": {"id": f"why:day:{key}"}}),
        msg(MAMA, type="text", text={"body": "Brak: jutro strój galowy"}),
        msg(MAMA, type="text", text={"body": "dzień dobry"}),
        msg(OBCY, type="button", button={"payload": f"ok:{key}"}),
    ]
    assert sum(handle_event(e, store, graph, [MAMA], NOW) for e in events) == 3
    assert [(r["rating"], r["reason"], r["note"]) for r in store.feedback()] == [
        ("confirmed", "", ""),
        ("wrong", "day", ""),
        ("missed", "", "jutro strój galowy"),
    ]
    lists = [p for p in graph.sent if p["type"] == "interactive"]
    assert len(lists) == 1 and len(lists[0]["interactive"]["action"]["sections"][0]["rows"]) == 4
    assert all(p["to"] == MAMA for p in graph.sent)


def test_webhook_verification_and_signature(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for name, value in {
        "WHATSAPP_APP_SECRET": SECRET,
        "WHATSAPP_VERIFY_TOKEN": VERIFY,
        "WHATSAPP_TO": MAMA,
        "WHATSAPP_TOKEN": TOKEN,
    }.items():
        monkeypatch.setenv(name, value)
    graph = Graph()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), webhook_handler(CFG, store, graph, threading.Lock(), lambda: NOW)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1/whatsapp"

    def get(q: str) -> tuple[int, bytes]:
        try:
            r = urllib.request.urlopen(base + q, timeout=5)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def post(body: bytes, sig: str) -> int:
        req = urllib.request.Request(
            base, data=body, method="POST", headers={"X-Hub-Signature-256": sig}
        )
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    assert get(f"?hub.mode=subscribe&hub.verify_token={VERIFY}&hub.challenge=123") == (200, b"123")
    assert get("?hub.mode=subscribe&hub.verify_token=zly&hub.challenge=123")[0] == 403
    body = json.dumps(
        msg(MAMA, type="button", button={"payload": f"ok:{F.identity[:16]}"})
    ).encode()
    good = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert post(body, "sha256=zly") == 403 and store.feedback() == []
    assert post(body, good) == 200
    server.shutdown()
    assert [r["rating"] for r in store.feedback()] == ["confirmed"]
    assert verify_signature(body, good, SECRET) and not verify_signature(body, good, "inny")
    assert not any(s in caplog.text for s in (TOKEN, SECRET, MAMA, VERIFY))
