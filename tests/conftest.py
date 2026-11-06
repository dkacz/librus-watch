"""Wspólna konfiguracja testów: poziom testu wynika z katalogu.

`tests/unit` dostaje znacznik `unit`, `tests/integration` znacznik
`integration`, `tests/e2e` znacznik `e2e`. Dzięki temu można uruchomić jeden
poziom, np. `uv run pytest -m integration`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_LEVELS = ("unit", "integration", "e2e")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Nadaje znacznik poziomu na podstawie katalogu testu.

    Args:
        items: Zebrane testy.
    """
    for item in items:
        parts = Path(str(item.fspath)).parts
        for level in _LEVELS:
            if level in parts:
                item.add_marker(getattr(pytest.mark, level))
