from collections.abc import Iterator
from datetime import date, datetime, time
from pathlib import Path

import pytest

from librus_watch.alerts import compose, mark_alert_sent, render
from librus_watch.config import AlertType, Config, Kind
from librus_watch.models import Finding, Origin
from librus_watch.store import Store

EVENING = datetime(2026, 11, 5, 19, 0)
NIGHT = datetime(2026, 11, 5, 23, 0)
MORNING = datetime(2026, 11, 6, 6, 30)
TOMORROW = date(2026, 11, 6)
CFG = Config.model_validate(
    {"children": [{"id": "iwo", "name": "Iwo"}, {"id": "olaf", "name": "Olaf"}]}
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path)
    yield s
    s.close()


def f(
    kind: Kind = Kind.early_start,
    day: date = TOMORROW,
    at: time | None = time(7, 30),
    child: str = "iwo",
    **kw: object,
) -> Finding:
    base: dict[str, object] = {
        "child": child,
        "day": day,
        "kind": kind,
        "what": "zbiórka",
        "origin": Origin.llm,
        "source_key": "message:1",
        "at": at,
        "quote": "jutro zbiórka o 7:30",
    }
    base.update(kw)
    return Finding(**base)  # type: ignore[arg-type]


def test_evening_is_silent_on_a_normal_day(store: Store) -> None:
    assert compose(CFG, store, EVENING, AlertType.evening) is None
    always = CFG.model_copy(
        update={
            "alerts": CFG.alerts.model_copy(
                update={"evening": CFG.alerts.evening.model_copy(update={"mode": "always"})}
            )
        }
    )
    alert = compose(always, store, EVENING, AlertType.evening)
    assert alert is not None and render(alert, always, EVENING.date()).endswith("bez zmian")


def test_evening_groups_children_and_includes_near_deadlines(store: Store) -> None:
    store.add_finding(f(), EVENING)
    store.add_finding(
        f(
            Kind.consent,
            day=date(2026, 11, 13),
            at=None,
            child="olaf",
            what="zgoda na wyjście",
            deadline=date(2026, 11, 7),
            quote="zgoda do soboty",
        ),
        EVENING,
    )
    store.add_finding(f(Kind.test, day=date(2026, 11, 12), at=None, source_key="x"), EVENING)
    alert = compose(CFG, store, EVENING, AlertType.evening)
    assert alert is not None and len(alert.lines) == 2
    text = render(alert, CFG, EVENING.date())
    assert text.startswith("Jutro: jutro (piątek 6.11)")
    assert "Iwo, jutro (piątek 6.11):\n• wcześniej do szkoły: zbiórka 07:30" in text
    assert "Olaf, sobota 7.11:" in text and "„jutro zbiórka o 7:30”" in text


def test_no_repeats_and_change_is_reported_as_zmiana(store: Store) -> None:
    store.add_finding(f(), EVENING)
    alert = compose(CFG, store, EVENING, AlertType.evening)
    assert alert is not None
    mark_alert_sent(alert, store, EVENING)
    assert compose(CFG, store, EVENING, AlertType.evening) is None
    store.add_finding(f(at=time(7, 15)), MORNING)
    morning = compose(CFG, store, MORNING, AlertType.morning)
    assert morning is not None
    text = render(morning, CFG, MORNING.date())
    assert "ZMIANA: wcześniej do szkoły: zbiórka 07:15; było: 6.11 07:30 zbiórka" in text


def test_instant_respects_quiet_hours_and_priority(store: Store) -> None:
    store.add_finding(f(), EVENING)
    store.add_finding(f(Kind.homework, at=None, what="zeszyt", source_key="hw"), EVENING)
    assert compose(CFG, store, NIGHT, AlertType.instant) is None  # godziny ciszy
    alert = compose(CFG, store, EVENING, AlertType.instant)
    assert alert is not None and [ln.finding.kind for ln in alert.lines] == [Kind.early_start]


def test_morning_only_today(store: Store) -> None:
    store.add_finding(f(day=date(2026, 11, 9)), EVENING)
    assert compose(CFG, store, MORNING, AlertType.morning) is None
    store.add_finding(
        f(
            Kind.cancel,
            day=TOMORROW,
            at=None,
            what="odwołana 1. lekcja",
            origin=Origin.plan_change,
            source_key="7",
        ),
        MORNING,
    )
    alert = compose(CFG, store, MORNING, AlertType.morning)
    assert alert is not None and render(alert, CFG, MORNING.date()).startswith("Dziś: dziś")
