import pytest

from dprobe.cli import main
from dprobe.connectors import REGISTRY
from dprobe.connectors.base import Connector
from dprobe.errors import ConnectError

CONFIG = """
    connections:
      web:
        driver: mysql
        url: web-db:3306/webapp
        user: app_ro
        password: sup3r-secret
        readonly: true
      hr:
        driver: oracle
        url: db1:1521/ORCL
        user: hr
        password_cmd: echo hr-pass
"""


class FakeConnector(Connector):
    fail_with: str | None = None

    def _connect(self):
        if self.fail_with:
            raise ConnectError(self.fail_with.format(password=self.password))
        return object()

    def server_version(self):
        return "Fake 1.0"

    def close(self):
        self.conn = None


@pytest.fixture
def fake(monkeypatch):
    for driver in REGISTRY:
        monkeypatch.setitem(REGISTRY, driver, FakeConnector)
    monkeypatch.setattr(FakeConnector, "fail_with", None)
    return FakeConnector


def test_labels_hides_secrets(write_config, capsys):
    write_config(CONFIG)
    assert main(["labels"]) == 0
    out = capsys.readouterr().out
    assert "web    mysql   web-db:3306/webapp  app_ro  password  yes" in out
    assert "hr     oracle  db1:1521/ORCL       hr      command" in out
    assert "sup3r-secret" not in out and "hr-pass" not in out


def test_config_option_after_subcommand(write_config, capsys):
    path = write_config(CONFIG, name="other.yaml")
    assert main(["labels", "--config", str(path)]) == 0
    assert main(["--config", str(path), "labels"]) == 0


def test_ping_ok(write_config, fake, capsys):
    write_config(CONFIG)
    assert main(["ping", "web", "hr"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split(":")[0] for line in out] == ["web", "hr"]
    assert out[0].endswith("(Fake 1.0)")


def test_ping_unknown_label_fails_before_connecting(write_config, fake, capsys):
    write_config(CONFIG)
    assert main(["ping", "web", "nope"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown label 'nope'" in captured.err


def test_ping_failure_is_redacted(write_config, fake, capsys, monkeypatch):
    write_config(CONFIG)
    monkeypatch.setattr(FakeConnector, "fail_with", "login failed for password {password}")
    assert main(["ping", "web"]) == 3
    err = capsys.readouterr().err
    assert "dprobe: error: web: login failed for password ***" in err
    assert "sup3r-secret" not in err


def test_missing_config(capsys):
    assert main(["labels"]) == 2
    assert "no config file found" in capsys.readouterr().err
