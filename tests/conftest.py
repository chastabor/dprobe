import textwrap
from pathlib import Path

import pytest

from dprobe import config, errors, tty


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(errors, "_secrets", set())
    monkeypatch.setattr(tty, "available", lambda: False)
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def write_config(tmp_path):
    def write(text: str, name: str = "dprobe.yaml", mode: int = 0o600) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        path.chmod(mode)
        return path

    return write


@pytest.fixture
def memory_keyring():
    """An in-memory keyring backend in place of the system one."""
    import keyring
    import keyring.backend
    import keyring.errors

    class Memory(keyring.backend.KeyringBackend):
        priority = 1

        def __init__(self):
            super().__init__()
            self.store = {}

        def get_password(self, service, username):
            return self.store.get((service, username))

        def set_password(self, service, username, password):
            self.store[(service, username)] = password

        def delete_password(self, service, username):
            if self.store.pop((service, username), None) is None:
                raise keyring.errors.PasswordDeleteError("not found")

    previous = keyring.get_keyring()
    memory = Memory()
    keyring.set_keyring(memory)
    yield memory
    keyring.set_keyring(previous)


@pytest.fixture
def terminal(monkeypatch):
    """A terminal whose prompts get answers from .answers, in order; prompts land in .asked."""

    class Terminal:
        def __init__(self):
            self.answers, self.asked = [], []

        def ask(self, prompt):
            self.asked.append(prompt)
            return self.answers.pop(0)

    fake = Terminal()
    monkeypatch.setattr(tty, "available", lambda: True)
    monkeypatch.setattr(tty, "ask", fake.ask)
    return fake
