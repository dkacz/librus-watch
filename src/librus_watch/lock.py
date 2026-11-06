"""Blokada jednej instancji programu na katalog danych (R8).

Dwie kopie programu na tej samej bazie logowałyby się dwa razy i mogłyby
wysłać to samo powiadomienie dwukrotnie. Blokada to `flock` na pliku
`librus-watch.lock` w katalogu danych: zwalnia ją system przy końcu procesu,
także po awarii, więc nie zostaje „martwy” plik blokady.
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path
from types import TracebackType

log = logging.getLogger(__name__)


class AlreadyRunning(RuntimeError):
    """Inna instancja programu używa już tego katalogu danych."""


class InstanceLock:
    """Blokada katalogu danych używana jako menedżer kontekstu."""

    def __init__(self, data_dir: Path) -> None:
        """Przygotowuje blokadę (bez zakładania).

        Args:
            data_dir: Katalog danych.
        """
        self.path = data_dir / "librus-watch.lock"
        self._fd: int | None = None

    def __enter__(self) -> InstanceLock:
        """Zakłada blokadę bez czekania.

        Returns:
            Ta sama blokada.

        Raises:
            AlreadyRunning: Gdy blokadę trzyma inny proces.
        """
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunning(f"katalog danych {self.path.parent} jest już w użyciu") from None
        self._fd = fd
        log.debug("blokada instancji założona")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Zwalnia blokadę.

        Args:
            exc_type: Typ wyjątku z bloku (ignorowany).
            exc: Wyjątek z bloku (ignorowany).
            tb: Traceback (ignorowany).
        """
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
