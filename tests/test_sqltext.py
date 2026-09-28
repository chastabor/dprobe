import pytest

from dprobe.connectors.mssql import MssqlConnector
from dprobe.connectors.oracle import OracleConnector
from dprobe.sqltext import first_keyword, is_plsql, lex, remove_trailing_line, remove_trailing_semicolon


def spans(sql, dialect):
    return [(kind, sql[s:e]) for kind, s, e in lex(sql, dialect)]


def test_lex_covers_text():
    sql = "SELECT 'a;b', \"x\" -- c;\n/* d; */ FROM t;"
    assert spans(sql, "oracle") == [
        ("code", "SELECT "), ("quoted", "'a;b'"), ("code", ", "), ("quoted", '"x"'),
        ("code", " "), ("comment", "-- c;"), ("code", "\n"), ("comment", "/* d; */"),
        ("code", " FROM t;"),
    ]
    assert "".join(text for _, text in spans(sql, "oracle")) == sql


@pytest.mark.parametrize(
    ("sql", "dialect", "quoted"),
    [
        ("'it''s'", "oracle", "'it''s'"),
        ("q'[it's]'", "oracle", "q'[it's]'"),
        ("nq'{a'b}'", "oracle", "q'{a'b}'"),
        ("q'!x!'", "oracle", "q'!x!'"),
        (r"'it\'s'", "mysql", r"'it\'s'"),
        ("`we``ird`", "mysql", "`we``ird`"),
        ("[a]]b]", "mssql", "[a]]b]"),
    ],
)
def test_lex_quotes(sql, dialect, quoted):
    assert ("quoted", quoted) in spans(sql, dialect)


def test_lex_dialect_comments():
    assert spans("5--3", "mysql") == [("code", "5--3")]
    assert ("comment", "-- x") in spans("5-- x", "mysql")
    assert ("comment", "# x") in spans("1 # x", "mysql")
    assert spans("1 # x", "oracle") == [("code", "1 # x")]
    assert spans("/* a /* b */ c */x", "mssql") == [("comment", "/* a /* b */ c */"), ("code", "x")]
    assert spans("/* a /* b */ c */x", "oracle")[0] == ("comment", "/* a /* b */")
    # An identifier ending in q isn't a q-quote.
    assert spans("seq'a'", "oracle") == [("code", "seq"), ("quoted", "'a'")]


def test_unterminated_runs_to_end():
    assert spans("SELECT 'abc", "oracle")[-1] == ("quoted", "'abc")
    assert spans("SELECT /* abc", "oracle")[-1] == ("comment", "/* abc")


@pytest.mark.parametrize(
    ("sql", "prepared"),
    [
        ("SELECT 1 FROM dual;", "SELECT 1 FROM dual"),
        ("SELECT 1 FROM dual;\n", "SELECT 1 FROM dual\n"),
        ("SELECT 1 FROM dual; -- done\n-- SELECT 2;\n", "SELECT 1 FROM dual -- done\n-- SELECT 2;\n"),
        ("SELECT ';' FROM dual", "SELECT ';' FROM dual"),
        ("SELECT 1 FROM dual;\n/\n", "SELECT 1 FROM dual\n\n"),
        ("SELECT 1 FROM dual;\n/\n-- end\n", "SELECT 1 FROM dual\n\n-- end\n"),
        ("BEGIN\n  NULL;\nEND;\n/", "BEGIN\n  NULL;\nEND;\n"),
        ("-- note\ndeclare x number; begin null; end;", "-- note\ndeclare x number; begin null; end;"),
        ("CREATE OR REPLACE PROCEDURE p AS BEGIN NULL; END;", "CREATE OR REPLACE PROCEDURE p AS BEGIN NULL; END;"),
        ("SELECT 10\n/ 2 FROM dual", "SELECT 10\n/ 2 FROM dual"),
    ],
)
def test_oracle_prepare(sql, prepared):
    assert OracleConnector.prepare(sql) == prepared


@pytest.mark.parametrize(
    ("sql", "prepared"),
    [
        ("SELECT 1\nGO\n", "SELECT 1\n\n"),
        ("SELECT 1\n  go  ", "SELECT 1\n  "),
        ("SELECT 1\nGO -- run it\n", "SELECT 1\n -- run it\n"),
        ("SELECT 1 /* GO */", "SELECT 1 /* GO */"),
        ("MERGE t USING s ON 1=1 WHEN MATCHED THEN DELETE;", "MERGE t USING s ON 1=1 WHEN MATCHED THEN DELETE;"),
        ("SELECT 1 AS GOAL", "SELECT 1 AS GOAL"),
    ],
)
def test_mssql_prepare(sql, prepared):
    assert MssqlConnector.prepare(sql) == prepared


def test_helpers():
    assert is_plsql("  /* x */ BEGIN NULL; END;")
    assert not is_plsql("SELECT 'BEGIN' FROM dual")
    assert not is_plsql("CREATE TABLE t (a NUMBER)")
    assert remove_trailing_semicolon("SELECT 1; /* c */", "oracle") == "SELECT 1 /* c */"
    assert remove_trailing_semicolon("SELECT ';'", "oracle") == "SELECT ';'"
    assert remove_trailing_line("a\n/", "/", "oracle") == "a\n"
    assert remove_trailing_line("'x\n/'", "/", "oracle") == "'x\n/'"
    assert first_keyword("-- c\n  update t set a = 1", "oracle") == "UPDATE"
    assert first_keyword("/* only a comment */", "oracle") == ""
    assert first_keyword("(SELECT 1)", "oracle") == ""
