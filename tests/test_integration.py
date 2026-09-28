"""Real connections. Set DPROBE_IT_CONFIG to a config whose it-* labels point
at test databases (it-oracle, it-mssql, it-mysql, ...); each label runs every
test. The tests create and drop a table named dprobe_it."""

import json
import os
import re
import time
from contextlib import suppress
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from dprobe.cli import main
from dprobe.config import load_config
from dprobe.connectors import create_connector
from dprobe.errors import QueryError

# Resolved at import: the autouse fixture changes the working directory.
IT_CONFIG = Path(os.environ["DPROBE_IT_CONFIG"]).resolve() if os.environ.get("DPROBE_IT_CONFIG") else None

pytestmark = pytest.mark.integration


def _labels():
    if IT_CONFIG is None:
        return [pytest.param(None, marks=pytest.mark.skip(reason="DPROBE_IT_CONFIG not set"))]
    return sorted(label for label in load_config(IT_CONFIG).connections if label.startswith("it-"))


TYPES_SQL = {
    "oracle": "SELECT 1 AS n, CAST(12.345 AS NUMBER(10,3)) AS d, TO_CLOB('long text') AS c,"
              " HEXTORAW('6162') AS r, DATE '2024-01-02' AS dt, NULL AS nul FROM dual;\n/\n",
    "mssql": "SELECT 1 AS n, CAST(12.345 AS DECIMAL(10,3)) AS d, CAST('long text' AS NVARCHAR(MAX)) AS c,"
             " 0x6162 AS r, CAST('2024-01-02' AS DATE) AS dt, NULL AS nul\nGO\n",
    "mysql": "SELECT 1 AS n, CAST(12.345 AS DECIMAL(10,3)) AS d, 'long text' AS c,"
             " X'6162' AS r, DATE '2024-01-02' AS dt, NULL AS nul;",
}
DUAL = {"oracle": " FROM dual"}
MANY_ROWS = {
    "oracle": "SELECT level FROM dual CONNECT BY level <= 300000",
    "mssql": "SELECT TOP 300000 a.object_id FROM sys.all_objects a CROSS JOIN sys.all_objects b",
    "mysql": "SELECT 1 FROM information_schema.columns a, information_schema.columns b LIMIT 300000",
}


@pytest.fixture(params=_labels())
def it(request, capsys):
    label = request.param
    config = load_config(IT_CONFIG)
    conn = config.get(label)

    def run(*args, sql=None, command="query"):
        argv = ["--config", str(IT_CONFIG), command, label, *args]
        status = main(argv + (["-e", sql] if sql is not None else []))
        out, err = capsys.readouterr()
        return status, out, err

    _recreate(conn, ["DROP TABLE dprobe_it"], [
        "CREATE TABLE dprobe_it (id INT, name VARCHAR(20))",
        "INSERT INTO dprobe_it VALUES (1, 'a')",
        "INSERT INTO dprobe_it VALUES (2, 'b')",
    ])
    yield run, conn
    with create_connector(conn) as db:
        db.execute("DROP TABLE dprobe_it")
        db.commit()


def test_ping(it):
    _, conn = it
    with create_connector(conn) as db:
        assert db.server_version()


def test_value_types_as_json(it):
    run, conn = it
    status, out, err = run("-f", "json", sql=TYPES_SQL[conn.driver])
    assert status == 0, err
    (row,) = json.loads(out, parse_float=Decimal)
    assert {k.lower(): v for k, v in row.items()} == {
        "n": 1, "d": Decimal("12.345"), "c": "long text", "r": "0x6162",
        "dt": "2024-01-02T00:00:00" if conn.driver == "oracle" else "2024-01-02", "nul": None,
    }


def test_native_sends_percent_signs_unchanged(it):
    run, conn = it
    status, out, err = run("-f", "csv", "--native", sql=f"SELECT 'A%' AS p, '50%%' AS q{DUAL.get(conn.driver, '')}")
    assert status == 0, err
    assert out.splitlines()[1] == "A%,50%%"


def test_native_oracle_trailing_semicolon(it):
    run, conn = it
    if conn.driver != "oracle":
        pytest.skip("Oracle only")
    with create_connector(conn) as db:
        major = int(db.conn.version.split(".")[0])
    status, _, err = run("--native", sql="SELECT 1 FROM dual;")
    if major >= 23:
        assert status == 0, err
    else:
        assert status == 1
        assert "without --native, dprobe removes a trailing ';'" in err


def test_stopping_early_on_a_large_result_is_fast(it):
    run, conn = it
    start = time.perf_counter()
    status, out, err = run("-f", "csv", "--max-rows", "5", sql=MANY_ROWS[conn.driver])
    assert status == 0, err
    assert len(out.splitlines()) == 6
    assert err.startswith("(first 5 rows")
    assert time.perf_counter() - start < 5


def test_rollback_and_commit(it):
    run, conn = it
    status, _, err = run(sql="UPDATE dprobe_it SET name = 'z'")
    assert (status, err.split(",")[0]) == (0, "(2 rows affected; rolled back (use --commit to keep changes)")
    status, out, _ = run("-f", "csv", sql="SELECT name FROM dprobe_it ORDER BY id")
    assert out.split() == ["NAME" if conn.driver == "oracle" else "name", "a", "b"]
    status, _, err = run("--commit", sql="UPDATE dprobe_it SET name = 'z' WHERE id = 1")
    assert (status, err.split(",")[0]) == (0, "(1 row affected; committed")
    status, out, _ = run("-f", "csv", sql="SELECT name FROM dprobe_it ORDER BY id")
    assert out.split()[1:] == ["z", "b"]


def test_readonly_blocks_dml(it):
    _, conn = it
    if conn.driver == "mssql":
        pytest.skip("SQL Server has no read-only transaction")
    with create_connector(replace(conn, readonly=True)) as db:
        with pytest.raises(QueryError, match="(ORA-01456|1792)"):
            db.execute("UPDATE dprobe_it SET name = 'z'")


def test_mssql_batch_shows_first_result_set(it):
    run, conn = it
    if conn.driver != "mssql":
        pytest.skip("SQL Server only")
    status, out, err = run("-f", "csv", sql="DECLARE @x int = 5; SELECT @x AS x; SELECT 2 AS y")
    assert status == 0, err
    assert out == "x\n5\n"
    assert "more result sets" in err


def test_utf8_round_trip(it):
    run, conn = it
    text = "naïve ✓ 日本 😀"
    literal = f"N'{text}'" if conn.driver == "mssql" else f"'{text}'"
    status, out, err = run("-f", "json", sql=f"SELECT {literal} AS s{DUAL.get(conn.driver, '')}")
    assert status == 0, err
    (row,) = json.loads(out)
    assert list(row.values()) == [text]


def test_binds(it):
    run, conn = it
    status, out, err = run("-f", "csv", "-b", "id:int=2",
                           sql="SELECT name FROM dprobe_it WHERE id = :ID AND name LIKE 'b%'")
    assert status == 0, err
    assert out.split()[1:] == ["b"]


def test_bind_value_types_round_trip(it):
    run, conn = it
    text = "O'Brien \\ ✓ 😀"
    status, out, err = run(
        "-f", "json", "-b", "i:int=5", "-b", "amt:decimal=12.50", "-b", "n:null=", "-b", f"s={text}",
        sql=f"SELECT :i AS i, :amt AS amt, :n AS n, :s AS s{DUAL.get(conn.driver, '')}",
    )
    assert status == 0, err
    (row,) = json.loads(out, parse_float=Decimal)
    row = {k.lower(): v for k, v in row.items()}
    assert (row["i"], row["amt"], row["n"], row["s"]) == (5, Decimal("12.5"), None, text)


def test_reserved_word_bind_names(it):
    # Oracle rejects :date and :level as bind names (ORA-01745); dprobe renames them.
    run, conn = it
    status, out, err = run("-f", "csv", "-b", "date=x", "-b", "level:int=3",
                           sql=f"SELECT :date AS d, :level AS l{DUAL.get(conn.driver, '')}")
    assert status == 0, err
    assert out.splitlines()[1] == "x,3"


META_DDL = {
    "oracle": [
        "CREATE TABLE dprobe_meta (id NUMBER(10) GENERATED BY DEFAULT AS IDENTITY,"
        " code VARCHAR2(20 CHAR) DEFAULT 'x' NOT NULL, amount NUMBER(10,2), note CLOB,"
        " CONSTRAINT dprobe_meta_pk PRIMARY KEY (id, code))",
        "COMMENT ON TABLE dprobe_meta IS 'meta table'",
        "COMMENT ON COLUMN dprobe_meta.amount IS 'the amount'",
        "CREATE VIEW dprobe_meta_v AS SELECT id, code FROM dprobe_meta",
    ],
    "mysql": [
        "CREATE TABLE dprobe_meta (id INT AUTO_INCREMENT, code VARCHAR(20) NOT NULL DEFAULT 'x',"
        " amount DECIMAL(10,2) COMMENT 'the amount', note TEXT, PRIMARY KEY (id, code))"
        " COMMENT = 'meta table'",
        "CREATE VIEW dprobe_meta_v AS SELECT id, code FROM dprobe_meta",
    ],
    "mssql": [
        "CREATE TABLE dprobe_meta (id INT IDENTITY, code NVARCHAR(20) NOT NULL DEFAULT 'x',"
        " amount DECIMAL(10,2), note NVARCHAR(MAX), CONSTRAINT dprobe_meta_pk PRIMARY KEY (id, code))",
        "EXEC sp_addextendedproperty 'MS_Description', 'meta table', 'SCHEMA', 'dbo', 'TABLE', 'dprobe_meta'",
        "EXEC sp_addextendedproperty 'MS_Description', 'the amount', 'SCHEMA', 'dbo', 'TABLE', 'dprobe_meta',"
        " 'COLUMN', 'amount'",
        "CREATE VIEW dprobe_meta_v AS SELECT id, code FROM dprobe_meta",
    ],
}
TYPES = {
    "oracle": ["NUMBER(10)", "VARCHAR2(20 CHAR)", "NUMBER(10,2)", "CLOB"],
    "mysql": ["int", "varchar(20)", "decimal(10,2)", "text"],
    "mssql": ["int", "nvarchar(20)", "decimal(10,2)", "nvarchar(max)"],
}


def _recreate(conn, drops, creates):
    """Run drops (ignoring failures, e.g. nothing to drop), then creates, and commit."""
    with create_connector(conn) as db:
        for sql in drops:
            with suppress(QueryError):
                db.execute(sql)
        for sql in creates:
            db.execute(sql)
        db.commit()


@pytest.fixture
def meta(it):
    run, conn = it
    _recreate(conn, ["DROP VIEW dprobe_meta_v", "DROP TABLE dprobe_meta"], META_DDL[conn.driver])
    yield run, conn
    with create_connector(conn) as db:
        db.execute("DROP VIEW dprobe_meta_v")
        db.execute("DROP TABLE dprobe_meta")
        db.commit()



def test_tables(meta):
    run, conn = meta
    status, out, err = run("-f", "json", "--like", "DPROBE_META%", command="tables")
    assert status == 0, err
    assert [(t["name"].lower(), t["type"], t["comment"]) for t in json.loads(out)] == [
        ("dprobe_meta", "TABLE", "meta table")
    ]
    status, out, err = run("-f", "json", "--like", "dprobe_meta%", "--views", command="tables")
    assert [(t["name"].lower(), t["type"]) for t in json.loads(out)] == [
        ("dprobe_meta", "TABLE"), ("dprobe_meta_v", "VIEW")
    ]
    assert err.startswith("(1 table, 1 view, ")


def test_describe(meta):
    run, conn = meta
    status, out, err = run("-f", "json", "dprobe_meta", command="describe")
    assert status == 0, err
    cols = {c["name"].lower(): c for c in json.loads(out)}
    assert list(cols) == ["id", "code", "amount", "note"]
    # MariaDB still shows the integer display width that MySQL 8.0.19 dropped: int(11).
    assert [re.sub(r"^int\(\d+\)$", "int", c["type"]) for c in cols.values()] == TYPES[conn.driver]
    assert (cols["id"]["pk"], cols["code"]["pk"], cols["amount"]["pk"]) == (1, 2, None)
    assert (cols["code"]["nullable"], cols["amount"]["nullable"]) == (False, True)
    assert cols["code"]["default"] == "'x'"
    assert cols["amount"]["comment"] == "the amount"
    assert cols["id"]["extra"] in ("identity", "auto_increment")


def test_describe_view_and_raw(meta):
    run, conn = meta
    status, out, err = run("-f", "json", "dprobe_meta_v", command="describe")
    assert status == 0, err
    assert [c["name"].lower() for c in json.loads(out)] == ["id", "code"]
    status, out, err = run("-f", "json", "--raw", "dprobe_meta", command="describe")
    assert status == 0, err
    assert len(json.loads(out)) == 4


def test_describe_name_case(meta):
    run, conn = meta
    status, out, err = run("-f", "csv", "DPROBE_META", command="describe")
    if conn.driver == "mysql":
        # Table names are case-sensitive on Linux; the error offers the real name.
        assert status == 1
        assert "did you mean probe.dprobe_meta?" in err
    else:
        assert status == 0, err


def test_describe_oracle_public_synonym(it):
    run, conn = it
    connections = load_config(IT_CONFIG).connections
    if conn.driver != "oracle" or "admin-oracle" not in connections:
        pytest.skip("needs Oracle and an admin-oracle label")
    admin = connections["admin-oracle"]
    _recreate(admin, ["DROP PUBLIC SYNONYM dprobe_other", "DROP TABLE system.dprobe_other"], [
        "CREATE TABLE system.dprobe_other (x NUMBER(5))",
        f"GRANT SELECT ON system.dprobe_other TO {conn.user}",
        "CREATE PUBLIC SYNONYM dprobe_other FOR system.dprobe_other",
    ])
    try:
        status, out, err = run("-f", "csv", "dprobe_other", command="describe")
        assert status == 0, err
        assert out.splitlines()[1].startswith("1,X,NUMBER(5),true")
        assert err.startswith("(SYSTEM.DPROBE_OTHER via synonym DPROBE_OTHER: 1 column, ")
    finally:
        with create_connector(admin) as db:
            db.execute("DROP PUBLIC SYNONYM dprobe_other")
            db.execute("DROP TABLE system.dprobe_other")
