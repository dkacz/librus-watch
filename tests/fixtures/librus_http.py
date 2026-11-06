"""Atrapa Librusa na poziomie transportu HTTP (adapter `requests`).

Prawdziwy kod `librus-apix` (logowanie, token API, strony wiadomości) i nasza
sesja wykonują żądania jak zwykle; zamiast sieci odpowiada ta atrapa. Ciasteczka
są dopisywane do słoika sesji tak, jak zrobiłby to nagłówek `Set-Cookie`.
Dane są wymyślone (kształty z rozpoznania).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from fixtures.gateway import payload

GATEWAY = "/gateway/api/2.0/"
MESSAGES = {
    "123": (
        "Anna Testowa [Wychowawca]",
        "Wycieczka",
        "2026-11-05 10:00:00",
        "Zbiórka 7:30 pod szkołą, proszę o zgodę.",
    ),
    "124": (
        "Anna Testowa [Wychowawca]",
        "Zebranie",
        "2026-11-04 12:00:00",
        "Zebranie w czwartek o 17:00.",
    ),
}


def _messages_list(ids: list[str]) -> str:
    rows = "".join(
        f"<tr class='line0'><td></td><td></td><td><a href='/wiadomosci/1/5/{i}/f0'>"
        f"{MESSAGES[i][0]}</a></td><td style='font-weight: bold'>{MESSAGES[i][1]}</td>"
        f"<td>{MESSAGES[i][2]}</td><td></td></tr>"
        for i in ids
    )
    return (
        "<html><div class='pagination'><span>Strona 1z1</span></div>"
        f"<table class='decorated stretch'><tbody>{rows}</tbody></table></html>"
    )


def _message_page(i: str) -> str:
    author, title, date, body = MESSAGES[i]
    return (
        "<html><table class='stretch'>"
        f"<tr><td class='left'>{author}</td></tr><tr><td class='left'>{title}</td></tr>"
        f"<tr><td class='left'>{date}</td></tr></table>"
        f"<div class='container-message-content'>{body}</div></html>"
    )


@dataclass
class FakeLibrus:
    """Stan atrapy: konta, nadpisania modułów i licznik żądań."""

    accounts: dict[str, str] = field(default_factory=lambda: {"1234567": "haslo-rodzica"})
    overrides: dict[str, int] = field(default_factory=dict)
    """Moduł API -> kod HTTP (np. {"Notes": 403})."""
    message_ids: list[str] = field(default_factory=lambda: ["123", "124"])
    expire_api: bool = False
    broken_list: bool = False
    broken_bodies: frozenset[str] = frozenset()
    calls: Counter[str] = field(default_factory=Counter)
    gateway_paths: list[str] = field(default_factory=list)

    def adapter(self, session: requests.Session) -> BaseAdapter:
        """Adapter do zamontowania w sesji `requests` klienta."""
        return _Adapter(self, session)


class _Adapter(BaseAdapter):
    def __init__(self, fake: FakeLibrus, session: requests.Session) -> None:
        super().__init__()
        self.fake = fake
        self.session = session

    def close(self) -> None:
        pass

    def _response(
        self,
        request: requests.PreparedRequest,
        status: int,
        body: str | bytes = b"",
        content_type: str = "text/html",
    ) -> requests.Response:
        r = requests.Response()
        r.status_code = status
        r._content = body.encode() if isinstance(body, str) else body
        r.headers = CaseInsensitiveDict({"Content-Type": content_type})
        r.encoding = "utf-8"
        r.url = request.url or ""
        r.request = request
        return r

    def _json(self, request: requests.PreparedRequest, status: int, data: Any) -> requests.Response:
        return self._response(request, status, json.dumps(data), "application/json")

    def send(self, request: requests.PreparedRequest, **_kwargs: Any) -> requests.Response:  # type: ignore[override]
        url = urlsplit(request.url or "")
        host, path, method = url.netloc, url.path, request.method
        cookies = request.headers.get("Cookie", "")
        fake = self.fake

        if host == "api.librus.pl":
            if path.startswith("/OAuth/Authorization") and method == "POST":
                fake.calls["login"] += 1
                form = parse_qs(str(request.body or ""))
                user, password = form.get("login", [""])[0], form.get("pass", [""])[0]
                if fake.accounts.get(user) != password:
                    return self._json(
                        request,
                        200,
                        {
                            "status": "error",
                            "errors": [{"message": f"Nieprawidłowy login lub hasło {user}"}],
                        },
                    )
                self.session.cookies.set("DZIENNIKSID", f"L01~dz{user}sesja0123456789")
                self.session.cookies.set("SDZIENNIKSID", f"sdz{user}sesja0123456789")
                return self._json(request, 200, {"status": "ok", "goTo": "/OAuth/2FA"})
            return self._response(request, 200, "{}", "application/json")

        if host != "synergia.librus.pl":
            return self._response(request, 404)
        if path.startswith("/loguj/"):
            return self._response(request, 200)
        if path == "/refreshToken":
            fake.calls["refresh"] += 1
            r = self._response(request, 200)
            r.cookies.set("oauth_token", "oauthtoken0123456789abcdef")
            self.session.cookies.set("oauth_token", "oauthtoken0123456789abcdef")
            return r
        if path.startswith(GATEWAY):
            module = path[len(GATEWAY) :]
            fake.calls["gateway"] += 1
            fake.gateway_paths.append(module)
            if fake.expire_api or "oauth_token=" not in cookies:
                return self._json(request, 401, {"Status": "Error", "Code": "Unauthorized"})
            if module in fake.overrides:
                return self._json(request, fake.overrides[module], {"Status": "Error"})
            query = f"?{url.query}" if url.query else ""
            try:
                return self._json(request, 200, payload(module + query))
            except KeyError:
                return self._json(request, 404, {"Status": "Error"})
        if path == "/wiadomosci/1/5":
            fake.calls["messages_" + (method or "").lower()] += 1
            if fake.broken_list and method == "POST":
                return self._response(request, 200, "<html>przebudowana strona</html>")
            return self._response(request, 200, _messages_list(fake.message_ids))
        if path.startswith("/wiadomosci/1/5/"):
            fake.calls["message_body"] += 1
            key = path.rsplit("/", 1)[1]
            if key in fake.broken_bodies:
                return self._response(request, 200, "<html>inna strona</html>")
            return self._response(request, 200, _message_page(key))
        return self._response(request, 404)
