from datetime import date, datetime
from decimal import Decimal

import pytest

from dprobe.binds import (
    BindValues, Declared, bind, bind_script, convert, declarations, load_binds_file, parse_bind_arg, placeholders,
)
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
    )
    assert bind(sql, "oracle", OracleConnector.placeholder, values) == (
        "SELECT * FROM t WHERE a = :id_ AND b LIKE 'x%' AND c = :id_ AND d = :other_",
        {"id_": 1, "other_": "o"},
    )


def test_bind_without_placeholders_passes_no_params():
    assert bind("SELECT '100%'", "mysql", MysqlConnector.placeholder, {}) == ("SELECT '100%'", None)


def test_bind_reports_every_missing_name():
    with pytest.raises(UsageError, match=r"no value for :a, :c; give one with -b NAME=VALUE"):
        bind_script(["SELECT :a, :b", "SELECT :c, :a"], "oracle", OracleConnector.placeholder, BindValues({"b": 1}))


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
        ("s:str=5", ("s", "5")),
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
        "id": 5, "d": date(2024, 1, 2), "name": "01234",
        "nothing": None, "amt": Decimal("12.50"),
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
        ("b.yaml", "ids: {a: 1}\n", "only values and lists of values"),
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


def test_declarations():
    sql = """-- @bind emp_id int
-- @bind Start date = 2024-01-02
-- @bind pad str = ' x '
-- @bind plain = abc
-- a comment mentioning @bind later
SELECT :emp_id, ':x -- @bind no' FROM t"""
    found = declarations(sql, "oracle")
    assert {name: (d.kind, d.default) for name, d in found.items()} == {
        "emp_id": ("int", None), "start": ("date", "2024-01-02"), "pad": ("str", " x "), "plain": (None, "abc"),
    }


@pytest.mark.parametrize(
    ("line", "message"),
    [("-- @bind", "expected -- @bind NAME"), ("-- @bind x number", "unknown type 'number'"),
     ("-- @bind 1x", "invalid bind name '1x'"), ("-- @bind x int 5", "expected -- @bind NAME")],
)
def test_declaration_errors(line, message):
    with pytest.raises(UsageError, match=message):
        declarations(line + "\nSELECT 1", "oracle")


def test_declared_types_and_defaults():
    declared = {"id": Declared("int", "7", "-- @bind id int = 7"), "d": Declared("date", None, "-- @bind d date"),
                "s": Declared("int", None, "-- @bind s int")}
    # A declared type converts untyped text, but an explicit type wins.
    assert parse_bind_arg("d=2024-01-02", declared) == ("d", date(2024, 1, 2))
    assert parse_bind_arg("s:str=x", declared) == ("s", "x")
    with pytest.raises(UsageError, match=r"-b d=soon: 'soon' is not a valid date"):
        parse_bind_arg("d=soon", declared)
    # A declared default fills a gap.
    assert BindValues(given={"d": date(2024, 1, 2)}, declared=declared).resolve(["id", "d"]) == {
        "id": 7, "d": date(2024, 1, 2)}


def test_bind_values_prompt_retries_and_cancels():
    asked = []
    answers = iter(["abc", "42"])

    def ask(prompt):
        asked.append(prompt)
        return next(answers)

    declared = {"n": Declared("int", None, "-- @bind n int")}
    assert BindValues(declared=declared, ask=ask).resolve(["n"]) == {"n": 42}
    assert asked == [":n (int): ", ":n: 'abc' is not a valid int\n:n (int): "]

    def cancel(prompt):
        raise EOFError

    with pytest.raises(UsageError, match="no value for :n \\(prompt cancelled\\)"):
        BindValues(ask=cancel).resolve(["n"])


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("ids:int[]=1, 2,3", ("ids", [1, 2, 3])),
        ('c:[]=a,"b,c"', ("c", ["a", "b,c"])),
        ("d:date[]=2024-01-02", ("d", [date(2024, 1, 2)])),
        ("x:str[]=", ("x", [])),
    ],
)
def test_parse_list_bind_arg(arg, expected):
    assert parse_bind_arg(arg) == expected


@pytest.mark.parametrize("arg", ["n:null[]=", "z:bogus[]=1", "q:[][]=1"])
def test_parse_list_bind_arg_errors(arg):
    with pytest.raises(UsageError, match="unknown type"):
        parse_bind_arg(arg)


def test_bind_expands_lists():
    sql = "SELECT * FROM t WHERE id IN (:ids) AND x = :x OR id IN (:IDS)"
    assert bind(sql, "oracle", OracleConnector.placeholder, {"ids": [1, 2], "x": "q"}) == (
        "SELECT * FROM t WHERE id IN (:ids__0_, :ids__1_) AND x = :x_ OR id IN (:ids__0_, :ids__1_)",
        {"ids__0_": 1, "ids__1_": 2, "x_": "q"},
    )
    assert bind("SELECT :ids", "mysql", MysqlConnector.placeholder, {"ids": ["a"]}) == (
        "SELECT %(ids__0)s", {"ids__0": "a"})


def test_bind_list_errors():
    with pytest.raises(UsageError, match="empty list, and IN \\(\\) isn't valid SQL"):
        bind("SELECT :ids", "mysql", MysqlConnector.placeholder, {"ids": []})
    with pytest.raises(UsageError, match="clash with :ids__0"):
        bind("SELECT :ids, :ids__0", "mysql", MysqlConnector.placeholder, {"ids": [1], "ids__0": 2})


def test_binds_file_lists(tmp_path):
    path = tmp_path / "b.yaml"
    path.write_text('ids: [1, 2]\n"codes:str[]": [1, "x"]\n')
    assert load_binds_file(path) == {"ids": [1, 2], "codes": ["1", "x"]}
    path.write_text('"ids:int": [1, 2]\n')
    with pytest.raises(UsageError, match="give a list type like int\\[\\]"):
        load_binds_file(path)
    path.write_text("ids: [[1]]\n")
    with pytest.raises(UsageError, match="only values and lists of values"):
        load_binds_file(path)


def test_declared_list_type():
    declared = declarations("-- @bind ids int[] = 1,2\nSELECT :ids", "oracle")
    assert declared["ids"].kind == "int[]"
    assert BindValues(declared=declared).resolve(["ids"]) == {"ids": [1, 2]}
    assert parse_bind_arg("ids=3, 4", declared) == ("ids", [3, 4])


def test_binds_file_uses_declared_types_for_strings(tmp_path):
    path = tmp_path / "b.yaml"
    path.write_text('d: "2024-01-02"\nn: 5\n')
    declared = {"d": Declared("date", None, ""), "n": Declared("decimal", None, "")}
    # Only string values are converted; YAML already typed n.
    assert load_binds_file(path, declared) == {"d": date(2024, 1, 2), "n": 5}


def test_bind_script_resolves_once():
    asked = []
    values = BindValues(ask=lambda prompt: asked.append(prompt) or "1")
    bound, used = bind_script(["SELECT :a", "SELECT :a, :b"], "mysql", MysqlConnector.placeholder, values)
    assert bound == [("SELECT %(a)s", {"a": "1"}), ("SELECT %(a)s, %(b)s", {"a": "1", "b": "1"})]
    assert (used, asked) == ({"a", "b"}, [":a (str): ", ":b (str): "])
