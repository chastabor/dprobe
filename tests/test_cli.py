import io
import re
import json
import sqlite3
from types import SimpleNamespace

import pytest

from dprobe.cli import main
from dprobe.connectors import REGISTRY
from dprobe.connectors.base import ColumnInfo, Connector, Described, TableInfo

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

    @classmethod
    def placeholder(cls, name):
        return f":{name}", name

    def list_tables(self, schema, like, views):
        types = "'table', 'view'" if views else "'table'"
        sql = (f"SELECT 'main', name, upper(type), NULL FROM sqlite_master WHERE type IN ({types}) "
               "AND (:pattern IS NULL OR upper(name) LIKE upper(:pattern)) ORDER BY name")
        return [TableInfo(*row) for row in self._catalog(sql, {"pattern": like})]

    def describe(self, schema, name):
        sql = ("SELECT cid + 1, name, type, \"notnull\" = 0, dflt_value, nullif(pk, 0), NULL, NULL "
               "FROM pragma_table_info(:name)")
        columns = [ColumnInfo(p, n, t, bool(nl), d, pk, e, c)
                   for p, n, t, nl, d, pk, e, c in self._catalog(sql, {"name": name})]
        return Described("main", name, columns) if columns else None

    def raw_columns(self, table):
        return self.execute("SELECT * FROM pragma_table_info(:name)", {"name": table.name})


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
        conn.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT 'x', score REAL)")
        conn.execute("CREATE VIEW people_v AS SELECT id FROM people")
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


def test_utf8_regardless_of_locale(tmp_path, write_config):
    # LC_ALL=C with UTF-8 mode off makes Python default to ASCII for files and stdio.
    import os
    import subprocess
    import sys

    write_config("connections:\n  café:\n    driver: mysql\n    url: h/x\n    user: josé\n")
    env = {**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONIOENCODING": ""}
    result = subprocess.run([sys.executable, "-m", "dprobe", "labels"], cwd=tmp_path, env=env,
                            capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    assert "café   mysql   h/x  josé" in result.stdout.decode("utf-8")


def test_query_binds(db, capsys, tmp_path):
    assert main(["query", "web", "-f", "csv", "-b", "id:int=2",
                 "-e", "SELECT name FROM people WHERE id = :ID OR name = ':id'"]) == 0
    assert capsys.readouterr().out == "name\nBo\n"
    (tmp_path / "b.yaml").write_text("id: 1\nmin: 9\n")
    assert main(["query", "web", "-f", "csv", "--binds", "b.yaml", "-b", "id:int=3",
                 "-e", "SELECT name FROM people WHERE id = :id OR score > :min ORDER BY id"]) == 0
    assert capsys.readouterr().out == "name\nAnn\nCy\n"


def test_query_missing_and_unused_binds(db, capsys):
    assert main(["query", "web", "-e", "SELECT :a, :b, :c", "-b", "b=1"]) == 2
    assert "no value for :a, :c" in capsys.readouterr().err
    assert main(["query", "web", "-f", "csv", "-e", "SELECT 1 AS x", "-b", "typo=1"]) == 0
    assert "warning: the statement has no :typo" in capsys.readouterr().err
    assert main(["query", "web", "-f", "csv", "-e", "SELECT :a_ AS x", "-b", "a_=1"]) == 0
    captured = capsys.readouterr()
    assert (captured.out, "warning" in captured.err) == ("x\n1\n", False)


def test_query_native_rejects_binds(db, capsys):
    assert main(["query", "web", "--native", "-b", "a=1", "-e", "SELECT 1"]) == 2
    assert "--native sends the SQL as written" in capsys.readouterr().err


def test_dry_run_does_not_connect(write_config, capsys):
    # The real MySQL connector: web-db doesn't resolve, so connecting would fail.
    write_config(CONFIG)
    assert main(["query", "web", "--dry-run", "-b", "id:int=7", "-b", "d:date=2024-01-02",
                 "-b", "s=O'Brien", "-b", "n:null=",
                 "-e", "SELECT * FROM t WHERE id = :id AND d = :d AND s = :s AND n = :n"]) == 0
    assert capsys.readouterr().out == (
        "SELECT * FROM t WHERE id = %(id)s AND d = %(d)s AND s = %(s)s AND n = %(n)s\n"
        "-- bind id = 7 (int)\n"
        "-- bind d = 2024-01-02 (date)\n"
        "-- bind s = \"O'Brien\"\n"
        "-- bind n = NULL\n"
    )


def test_dry_run_shows_cleanup_and_native(write_config, capsys):
    write_config(CONFIG)
    assert main(["query", "hr", "--dry-run", "-e", "SELECT 1 FROM dual;\n/\n"]) == 0
    assert capsys.readouterr().out == "SELECT 1 FROM dual\n"
    assert main(["query", "hr", "--dry-run", "--native", "-e", "SELECT :x FROM dual;"]) == 0
    assert capsys.readouterr().out == "SELECT :x FROM dual;\n"


def test_tables(db, capsys):
    assert main(["tables", "web"]) == 0
    captured = capsys.readouterr()
    assert captured.out == (
        "schema  name    type   comment\n"
        "------  ------  -----  -------\n"
        "main    people  TABLE  NULL\n"
    )
    assert captured.err.startswith("(1 table, ")
    assert main(["tables", "web", "--views", "--like", "PEOPLE%", "-f", "csv"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "schema,name,type,comment\nmain,people,TABLE,\nmain,people_v,VIEW,\n"
    assert captured.err.startswith("(1 table, 1 view, ")


def test_describe(db, capsys):
    assert main(["describe", "web", "people", "-f", "json"]) == 0
    captured = capsys.readouterr()
    rows = json.loads(captured.out)
    assert [r["name"] for r in rows] == ["id", "name", "score"]
    assert rows[1] == {"position": 2, "name": "name", "type": "TEXT", "nullable": False,
                       "default": "'x'", "pk": None, "extra": None, "comment": None}
    assert rows[0]["pk"] == 1
    assert captured.err.startswith("(main.people: 3 columns, ")
    assert main(["describe", "web", "people", "--raw", "-f", "csv"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "cid,name,type,notnull,dflt_value,pk"


def test_describe_not_found_suggests(db, capsys):
    assert main(["describe", "web", "peopl_"]) == 1
    assert "no table or view peopl_; did you mean main.people?" in capsys.readouterr().err
    assert main(["describe", "web", "nothing"]) == 1
    assert capsys.readouterr().err.strip().endswith("no table or view nothing")


@pytest.mark.parametrize(
    ("name", "message"),
    [("a.b.c", "expected \\[SCHEMA.\\]TABLE"), ('"open', "unterminated quote"), ("a.", "empty name")],
)
def test_describe_bad_names(db, capsys, name, message):
    assert main(["describe", "web", name]) == 2
    assert re.search(message, capsys.readouterr().err)
