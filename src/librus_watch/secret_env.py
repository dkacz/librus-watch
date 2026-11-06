"""Sekrety ze zmiennych środowiskowych albo plików oraz redakcja logów.

Sekret `NAZWA` można podać na dwa sposoby:

* `NAZWA_FILE=/run/secrets/nazwa`: ścieżka do pliku z wartością (zalecane,
  zgodne z sekretami Podmana i Dockera; wartość nie widnieje w `inspect`),
* `NAZWA=wartość`: zmienna środowiskowa (wygodne lokalnie).

Każda wczytana wartość trafia do rejestru w pamięci procesu. Filtr
`RedactingFilter` zamienia ją na `***` w każdym komunikacie logu, także
w tracebackach wyjątków zgłoszonych przez biblioteki (np. gdy biblioteka HTTP
wypisze URL z tokenem).
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from pathlib import Path
from types import TracebackType

from pydantic import SecretStr

_REGISTRY: set[str] = set()
_MIN_LEN = 4
_MIN_FRAGMENT_LEN = 12
REDACTED = "***"


class MissingSecretError(RuntimeError):
    """Wymagany sekret nie został podany ani w pliku, ani w zmiennej środowiskowej."""


def register(value: str) -> None:
    """Dodaje wartość do rejestru redagowanego w logach.

    Args:
        value: Wartość sekretu. Wartości krótsze niż 4 znaki są pomijane, żeby
            redakcja nie niszczyła zwykłych słów w logach.
    """
    if len(value) >= _MIN_LEN:
        _REGISTRY.add(value)


def register_all(value: str) -> None:
    """Rejestruje wartość, jej linie i dłuższe fragmenty między separatorami.

    Fragmenty są potrzebne, bo sekret bywa logowany w kawałkach: adresy Apprise
    mają token w środku (`tgram://TOKEN/CHAT`), a klucz sesji Librusa
    (`DZIENNIKSID:SDZIENNIKSID`) trafia do nagłówków jako dwa osobne ciasteczka.

    Args:
        value: Wartość sekretu.
    """
    register(value)
    for line in value.splitlines():
        register(line.strip())
        for part in re.split(r"[/:@?&=,;\s]", line):
            if len(part) >= _MIN_FRAGMENT_LEN:
                register(part)


def get_secret(name: str, *, required: bool = True) -> SecretStr | None:
    """Wczytuje sekret i rejestruje go do redakcji (`register_all`).

    `NAME_FILE` ma pierwszeństwo przed `NAME`. Ustawione `NAME_FILE`, którego
    nie da się odczytać, jest błędem także dla sekretu opcjonalnego, bo
    oznacza pomyłkę w konfiguracji, a nie świadomy brak.

    Args:
        name: Nazwa sekretu, np. `LIBRUS_ANIA_PASSWORD`.
        required: Czy brak sekretu jest błędem.

    Returns:
        Wartość opakowana w `SecretStr` (repr i str maskują treść) albo `None`,
        gdy sekret jest opcjonalny i nie został podany.

    Raises:
        MissingSecretError: Gdy `required=True`, a sekretu nie ma, albo gdy
            pliku z `NAME_FILE` nie da się odczytać.
    """
    path = os.environ.get(f"{name}_FILE")
    try:
        value = Path(path).read_text(encoding="utf-8").strip() if path else os.environ.get(name)
    except (OSError, UnicodeDecodeError) as e:
        raise MissingSecretError(
            f"nie można odczytać pliku sekretu {name}_FILE={path} ({type(e).__name__})"
        ) from None
    if not value:
        if required:
            raise MissingSecretError(f"brak sekretu {name} (ustaw {name}_FILE albo {name})")
        return None
    register_all(value)
    return SecretStr(value)


def redact(text: str) -> str:
    """Zamienia wszystkie zarejestrowane sekrety w tekście na `***`.

    Args:
        text: Dowolny tekst, np. sformatowany komunikat logu.

    Returns:
        Tekst bez wartości sekretów. Najdłuższe wartości są zamieniane jako
        pierwsze, żeby fragment nie zostawił reszty dłuższego sekretu.
    """
    for value in sorted(_REGISTRY, key=len, reverse=True):
        if value in text:
            text = text.replace(value, REDACTED)
    return text


class RedactingFilter(logging.Filter):
    """Filtr logowania usuwający sekrety z komunikatu i tracebacku."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Formatuje rekord, redaguje go i zastępuje treść wersją bezpieczną.

        Args:
            record: Rekord logu. Jest modyfikowany w miejscu: argumenty są
                wklejane do treści, a traceback dołączany jako tekst, więc żaden
                handler nie sformatuje już surowych danych.

        Returns:
            Zawsze `True` (rekord jest przepuszczany).
        """
        try:
            message = record.getMessage()
        except Exception:  # błąd w wywołaniu logu nie może wywrócić programu
            # argumenty są pomijane, bo mogą zawierać dane, których nie da się sformatować
            message = f"{record.msg!s} [błąd formatowania argumentów logu]"
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        if record.stack_info:
            message += "\n" + record.stack_info
            record.stack_info = None
        record.msg = redact(message)
        record.args = None
        return True


_log = logging.getLogger("librus_watch")


def _excepthook(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    """Kieruje nieobsłużony wyjątek do logu, żeby przeszedł przez redakcję.

    Args:
        exc_type: Typ wyjątku.
        exc: Wyjątek.
        tb: Traceback.
    """
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    _log.critical("nieobsłużony wyjątek, program kończy pracę", exc_info=(exc_type, exc, tb))


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    """Jak `_excepthook`, dla wyjątków z wątków.

    Args:
        args: Dane wyjątku z modułu `threading`.
    """
    if args.exc_value is None:
        return
    name = args.thread.name if args.thread else "?"
    _log.critical(
        "nieobsłużony wyjątek w wątku %s",
        name,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def setup_logging(level: str = "INFO") -> None:
    """Konfiguruje logowanie na stdout z redakcją sekretów.

    Logi idą na stdout, żeby rotacją zajął się journald albo silnik kontenerów
    (program nie tworzy rosnących plików logu). Biblioteki HTTP i Apprise są
    wyciszane do poziomu WARNING, bo na niższych poziomach logują pełne URL-e.
    Nieobsłużone wyjątki (także z wątków) trafiają do logu, a nie wprost na
    stderr, więc ich treść też przechodzi przez redakcję.

    Args:
        level: Poziom logowania (DEBUG, INFO, WARNING, ERROR).
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("urllib3", "requests", "httpx", "httpcore", "openai", "apprise"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
