"""Sesja Synergii jednego dziecka: logowanie, token API i żądania JSON.

Każda sesja ma własnego klienta `librus-apix` z własnym pojemnikiem na
ciasteczka (domyślny w bibliotece jest współdzielony przez wszystkie klienty,
ADR-006) i limitem czasu na każde żądanie. Ciasteczka sesji i token API są
rejestrowane do redakcji logów zaraz po otrzymaniu.

Wyjątki biblioteki i `requests` są zamieniane na klasy z `librus_watch.errors`
bez przyczyny (`from None`), bo komunikaty biblioteki potrafią zawierać treść
strony Librusa.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import Any

import requests
from librus_apix.client import Client, Token
from librus_apix.exceptions import MaintananceError
from requests.cookies import RequestsCookieJar

from librus_watch.config import Credentials
from librus_watch.errors import AuthError, ModuleUnavailable, SchemaError, TransientError
from librus_watch.secret_env import register_all

API_URL = "https://synergia.librus.pl/gateway/api/2.0/"
_NETWORK = (requests.Timeout, requests.ConnectionError)
log = logging.getLogger(__name__)


def new_client(timeout: float) -> Client:
    """Tworzy klienta `librus-apix` z własnym pojemnikiem na ciasteczka i limitem czasu.

    Args:
        timeout: Limit czasu pojedynczego żądania w sekundach.

    Returns:
        Niezalogowany klient.
    """
    client = Client(Token(), extra_cookies=RequestsCookieJar())
    client._session.request = partial(client._session.request, timeout=timeout)
    return client


def module_name(path: str) -> str:
    """Nazwa modułu API do komunikatów: ścieżka bez parametrów.

    Args:
        path: Ścieżka względem `API_URL`, np. `Timetables?weekStart=2026-11-09`.

    Returns:
        Np. `Timetables`.
    """
    return path.split("?", 1)[0]


class LibrusSession:
    """Zalogowana sesja jednego dziecka."""

    def __init__(self, child_id: str, client: Client) -> None:
        """Opakowuje zalogowanego klienta; zwykle tworzona przez `login`.

        Args:
            child_id: Id dziecka z konfiguracji (do logów).
            client: Zalogowany klient `librus-apix`.
        """
        self.child_id = child_id
        self.client = client

    @classmethod
    def login(
        cls,
        child_id: str,
        creds: Credentials,
        timeout: float,
        client_factory: Callable[[float], Client] = new_client,
    ) -> LibrusSession:
        """Loguje się do Synergii i pobiera token JSON-owego API.

        Każdy nieznany błąd logowania jest traktowany jak zły login (ADR-007),
        żeby program nie ponawiał prób na koncie, którego nie rozumie.

        Args:
            child_id: Id dziecka z konfiguracji.
            creds: Login rodzica i hasło.
            timeout: Limit czasu żądań w sekundach.
            client_factory: Tworzy klienta (podmieniane w testach).

        Returns:
            Zalogowana sesja z odświeżonym tokenem API.

        Raises:
            TransientError: Limit czasu, błąd sieci albo prace serwisowe.
            AuthError: Logowanie odrzucone lub nieudane z innego powodu.
        """
        client = client_factory(timeout)
        try:
            token = client.get_token(
                creds.login.get_secret_value(), creds.password.get_secret_value()
            )
        except MaintananceError:
            raise TransientError("logowanie: prace serwisowe Librusa") from None
        except _NETWORK as e:
            raise TransientError(f"logowanie: {type(e).__name__}") from None
        except Exception as e:  # nieznany błąd logowania = zły login (ADR-007)
            raise AuthError(f"logowanie odrzucone ({type(e).__name__})") from None
        register_all(token.API_Key)
        session = cls(child_id, client)
        session.refresh_api_token()
        log.info("zalogowano: dziecko %s", child_id)
        return session

    def refresh_api_token(self) -> None:
        """Odświeża ciasteczko `oauth_token`, którego wymaga JSON-owe API.

        Raises:
            TransientError: Limit czasu albo błąd sieci.
            AuthError: Librus odmówił tokenu (np. sesja wygasła).
        """
        try:
            oauth = self.client.refresh_oauth()
        except _NETWORK as e:
            raise TransientError(f"token API: {type(e).__name__}") from None
        except Exception as e:
            raise AuthError(f"token API odrzucony ({type(e).__name__})") from None
        if not oauth:
            raise AuthError("token API: brak oauth_token w odpowiedzi")
        register_all(oauth)

    def get_json(self, path: str) -> Any:
        """Pobiera moduł JSON-owego API.

        Args:
            path: Ścieżka względem `API_URL`, z parametrami.

        Returns:
            Zdekodowana odpowiedź JSON.

        Raises:
            TransientError: Limit czasu, sieć, 429 albo 5xx.
            AuthError: 401 (sesja wygasła).
            ModuleUnavailable: 403 albo 404.
            SchemaError: Inny kod albo odpowiedź, która nie jest JSON.
        """
        name = module_name(path)
        try:
            response = self.client.get(API_URL + path)
        except _NETWORK as e:
            raise TransientError(f"{name}: {type(e).__name__}") from None
        status = response.status_code
        if status == 401:
            raise AuthError(f"{name}: sesja wygasła (401)")
        if status in (403, 404):
            raise ModuleUnavailable(f"{name}: {status}")
        if status == 429 or status >= 500:
            raise TransientError(f"{name}: HTTP {status}")
        if status != 200:
            raise SchemaError(f"{name}: nieoczekiwany kod HTTP {status}")
        try:
            return response.json()
        except ValueError:
            raise SchemaError(f"{name}: odpowiedź nie jest JSON") from None
