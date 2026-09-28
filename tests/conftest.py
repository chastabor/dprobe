import textwrap
from pathlib import Path

import pytest

from dprobe import config, errors


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(errors, "_secrets", set())
    monkeypatch.setattr(config, "_can_prompt", lambda: False)
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def write_config(tmp_path):
    def write(text: str, name: str = "dprobe.yaml", mode: int = 0o600) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        path.chmod(mode)
        return path

    return write
