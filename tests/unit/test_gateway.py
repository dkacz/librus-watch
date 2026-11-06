from datetime import date
from typing import Any

import pytest
from fixtures.gateway import payload

from librus_watch.errors import AuthError, ModuleUnavailable, SchemaError, TransientError
from librus_watch.librus.gateway import MODULES, fetch_all, ref_id

TODAY = date(2026, 11, 6)  # piątek; tygodnie planu od 2026-11-02


class FakeSource:
    def __init__(self, overrides: dict[str, Any] | None = None) -> None:
        self.overrides = overrides or {}
        self.paths: list[str] = []

    def get_json(self, path: str) -> Any:
        self.paths.append(path)
        override = self.overrides.get(path.partition("?")[0])
        if isinstance(override, BaseException):
            raise override
        return override if override is not None else payload(path)


def test_every_module_parses_its_contract_payload() -> None:
    data = fetch_all(FakeSource(), TODAY)
    assert data.complete and data.unavailable == []
    assert set(data.records) == {m.name for m in MODULES}
    assert len(data.records["Me"]) == 1
    assert data.records["HomeWorkAssignments"][0]["DueDate"] == "2026-11-23"
    assert data.records["GradeComments"][0]["Text"].startswith("Poprawa")
    assert data.records["SchoolTrips"][0]["destination"] == "Muzeum"
    assert data.records["OtherActivities"][0]["status"] == "CANCELED"


def test_timetable_weeks_are_fetched_and_flattened() -> None:
    source = FakeSource()
    lessons = fetch_all(source, TODAY, weeks=2).records["Timetables"]
    assert [p for p in source.paths if p.startswith("Timetables?")] == [
        "Timetables?weekStart=2026-11-02",
        "Timetables?weekStart=2026-11-09",
    ]
    week1 = [ls for ls in lessons if ls["_day"] == "2026-11-02"]
    assert [(ls["_slot"], ls["LessonNo"]) for ls in week1] == [(1, "1"), (2, "2"), (2, "2")]
    assert any(ls["IsCanceled"] for ls in lessons)


def test_other_activities_range_covers_timetable_weeks() -> None:
    source = FakeSource()
    fetch_all(source, TODAY, weeks=2)
    assert any("dateFrom=2026-11-02&dateTo=2026-11-15" in p for p in source.paths)


@pytest.mark.parametrize(
    ("override", "where"),
    [
        (ModuleUnavailable("Notes: 403"), "unavailable"),
        (TransientError("Notes: HTTP 503"), "failed"),
        (SchemaError("Notes: odpowiedź nie jest JSON"), "failed"),
        ({"Uwagi": []}, "failed"),  # brak klucza głównego
        ({"Notes": {"a": 1}}, "failed"),  # zły typ
        ({"Notes": [1, 2]}, "failed"),  # lista nie-obiektów
    ],
)
def test_one_module_failure_does_not_stop_others(override: Any, where: str) -> None:
    data = fetch_all(FakeSource({"Notes": override}), TODAY)
    assert "Notes" not in data.records
    assert len(data.records) == len(MODULES) - 1
    if where == "unavailable":
        assert data.unavailable == ["Notes"] and data.complete
    else:
        assert list(data.failed) == ["Notes"] and not data.complete


def test_bad_timetable_shape_is_schema_error() -> None:
    data = fetch_all(FakeSource({"Timetables": {"Timetable": {"2026-11-02": [["x"]]}}}), TODAY)
    assert data.failed == {"Timetables": "SchemaError"}


def test_auth_error_stops_everything() -> None:
    with pytest.raises(AuthError):
        fetch_all(FakeSource({"Classes": AuthError("Classes: sesja wygasła (401)")}), TODAY)


def test_skip_and_incomplete_pagination(caplog: pytest.LogCaptureFixture) -> None:
    users = {"Users": [{"Id": 1}], "pagination": {"limit": 1, "page": 1, "total": 3}}
    source = FakeSource({"Users": users})
    data = fetch_all(source, TODAY, skip=frozenset({"Grades", "GradeComments"}))
    assert "Grades" not in data.records and not any(p.startswith("Grades") for p in source.paths)
    assert "Users: niepełna lista" in caplog.text


def test_failure_logs_have_no_payload(caplog: pytest.LogCaptureFixture) -> None:
    fetch_all(FakeSource({"SchoolNotices": {"Ogloszenia": [{"Content": "Jan Kowalski"}]}}), TODAY)
    assert "SchoolNotices" in caplog.text and "Kowalski" not in caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"Id": 42, "Url": "x"}, "42"),
        ("41999", "41999"),
        (7, "7"),
        (None, None),
        ({}, None),
        ("", None),
    ],
)
def test_ref_id(value: Any, expected: str | None) -> None:
    assert ref_id(value) == expected
