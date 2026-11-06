from datetime import datetime
from pathlib import Path

import pytest
from fixtures.librus_http import FakeLibrus

from librus_watch.cli import main
from librus_watch.config import Config, Credentials, child_credentials, load_config
from librus_watch.errors import AuthError
from librus_watch.store import Store
from librus_watch.sync import LoginBlocked, login_child

NOW = datetime(2026, 11, 6, 12, 0)
EVENING = datetime(2026, 11, 6, 20, 0)
NEXT_MORNING = datetime(2026, 11, 7, 6, 30)


def env(config_file: Path) -> tuple[Config, Store, Credentials]:
    cfg = load_config(config_file)
    return cfg, Store(cfg.data_dir), child_credentials(cfg.children[0])


def test_device_cookie_is_remembered_and_sent_next_time(
    fake_librus: FakeLibrus, config_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg, store, creds = env(config_file)
    login_child(cfg, store, "ania", creds, NOW)
    assert fake_librus.calls["known_device"] == 0
    assert store.get_meta("device_cookie:ania") == "urzadzenie0123456789abcdef"
    login_child(cfg, store, "ania", creds, EVENING)
    assert fake_librus.calls["known_device"] == 1
    st = store.fetch_state("ania", NOW.date())
    assert (st.logins, st.last_success, st.blocked_until) == (2, EVENING, None)
    assert "urzadzenie0123456789abcdef" not in caplog.text
    store.close()


def test_captcha_blocks_account_until_next_morning(
    fake_librus: FakeLibrus, config_file: Path
) -> None:
    cfg, store, creds = env(config_file)
    fake_librus.captcha = True
    with pytest.raises(AuthError, match="captcha"):
        login_child(cfg, store, "ania", creds, NOW)
    fake_librus.captcha = False
    with pytest.raises(LoginBlocked, match="wstrzymane do 2026-11-07 06:00"):
        login_child(cfg, store, "ania", creds, EVENING)
    assert fake_librus.calls["login"] == 1  # drugiej próby nie było
    login_child(cfg, store, "ania", creds, NEXT_MORNING)  # po blokadzie znowu można
    assert fake_librus.calls["login"] == 2
    store.close()


def test_daily_login_limit_and_force(
    fake_librus: FakeLibrus, config_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg, store, creds = env(config_file)
    cfg = cfg.model_copy(update={"fetch": cfg.fetch.model_copy(update={"max_logins_per_day": 2})})
    login_child(cfg, store, "ania", creds, NOW)
    login_child(cfg, store, "ania", creds, NOW)
    with pytest.raises(LoginBlocked, match="dzienny limit"):
        login_child(cfg, store, "ania", creds, EVENING)
    login_child(cfg, store, "ania", creds, EVENING, force=True)
    assert fake_librus.calls["login"] == 3
    assert "mimo blokady lub limitu" in caplog.text
    login_child(cfg, store, "ania", creds, NEXT_MORNING)  # nowa doba, nowy limit
    store.close()


def test_cli_dry_run_counts_login_and_skips_blocked_child(
    fake_librus: FakeLibrus, config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--config", str(config_file), "fetch", "--dry-run"]
    assert main(args) == 0
    _, store, _ = env(config_file)
    assert store.fetch_state("ania", datetime.now().date()).logins == 1
    assert store.texts("ania") == []  # dry-run nie zapisuje danych
    store.close()
    fake_librus.captcha = True
    assert main(args) == 1
    fake_librus.captcha = False
    assert main(args) == 1 and "ania: pominięte (logowanie wstrzymane" in capsys.readouterr().out
    assert fake_librus.calls["login"] == 2
    assert main([*args, "--force"]) == 0 and fake_librus.calls["login"] == 3
