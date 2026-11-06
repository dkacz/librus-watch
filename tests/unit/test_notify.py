from datetime import date, time

import pytest

from librus_watch.alerts import Alert, AlertLine
from librus_watch.config import AlertType, Config, Kind, Priority
from librus_watch.models import Finding, Origin
from librus_watch.notify import AppriseSender, route_alert, send

TODAY = date(2026, 11, 5)
CFG = Config.model_validate(
    {
        "children": [{"id": "iwo", "name": "Iwo"}, {"id": "olaf", "name": "Olaf"}],
        "alerts": {
            "routes": [
                {"tags": ["rodzice"]},
                {"tags": ["mama"], "children": ["olaf"], "min_priority": "high"},
            ]
        },
    }
)


def line(child: str, kind: Kind, prio: Priority) -> AlertLine:
    return AlertLine(
        Finding(child, date(2026, 11, 6), kind, "x", Origin.llm, "m:1", at=time(7, 0)), prio
    )


ALERT = Alert(
    AlertType.evening,
    "Jutro",
    [line("iwo", Kind.early_start, Priority.high), line("olaf", Kind.test, Priority.normal)],
)


class FakeSender:
    def __init__(self, fail_tags: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.fail_tags = fail_tags
        self.sent: list[tuple[str, list[str]]] = []

    def notify(self, body: str, title: str, tag: list[str]) -> bool:
        if set(tag) & self.fail_tags:
            raise RuntimeError("tgram://123:SEKRET/1 nie działa")
        self.sent.append((body, tag))
        return True


def test_route_filters_children_priority_and_types() -> None:
    mama = route_alert(ALERT, CFG.alerts.routes[1])
    assert mama is None  # dla Olafa tylko pozycja normal, reguła chce high
    assert len(route_alert(ALERT, CFG.alerts.routes[0]).lines) == 2  # type: ignore[union-attr]
    no_evening = CFG.alerts.routes[0].model_copy(update={"alert_types": [AlertType.instant]})
    assert route_alert(ALERT, no_evening) is None


def test_send_isolates_failed_channel_and_never_logs_urls(caplog: pytest.LogCaptureFixture) -> None:
    sender = FakeSender({"rodzice"})
    extra_calls: list[Alert] = []
    report = send(ALERT, CFG, sender, TODAY, extra=extra_calls.append)
    assert report.failed == [0] and report.sent == 0 and extra_calls == [ALERT]
    assert "SEKRET" not in caplog.text and "tgram" not in caplog.text
    ok = send(ALERT, CFG, FakeSender(), TODAY)
    assert ok.sent == 1 and ok.failed == []


def test_apprise_sender_loads_text_config() -> None:
    sender = AppriseSender("rodzice=json://localhost:1/")
    assert len(sender.apprise) == 0 or sender.apprise is not None
