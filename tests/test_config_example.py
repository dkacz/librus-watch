from pathlib import Path

import pytest

from librus_watch.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def test_example_config_loads() -> None:
    cfg = load_config(ROOT / "config.example.yaml")
    assert [c.id for c in cfg.children] == ["ania", "kuba"]
    assert cfg.llm.api_key_required is False


@pytest.mark.parametrize("key", ["password", "api_key", "bot_token", "login"])
def test_secret_like_keys_are_rejected(tmp_path: Path, key: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"children: [{{id: a, name: A}}]\nllm: {{{key}: x}}\n")
    with pytest.raises(ValueError, match="wygląda na sekret"):
        load_config(path)
