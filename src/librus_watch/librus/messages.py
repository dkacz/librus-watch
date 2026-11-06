"""Wiadomości odebrane: lista i treść (przez strony Synergii w `librus-apix`).

JSON-owe API nie udostępnia wiadomości, więc ten moduł jako jedyny korzysta
ze stron HTML. Lista jest przeglądana od najnowszych i kończy się na pierwszej
znanej wiadomości (albo na limicie stron). Treść pobieramy raz na wiadomość,
bo otwarcie wiadomości może oznaczyć ją w Librusie jako przeczytaną
(`docs/librus-apix-notes.md`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

import requests
from librus_apix import messages as apix_messages
from librus_apix.client import Client
from librus_apix.exceptions import TokenError

from librus_watch.errors import AuthError, SchemaError, TransientError
from librus_watch.models import MessageRef, MessageSource

log = logging.getLogger(__name__)
_NETWORK = (requests.Timeout, requests.ConnectionError)


def _call[T](what: str, fn: Callable[[], T]) -> T:
    """Wywołuje funkcję `librus-apix` i zamienia jej wyjątki na klasy programu.

    Args:
        what: Opis operacji do komunikatu.
        fn: Wywołanie biblioteki.

    Returns:
        Wynik wywołania.

    Raises:
        TransientError: Limit czasu albo błąd sieci.
        AuthError: Sesja wygasła („Brak dostępu”).
        SchemaError: Strona o nieoczekiwanej strukturze.
    """
    try:
        return fn()
    except _NETWORK as e:
        raise TransientError(f"{what}: {type(e).__name__}") from None
    except TokenError:
        raise AuthError(f"{what}: sesja wygasła") from None
    except Exception as e:  # parser HTML: każdy inny błąd to zmiana strony
        raise SchemaError(f"{what}: {type(e).__name__}") from None


def list_new(
    client: Client,
    child_id: str,
    is_known: Callable[[str], bool],
    max_pages: int,
) -> list[MessageRef]:
    """Zwraca wiadomości odebrane, których program jeszcze nie zna.

    Args:
        client: Zalogowany klient `librus-apix` (z `LibrusSession.client`).
        child_id: Id dziecka.
        is_known: Czy wiadomość o danym kluczu jest już w bazie.
        max_pages: Bezpiecznik: maksymalna liczba przeglądanych stron.

    Returns:
        Nowe wiadomości od najnowszej, bez treści.
    """
    last = _call("wiadomości", lambda: apix_messages.get_max_page_number(client))
    out: list[MessageRef] = []
    for page in range(min(last + 1, max_pages)):
        items = _call("wiadomości", lambda p=page: apix_messages.get_received(client, p))  # type: ignore[misc]
        for m in items:
            if not m.href:
                continue
            if is_known(m.href):
                log.debug("wiadomości: %d nowych (stop na znanej)", len(out))
                return out
            out.append(
                MessageRef(
                    child=child_id,
                    source=MessageSource.message,
                    key=m.href,
                    sent=date.fromisoformat(m.date[:10]),
                    author=m.author.strip(),
                    title=m.title.strip(),
                )
            )
    log.debug("wiadomości: %d nowych", len(out))
    return out


def fetch_body(client: Client, message: MessageRef) -> str:
    """Pobiera treść jednej wiadomości.

    Args:
        client: Zalogowany klient `librus-apix`.
        message: Wiadomość z `list_new`.

    Returns:
        Treść wiadomości.
    """
    data = _call("treść wiadomości", lambda: apix_messages.message_content(client, message.key))
    return str(data.content).strip()
