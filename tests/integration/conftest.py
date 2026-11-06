from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fixtures.librus_http import FakeLibrus
from librus_apix.client import Client

from librus_watch import secret_env
from librus_watch.librus import session as session_module

CONFIG = """
timezone: Europe/Warsaw
data_dir: {data}
children:
  - id: ania
    name: Ania
llm:
  enabled: false
"""


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    secret_env._REGISTRY.clear()
    yield
    secret_env._REGISTRY.clear()


@pytest.fixture
def fake_librus(monkeypatch: pytest.MonkeyPatch) -> FakeLibrus:
    """Każdy nowy klient `librus-apix` rozmawia z atrapą zamiast z siecią."""
    fake = FakeLibrus()
    original = session_module.new_client

    def new_client(timeout: float) -> Client:
        client = original(timeout)
        client._session.mount("https://", fake.adapter(client._session))
        return client

    monkeypatch.setattr(session_module, "new_client", new_client)
    return fake


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG.format(data=tmp_path / "data"))
    monkeypatch.setenv("LIBRUS_ANIA_LOGIN", "1234567")
    monkeypatch.setenv("LIBRUS_ANIA_PASSWORD", "haslo-rodzica")
    return path
