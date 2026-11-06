"""E6 na poleceniach: brief (na sucho i z wysyłką), brak powtórek po restarcie,
test-notify, telegram-poll; kanały na atrapach, bez sieci."""

from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from librus_watch import cli
from librus_watch.config import Kind, load_config
from librus_watch.models import Finding, Origin
from librus_watch.store import Store

TOKEN = "123456:SEKRETNYTOKENbota0123456789abcdef"


class Channels:
    def __init__(self) -> None:
        self.apprise: list[tuple[str, list[str]]] = []
        self.bot: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []

    def notify(self, body: str, title: str, tag: list[str]) -> bool:
        self.apprise.append((body, tag))
        return True

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.bot.append((method, payload))
        if method == "getUpdates":
            return {"ok": True, "result": self.updates}
        return {"ok": True, "result": {}}


@pytest.fixture
def channels(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Channels:
    ch = Channels()
    monkeypatch.setattr(cli, "_channels", lambda cfg: (ch, (ch.call, [42])))
    monkeypatch.setattr(cli, "http_call", lambda token: ch.call)
    token = tmp_path / "tg_token"
    token.write_text(TOKEN)
    ids = tmp_path / "tg_ids"
    ids.write_text("42\n")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_FILE", str(token))
    monkeypatch.setenv("TELEGRAM_CHAT_IDS_FILE", str(ids))
    return ch


def tomorrow_finding(config_file: Path, at: time = time(7, 30)) -> Finding:
    cfg = load_config(config_file)
    day = datetime.now().date() + timedelta(days=1)
    f = Finding(
        "ania",
        day,
        Kind.early_start,
        "zbiórka",
        Origin.llm,
        "message:1",
        at=at,
        quote="jutro zbiórka",
    )
    store = Store(cfg.data_dir)
    store.add_finding(f, datetime.now())
    store.close()
    return f


def test_brief_dry_run_send_and_no_repeat_after_restart(
    channels: Channels,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    args = ["--config", str(config_file), "brief", "evening"]
    assert cli.main(args) == 0 and "nic do wysłania" in capsys.readouterr().out
    tomorrow_finding(config_file)
    assert cli.main([*args, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Ania, jutro" in out and "wcześniej do szkoły: zbiórka 07:30" in out
    assert channels.apprise == [] and channels.bot == []
    assert cli.main(args) == 0 and "wysłane: 1 pozycji" in capsys.readouterr().out
    assert len(channels.apprise) == 1 and len(channels.bot) == 2
    # nowe uruchomienie programu (nowe połączenie z bazą): bez powtórki
    assert cli.main(args) == 0 and "nic do wysłania" in capsys.readouterr().out
    # zmiana już wysłanej rzeczy wychodzi jako ZMIANA
    tomorrow_finding(config_file, at=time(7, 15))
    assert cli.main(args) == 0
    assert "ZMIANA" in channels.apprise[-1][0]
    assert TOKEN not in caplog.text


def test_test_notify_and_telegram_poll(
    channels: Channels,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--config", str(config_file), "test-notify"]) == 0
    texts = [p["text"] for m, p in channels.bot if m == "sendMessage"]
    assert texts[0] == "librus-watch: powiadomienie testowe" and "(TEST)" in texts[1]
    channels.updates = [{"update_id": 1, "message": {"chat": {"id": 42}, "text": "/brak strój"}}]
    assert cli.main(["--config", str(config_file), "telegram-poll"]) == 0
    assert "oceny i zgłoszenia: 1" in capsys.readouterr().out
    store = Store(load_config(config_file).data_dir)
    assert [r["rating"] for r in store.feedback()] == ["missed"]
    store.close()


def test_no_channels_and_bad_config(
    monkeypatch: pytest.MonkeyPatch,
    config_file: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("APPRISE_CONFIG", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_FILE", raising=False)
    assert cli.main(["--config", str(config_file), "test-notify"]) == 1
    assert "brak kanałów" in capsys.readouterr().err
    assert cli.main(["--config", str(config_file), "telegram-poll"]) == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text("children: []\\n")
    assert cli.main(["--config", str(bad), "brief", "morning"]) == 2
