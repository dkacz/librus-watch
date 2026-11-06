import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from librus_watch import secret_env
from librus_watch.secret_env import (
    REDACTED,
    MissingSecretError,
    RedactingFilter,
    get_secret,
    redact,
    setup_logging,
)

TOKEN = "123456789:AAHfakeTelegramTokenForTests"
APPRISE = f"rodzice=tgram://{TOKEN}/-1001234567890"


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    secret_env._REGISTRY.clear()
    for name in ("X", "X_FILE", "APPRISE_CONFIG", "APPRISE_CONFIG_FILE"):
        monkeypatch.delenv(name, raising=False)
    yield
    secret_env._REGISTRY.clear()


def test_file_has_priority_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "x"
    f.write_text("z-pliku-secret\n")
    monkeypatch.setenv("X_FILE", str(f))
    monkeypatch.setenv("X", "ze-zmiennej")
    value = get_secret("X")
    assert value is not None
    assert value.get_secret_value() == "z-pliku-secret"


def test_env_value_is_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X", "haslo-dziecka")
    value = get_secret("X")
    assert "haslo-dziecka" not in repr(value)
    assert "haslo-dziecka" not in str(value)


def test_missing_required_raises_and_optional_returns_none() -> None:
    with pytest.raises(MissingSecretError, match="X_FILE"):
        get_secret("X")
    assert get_secret("X", required=False) is None


def test_empty_file_counts_as_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "x"
    f.write_text("  \n")
    monkeypatch.setenv("X_FILE", str(f))
    with pytest.raises(MissingSecretError):
        get_secret("X")


def test_apprise_token_is_redacted_even_outside_full_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPRISE_CONFIG", APPRISE)
    get_secret("APPRISE_CONFIG")
    out = redact(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage failed")
    assert TOKEN not in out
    assert "AAHfakeTelegramTokenForTests" not in out
    assert REDACTED in out


def test_short_values_are_not_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X", "abc")
    get_secret("X")
    assert redact("abc abc") == "abc abc"


def test_longest_value_is_replaced_first() -> None:
    secret_env.register("tajne")
    secret_env.register("bardzo-tajne-haslo")
    assert redact("haslo: bardzo-tajne-haslo") == f"haslo: {REDACTED}"


def test_filter_redacts_args_and_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X", "haslo-dziecka")
    get_secret("X")
    try:
        raise RuntimeError("login failed for haslo-dziecka")
    except RuntimeError:
        record = logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "proba %s", ("haslo-dziecka",), sys.exc_info()
        )
    assert RedactingFilter().filter(record)
    text = logging.Formatter().format(record)
    assert "haslo-dziecka" not in text
    assert "RuntimeError" in text
    assert record.args is None and record.exc_info is None


def test_setup_logging_redacts_and_silences_http_libs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("X", "haslo-dziecka")
    get_secret("X")
    setup_logging("DEBUG")
    logging.getLogger("librus_watch").info("uzywam %s", "haslo-dziecka")
    logging.getLogger("urllib3").info("GET /?pass=haslo-dziecka")
    err = capsys.readouterr().err
    assert "haslo-dziecka" not in err
    assert f"uzywam {REDACTED}" in err
    assert "GET /" not in err
