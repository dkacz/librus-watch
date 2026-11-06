"""E4 na pełnym cyklu: dwa kolejne pobrania, zmiany w Librusie, wycofania."""

import copy
from datetime import date, datetime
from pathlib import Path

from fixtures.gateway import PAYLOADS
from fixtures.librus_http import FakeLibrus

from librus_watch.cli import main
from librus_watch.config import Config, ContentType, Kind, child_credentials, load_config
from librus_watch.models import Origin
from librus_watch.store import Store
from librus_watch.sync import login_child, sync_child

NOW = datetime(2026, 11, 6, 12, 0)
LATER = datetime(2026, 11, 6, 15, 0)


def cycle(cfg: Config, store: Store, now: datetime, dry_run: bool = False):  # type: ignore[no-untyped-def]
    session = login_child(cfg, store, "ania", child_credentials(cfg.children[0]), now)
    return sync_child(cfg, "ania", session, store, now=now, dry_run=dry_run)


def found(report) -> set[tuple[date, Kind]]:  # type: ignore[no-untyped-def]
    return {(f.day, f.kind) for f in report.findings}


def test_first_cycle_finds_deviations_second_is_quiet(
    fake_librus: FakeLibrus, config_file: Path
) -> None:
    cfg = load_config(config_file)
    store = Store(cfg.data_dir)
    first = cycle(cfg, store, NOW)
    assert found(first) == {
        (date(2026, 11, 10), Kind.day_off),  # jedyna lekcja wtorku odwołana
        (date(2026, 11, 12), Kind.cancel),  # zmiana planu i odwołane zajęcia (dwie tożsamości)
        (date(2026, 11, 13), Kind.substitution),
        (date(2026, 11, 18), Kind.trip),  # terminarz i moduł wycieczek: jedna pozycja
        (date(2026, 11, 19), Kind.meeting),
        (date(2026, 11, 23), Kind.homework),
    }
    assert sum(1 for f in first.findings if f.kind is Kind.cancel) == 2
    assert len([f for f in first.findings if f.kind is Kind.trip]) == 1
    second = cycle(cfg, store, LATER)
    assert second.findings == [] and second.retracted == 0
    store.close()


def test_changes_in_librus_produce_change_retraction_and_removal(
    fake_librus: FakeLibrus, config_file: Path
) -> None:
    cfg = load_config(config_file)
    store = Store(cfg.data_dir)
    cycle(cfg, store, NOW)

    agenda = copy.deepcopy(PAYLOADS["HomeWorks"])
    agenda["HomeWorks"][0]["Date"] = "2026-11-20"  # wycieczka przesunięta
    agenda["HomeWorks"].append(
        {"Id": 12, "Category": {"Id": 4}, "Content": "x", "Date": "2026-11-25"}
    )
    subs = copy.deepcopy(PAYLOADS["Calendars/Substitutions"])
    subs["Substitutions"] = subs["Substitutions"][:1]  # zastępstwo z 13.11 zniknęło
    fake_librus.payloads = {
        "HomeWorks": agenda,
        "Calendars/Substitutions": subs,
        "HomeWorkAssignments": {"HomeWorkAssignments": []},
    }
    report = cycle(cfg, store, LATER)
    assert found(report) == {
        (date(2026, 11, 20), Kind.change),
        (date(2026, 11, 25), Kind.trip),
        (date(2026, 11, 23), Kind.cancel),  # zadanie usunięte z kompletnie pobranego modułu
    }
    change = next(f for f in report.findings if f.kind is Kind.change)
    assert change.before == "18.11 07:30" and store.previous_version(change) is not None
    assert report.retracted == 1 and report.removed == 1
    current = store.child_findings("ania", {Origin.plan_change}, NOW.date(), date(2026, 11, 30))
    assert [f.kind for f in current] == [Kind.cancel]
    assert store.entries("ania", ContentType.homework) == []
    store.close()


def test_failed_modules_neither_remove_nor_retract(
    fake_librus: FakeLibrus, config_file: Path
) -> None:
    cfg = load_config(config_file)
    store = Store(cfg.data_dir)
    cycle(cfg, store, NOW)
    fake_librus.overrides.update({"HomeWorkAssignments": 503, "Calendars/Substitutions": 503})
    report = cycle(cfg, store, LATER)
    assert report.removed == 0 and report.retracted == 0
    assert store.entries("ania", ContentType.homework)
    store.close()


def test_entry_outside_fetched_range_is_not_removed(
    fake_librus: FakeLibrus, config_file: Path
) -> None:
    cfg = load_config(config_file)
    store = Store(cfg.data_dir)
    far = {
        "data": [
            {
                "identifier": "LID-OAR-9",
                "status": "ACTUAL",
                "date": "2026-12-20",
                "startTime": "14:00",
                "title": "Chór",
            }
        ]
    }
    fake_librus.payloads = {"Timetables/OtherActivitiesRegister": far}
    cycle(cfg, store, NOW)
    fake_librus.payloads = {"Timetables/OtherActivitiesRegister": {"data": []}}
    report = cycle(cfg, store, LATER)
    assert report.removed == 0
    assert [e.key for e in store.entries("ania", ContentType.activity)] == ["LID-OAR-9"]
    store.close()


def test_dry_run_lists_findings_without_storing(
    fake_librus: FakeLibrus,
    config_file: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    assert main(["--config", str(config_file), "fetch", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "odstępstwa: " in out and "day_off (timetable)" in out
    assert "Muzeum" not in out and "Anna Testowa" not in out
    cfg = load_config(config_file)
    store = Store(cfg.data_dir)
    assert store.current_findings(date(2026, 1, 1), date(2027, 1, 1)) == []
    store.close()
