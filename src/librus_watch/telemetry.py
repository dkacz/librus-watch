"""Telemetria zbiorcza (E12, ADR-022): raz w tygodniu same liczby, opt-out.

Paczka zawiera wyłącznie liczby z ostatnich 7 dni, zaokrąglone do 5 po
dodaniu szumu, wersje programu, promptu i schematu oraz losowy identyfikator
instalacji zmieniany co tydzień. Nie ma w niej treści, imion, identyfikatorów
szkoły, klasy ani dziecka, loginów, adresów kanałów ani nazwy modelu.

Odbiornik (`serve`) przyjmuje tylko paczki zgodne ze schematem i do 4 KB
i dopisuje je do pliku JSONL.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
from collections import Counter, deque
from dataclasses import dataclass
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
MAX_CORPUS_BYTES = 128 * 1024
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


@dataclass
class Limits:
    """Ochrona publicznego odbiornika (bez zapisu adresów IP).

    Attributes:
        per_ip_hour: Paczki na godzinę z jednego adresu (`CF-Connecting-IP`).
        daily: Paczki na dobę łącznie.
        max_bytes: Łączny rozmiar plików wyjściowych; powyżej odbiornik nie przyjmuje paczek.
        seen: Ile ostatnich odcisków paczek pamiętać (odrzucanie powtórek).
    """

    per_ip_hour: int = 10
    daily: int = 5000
    max_bytes: int = 200 * 1024 * 1024
    seen: int = 20000


class Guard:
    """Liczniki limitów w pamięci procesu (zerowane z upływem godziny i doby)."""

    def __init__(self, limits: Limits, clock: Any = None) -> None:
        """Tworzy liczniki."""
        self.limits = limits
        self.clock = clock or datetime.now
        self.lock = threading.Lock()
        self.hour: tuple[str, Counter[str]] = ("", Counter())
        self.day: tuple[str, int] = ("", 0)
        self.fps: deque[str] = deque(maxlen=limits.seen)
        self.fp_set: set[str] = set()

    def admit(self, ip: str) -> bool:
        """Czy przyjąć kolejną paczkę od adresu (limit godzinowy i dobowy)."""
        now = self.clock()
        hour, day = now.strftime("%Y%m%d%H"), now.strftime("%Y%m%d")
        with self.lock:
            if self.hour[0] != hour:
                self.hour = (hour, Counter())
            if self.day[0] != day:
                self.day = (day, 0)
            key = hashlib.sha256(ip.encode()).hexdigest()[:16]
            if self.hour[1][key] >= self.limits.per_ip_hour or self.day[1] >= self.limits.daily:
                return False
            self.hour[1][key] += 1
            self.day = (day, self.day[1] + 1)
            return True

    def duplicate(self, body: bytes) -> bool:
        """Czy ta sama paczka już przyszła (pamięć ostatnich odcisków)."""
        fp = hashlib.sha256(body).hexdigest()
        with self.lock:
            if fp in self.fp_set:
                return True
            if len(self.fps) == self.fps.maxlen:
                self.fp_set.discard(self.fps[0])
            self.fps.append(fp)
            self.fp_set.add(fp)
            return False


def serve(port: int, out: Path, limits: Limits | None = None) -> None:  # pragma: no cover
    """Odbiornik paczek: POST /v1/telemetry i /v1/corpus, zapis do JSONL."""
    ThreadingHTTPServer(("0.0.0.0", port), handler(out, limits)).serve_forever()  # noqa: S104


def handler(
    out: Path, limits: Limits | None = None, guard: Guard | None = None
) -> type[BaseHTTPRequestHandler]:
    """Klasa obsługi żądań odbiornika z plikiem wyjściowym i limitami."""
    active: Guard = guard or Guard(limits or Limits())

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            from librus_watch.corpus import CorpusPacket

            length = int(self.headers.get("Content-Length") or 0)
            routes: dict[str, tuple[type[BaseModel], int, Path]] = {
                "/v1/telemetry": (Packet, MAX_BYTES, out),
                "/v1/corpus": (CorpusPacket, MAX_CORPUS_BYTES, out.with_name("corpus.jsonl")),
            }
            route = routes.get(self.path)
            if route is None or not 0 < length <= route[1]:
                self._reply(400)
                return
            used = sum(p.stat().st_size for p in (out, out.with_name("corpus.jsonl")) if p.exists())
            if used >= active.limits.max_bytes:
                log.warning("odbiornik: limit miejsca na dysku osiągnięty, paczki odrzucane")
                self._reply(507)
                return
            ip = self.headers.get("CF-Connecting-IP") or self.client_address[0]
            if not active.admit(ip):
                self._reply(429)
                return
            body = self.rfile.read(length)
            try:
                packet = route[0].model_validate_json(body)
            except ValidationError:
                self._reply(422)
                return
            if active.duplicate(body):
                self._reply(204)  # powtórka: przyjęta, ale niezapisana
                return
            with route[2].open("a") as f:
                f.write(packet.model_dump_json() + "\n")
            self._reply(204)

        def _reply(self, code: int) -> None:
            self.send_response(code)
            self.end_headers()

        def log_message(self, *_args: object) -> None:  # bez adresów IP w logach
            return

    return Handler


_ = ContentType
