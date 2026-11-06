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
from pathlib import Path

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


def get_secret(name: str, *, required: bool = True) -> SecretStr | None:
    """Wczytuje sekret i rejestruje go do redakcji.

    `NAME_FILE` ma pierwszeństwo przed `NAME`. Oprócz całej wartości
    rejestrowane są też jej linie oraz dłuższe fragmenty między separatorami
    URL, bo adresy Apprise zawierają tokeny w środku (`tgram://TOKEN/CHAT`).

    Args:
        name: Nazwa sekretu, np. `LIBRUS_ANIA_PASSWORD`.
        required: Czy brak sekretu jest błędem.

    Returns:
        Wartość opakowana w `SecretStr` (repr i str maskują treść) albo `None`,
        gdy sekret jest opcjonalny i nie został podany.

    Raises:
        MissingSecretError: Gdy `required=True`, a sekretu nie ma.
    """
    path = os.environ.get(f"{name}_FILE")
    value = Path(path).read_text(encoding="utf-8").strip() if path else os.environ.get(name)
    if not value:
        if required:
            raise MissingSecretError(f"brak sekretu {name} (ustaw {name}_FILE albo {name})")
        return None
    register(value)
    for line in value.splitlines():
        register(line.strip())
        for part in re.split(r"[/:@?&=,\s]", line):
            if len(part) >= _MIN_FRAGMENT_LEN:
                register(part)
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
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        record.msg = redact(message)
        record.args = None
        return True


def setup_logging(level: str = "INFO") -> None:
    """Konfiguruje logowanie na stdout z redakcją sekretów.

    Logi idą na stdout, żeby rotacją zajął się journald albo silnik kontenerów
    (program nie tworzy rosnących plików logu). Biblioteki HTTP i Apprise są
    wyciszane do poziomu WARNING, bo na niższych poziomach logują pełne URL-e.

    Args:
        level: Poziom logowania (DEBUG, INFO, WARNING, ERROR).
    """
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("urllib3", "requests", "httpx", "httpcore", "openai", "apprise"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
