import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from librus_watch import admin

WALL = 1_780_000_000.0


def now_doc(**kw: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": "2026-11-06T12:00:00",
        "generated_epoch": int(WALL - 600),
        "last_cycle": "2026-11-06T11:55:00",
        "fetch_interval_minutes": 60,
        "children": [
            {
                "id": "ania", "last_success": "2026-11-06T11:55:00", "failures_today": 0,
                "logins_today": 2, "next_attempt": None, "blocked_until": None,
                "last_error": None,
            }
        ],
        "modules": {"ania": {"Notes": "2026-11-06T11:55:00"}},
        "llm_today": {"requests": 1, "tokens": 10},
        "audit_open": 0,
    }  # fmt: skip
    doc.update(kw)
    return doc


def test_health_ok_idle_and_down() -> None:
    assert admin.health(now_doc(), WALL)["service"] == "ok"
    assert admin.health(now_doc(), WALL)["reason"] == "działa"
    idle = admin.health(now_doc(generated_epoch=int(WALL - 5 * 3600)), WALL)
    assert idle["service"] == "ok" and "bezczynna" in idle["reason"]
    down = admin.health(now_doc(generated_epoch=int(WALL - 20 * 3600)), WALL)
    assert down["service"] == "down" and "20 h" in down["reason"]
    assert admin.health(None)["service"] == "down"
    assert admin.health(now_doc(), WALL)["cycle_age_minutes"] == 5


def test_health_uses_wall_clock_by_default() -> None:
    assert admin.health(now_doc(generated_epoch=0))["service"] == "down"


@pytest.mark.parametrize(
    ("child", "state", "text"),
    [
        ({"blocked_until": "2026-11-07T06:00:00"}, "down", "ochrona konta"),
        ({"failures_today": 2}, "warn", "błędy dziś: 2"),
        ({"last_success": "2026-11-04T10:00:00"}, "warn", "od doby"),
        ({"last_success": None}, "warn", "jeszcze bez"),
        ({"blocked_until": "2026-11-05T06:00:00"}, "ok", "ok"),
    ],
)
def test_child_health(child: dict[str, Any], state: str, text: str) -> None:
    doc = now_doc()
    doc["children"][0].update(child)
    got = admin.health(doc, WALL)["children"]["ania"]
    assert got["state"] == state and text in got["reason"]


def test_health_ignores_garbage_timestamps() -> None:
    h = admin.health(now_doc(last_cycle="śmieci", generated_at="x"), WALL)
    assert h["cycle_age_minutes"] is None


def test_load_now_rejects_missing_broken_and_other_schema(tmp_path: Path) -> None:
    assert admin.load_now(tmp_path) is None
    (tmp_path / "now.json").write_text("{nie json")
    assert admin.load_now(tmp_path) is None
    (tmp_path / "now.json").write_text(json.dumps({"schema_version": 2}))
    assert admin.load_now(tmp_path) is None
    (tmp_path / "now.json").write_text("[1]")
    assert admin.load_now(tmp_path) is None
    (tmp_path / "now.json").write_text(json.dumps(now_doc()))
    assert admin.load_now(tmp_path) is not None


def test_load_daily_tail_and_garbage(tmp_path: Path) -> None:
    assert admin.load_daily(tmp_path, 5) == []
    lines = [json.dumps({"day": f"2026-11-0{i}"}) for i in range(1, 6)]
    (tmp_path / "daily.jsonl").write_text("\n".join([*lines, "zepsute", "[1]"]) + "\n")
    assert [r["day"] for r in admin.load_daily(tmp_path, 2)] == ["2026-11-04", "2026-11-05"]
    assert len(admin.load_daily(tmp_path, 0)) == 1
    assert len(admin.load_daily(tmp_path, 10_000)) == 5


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), admin.handler(tmp_path))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path
    srv.shutdown()
    srv.server_close()


def get(url: str) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_pages_and_security_headers(server: tuple[str, Path]) -> None:
    base, _ = server
    for path, ctype in (
        ("/", "text/html"),
        ("/app.js", "text/javascript"),
        ("/app.css", "text/css"),
    ):
        code, headers, body = get(base + path)
        assert code == 200 and headers["Content-Type"].startswith(ctype) and body
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Frame-Options"] == "DENY"
        assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert get(base + "/healthz")[2] == b"ok"
    assert get(base + "/nie-ma")[0] == 404


def test_page_has_no_external_resources_or_innerhtml() -> None:
    from librus_watch.admin_page import APP_CSS, APP_JS, INDEX_HTML

    assert "innerHTML" not in APP_JS
    for blob in (INDEX_HTML, APP_JS, APP_CSS):
        assert "http://" not in blob.replace("http://www.w3.org/2000/svg", "")
        assert "https://" not in blob


def test_api_without_snapshot_reports_down(server: tuple[str, Path]) -> None:
    base, _ = server
    code, _, body = get(base + "/api/now")
    data = json.loads(body)
    assert code == 200 and data["now"] is None and data["health"]["service"] == "down"
    assert json.loads(get(base + "/api/daily")[2]) == {"daily": []}


def test_api_serves_snapshot_and_limits_days(server: tuple[str, Path]) -> None:
    base, d = server
    (d / "now.json").write_text(json.dumps(now_doc(generated_epoch=2_000_000_000)))
    rows = "".join(json.dumps({"day": f"2026-10-{i:02d}"}) + "\n" for i in range(1, 11))
    (d / "daily.jsonl").write_text(rows)
    assert json.loads(get(base + "/api/now")[2])["now"]["children"][0]["id"] == "ania"
    assert len(json.loads(get(base + "/api/daily?days=3")[2])["daily"]) == 3
    assert len(json.loads(get(base + "/api/daily?days=abc")[2])["daily"]) == 10


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_other_methods_are_denied_outside_rate(server: tuple[str, Path], method: str) -> None:
    base, _ = server
    req = urllib.request.Request(base + "/", method=method, data=b"x")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 405


def test_server_logs_no_requests(
    server: tuple[str, Path], caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    base, _ = server
    caplog.set_level("DEBUG")
    get(base + "/api/daily?days=3&token=SEKRET")
    assert "SEKRET" not in caplog.text
    assert "SEKRET" not in capsys.readouterr().err


# --- strumień rekordów i oceny (ADR-026) ------------------------------------------------------


@pytest.fixture
def rec_server(tmp_path: Path) -> Iterator[tuple[str, Path, Path]]:
    inbox = tmp_path / "inbox"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), admin.handler(tmp_path / "stats", inbox))
    (tmp_path / "stats").mkdir()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path / "stats", inbox
    srv.shutdown()
    srv.server_close()


def post(url: str, body: object, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    h = {"Content-Type": "application/json", "X-Panel": "1"} if headers is None else headers
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_records_disabled_then_served(rec_server: tuple[str, Path, Path]) -> None:
    base, stats, _ = rec_server
    off = json.loads(get(base + "/api/records")[2])
    assert off["enabled"] is False and off["records"] == [] and off["can_rate"] is True
    rows = [{"ref": f"a|b|{i}", "title": f"t{i}"} for i in range(5)]
    (stats / "records.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows) + "zepsute\n[1]\n"
    )
    on = json.loads(get(base + "/api/records?limit=2")[2])
    assert on["enabled"] and [r["ref"] for r in on["records"]] == ["a|b|0", "a|b|1"]
    assert "day" in on["reasons"]
    assert len(json.loads(get(base + "/api/records?limit=abc")[2])["records"]) == 5


def test_rate_queues_file_and_shows_pending(rec_server: tuple[str, Path, Path]) -> None:
    base, _, inbox = rec_server
    code, _ = post(base + "/api/rate", {"action": "wrong", "identity": "abc", "reason": "day"})
    assert code == 200
    files = list(inbox.glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text()) == {
        "action": "wrong", "identity": "abc", "reason": "day",
    }  # fmt: skip
    assert not list(inbox.glob("*.tmp"))
    assert post(base + "/api/rate", {"action": "missed", "ref": "a|b|c"})[0] == 200
    pend = json.loads(get(base + "/api/records")[2])["pending"]
    assert {p["action"] for p in pend} == {"wrong", "missed"}


@pytest.mark.parametrize(
    "body",
    [
        {"action": "delete", "identity": "x"},
        {"action": "confirmed"},
        {"action": "confirmed", "identity": ""},
        {"action": "confirmed", "identity": 5},
        {"action": "wrong", "identity": "x", "reason": "zmyslone"},
        {"action": "missed", "identity": "x"},
        [1],
    ],
)
def test_rate_rejects_invalid(rec_server: tuple[str, Path, Path], body: object) -> None:
    base, _, inbox = rec_server
    assert post(base + "/api/rate", body)[0] == 422
    assert not inbox.exists() or not list(inbox.glob("*"))


def test_rate_csrf_and_size_guards(rec_server: tuple[str, Path, Path]) -> None:
    base, _, inbox = rec_server
    ok = {"action": "missed", "ref": "a|b|c"}
    assert post(base + "/api/rate", ok, {"Content-Type": "application/json"})[0] == 403
    assert post(base + "/api/rate", ok, {"Content-Type": "text/plain", "X-Panel": "1"})[0] == 403
    evil = {"Content-Type": "application/json", "X-Panel": "1", "Origin": "https://evil.example"}
    assert post(base + "/api/rate", ok, evil)[0] == 403
    same = {**evil, "Origin": base}
    assert post(base + "/api/rate", ok, same)[0] == 200
    assert post(base + "/api/rate", b"")[0] == 400
    assert post(base + "/api/rate", b"x" * 5000)[0] == 400
    assert post(base + "/api/rate", b"{zepsute")[0] == 422
    assert len(list(inbox.glob("*.json"))) == 1


def test_rate_full_inbox_and_disabled(
    rec_server: tuple[str, Path, Path], server: tuple[str, Path]
) -> None:
    base, _, inbox = rec_server
    inbox.mkdir()
    for i in range(admin.MAX_PENDING):
        (inbox / f"{i}.json").write_text("{}")
    assert post(base + "/api/rate", {"action": "missed", "ref": "a|b|c"})[0] == 429
    assert len(admin.pending(inbox)) == admin.MAX_PENDING
    plain, _ = server  # panel bez skrzynki
    assert post(plain + "/api/rate", {"action": "missed", "ref": "a|b|c"})[0] == 503


def test_pending_ignores_broken_files(tmp_path: Path) -> None:
    assert admin.pending(None) == [] and admin.pending(tmp_path / "nie-ma") == []
    (tmp_path / "a.json").write_text("zepsute")
    (tmp_path / "b.json").write_text("[1]")
    (tmp_path / "c.json").write_text(json.dumps({"action": "missed", "ref": "r", "x": 1}))
    assert admin.pending(tmp_path) == [{"action": "missed", "identity": None, "ref": "r"}]


def test_page_wires_stream_and_csrf_header() -> None:
    from librus_watch.admin_page import APP_JS

    assert "/api/rate" in APP_JS and "'X-Panel': '1'" in APP_JS and "/api/records" in APP_JS
