from datetime import date
from typing import Any

import pytest
import requests
from librus_apix.exceptions import ParseError, TokenError
from librus_apix.messages import Message, MessageData

from librus_watch.errors import AuthError, SchemaError, TransientError
from librus_watch.librus import messages
from librus_watch.models import MessageRef, MessageSource

CLIENT: Any = object()


def msg(href: str, day: str = "2026-11-05") -> Message:
    return Message(
        author=" Anna Testowa (Anna Testowa) [Wychowawca] ",
        title=" Wycieczka ",
        date=f"{day} 10:00:00",
        href=href,
        unread=True,
        has_attachment=False,
    )


@pytest.fixture
def pages(monkeypatch: pytest.MonkeyPatch) -> list[list[Message]]:
    data: list[list[Message]] = [[msg("30"), msg("29"), msg("")], [msg("28"), msg("27")]]
    monkeypatch.setattr(messages.apix_messages, "get_max_page_number", lambda _c: len(data) - 1)
    monkeypatch.setattr(messages.apix_messages, "get_received", lambda _c, p: data[p])
    return data


def test_list_new_stops_on_first_known_message(pages: list[list[Message]]) -> None:
    got = messages.list_new(CLIENT, "ania", lambda key: key == "28", max_pages=5)
    assert [m.key for m in got] == ["30", "29"]
    assert got[0] == MessageRef(
        child="ania",
        source=MessageSource.message,
        key="30",
        sent=date(2026, 11, 5),
        author="Anna Testowa (Anna Testowa) [Wychowawca]",
        title="Wycieczka",
    )


def test_list_new_respects_page_limit(pages: list[list[Message]]) -> None:
    assert [m.key for m in messages.list_new(CLIENT, "ania", lambda _k: False, 1)] == ["30", "29"]
    assert len(messages.list_new(CLIENT, "ania", lambda _k: False, 5)) == 4


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TokenError("Malformed or expired token."), AuthError),
        (ParseError("Error in parsing messages."), SchemaError),
        (IndexError("list index out of range"), SchemaError),
        (requests.Timeout(), TransientError),
    ],
)
def test_library_errors_are_classified(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, expected: type[Exception]
) -> None:
    def boom(*_a: Any) -> Any:
        raise error

    monkeypatch.setattr(messages.apix_messages, "get_max_page_number", boom)
    monkeypatch.setattr(messages.apix_messages, "message_content", boom)
    with pytest.raises(expected):
        messages.list_new(CLIENT, "ania", lambda _k: False, 5)
    ref = MessageRef("ania", MessageSource.message, "30", date(2026, 11, 5), "a", "t")
    with pytest.raises(expected) as exc:
        messages.fetch_body(CLIENT, ref)
    assert exc.value.__cause__ is None


def test_fetch_body_strips_content(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def content(_c: Any, href: str) -> MessageData:
        seen.append(href)
        return MessageData("a", "t", "\n  Zbiórka 7:30.  \n", "2026-11-05")

    monkeypatch.setattr(messages.apix_messages, "message_content", content)
    ref = MessageRef("ania", MessageSource.message, "30", date(2026, 11, 5), "a", "t")
    assert messages.fetch_body(CLIENT, ref) == "Zbiórka 7:30."
    assert seen == ["30"]
