import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from fixtures.librus_http import FakeLibrus

from librus_watch.cli import main
from librus_watch.config import Config, ContentType, child_credentials, load_config
from librus_watch.errors import AuthError
from librus_watch.librus.session import LibrusSession
from librus_watch.lock import InstanceLock
from librus_watch.store import _MIGRATIONS, Store
from librus_watch.sync import RARE_MODULES, sync_child

NOW = datetime(2026, 11, 6, 12, 0)
LATER = datetime(2026, 11, 6, 13, 0)
TOMORROW = datetime(2026, 11, 7, 13, 0)
C = ContentType
PRIVATE = ("haslo-rodzica", "1234567", "Anna Testowa", "Zbiórka", "Muzeum", "sesja0123456789")


def setup(config_file: Path, **changes: object) -> tuple[Config, Store, LibrusSession]:
    cfg = load_config(config_file).model_copy(update=changes)
    store = Store(cfg.data_dir)
    child = cfg.children[0]
    session = LibrusSession.login(child.id, child_credentials(child), 10)
    return cfg, store, session


def test_full_cycle_then_second_cycle_is_quiet_and_uses_cache(
    fake_librus: FakeLibrus, config_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    cfg, store, session = setup(config_file)
    report = sync_child(cfg, "ania", session, store, now=NOW)
    assert report.ok and fake_librus.calls["login"] == 1
    assert report.records["Timetables"] > 0 and report.records["messages"] == 2
    assert report.bodies == 2 and fake_librus.calls["message_body"] == 2
    assert report.changes[C.notice.value]["new"] == 1
    assert report.changes[C.plan_change.value]["new"] == 2
    assert C.grade.value not in report.changes and "entry:grade" not in report.changes

    assert next(t.body or "" for t in store.texts("ania", C.message)).startswith("Zbiórka")
    assert {t.source for t in store.texts("ania")} >= {
        C.message,
        C.notice,
        C.agenda,
        C.homework,
        C.grade_comment,
        C.trip,
        C.conference,
    }
    assert store.lookups("ania", "subjects") == {"501": "Matematyka"}
    assert len(store.plan_changes("ania")) == 2 and len(store.free_days("ania")) == 2
    assert store.lessons("ania", datetime(2026, 11, 2).date())

    first_gateway = fake_librus.calls["gateway"]
    session2 = LibrusSession.login("ania", child_credentials(cfg.children[0]), 10)
    second = sync_child(cfg, "ania", session2, store, now=LATER)
    assert second.ok and set(second.not_fetched) == RARE_MODULES
    assert fake_librus.calls["gateway"] - first_gateway < first_gateway
    assert all(set(c) == {"same"} for c in second.changes.values())
    assert second.records["messages"] == 0 and fake_librus.calls["message_body"] == 2
    # słowniki z bazy: nazwy przedmiotów dalej rozpoznane
    assert store.plan_changes("ania")[0].subject == "Matematyka"

    third = sync_child(cfg, "ania", session2, store, now=TOMORROW)
    assert not set(third.not_fetched) & RARE_MODULES  # po dobie słowniki od nowa

    text = caplog.text
    assert not any(p in text for p in PRIVATE)
    store.close()


def test_dry_run_writes_nothing_and_opens_no_message(
    fake_librus: FakeLibrus, config_file: Path
) -> None:
    cfg, store, session = setup(config_file)
    report = sync_child(cfg, "ania", session, store, now=NOW, dry_run=True)
    assert report.dry_run and report.records["messages"] == 2 and report.changes == {}
    assert store.texts("ania") == [] and store.lookups("ania", "subjects") == {}
    assert fake_librus.calls["message_body"] == 0
    store.close()


def test_ignored_grades_and_comments_are_not_even_requested(
    fake_librus: FakeLibrus, config_file: Path
) -> None:
    cfg, store, session = setup(
        config_file, ignore_content=frozenset({C.grade, C.grade_comment, C.message})
    )
    report = sync_child(cfg, "ania", session, store, now=NOW)
    assert not any(p.startswith("Grades") for p in fake_librus.gateway_paths)
    assert "messages" not in report.records and fake_librus.calls["messages_get"] == 0
    assert C.grade_comment.value not in report.changes
    store.close()


def test_unavailable_module_does_not_stop_the_cycle(
    fake_librus: FakeLibrus, config_file: Path
) -> None:
    fake_librus.overrides["SchoolNotices"] = 403
    fake_librus.overrides["Calendars/Substitutions"] = 503
    cfg, store, session = setup(config_file)
    report = sync_child(cfg, "ania", session, store, now=NOW)
    assert report.unavailable == ["SchoolNotices"]
    assert report.failed == {"Substitutions": "TransientError"} and not report.ok
    assert store.texts("ania", C.homework) and store.plan_changes("ania") == []
    store.close()


def test_wrong_password_is_auth_error_after_one_attempt(
    fake_librus: FakeLibrus, config_file: Path
) -> None:
    fake_librus.accounts = {"1234567": "inne-haslo"}
    cfg = load_config(config_file)
    with pytest.raises(AuthError) as exc:
        LibrusSession.login("ania", child_credentials(cfg.children[0]), 10)
    assert fake_librus.calls["login"] == 1 and fake_librus.calls["gateway"] == 0
    assert "1234567" not in str(exc.value)


def test_expired_api_session_stops_cycle_with_auth_error(
    fake_librus: FakeLibrus, config_file: Path
) -> None:
    cfg, store, session = setup(config_file)
    fake_librus.expire_api = True
    with pytest.raises(AuthError):
        sync_child(cfg, "ania", session, store, now=NOW)
    store.close()


def test_cycle_on_migrated_v1_database(fake_librus: FakeLibrus, config_file: Path) -> None:
    cfg = load_config(config_file)
    cfg.data_dir.mkdir(mode=0o700)
    db = sqlite3.connect(cfg.data_dir / "librus-watch.db")
    db.executescript(
        _MIGRATIONS[1] + "PRAGMA user_version=1;"
        "INSERT INTO messages(child, source, key, sent, author, title, body, extracted_at, "
        "first_seen) VALUES ('ania','message','124','2026-11-04','A','Zebranie','stara treść',"
        "'2026-11-04T19:00:00','2026-11-04T18:00:00');"
    )
    db.close()
    cfg, store, session = setup(config_file)
    report = sync_child(cfg, "ania", session, store, now=NOW)
    assert report.records["messages"] == 1  # 124 znana z bazy v1, nowa tylko 123
    bodies = {t.key: t.body for t in store.texts("ania", C.message)}
    assert bodies == {"124": "stara treść", "123": bodies["123"]} and bodies["123"]
    store.close()


# --- polecenie fetch ------------------------------------------------------------


def test_cli_fetch_dry_run_and_real_run(
    fake_librus: FakeLibrus, config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config", str(config_file), "fetch", "--dry-run"]) == 0
    out = capsys.readouterr()
    assert "ania (dry-run):" in out.out and "Timetables=" in out.out
    assert main(["--config", str(config_file), "fetch"]) == 0
    out2 = capsys.readouterr()
    assert "notice: new=1" in out2.out and "pobrane treści wiadomości: 2" in out2.out
    everything = out.out + out.err + out2.out + out2.err
    assert not any(p in everything for p in PRIVATE)


def test_cli_fetch_reports_errors_with_exit_codes(
    fake_librus: FakeLibrus,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    args = ["--config", str(config_file), "fetch"]
    fake_librus.overrides["Notes"] = 500
    assert main(args) == 1  # błąd modułu
    fake_librus.overrides.clear()
    assert main([*args, "--child", "kuba"]) == 2
    with InstanceLock(load_config(config_file).data_dir):
        assert main(args) == 3
    fake_librus.accounts = {}
    assert main(args) == 1 and "ania: błąd (AuthError)" in capsys.readouterr().out
    monkeypatch.delenv("LIBRUS_ANIA_PASSWORD")
    assert main(args) == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text("children: [{id: a, name: A}]\npassword: tajne-haslo-z-pliku\n")
    assert main(["--config", str(bad), "fetch"]) == 2
    err = capsys.readouterr().err
    assert "wygląda na sekret" in err and "tajne-haslo-z-pliku" not in err


def test_broken_message_pages_are_isolated(
    fake_librus: FakeLibrus, config_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake_librus.broken_bodies = frozenset({"123"})
    cfg, store, session = setup(config_file)
    report = sync_child(cfg, "ania", session, store, now=NOW)
    assert report.ok and report.bodies == 1
    pending = store.texts_without_body("ania", C.message, cfg.fetch.message_body_attempts)
    assert [t.key for t in pending] == ["123"]  # spróbuje ponownie w kolejnym cyklu
    assert "wiadomość 123: treść niepobrana (SchemaError)" in caplog.text

    fake_librus.broken_list = True
    second = sync_child(cfg, "ania", session, store, now=LATER)
    assert second.failed == {"messages": "SchemaError"}
    assert store.texts("ania", C.notice)  # reszta cyklu zapisana
    store.close()


def test_old_messages_are_not_opened(fake_librus: FakeLibrus, config_file: Path) -> None:
    cfg, store, session = setup(config_file)
    cfg = cfg.model_copy(update={"fetch": cfg.fetch.model_copy(update={"message_body_days": 1})})
    report = sync_child(cfg, "ania", session, store, now=NOW)  # 123 z 5.11, 124 z 4.11
    assert report.bodies == 1 and fake_librus.calls["message_body"] == 1
    assert {t.key: t.body is None for t in store.texts("ania", C.message)} == {
        "123": False,
        "124": True,
    }
    store.close()
