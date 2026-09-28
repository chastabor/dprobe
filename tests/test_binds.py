from datetime import date, datetime
from decimal import Decimal

import pytest

from dprobe.binds import bind, convert, load_binds_file, parse_bind_arg, placeholders
from dprobe.connectors.mysql import MysqlConnector
from dprobe.connectors.oracle import OracleConnector
from dprobe.errors import UsageError


def names(sql, dialect="oracle"):
    return [name for name, _, _ in placeholders(sql, dialect)]


def test_placeholders_skip_strings_comments_and_non_binds():
    sql = """SELECT ':a', ":b", q'[:c]' -- :d
             /* :e */ FROM t WHERE x = :f AND y := 1 AND t::date AND '12:30' AND 12:30 AND (:G)"""
    assert names(sql) == ["f", "g"]
    assert names("SELECT `:a`, \"x :b\", :c # :d", "mysql") == ["c"]
    # MySQL has no [identifiers], so [:b] holds a real placeholder there.
    assert names("SELECT [:b]", "mysql") == ["b"]
    assert names("SELECT [:a], :b", "mssql") == ["b"]


def test_ddl_has_no_binds():
    trigger = "CREATE TRIGGER t BEFORE INSERT ON x FOR EACH ROW BEGIN :NEW.id := 1; END;"
    assert names(trigger) == []
    assert names("-- c\n  alter table x add y int default :v") == []


def test_bind_rewrites_per_driver():
    sql = "SELECT * FROM t WHERE a = :ID AND b LIKE 'x%' AND c = :id AND d = :other"
    values = {"id": 1, "other": "o", "unused": 3}
    assert bind(sql, "mysql", MysqlConnector.placeholder, values) == (
        "SELECT * FROM t WHERE a = %(id)s AND b LIKE 'x%' AND c = %(id)s AND d = %(other)s",
        {"id": 1, "other": "o"},
        {"id", "other"},
    )
    assert bind(sql, "oracle", OracleConnector.placeholder, values) == (
        "SELECT * FROM t WHERE a = :id_ AND b LIKE 'x%' AND c = :id_ AND d = :other_",
        {"id_": 1, "other_": "o"},
        {"id", "other"},
    )


def test_bind_without_placeholders_passes_no_params():
    assert bind("SELECT '100%'", "mysql", MysqlConnector.placeholder, {"x": 1}) == ("SELECT '100%'", None, set())


def test_bind_reports_every_missing_name():
    with pytest.raises(UsageError, match=r"no value for :a, :c; give one with -b NAME=VALUE"):
        bind("SELECT :a, :b, :c, :a", "oracle", OracleConnector.placeholder, {"b": 1})


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("id=100", ("id", "100")),
        ("ID:int=100", ("id", 100)),
        ("n:float=1.5", ("n", 1.5)),
        ("amt:decimal=12.50", ("amt", Decimal("12.50"))),
        ("d:date=2024-01-02", ("d", date(2024, 1, 2))),
        ("t:datetime=2024-01-02 03:04:05", ("t", datetime(2024, 1, 2, 3, 4, 5))),
        ("ok:bool=TRUE", ("ok", True)),
        ("ok:bool=0", ("ok", False)),
        ("x:null=", ("x", None)),
        ("s=a=b", ("s", "a=b")),
        ("zip=01234", ("zip", "01234")),
        ("empty=", ("empty", "")),
    ],
)
def test_parse_bind_arg(arg, expected):
    assert parse_bind_arg(arg) == expected


@pytest.mark.parametrize(
    ("arg", "message"),
    [
        ("id", "expected NAME\\[:TYPE\\]=VALUE"),
        ("1x=5", "invalid bind name '1x'"),
        ("id:number=5", "unknown type 'number'"),
        ("id:int=abc", "'abc' is not a valid int"),
        ("d:date=01/02/2024", "is not a valid date"),
        ("a:decimal=x", "is not a valid decimal"),
        ("b:bool=yes", "'yes' is not a valid bool"),
        ("x:null=0", "null takes no value"),
    ],
)
def test_parse_bind_arg_errors(arg, message):
    with pytest.raises(UsageError, match=message):
        parse_bind_arg(arg)


def test_binds_file_yaml(tmp_path):
    path = tmp_path / "b.yaml"
    path.write_text('ID: 5\nd: 2024-01-02\nname: "01234"\nnothing: null\n"amt:decimal": "12.50"\n')
    assert load_binds_file(path) == {
        "id": 5, "d": date(2024, 1, 2), "name": "01234", "nothing": None, "amt": Decimal("12.50"),
    }


def test_binds_file_json(tmp_path):
    path = tmp_path / "b.json"
    # With the byte-order mark Notepad writes, which json.loads rejects on its own.
    path.write_text('\ufeff{"id": 5, "d:date": "2024-01-02", "n": null}', encoding="utf-8")
    assert load_binds_file(path) == {"id": 5, "d": date(2024, 1, 2), "n": None}


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        ("b.yaml", "- 1\n- 2\n", "expected a mapping"),
        ("b.yaml", "ids: [1, 2]\n", "lists and mappings can't be bound"),
        ("b.json", "{bad", "invalid JSON"),
        ("b.yaml", "a: [\n", "invalid YAML"),
        ("b.yaml", "'bad name': 1\n", "invalid bind name 'bad name'"),
    ],
)
def test_binds_file_errors(tmp_path, name, text, message):
    path = tmp_path / name
    path.write_text(text)
    with pytest.raises(UsageError, match=message):
        load_binds_file(path)


def test_binds_file_empty_and_missing(tmp_path):
    (tmp_path / "empty.yaml").write_text("")
    assert load_binds_file(tmp_path / "empty.yaml") == {}
    with pytest.raises(UsageError, match="cannot read"):
        load_binds_file(tmp_path / "missing.yaml")


def test_convert_str_is_untouched():
    assert convert(" 12 ", "str", "x") == " 12 "
