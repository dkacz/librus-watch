"""Bot Telegrama: powiadomienia z przyciskami ocen i polecenie `/brak` (ADR-019).

Token bota i identyfikatory czatów to sekrety (`TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_IDS`). Bot pisze i odpowiada wyłącznie na dozwolonych czatach.
Odpowiedzi (kliknięcia, `/brak`) odbiera przez `getUpdates`; ostatni
przetworzony numer aktualizacji jest w bazie. Ten sam token nie może być
odpytywany z dwóch miejsc naraz (Telegram zwraca 409).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from librus_watch.alerts import Alert, day_label, line_text
from librus_watch.config import Config
from librus_watch.secret_env import get_secret
from librus_watch.store import Store

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
REASONS = {
    "day": "nie ten dzień",
    "child": "nie to dziecko",
    "normal": "nic nietypowego",
    "false": "nieprawda",
}
Call = Callable[[str, dict[str, Any]], dict[str, Any]]


def http_call(token: str) -> Call:
    """Wywołanie Bot API przez `requests` (limit czasu, bez tokenu w logach)."""
    import requests

    def call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = requests.post(API.format(token=token, method=method), json=payload, timeout=30)
        data: dict[str, Any] = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('error_code')}")
        return data

    return call


def chat_ids() -> list[int]:
    """Dozwolone czaty z sekretu `TELEGRAM_CHAT_IDS` (jeden identyfikator w linii)."""
    raw = get_secret("TELEGRAM_CHAT_IDS", required=True)
    return [int(x) for x in raw.get_secret_value().split()] if raw else []


def _short(identity: str) -> str:
    """Skrót tożsamości do `callback_data` (limit 64 bajtów)."""
    return identity[:16]


def send_alert(alert: Alert, cfg: Config, call: Call, chats: list[int], today: date) -> int:
    """Wysyła powiadomienie: nagłówek i każda pozycja osobno z przyciskami.

    Returns:
        Liczba wysłanych wiadomości.
    """
    names = {c.id: c.name for c in cfg.children}
    sent = 0
    for chat in chats:
        call("sendMessage", {"chat_id": chat, "text": alert.title})
        sent += 1
        for ln in sorted(alert.lines, key=lambda ln: (ln.finding.action_day, ln.finding.child)):
            f = ln.finding
            text = (
                f"{names.get(f.child, f.child)}, {day_label(f.action_day, today)}\n{line_text(ln)}"
            )
            if f.quote:
                text += f"\n„{f.quote}”"
            key = _short(f.identity)
            buttons = [
                [
                    {"text": "Trafne", "callback_data": f"ok:{key}"},
                    {"text": "Błędne", "callback_data": f"bad:{key}"},
                ]
            ]
            call(
                "sendMessage",
                {"chat_id": chat, "text": text, "reply_markup": {"inline_keyboard": buttons}},
            )
            sent += 1
    return sent


def _identity(store: Store, short: str) -> str:
    """Pełna tożsamość pozycji po skrócie z przycisku."""
    row = store.db.execute(
        "SELECT identity FROM findings WHERE identity LIKE ? LIMIT 1", (short + "%",)
    ).fetchone()
    return str(row["identity"]) if row else f"?{short}"  # np. pozycja z test-notify


def _edit(call: Call, chat: int, msg: dict[str, Any], text: str, buttons: list[Any] | None) -> None:
    """Podmienia treść i przyciski wiadomości z kliknięciem (trwałe potwierdzenie oceny)."""
    payload: dict[str, Any] = {"chat_id": chat, "message_id": msg.get("message_id"), "text": text}
    payload["reply_markup"] = {"inline_keyboard": buttons or []}
    try:
        call("editMessageText", payload)
    except Exception as e:  # np. wiadomość zbyt stara do edycji: ocena i tak zapisana
        log.warning("telegram: nie udało się oznaczyć wiadomości (%s)", type(e).__name__)


def process_updates(store: Store, call: Call, chats: list[int], now: datetime) -> int:
    """Odbiera kliknięcia i polecenia, zapisuje oceny.

    Returns:
        Liczba przetworzonych aktualizacji z dozwolonych czatów.
    """
    offset = int(store.get_meta("telegram_offset") or 0)
    data = call("getUpdates", {"offset": offset, "timeout": 0})
    done = 0
    for u in data.get("result", []):
        store.set_meta("telegram_offset", str(int(u["update_id"]) + 1))
        if "callback_query" in u:
            q = u["callback_query"]
            msg = q.get("message", {})
            chat = msg.get("chat", {}).get("id")
            if chat not in chats:
                continue
            action, _, rest = str(q.get("data", "")).partition(":")
            text = str(msg.get("text", ""))
            if action == "ok":
                store.add_feedback(_identity(store, rest), "confirmed", now)
                _edit(call, chat, msg, f"{text}\n\nOcena: trafne", None)
                answer = "Zapisane jako trafne."
            elif action == "bad":
                buttons = [
                    [{"text": t, "callback_data": f"why:{k}:{rest}"}] for k, t in REASONS.items()
                ]
                _edit(call, chat, msg, f"{text}\n\nCo było nie tak?", buttons)
                answer = "Wybierz powód."
            elif action == "why":
                reason, _, key = rest.partition(":")
                store.add_feedback(_identity(store, key), "wrong", now, reason=reason)
                base = text.removesuffix("\n\nCo było nie tak?")
                _edit(
                    call,
                    chat,
                    msg,
                    f"{base}\n\nOcena: błędne ({REASONS.get(reason, reason)})",
                    None,
                )
                answer = "Zapisane jako błędne."
            else:
                continue
            try:  # Telegram przyjmuje odpowiedź tylko krótko po kliknięciu
                call("answerCallbackQuery", {"callback_query_id": q["id"], "text": answer})
            except Exception:
                log.debug("telegram: spóźniona odpowiedź na kliknięcie pominięta")
            done += 1
        elif "message" in u:
            m = u["message"]
            chat = m.get("chat", {}).get("id")
            command, _, note = str(m.get("text", "")).partition(" ")
            if chat not in chats or command.split("@")[0] != "/brak":
                continue
            store.add_feedback(None, "missed", now, note=note.strip())
            call("sendMessage", {"chat_id": chat, "text": "Zapisane jako pominięte. Dziękuję."})
            done += 1
    if done:
        log.info("telegram: %d ocen i zgłoszeń", done)
    return done
