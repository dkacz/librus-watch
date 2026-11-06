from pathlib import Path

import pytest
from pydantic import ValidationError

from librus_watch.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_example_config_loads() -> None:
    cfg = load_config(ROOT / "config.example.yaml")
    assert [c.id for c in cfg.children] == ["ania", "kuba"]
    assert cfg.llm.api_key_required is False


def test_validation_error_does_not_echo_values(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "children: [{id: a, name: A}]\n"
        "llm: {base_url: 'http://example.com/v1?k=SEKRET123', timeout_seconds: SEKRET456}\n"
    )
    with pytest.raises(ValidationError) as exc:
        load_config(path)
    assert "SEKRET123" not in str(exc.value) and "SEKRET456" not in str(exc.value)


def test_broken_yaml_reports_line_without_content(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("children:\n  - id: a\n    name: [SEKRET789\n")
    with pytest.raises(ConfigError, match="linia") as exc:
        load_config(path)
    assert "SEKRET789" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


@pytest.mark.parametrize("content", [None, "- a\n- b\n"])
def test_missing_file_or_wrong_shape_is_config_error(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "config.yaml"
    if content is not None:
        path.write_text(content)
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("key", ["password", "api_key", "bot_token", "login"])
def test_secret_like_keys_are_rejected(tmp_path: Path, key: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"children: [{{id: a, name: A}}]\nllm: {{{key}: x}}\n")
    with pytest.raises(ValueError, match="wygląda na sekret"):
        load_config(path)
