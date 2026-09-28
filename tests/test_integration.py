"""Real connections. Set DPROBE_IT_CONFIG to a config whose it-* labels point
at test databases (it-oracle, it-mssql, it-mysql, ...); each label runs every
test. The tests create and drop a table named dprobe_it."""

import json
import os
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

    def run(*args, sql=None):
        argv = ["--config", str(IT_CONFIG), "query", label, *args]
        status = main(argv + (["-e", sql] if sql is not None else []))
        out, err = capsys.readouterr()
        return status, out, err

    with create_connector(conn) as db:
        with suppress(QueryError):
            db.execute("DROP TABLE dprobe_it")
        db.execute("CREATE TABLE dprobe_it (id INT, name VARCHAR(20))")
        for row in ("1, 'a'", "2, 'b'"):
            db.execute(f"INSERT INTO dprobe_it VALUES ({row})")
        db.commit()
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
