import pytest

from dprobe.connectors.mssql import _unwrap, mssql_type
from dprobe.connectors.mysql import mysql_default
from dprobe.connectors.oracle import OracleConnector, oracle_type


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("VARCHAR2", 80, 20, "C", None, None), "VARCHAR2(20 CHAR)"),
        (("VARCHAR2", 20, 20, "B", None, None), "VARCHAR2(20)"),
        (("NVARCHAR2", 40, 20, "C", None, None), "NVARCHAR2(20)"),
        (("NUMBER", 22, 0, None, 10, 2), "NUMBER(10,2)"),
        (("NUMBER", 22, 0, None, 10, 0), "NUMBER(10)"),
        (("NUMBER", 22, 0, None, None, 0), "NUMBER(*,0)"),
        (("NUMBER", 22, 0, None, None, None), "NUMBER"),
        (("FLOAT", 22, 0, None, 126, None), "FLOAT(126)"),
        (("RAW", 16, 0, None, None, None), "RAW(16)"),
        (("TIMESTAMP(6) WITH TIME ZONE", 13, 0, None, None, 6), "TIMESTAMP(6) WITH TIME ZONE"),
        (("CLOB", 4000, 0, None, None, None), "CLOB"),
    ],
)
def test_oracle_type(args, expected):
    assert oracle_type(*args) == expected


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("nvarchar", 40, 0, 0), "nvarchar(20)"),
        (("nvarchar", -1, 0, 0), "nvarchar(max)"),
        (("varbinary", -1, 0, 0), "varbinary(max)"),
        (("char", 3, 0, 0), "char(3)"),
        (("decimal", 9, 10, 2), "decimal(10,2)"),
        (("datetime2", 7, 23, 3), "datetime2(3)"),
        (("int", 4, 10, 0), "int"),
    ],
)
def test_mssql_type(args, expected):
    assert mssql_type(*args) == expected


@pytest.mark.parametrize(
    ("definition", "expected"),
    [("((0))", "0"), ("('x')", "'x'"), ("(getdate())", "getdate()"), ("('a)b')", "'a)b'"), ("(1)+(2)", "(1)+(2)")],
)
def test_mssql_unwrap_default(definition, expected):
    assert _unwrap(definition) == expected


def test_oracle_identifier_case():
    assert (OracleConnector.identifier("emp", False), OracleConnector.identifier("Emp", True)) == ("EMP", "Emp")


@pytest.mark.parametrize(
    ("default", "column_type", "extra", "mariadb", "expected"),
    [
        ("none", "varchar(30)", "", False, "'none'"),
        ("it's", "varchar(30)", "", False, "'it''s'"),
        ("2024-01-02", "date", "", False, "'2024-01-02'"),
        ("0", "int", "", False, "0"),
        ("1.50", "decimal(10,2)", "", False, "1.50"),
        ("CURRENT_TIMESTAMP", "datetime", "DEFAULT_GENERATED", False, "CURRENT_TIMESTAMP"),
        ("CURRENT_TIMESTAMP(3)", "timestamp(3)", "", False, "CURRENT_TIMESTAMP(3)"),
        ("(uuid())", "char(36)", "DEFAULT_GENERATED", False, "(uuid())"),
        (None, "varchar(30)", "", False, None),
        ("'none'", "varchar(30)", "", True, "'none'"),
        ("NULL", "varchar(30)", "", True, None),
    ],
)
def test_mysql_default(default, column_type, extra, mariadb, expected):
    assert mysql_default(default, column_type, extra, mariadb=mariadb) == expected
