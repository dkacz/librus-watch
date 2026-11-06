"""Telemetria zbiorcza (E12, ADR-022): raz w tygodniu same liczby, opt-out.

Paczka zawiera wyłącznie liczby z ostatnich 7 dni, zaokrąglone do 5 po
dodaniu szumu, wersje programu, promptu i schematu oraz losowy identyfikator
instalacji zmieniany co tydzień. Nie ma w niej treści, imion, identyfikatorów
szkoły, klasy ani dziecka, loginów, adresów kanałów ani nazwy modelu.

Odbiornik (`serve`) przyjmuje tylko paczki zgodne ze schematem i do 4 KB
i dopisuje je do pliku JSONL.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections import Counter
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from librus_watch import __version__
from librus_watch.config import Config, ContentType, Kind
from librus_watch.librus.gateway import MODULES
from librus_watch.reading import PROMPT_VERSION, SCHEMA_VERSION
from librus_watch.store import Store

log = logging.getLogger(__name__)

PACKET_SCHEMA = 1
MAX_BYTES = 4096
ROUND = 5
NOISE = 2
SIEVE_REASONS = ("ok", "signal", "empty", "expired", "old", "no_signal", "triage_out")
RATINGS = ("confirmed", "wrong", "missed", "late")


class Packet(BaseModel):
    """Schemat paczki (odbiornik odrzuca wszystko inne)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1]
    install: str = Field(pattern=r"^[0-9a-f]{16}$")
    week: str = Field(pattern=r"^\d{4}-W\d{2}$")
    app_version: str = Field(max_length=20)
    prompt_version: str = Field(max_length=10)
    answer_schema: str = Field(max_length=10)
    children: int = Field(ge=0, le=50)
    llm_requests: int = Field(ge=0)
    findings: dict[Kind, int]
    ratings: dict[Literal["confirmed", "wrong", "missed", "late"], int]
    sieve: dict[Literal["ok", "signal", "empty", "expired", "old", "no_signal", "triage_out"], int]
    module_errors: dict[str, int]


def _blur(n: int, rng: secrets.SystemRandom) -> int:
    """Szum i zaokrąglenie do 5 (bez wartości ujemnych)."""
    noisy = n + rng.randint(-NOISE, NOISE) if n else 0
    return max(0, ROUND * round(noisy / ROUND))


def note_module_errors(store: Store, now: datetime, failed: dict[str, str]) -> None:
    """Dolicza błędy modułów Librusa z cyklu (tylko nazwy modułów i liczby)."""
    if not failed:
        return
    key = f"module_errors:{now.date()}"
    counts = Counter(json.loads(store.get_meta(key) or "{}"))
    counts.update(failed.keys())
    store.set_meta(key, json.dumps(dict(counts)))


def build_packet(cfg: Config, store: Store, now: datetime) -> dict[str, Any]:
    """Składa paczkę z ostatnich 7 dni (do wysyłki i do `telemetry --show`)."""
    since = now - timedelta(days=7)
    days = [(now - timedelta(days=i)).date() for i in range(7)]
    rng = secrets.SystemRandom()
    requests = sum(store.llm_usage(d)[0] for d in days)
    kinds = Counter(
        r[0]
        for r in store.db.execute(
            "SELECT kind FROM findings WHERE created_at>=?", (since.isoformat(),)
        )
    )
    ratings = Counter(
        r[0]
        for r in store.db.execute(
            "SELECT rating FROM feedback WHERE created_at>=?", (since.isoformat(),)
        )
    )
    sieve = Counter(
        r[0]
        for r in store.db.execute(
            "SELECT sieve FROM texts WHERE sieve IS NOT NULL AND first_seen>=?",
            (since.isoformat(),),
        )
    )
    known = {m.name for m in MODULES} | {"messages"}
    errors: Counter[str] = Counter()
    for d in days:
        for module, n in json.loads(store.get_meta(f"module_errors:{d}") or "{}").items():
            if module in known:
                errors[module] += int(n)
    year, week, _ = now.isocalendar()
    return {
        "schema_version": PACKET_SCHEMA,
        "install": secrets.token_hex(8),
        "week": f"{year}-W{week:02d}",
        "app_version": __version__,
        "prompt_version": PROMPT_VERSION,
        "answer_schema": SCHEMA_VERSION,
        "children": len(cfg.children),
        "llm_requests": _blur(requests, rng),
        "findings": {k.value: _blur(kinds.get(k.value, 0), rng) for k in Kind},
        "ratings": {r: _blur(ratings.get(r, 0), rng) for r in RATINGS},
        "sieve": {s: _blur(sieve.get(s, 0), rng) for s in SIEVE_REASONS},
        "module_errors": {m: _blur(n, rng) for m, n in sorted(errors.items())},
    }


def endpoint_allowed(url: str) -> bool:
    """HTTPS, a zwykły HTTP tylko dla nazw wewnętrznych (bez kropki) i localhost."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme == "https":
        return bool(host)
    return parts.scheme == "http" and (host == "localhost" or ("." not in host and bool(host)))


def send_weekly(cfg: Config, store: Store, now: datetime, post: Any = None) -> bool:
    """Wysyła paczkę raz w tygodniu (gdy włączona i jest adres). Zwraca `True` po wysyłce."""
    t = cfg.telemetry
    if not t.enabled or not t.endpoint:
        return False
    year, week, _ = now.isocalendar()
    key = f"telemetry_sent:{year}-W{week:02d}"
    if store.get_meta(key):
        return False
    if not endpoint_allowed(t.endpoint):
        log.warning("telemetria: niedozwolony adres (wymagany HTTPS), wysyłka pominięta")
        return False
    packet = build_packet(cfg, store, now)
    if post is None:
        import requests

        def post(url: str, data: dict[str, Any]) -> int:
            return requests.post(url, json=data, timeout=10).status_code

    try:
        status = post(t.endpoint, packet)
    except Exception as e:  # telemetria nigdy nie zatrzymuje usługi
        log.warning("telemetria: wysyłka nieudana (%s)", type(e).__name__)
        return False
    if status >= 300:
        log.warning("telemetria: odbiornik odpowiedział %d", status)
        return False
    store.set_meta(key, now.isoformat())
    log.info("telemetria: paczka tygodniowa wysłana")
    return True


def serve(port: int, out: Path) -> None:  # pragma: no cover - pętla serwera, testy wołają handler
    """Odbiornik paczek: POST /v1/telemetry, zapis do JSONL."""
    ThreadingHTTPServer(("0.0.0.0", port), handler(out)).serve_forever()  # noqa: S104


def handler(out: Path) -> type[BaseHTTPRequestHandler]:
    """Klasa obsługi żądań odbiornika z plikiem wyjściowym."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if self.path != "/v1/telemetry" or not 0 < length <= MAX_BYTES:
                self._reply(400)
                return
            try:
                packet = Packet.model_validate_json(self.rfile.read(length))
            except ValidationError:
                self._reply(422)
                return
            with out.open("a") as f:
                f.write(packet.model_dump_json() + "\n")
            self._reply(204)

        def _reply(self, code: int) -> None:
            self.send_response(code)
            self.end_headers()

        def log_message(self, *_args: object) -> None:  # bez adresów IP w logach
            return

    return Handler


_ = ContentType
