import io
import re
import json
import sqlite3
from types import SimpleNamespace

import pytest

from dprobe.cli import main
from dprobe.connectors import REGISTRY
from dprobe.connectors.base import ColumnInfo, Connector, Found, SchemaInfo, TableInfo, group_indexes

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
    dialect = "mysql"

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
        return Found("main", name, columns) if columns else None

    def raw_columns(self, table):
        return self.execute("SELECT * FROM pragma_table_info(:name)", {"name": table.name})

    def indexes(self, schema, name):
        if not self._catalog("SELECT 1 FROM sqlite_master WHERE name = :name", {"name": name}):
            return None
        sql = ("SELECT l.name, l.\"unique\", l.origin = 'pk', NULL, i.name, 0, 0 FROM pragma_index_list(:name) l "
               "JOIN pragma_index_info(l.name) i ORDER BY l.name, i.seqno")
        return Found("main", name, group_indexes(self._catalog(sql, {"name": name})))

    def schemas(self):
        return [SchemaInfo("main", True, False), SchemaInfo("temp", False, True)]


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
        conn.execute("CREATE UNIQUE INDEX people_name ON people (name, score)")
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
        " 1  Ann     9.5\n"
        " 2  Bo     NULL\n"
        " 3  Cy      7.0\n"
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
    assert captured.out.splitlines()[-1] == " 2"
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


def test_indexes(db, capsys):
    assert main(["indexes", "web", "people", "-f", "json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == [
        {"name": "people_name", "columns": "name, score", "unique": True, "primary": False,
         "type": None, "include": None},
    ]
    assert captured.err.startswith("(main.people: 1 index, ")
    assert main(["indexes", "web", "nothing"]) == 1
    assert "no table or view nothing" in capsys.readouterr().err


def test_schemas_hide_system_ones(db, capsys):
    assert main(["schemas", "web", "-f", "csv"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "name,current,system\nmain,true,false\n"
    assert captured.err.startswith("(1 schema, ") and "; 1 system schema hidden, --all shows them)" in captured.err
    assert main(["schemas", "web", "--all", "-f", "csv"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "temp,false,true"


def test_query_meta(db, capsys):
    assert main(["query", "web", "--meta", "-f", "csv", "-e", "SELECT id, name AS n FROM people"]) == 0
    captured = capsys.readouterr()
    assert [line.split(",")[:2] for line in captured.out.splitlines()] == [
        ["position", "name"], ["1", "id"], ["2", "n"]]
    assert captured.err.startswith("(2 result columns, ")
    # The base implementation runs the statement, so it refuses anything but a query.
    assert main(["query", "web", "--meta", "-e", "DELETE FROM people"]) == 2
    assert "only takes a query" in capsys.readouterr().err


def test_query_declared_binds_and_prompt(db, capsys, tmp_path, request):
    (tmp_path / "q.sql").write_text("-- @bind id int = 2\n-- @bind extra\nSELECT name FROM people WHERE id = :id")
    assert main(["query", "web", "q.sql", "-f", "csv"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "name\nBo\n"
    assert "warning: the statement has no :extra" in captured.err
    assert main(["query", "web", "q.sql", "-f", "csv", "-b", "id=3"]) == 0
    assert capsys.readouterr().out == "name\nCy\n"

    request.getfixturevalue("terminal").answers.append("1")
    assert main(["query", "web", "-f", "csv", "-e", "SELECT name FROM people WHERE id = :who"]) == 0
    assert capsys.readouterr().out == "name\nAnn\n"


class MultiSetConnector(SqliteConnector):
    """Every query also returns a second result set, like a SQL Server batch or Oracle implicit results."""

    def _result_sets(self, cursor):
        yield from super()._result_sets(cursor)
        extra = self.conn.cursor()
        extra.execute("SELECT 'second' AS s")
        yield extra


def test_query_several_statements_need_script(db, capsys):
    assert main(["query", "web", "-e", "SELECT 1; SELECT 2"]) == 2
    assert "the SQL holds 2 statements (lines 1, 1); add --script" in capsys.readouterr().err
    assert main(["query", "web", "--script", "--native", "-e", "SELECT 1"]) == 2


def test_script_runs_in_order_and_rolls_back(db, capsys):
    script = "UPDATE people SET name = 'Z' WHERE id = 1;\nSELECT name FROM people ORDER BY id;\nSELECT 1 AS one;"
    assert main(["query", "web", "--script", "-f", "csv", "-e", script]) == 0
    captured = capsys.readouterr()
    assert captured.out == "name\nZ\nBo\nCy\n\none\n1\n"
    err = captured.err.splitlines()
    assert err[:3] == ["(statement 1, line 1: 1 row affected)", "(statement 2, line 2: 3 rows)",
                       "(statement 3, line 3: 1 row)"]
    assert err[3].startswith("(3 statements; rolled back (use --commit to keep changes), ")
    assert names(db) == ["Ann", "Bo", "Cy"]
    assert main(["query", "web", "--script", "--commit", "-e", script]) == 0
    assert capsys.readouterr().err.splitlines()[-1].startswith("(3 statements; committed, ")
    assert names(db) == ["Z", "Bo", "Cy"]


def test_script_stops_at_first_error_and_commits_nothing(db, capsys):
    script = "UPDATE people SET name = 'Z';\nSELECT * FROM nope;\nUPDATE people SET name = 'Y';"
    assert main(["query", "web", "--script", "--commit", "-e", script]) == 1
    assert "dprobe: error: statement 2, line 2: no such table: nope" in capsys.readouterr().err
    assert names(db) == ["Ann", "Bo", "Cy"]


def test_script_json_and_truncation(db, capsys):
    script = "SELECT id FROM people ORDER BY id; SELECT name FROM people WHERE id = 3"
    assert main(["query", "web", "--script", "-f", "json", "--max-rows", "1", "-e", script]) == 0
    captured = capsys.readouterr()
    assert captured.out == '[\n{"id": 1}\n]\n[\n{"name": "Cy"}\n]\n'
    assert "(statement 1, line 1: first 1 row)" in captured.err
    assert captured.err.splitlines()[-1].endswith("; --max-rows 0 shows all)")


def test_script_binds_resolved_once(db, capsys, terminal):
    terminal.answers.append("2")
    script = "-- @bind id int\nSELECT name FROM people WHERE id = :id;\nSELECT id FROM people WHERE id = :id;"
    assert main(["query", "web", "--script", "-f", "csv", "-e", script]) == 0
    assert capsys.readouterr().out == "name\nBo\n\nid\n2\n"
    assert terminal.asked == [":id (int): "]


def test_dry_run_script(write_config, capsys):
    write_config(CONFIG)
    assert main(["query", "web", "--script", "--dry-run", "-b", "id:int=7",
                 "-e", "SELECT :id; -- next\nDELETE FROM t WHERE id IN (:id)"]) == 0
    assert capsys.readouterr().out == (
        # MySQL keeps a trailing ";", which it accepts.
        "-- statement 1, line 1\nSELECT %(id)s;\n-- bind id = 7 (int)\n\n"
        "-- statement 2, line 2\n-- next\nDELETE FROM t WHERE id IN (%(id)s)\n-- bind id = 7 (int)\n"
    )


def test_query_prints_every_result_set(db, capsys, monkeypatch):
    for driver in REGISTRY:
        monkeypatch.setitem(REGISTRY, driver, MultiSetConnector)
    assert main(["query", "web", "-f", "csv", "-e", "SELECT id FROM people WHERE id = 1"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "id\n1\n\ns\nsecond\n"
    err = captured.err.splitlines()
    assert err[:2] == ["(result 1: 1 row)", "(result 2: 1 row)"]
    assert err[2].startswith("(2 results, ")


def test_query_list_bind(db, capsys):
    assert main(["query", "web", "-f", "csv", "-b", "ids:int[]=1,3",
                 "-e", "SELECT name FROM people WHERE id IN (:ids) ORDER BY id"]) == 0
    assert capsys.readouterr().out == "name\nAnn\nCy\n"


def test_keyring_command(write_config, capsys, monkeypatch, memory_keyring):
    memory = memory_keyring
    write_config("connections:\n  kr:\n    driver: mysql\n    url: h/d\n    user: u\n    keyring: dprobe\n")
    assert main(["keyring", "kr"]) == 2
    assert "no terminal to prompt on" in capsys.readouterr().err

    from dprobe import config, tty

    monkeypatch.setattr(tty, "available", lambda: True)
    monkeypatch.setattr(config.getpass, "getpass", lambda prompt: "typed-secret")
    assert main(["keyring", "kr"]) == 0
    assert "stored the password for u in keyring service dprobe" in capsys.readouterr().out
    assert memory.get_password("dprobe", "u") == "typed-secret"
    assert main(["keyring", "kr", "--delete"]) == 0
    assert memory.get_password("dprobe", "u") is None


class RecordingConnector(SqliteConnector):
    seen: list = []

    def _connect_args(self):
        self.seen.append((self.config.user, self.password))
        return super()._connect_args()


def test_password_from_an_environment_variable(db, write_config, monkeypatch, capsys):
    write_config("connections:\n  env:\n    driver: mysql\n    url: h/d\n"
                 "    user: ${WEB_USER}\n    password: ${WEB_PASS}\n", mode=0o644)
    monkeypatch.setenv("WEB_USER", "app_ro")
    monkeypatch.setenv("WEB_PASS", "s3cr3t-from-env")
    monkeypatch.setitem(REGISTRY, "mysql", RecordingConnector)
    monkeypatch.setattr(RecordingConnector, "seen", [])
    assert main(["query", "env", "-f", "csv", "-e", "SELECT 1 AS x"]) == 0
    captured = capsys.readouterr()
    assert (captured.out, RecordingConnector.seen) == ("x\n1\n", [("app_ro", "s3cr3t-from-env")])
    # A world-readable file is fine: no warning.
    assert "warning" not in captured.err
    monkeypatch.delenv("WEB_PASS")
    assert main(["query", "env", "-e", "SELECT 1"]) == 2
    assert "connections.env.password: environment variable WEB_PASS is not set" in capsys.readouterr().err


def test_labels_with_an_override_file(write_config, capsys):
    write_config(CONFIG)
    write_config("connections:\n  web:\n    password: null\n    password_cmd: pass show web\n",
                 name="dprobe.override.yaml")
    assert main(["-v", "labels"]) == 0
    captured = capsys.readouterr()
    assert "web    mysql   web-db:3306/webapp  app_ro  command" in captured.out
    assert "dprobe: using dprobe.yaml with dprobe.override.yaml" in captured.err
