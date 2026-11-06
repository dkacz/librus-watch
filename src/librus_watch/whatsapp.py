"""Bot WhatsAppa przez Cloud API (E7b, ADR-024).

Wysyłka: każda pozycja to wiadomość szablonowa (`whatsapp.template`) z dwoma
przyciskami szybkiej odpowiedzi; ładunek przycisku to `ok:<skrót>` albo
`bad:<skrót>` (jak w Telegramie). Po „Błędne” program wysyła listę powodów
(okno 24 h, bez szablonu). Wiadomość zaczynająca się od „brak” to zgłoszenie
pominięcia. Komunikaty (raport o awarii, informacje) idą szablonem
`whatsapp.info_template`.

Odbiór: webhook (`/v1/whatsapp`) w usłudze: GET z `hub.verify_token` przy
rejestracji, POST z podpisem `X-Hub-Signature-256` (HMAC-SHA256 sekretem
aplikacji). Program odpowiada tylko numerom z `WHATSAPP_TO`. Token, numery
i treści nigdy nie trafiają do logów.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from collections.abc import Callable
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from librus_watch.alerts import Alert, day_label, line_text
from librus_watch.config import Config
from librus_watch.secret_env import get_secret
from librus_watch.store import Store
from librus_watch.telegram import REASONS, _identity

log = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com/{version}/{phone_id}/messages"
Call = Callable[[dict[str, Any]], dict[str, Any]]


def graph_call(cfg: Config) -> Call | None:
    """Wysyłka przez Graph API albo `None`, gdy WhatsApp nie jest skonfigurowany."""
    token = get_secret("WHATSAPP_TOKEN", required=False)
    phone = get_secret("WHATSAPP_PHONE_ID", required=False)
    if token is None or phone is None:
        return None
    url = GRAPH.format(version=cfg.whatsapp.api_version, phone_id=phone.get_secret_value())
    headers = {"Authorization": f"Bearer {token.get_secret_value()}"}

    def call(payload: dict[str, Any]) -> dict[str, Any]:
        import requests

        r = requests.post(
            url, json={"messaging_product": "whatsapp", **payload}, headers=headers, timeout=20
        )
        if r.status_code >= 300:
            raise RuntimeError(f"WhatsApp {r.status_code}")
        data: dict[str, Any] = r.json()
        return data

    return call


def recipients() -> list[str]:
    """Numery odbiorców i dozwolonych nadawców (`WHATSAPP_TO`, jeden w linii, bez „+”)."""
    raw = get_secret("WHATSAPP_TO", required=False)
    return [x.lstrip("+") for x in raw.get_secret_value().split()] if raw else []


def _param(text: str, limit: int = 900) -> dict[str, str]:
    """Parametr szablonu: bez nowych linii (wymóg Mety), niepusty, przycięty."""
    flat = " ".join(text.split()) or "–"
    return {"type": "text", "text": flat[:limit]}


def send_alert(alert: Alert, cfg: Config, call: Call, to: list[str], today: date) -> int:
    """Każda pozycja jako szablon z przyciskami; zwraca liczbę wysłanych wiadomości."""
    names = {c.id: c.name for c in cfg.children}
    sent = 0
    for number in to:
        for ln in sorted(alert.lines, key=lambda ln: (ln.finding.action_day, ln.finding.child)):
            f = ln.finding
            key = f.identity[:16]
            who = f"{names.get(f.child, f.child)}, {day_label(f.action_day, today)}"
            call(
                {
                    "to": number,
                    "type": "template",
                    "template": {
                        "name": cfg.whatsapp.template,
                        "language": {"code": cfg.whatsapp.language},
                        "components": [
                            {
                                "type": "body",
                                "parameters": [
                                    _param(who),
                                    _param(line_text(ln)),
                                    _param(f.quote or "–"),
                                ],
                            },
                            {
                                "type": "button",
                                "sub_type": "quick_reply",
                                "index": "0",
                                "parameters": [{"type": "payload", "payload": f"ok:{key}"}],
                            },
                            {
                                "type": "button",
                                "sub_type": "quick_reply",
                                "index": "1",
                                "parameters": [{"type": "payload", "payload": f"bad:{key}"}],
                            },
                        ],
                    },
                }
            )
            sent += 1
    return sent


def send_info(text: str, cfg: Config, call: Call, to: list[str]) -> int:
    """Komunikat (raport, informacja) szablonem `info_template`."""
    for number in to:
        call(
            {
                "to": number,
                "type": "template",
                "template": {
                    "name": cfg.whatsapp.info_template,
                    "language": {"code": cfg.whatsapp.language},
                    "components": [{"type": "body", "parameters": [_param(text)]}],
                },
            }
        )
    return len(to)


def verify_signature(body: bytes, header: str, secret: str) -> bool:
    """Sprawdza `X-Hub-Signature-256` (HMAC-SHA256 treści sekretem aplikacji)."""
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def handle_event(
    data: dict[str, Any],
    store: Store,
    call: Call,
    allowed: list[str],
    now: datetime,
    link: Callable[[str], str | None] | None = None,
) -> int:
    """Przetwarza zdarzenie webhooka; zwraca liczbę zapisanych ocen i zgłoszeń."""
    done = 0
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []):
                sender = str(m.get("from", ""))
                if sender not in allowed:
                    continue
                payload = ""
                if m.get("type") == "button":
                    payload = str(m.get("button", {}).get("payload", ""))
                elif m.get("type") == "interactive":
                    i = m.get("interactive", {})
                    payload = str(
                        (i.get("button_reply") or i.get("list_reply") or {}).get("id", "")
                    )
                elif m.get("type") == "text":
                    text = str(m.get("text", {}).get("body", "")).strip()
                    if text.lower().startswith("brak"):
                        note = text[4:].strip(" :,-")
                        store.add_feedback(
                            None, "missed", now, note=note, text_ref=link(note) if link else None
                        )
                        _text(call, sender, "Zapisane jako pominięte. Dziękuję.")
                        done += 1
                    continue
                action, _, rest = payload.partition(":")
                if action == "ok":
                    store.add_feedback(_identity(store, rest), "confirmed", now)
                    _text(call, sender, "Zapisane jako trafne.")
                elif action == "bad":
                    rows = [{"id": f"why:{k}:{rest}", "title": t[:24]} for k, t in REASONS.items()]
                    call(
                        {
                            "to": sender,
                            "type": "interactive",
                            "interactive": {
                                "type": "list",
                                "body": {"text": "Co było nie tak?"},
                                "action": {
                                    "button": "Wybierz powód",
                                    "sections": [{"title": "Powód", "rows": rows}],
                                },
                            },
                        }
                    )
                    continue
                elif action == "why":
                    reason, _, key = rest.partition(":")
                    store.add_feedback(_identity(store, key), "wrong", now, reason=reason)
                    _text(call, sender, f"Zapisane jako błędne ({REASONS.get(reason, reason)}).")
                else:
                    continue
                done += 1
    if done:
        log.info("whatsapp: %d ocen i zgłoszeń", done)
    return done


def _text(call: Call, to: str, body: str) -> None:
    """Odpowiedź tekstowa w oknie 24 h; błąd nie przerywa obsługi."""
    try:
        call({"to": to, "type": "text", "text": {"body": body}})
    except Exception as e:
        log.warning("whatsapp: odpowiedź nieudana (%s)", type(e).__name__)


def webhook_handler(
    cfg: Config,
    store: Store,
    call: Call,
    lock: threading.Lock,
    clock: Callable[[], datetime],
    link: Callable[[str], str | None] | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Klasa obsługi webhooka (rejestracja i zdarzenia)."""
    secret = get_secret("WHATSAPP_APP_SECRET")
    verify = get_secret("WHATSAPP_VERIFY_TOKEN")
    allowed = recipients()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            q = parse_qs(urlsplit(self.path).query)
            ok = (
                urlsplit(self.path).path == "/v1/whatsapp"
                and verify is not None
                and q.get("hub.mode") == ["subscribe"]
                and hmac.compare_digest(
                    q.get("hub.verify_token", [""])[0], verify.get_secret_value()
                )
            )
            body = q.get("hub.challenge", [""])[0].encode() if ok else b""
            self.send_response(200 if ok else 403)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if 0 < length <= 256 * 1024 else b""
            sig = self.headers.get("X-Hub-Signature-256", "")
            if (
                urlsplit(self.path).path != "/v1/whatsapp"
                or secret is None
                or not body
                or not verify_signature(body, sig, secret.get_secret_value())
            ):
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(200)  # Meta oczekuje szybkiej odpowiedzi
            self.end_headers()
            try:
                with lock:
                    handle_event(json.loads(body), store, call, allowed, clock(), link)
            except Exception as e:
                log.warning("whatsapp: zdarzenie nieobsłużone (%s)", type(e).__name__)

        def log_message(self, *_args: object) -> None:
            return

    return Handler


def serve_webhook(cfg: Config, stop: threading.Event, clock: Callable[[], datetime]) -> None:
    """Webhook w osobnym wątku usługi (port `whatsapp.webhook_port`)."""
    call = graph_call(cfg)
    if call is None or get_secret("WHATSAPP_APP_SECRET", required=False) is None:
        return
    from librus_watch.feedback import link_missed

    store = Store(cfg.data_dir)
    lock = threading.Lock()
    server = ThreadingHTTPServer(
        ("0.0.0.0", cfg.whatsapp.webhook_port),  # noqa: S104 - w kontenerze, za tunelem
        webhook_handler(
            cfg, store, call, lock, clock, link=lambda note: link_missed(cfg, store, note, clock())
        ),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("whatsapp: webhook nasłuchuje")
    stop.wait()
    server.shutdown()
    store.close()
