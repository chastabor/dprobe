import io
import json
import sqlite3
from types import SimpleNamespace

import pytest

from dprobe.cli import main
from dprobe.connectors import REGISTRY
from dprobe.connectors.base import Connector

CONFIG = """
    connections:
      web:
        driver: mysql
        url: web-db:3306/webapp
        user: app_ro
        password: sup3r-secret
      hr:
        driver: oracle
        url: db1:1521/ORCL
        user: hr
        password_cmd: echo hr-pass
        readonly: true
"""


class SqliteConnector(Connector):
    """Real DB-API behavior without a server; the database file is set per test."""

    database = ":memory:"

    def _import_driver(self):
        return sqlite3

    def _connect_args(self):
        return {"database": self.database}

    def server_version(self):
        return f"SQLite {sqlite3.sqlite_version}"

    @classmethod
    def prepare(cls, sql):
        # Stand-in for a driver's cleanup, so tests can tell whether it ran.
        return sql.replace("PREPARE_ME", "1")


class FailingConnector(SqliteConnector):
    def _import_driver(self):
        def connect(**kwargs):
            raise sqlite3.OperationalError(f"login failed for password {self.password}")

        return SimpleNamespace(Error=sqlite3.Error, connect=connect)


@pytest.fixture
def db(monkeypatch, tmp_path, write_config):
    write_config(CONFIG)
    path = tmp_path / "test.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE people (id INTEGER, name TEXT, score REAL)")
        conn.executemany("INSERT INTO people VALUES (?, ?, ?)",
                         [(1, "Ann", 9.5), (2, "Bo", None), (3, "Cy", 7.0)])
    monkeypatch.setattr(SqliteConnector, "database", str(path))
    for driver in REGISTRY:
        monkeypatch.setitem(REGISTRY, driver, SqliteConnector)
    return path


def names(path):
    with sqlite3.connect(path) as conn:
        return [name for (name,) in conn.execute("SELECT name FROM people ORDER BY id")]


def test_labels_hides_secrets(write_config, capsys):
    write_config(CONFIG)
    assert main(["labels"]) == 0
    out = capsys.readouterr().out
    assert "web    mysql   web-db:3306/webapp  app_ro  password\n" in out
    assert "hr     oracle  db1:1521/ORCL       hr      command   yes" in out
    assert "sup3r-secret" not in out and "hr-pass" not in out


def test_config_option_after_subcommand(write_config):
    path = write_config(CONFIG, name="other.yaml")
    assert main(["labels", "--config", str(path)]) == 0
    assert main(["--config", str(path), "labels"]) == 0


def test_missing_config(capsys):
    assert main(["labels"]) == 2
    assert "no config file found" in capsys.readouterr().err


def test_ping_ok(db, capsys):
    assert main(["ping", "web", "hr"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split(":")[0] for line in out] == ["web", "hr"]
    assert out[0].endswith(f"(SQLite {sqlite3.sqlite_version})")


def test_ping_unknown_label_fails_before_connecting(db, capsys):
    assert main(["ping", "web", "nope"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown label 'nope'" in captured.err


def test_ping_failure_is_redacted(db, capsys, monkeypatch):
    monkeypatch.setitem(REGISTRY, "mysql", FailingConnector)
    assert main(["ping", "web"]) == 3
    err = capsys.readouterr().err
    assert "dprobe: error: web: login failed for password ***" in err
    assert "sup3r-secret" not in err


def test_query_table(db, capsys, tmp_path):
    (tmp_path / "q.sql").write_text("﻿SELECT id, name, score FROM people ORDER BY id")
    assert main(["query", "web", "q.sql"]) == 0
    captured = capsys.readouterr()
    assert captured.out == (
        "id  name  score\n"
        "--  ----  -----\n"
        "1   Ann   9.5\n"
        "2   Bo    NULL\n"
        "3   Cy    7.0\n"
    )
    assert captured.err.startswith("(3 rows, ")


def test_query_formats(db, capsys):
    assert main(["query", "web", "-e", "SELECT id, score FROM people ORDER BY id", "-f", "csv", "--null", "-"]) == 0
    assert capsys.readouterr().out == "id,score\n1,9.5\n2,-\n3,7.0\n"
    assert main(["query", "web", "-e", "SELECT id, name FROM people WHERE id = 1", "-f", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"id": 1, "name": "Ann"}]


def test_query_stdin_and_output_file(db, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin", io.StringIO("SELECT name FROM people WHERE id = 3"))
    assert main(["query", "web", "-", "-f", "jsonl", "-o", "out.jsonl"]) == 0
    assert (tmp_path / "out.jsonl").read_text() == '{"name": "Cy"}\n'
    assert capsys.readouterr().out == ""


def test_query_max_rows(db, capsys):
    assert main(["query", "web", "-e", "SELECT id FROM people ORDER BY id", "--max-rows", "2"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[-1] == "2"
    assert captured.err.startswith("(first 2 rows, ") and "--max-rows 0 shows all" in captured.err


def test_query_rolls_back_by_default(db, capsys):
    assert main(["query", "web", "-e", "UPDATE people SET name = 'Zed'"]) == 0
    assert capsys.readouterr().err.startswith("(3 rows affected; rolled back (use --commit to keep changes), ")
    assert names(db) == ["Ann", "Bo", "Cy"]
    assert main(["query", "web", "-e", "UPDATE people SET name = 'Zed' WHERE id = 1", "--commit"]) == 0
    assert capsys.readouterr().err.startswith("(1 row affected; committed, ")
    assert names(db) == ["Zed", "Bo", "Cy"]


def test_query_commit_refused_when_readonly(db, capsys):
    assert main(["query", "hr", "-e", "SELECT 1", "--commit"]) == 2
    assert "hr is readonly" in capsys.readouterr().err


def test_query_native_skips_prepare(db, capsys):
    assert main(["query", "web", "-e", "SELECT PREPARE_ME AS x", "-f", "csv"]) == 0
    assert capsys.readouterr().out == "x\n1\n"
    assert main(["query", "web", "-e", "SELECT PREPARE_ME AS x", "--native"]) == 1
    err = capsys.readouterr().err
    assert "no such column: PREPARE_ME (without --native, dprobe removes" in err


def test_query_ddl_status(db, capsys):
    assert main(["query", "web", "-e", "CREATE TABLE extra (a INTEGER)"]) == 0
    assert capsys.readouterr().err.startswith(
        "(statement executed; rolled back (use --commit to keep changes), "
    )


def test_query_sql_error_keeps_existing_output_file(db, capsys, tmp_path):
    (tmp_path / "out.csv").write_text("keep me")
    assert main(["query", "web", "-e", "SELECT nope FROM people", "-o", "out.csv"]) == 1
    assert "dprobe: error: no such column: nope" in capsys.readouterr().err
    assert (tmp_path / "out.csv").read_text() == "keep me"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["query", "web"], "give exactly one of"),
        (["query", "web", "q.sql", "-e", "SELECT 1"], "give exactly one of"),
        (["query", "web", "missing.sql"], "cannot read missing.sql"),
        (["query", "web", "-e", "  "], "no SQL to run"),
    ],
)
def test_query_usage_errors(db, capsys, argv, message):
    assert main(argv) == 2
    assert message in capsys.readouterr().err


def test_query_rejects_non_utf8(db, capsys, tmp_path):
    (tmp_path / "u16.sql").write_bytes("SELECT 1".encode("utf-16"))
    assert main(["query", "web", "u16.sql"]) == 2
    assert "is not UTF-8" in capsys.readouterr().err
