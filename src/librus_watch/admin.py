"""Panel admina „Moja instancja”.

Panel czyta migawkę z `stats.py` (`now.json`, `daily.jsonl` i, gdy właściciel je włączył,
`records.jsonl` ze strumieniem rekordów, ADR-026), nigdy bazy ani sekretów. Jedyny zapis to
`POST /api/rate`: plik z oceną w skrzynce, który usługa waliduje i zapisuje (`records.py`).
Serwer nie zapisuje logów z adresami i parametrami zapytań i nie wysyła nic na zewnątrz.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from librus_watch.admin_page import APP_CSS, APP_JS, INDEX_HTML
from librus_watch.stats import DAILY_FILE, NOW_FILE, STATS_SCHEMA
from librus_watch.telegram import REASONS

log = logging.getLogger(__name__)

IDLE_HOURS = 16
"""Powyżej tylu godzin bez cyklu usługa jest uznana za nieczynną (noc poza godzinami to ok. 9 h)."""
MAX_DAYS = 400
RECORDS_FILE = "records.jsonl"
MAX_BODY = 2048
MAX_PENDING = 100
ACTIONS = ("confirmed", "wrong", "missed")
CSRF_HEADER = "X-Panel"

HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


def load_now(stats_dir: Path) -> dict[str, Any] | None:
    """Wczytuje `now.json`; `None`, gdy brak pliku, jest uszkodzony albo ma inny schemat."""
    try:
        data = json.loads((stats_dir / NOW_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != STATS_SCHEMA:
        return None
    return data


def load_daily(stats_dir: Path, days: int) -> list[dict[str, Any]]:
    """Ostatnie `days` linii `daily.jsonl` (uszkodzone linie są pomijane)."""
    try:
        text = (stats_dir / DAILY_FILE).read_text()
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("day"), str):
            rows.append(row)
    return rows[-max(1, min(days, MAX_DAYS)) :]


def _parse(v: object) -> datetime | None:
    """Tekst ISO jako data albo `None`."""
    try:
        return datetime.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


def health(now: dict[str, Any] | None, wall_epoch: float | None = None) -> dict[str, Any]:
    """Ocena zdrowia usługi i dzieci (`ok`, `warn`, `down`) z migawki.

    Wiek pliku liczymy zegarem ściany (`generated_epoch`), a wiek ostatniego cyklu i stan
    kont porównaniem znaczników z samej migawki (ta sama strefa, bez zgadywania).
    """
    if now is None:
        return {"service": "down", "reason": "brak migawki statystyk", "children": {}}
    wall = time.time() if wall_epoch is None else wall_epoch
    interval = int(now.get("fetch_interval_minutes") or 60)
    file_age_h = (wall - float(now.get("generated_epoch") or 0)) / 3600
    generated = _parse(now.get("generated_at"))
    last = _parse(now.get("last_cycle"))
    if file_age_h > IDLE_HOURS:
        service, reason = "down", f"brak cyklu od {int(file_age_h)} h"
    elif file_age_h * 60 > 2 * interval:
        service, reason = "ok", "bezczynna (poza godzinami pobierania?)"
    else:
        service, reason = "ok", "działa"
    children: dict[str, dict[str, str]] = {}
    for c in now.get("children", []):
        state, why = "ok", "ok"
        blocked = _parse(c.get("blocked_until"))
        success = _parse(c.get("last_success"))
        if generated and blocked and blocked > generated:
            state, why = "down", "wstrzymane (ochrona konta)"
        elif c.get("failures_today"):
            state, why = "warn", f"błędy dziś: {c['failures_today']}"
        elif generated and success and (generated - success).total_seconds() > 24 * 3600:
            state, why = "warn", "brak udanego pobrania od doby"
        elif success is None:
            state, why = "warn", "jeszcze bez udanego pobrania"
        children[str(c.get("id"))] = {"state": state, "reason": why}
    return {
        "service": service,
        "reason": reason,
        "cycle_age_minutes": int((generated - last).total_seconds() // 60)
        if generated and last
        else None,
        "children": children,
    }


def load_records(stats_dir: Path, limit: int) -> list[dict[str, Any]] | None:
    """Rekordy ze strumienia (od najnowszej analizy); `None`, gdy strumień jest wyłączony."""
    try:
        text = (stats_dir / RECORDS_FILE).read_text()
    except OSError:
        return None
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("ref"), str):
            rows.append(row)
    return rows[: max(1, min(limit, 300))]


def pending(inbox: Path | None) -> list[dict[str, Any]]:
    """Oceny czekające w skrzynce na przyjęcie przez usługę (tylko pola potrzebne w UI)."""
    if inbox is None or not inbox.is_dir():
        return []
    out = []
    for p in sorted(inbox.glob("*.json"))[:MAX_PENDING]:
        try:
            req = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(req, dict):
            out.append({k: req.get(k) for k in ("action", "identity", "ref")})
    return out


def validate_rating(req: object) -> dict[str, str] | None:
    """Sprawdza żądanie oceny z UI i zwraca czystą wersję albo `None`."""
    if not isinstance(req, dict) or req.get("action") not in ACTIONS:
        return None
    action = str(req["action"])
    key = "ref" if action == "missed" else "identity"
    val = req.get(key)
    if not isinstance(val, str) or not 0 < len(val) <= 300:
        return None
    clean = {"action": action, key: val}
    reason = req.get("reason") or ""
    if action == "wrong" and reason:
        if reason not in REASONS:
            return None
        clean["reason"] = str(reason)
    return clean


def queue_rating(inbox: Path, clean: dict[str, str]) -> bool:
    """Zapisuje ocenę w skrzynce (atomowo); `False`, gdy skrzynka jest pełna."""
    inbox.mkdir(parents=True, exist_ok=True)
    if len(list(inbox.glob("*.json"))) >= MAX_PENDING:
        return False
    name = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    tmp = inbox / f"{name}.tmp"
    tmp.write_text(json.dumps(clean))
    os.chmod(tmp, 0o600)
    tmp.replace(inbox / f"{name}.json")
    return True


def handler(stats_dir: Path, inbox: Path | None = None) -> type[BaseHTTPRequestHandler]:
    """Klasa obsługi żądań panelu (GET; jedyny zapis to `POST /api/rate` do skrzynki)."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: object) -> None:
            self._send(200, json.dumps(obj).encode(), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path == "/":
                self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
            elif url.path == "/app.js":
                self._send(200, APP_JS.encode(), "text/javascript; charset=utf-8")
            elif url.path == "/app.css":
                self._send(200, APP_CSS.encode(), "text/css; charset=utf-8")
            elif url.path == "/healthz":
                self._send(200, b"ok", "text/plain")
            elif url.path == "/api/now":
                now = load_now(stats_dir)
                self._json({"now": now, "health": health(now)})
            elif url.path == "/api/daily":
                raw = parse_qs(url.query).get("days", ["30"])[0]
                days = int(raw) if raw.isdigit() else 30
                self._json({"daily": load_daily(stats_dir, days)})
            elif url.path == "/api/records":
                raw = parse_qs(url.query).get("limit", ["100"])[0]
                rows = load_records(stats_dir, int(raw) if raw.isdigit() else 100)
                self._json(
                    {
                        "enabled": rows is not None,
                        "can_rate": inbox is not None,
                        "records": rows or [],
                        "pending": pending(inbox),
                        "reasons": REASONS,
                    }
                )
            else:
                self._send(404, b"nie ma takiej strony", "text/plain; charset=utf-8")

        def _deny(self) -> None:
            self._send(405, b"metoda niedozwolona", "text/plain; charset=utf-8")

        def _text(self, code: int, text: str) -> None:
            self._send(code, text.encode(), "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/api/rate":
                self._deny()
                return
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            origin = self.headers.get("Origin")
            same = origin is None or urlsplit(origin).netloc == self.headers.get("Host")
            if self.headers.get(CSRF_HEADER) != "1" or ctype != "application/json" or not same:
                self._text(403, "niedozwolone")
                return
            if inbox is None:
                self._text(503, "oceny wylaczone")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                self._text(400, "zly rozmiar")
                return
            try:
                clean = validate_rating(json.loads(self.rfile.read(length)))
            except ValueError:
                clean = None
            if clean is None:
                self._text(422, "nieprawidlowa ocena")
            elif not queue_rating(inbox, clean):
                self._text(429, "skrzynka pelna")
            else:
                self._json({"queued": True})

        do_PUT = do_DELETE = do_PATCH = _deny

        def log_message(self, *_args: object) -> None:  # bez adresów i parametrów w logach
            return

    return Handler


def serve(  # pragma: no cover - serwer
    port: int, stats_dir: Path, inbox: Path | None = None
) -> None:
    """Uruchamia panel na wszystkich interfejsach kontenera (port wystawia tylko loopback hosta)."""
    log.info("panel: nasłuchuje na porcie %d", port)
    ThreadingHTTPServer(("0.0.0.0", port), handler(stats_dir, inbox)).serve_forever()  # noqa: S104
