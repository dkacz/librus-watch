from collections.abc import Iterator
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest

from librus_watch.alerts import Alert, AlertLine
from librus_watch.config import AlertType, Config, Kind, Priority
from librus_watch.models import Finding, Origin
from librus_watch.store import Store
from librus_watch.telegram import chat_ids, process_updates, send_alert

NOW = datetime(2026, 11, 5, 19, 0)
CHAT = 5550001
CFG = Config.model_validate({"children": [{"id": "iwo", "name": "Iwo"}]})
F = Finding(
    "iwo",
    date(2026, 11, 6),
    Kind.early_start,
    "zbiórka",
    Origin.llm,
    "message:1",
    at=time(7, 30),
    quote="jutro zbiórka o 7:30",
)


class FakeBot:
    def __init__(self, updates: list[dict[str, Any]] | None = None) -> None:
        self.updates = updates or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, payload))
        if method == "getUpdates":
            return {
                "ok": True,
                "result": [u for u in self.updates if u["update_id"] >= payload["offset"]],
            }
        return {"ok": True, "result": {}}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path)
    s.add_finding(F, NOW)
    yield s
    s.close()


def click(uid: int, data: str, chat: int = CHAT) -> dict[str, Any]:
    return {
        "update_id": uid,
        "callback_query": {
            "id": f"q{uid}",
            "data": data,
            "message": {"chat": {"id": chat}, "message_id": 10, "text": "Iwo, jutro\nzbiórka"},
        },
    }


def test_send_alert_has_buttons_per_item() -> None:
    bot = FakeBot()
    alert = Alert(AlertType.evening, "Jutro: jutro (piątek 6.11)", [AlertLine(F, Priority.high)])
    assert send_alert(alert, CFG, bot, [CHAT], date(2026, 11, 5)) == 2
    _method, payload = bot.calls[1]
    assert payload["text"].startswith(
        "Iwo, jutro (piątek 6.11)\nwcześniej do szkoły: zbiórka 07:30"
    )
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["Trafne", "Błędne"]
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons)


def test_ratings_reasons_missed_and_foreign_chats(store: Store) -> None:
    key = F.identity[:16]
    bot = FakeBot(
        [
            click(1, f"ok:{key}"),
            click(2, f"bad:{key}"),
            click(3, f"why:day:{key}"),
            click(4, f"ok:{key}", chat=999),  # obcy czat
            {"update_id": 5, "message": {"chat": {"id": CHAT}, "text": "/brak jutro strój galowy"}},
            {"update_id": 6, "message": {"chat": {"id": CHAT}, "text": "dzień dobry"}},
            {"update_id": 7, "message": {"chat": {"id": 999}, "text": "/brak spam"}},
            click(8, f"why:day:{key}"),  # powtórzenie ostatniej oceny: bez drugiego wpisu
        ]
    )
    assert process_updates(store, bot, [CHAT], NOW) == 5
    assert [
        {k: r[k] for k in ("identity", "rating", "reason", "note")} for r in store.feedback()
    ] == [
        {"identity": F.identity, "rating": "confirmed", "reason": "", "note": ""},
        {"identity": F.identity, "rating": "wrong", "reason": "day", "note": ""},
        {"identity": None, "rating": "missed", "reason": "", "note": "jutro strój galowy"},
    ]
    assert store.get_meta("telegram_offset") == "9"
    edits = [p["text"] for m, p in bot.calls if m == "editMessageText"][:3]
    assert edits == [
        "Iwo, jutro\nzbiórka\n\nOcena: trafne",
        "Iwo, jutro\nzbiórka\n\nCo było nie tak?",
        "Iwo, jutro\nzbiórka\n\nOcena: błędne (nie ten dzień)",
    ]
    assert process_updates(store, bot, [CHAT], NOW) == 0  # nic nowego


def test_chat_ids_from_secret_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "ids"
    path.write_text("5866864104\n-100123\n")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS_FILE", str(path))
    assert chat_ids() == [5866864104, -100123]


def test_click_on_an_older_notification_still_counts(store: Store) -> None:
    newer = Finding("iwo", date(2026, 11, 9), Kind.test, "sprawdzian", Origin.schedule, "agenda:7")
    store.add_finding(newer, NOW)
    old_key, new_key = F.identity[:16], newer.identity[:16]
    bot = FakeBot([click(20, f"ok:{new_key}"), click(21, f"why:false:{old_key}")])
    assert process_updates(store, bot, [CHAT], NOW) == 2
    assert [(r["identity"], r["rating"]) for r in store.feedback()] == [
        (newer.identity, "confirmed"),
        (F.identity, "wrong"),
    ]
