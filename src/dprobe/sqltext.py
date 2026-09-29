"""Lexing of SQL text into code, quoted and comment spans, and statement-ending cleanup."""

import re
from bisect import bisect_right
from collections.abc import Iterator
from typing import Literal

Kind = Literal["code", "quoted", "comment"]

_Q_CLOSERS = {"[": "]", "{": "}", "(": ")", "<": ">"}
_MYSQL_DASH_FOLLOWERS = ("", " ", "\t", "\n", "\r")
_PLSQL_START = re.compile(
    r"\s*(BEGIN|DECLARE|CREATE\s+(OR\s+REPLACE\s+)?((NON)?EDITIONABLE\s+)?"
    r"(PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE))\b",
    re.IGNORECASE,
)
# Where a quote or comment could start, so lex() can skip plain code quickly.
_CANDIDATES = {
    "oracle": re.compile(r"--|/\*|[qQ]'|['\"]"),
    "mysql": re.compile(r"--|#|/\*|['\"`]"),
    "mssql": re.compile(r"--|/\*|['\"\[]"),
}
_FIRST_CODE = re.compile(r"\S")
# Enough leading code to read the first keywords, e.g. CREATE OR REPLACE PROCEDURE.
_PREFIX = 200


def lex(sql: str, dialect: str) -> Iterator[tuple[Kind, int, int]]:
    """Yield (kind, start, end) spans that cover sql end to end.

    "quoted" is any string literal or quoted identifier. Dialect differences:
    oracle has q'[...]' strings; mysql has backslash escapes, # comments, and
    "--" only starts a comment when followed by whitespace; mssql has
    [identifiers] and nested /* */ comments. An unterminated quote or comment
    runs to the end of the text.
    """
    candidates = _CANDIDATES.get(dialect, _CANDIDATES["oracle"])
    code_start = i = 0
    while match := candidates.search(sql, i):
        i = match.start()
        special = _special_at(sql, i, dialect)
        if special is None:
            i += 1
            continue
        kind, end = special
        if code_start < i:
            yield "code", code_start, i
        yield kind, i, end
        code_start = i = end
    if code_start < len(sql):
        yield "code", code_start, len(sql)


def is_plsql(sql: str) -> bool:
    """True for anonymous blocks and CREATE PROCEDURE/FUNCTION/PACKAGE/TRIGGER/TYPE."""
    return _PLSQL_START.match(_code_prefix(sql, "oracle")) is not None


def first_keyword(sql: str, dialect: str) -> str:
    """The first word outside comments, upper-cased; "" if there is none."""
    match = re.match(r"\s*([A-Za-z_]\w*)", _code_prefix(sql, dialect))
    return match[1].upper() if match else ""


_GO_LINE = re.compile(r"^[ \t]*GO[ \t]*$", re.IGNORECASE | re.MULTILINE)
_SLASH_LINE = re.compile(r"^[ \t]*/[ \t]*$", re.MULTILINE)
_DELIMITER_LINE = re.compile(r"^[ \t]*DELIMITER\b", re.IGNORECASE | re.MULTILINE)
_SEMICOLON = re.compile(";")


def split_statements(sql: str, dialect: str) -> list[tuple[int, str]]:
    """Split a script into (line number of the first code, statement) pairs.

    mssql splits on GO lines, into batches that may each hold several
    statements. oracle and mysql split after a ";" outside strings and
    comments; an Oracle PL/SQL block (BEGIN, DECLARE, CREATE PROCEDURE, ...)
    runs to a "/" line instead, as in SQL*Plus. Each piece keeps its
    terminator for prepare() to remove, and pieces with no code are dropped.
    MySQL's DELIMITER is a mysql-client command and raises ValueError.
    """
    spans = list(lex(sql, dialect))
    code = [(start, end) for kind, start, end in spans if kind == "code"]
    code_starts = [start for start, _ in code]
    # Same length as sql with comments blanked, for finding keywords and the first code.
    blank = "".join(" " * (end - start) if kind == "comment" else sql[start:end] for kind, start, end in spans)

    def in_code(pos: int) -> bool:
        i = bisect_right(code_starts, pos) - 1
        return i >= 0 and pos < code[i][1]

    def ends(pattern: re.Pattern[str]) -> list[int]:
        return [m.end() for m in pattern.finditer(sql) if in_code(m.start())]

    def after(positions: list[int], pos: int) -> int:
        i = bisect_right(positions, pos)
        return positions[i] if i < len(positions) else len(sql)

    if dialect == "mysql" and ends(_DELIMITER_LINE):
        raise ValueError("DELIMITER is a mysql client command; dprobe runs each statement as written")
    semicolons = [] if dialect == "mssql" else ends(_SEMICOLON)
    lines = ends(_GO_LINE if dialect == "mssql" else _SLASH_LINE) if dialect in ("mssql", "oracle") else []
    pieces, pos, line, counted = [], 0, 1, 0
    while pos < len(sql):
        if dialect == "mssql" or (dialect == "oracle" and _PLSQL_START.match(blank, pos)):
            end = after(lines, pos)
        else:
            end = min(after(semicolons, pos), after(lines, pos))
        # The line is where the SQL itself starts, after any leading comments.
        if first := _FIRST_CODE.search(blank, pos, end):
            line += sql.count("\n", counted, first.start())
            counted = first.start()
            pieces.append((line, sql[pos:end]))
        pos = end
    return pieces


def split_name(text: str) -> list[tuple[str, bool]]:
    """Split a dotted name such as schema.table into (part, quoted) pairs.

    A part may be quoted with "...", [...] or `...`, which keeps its dots and
    case; a doubled closing character inside is an escaped one.
    """
    parts, i = [], 0
    while True:
        close = {'"': '"', "[": "]", "`": "`"}.get(text[i : i + 1])
        if close:
            end = _quote_end(text, i, close, backslash=False)
            if text[end - 1 : end] != close or end == i + 1:
                raise ValueError(f"unterminated quote in {text!r}")
            parts.append((text[i + 1 : end - 1].replace(close * 2, close), True))
        else:
            end = text.find(".", i)
            end = len(text) if end == -1 else end
            parts.append((text[i:end].strip(), False))
        if not parts[-1][0]:
            raise ValueError(f"empty name in {text!r}")
        if end == len(text):
            return parts
        if text[end] != ".":
            raise ValueError(f"expected '.' after a quoted name in {text!r}")
        i = end + 1


def remove_trailing_semicolon(sql: str, dialect: str) -> str:
    """Drop the statement's final ";", even when comments follow it."""
    end = _last_code_end(sql, dialect)
    if end is None or sql[end - 1] != ";":
        return sql
    return sql[: end - 1] + sql[end:]


def remove_trailing_line(sql: str, pattern: str, dialect: str) -> str:
    """Drop a final line such as "/" or "GO" (pattern, any case), even when comments follow it."""
    end = _last_code_end(sql, dialect)
    if end is None:
        return sql
    start = sql.rfind("\n", 0, end) + 1
    if not re.fullmatch(pattern, sql[start:end].strip(), re.IGNORECASE):
        return sql
    return sql[:start] + sql[end:]


def has_code(sql: str, dialect: str) -> bool:
    """True unless sql is only whitespace and comments."""
    return any(kind != "comment" and not sql[s:e].isspace() for kind, s, e in lex(sql, dialect))


def _code_prefix(sql: str, dialect: str) -> str:
    """The start of sql with comments blanked, stopping after _PREFIX characters of code."""
    parts, size = [], 0
    for kind, start, end in lex(sql, dialect):
        if kind == "comment":
            parts.append(" ")
            continue
        parts.append(sql[start : min(end, start + _PREFIX - size)])
        size += end - start
        if size >= _PREFIX:
            break
    return "".join(parts)


def _last_code_end(sql: str, dialect: str) -> int | None:
    """End of the last non-blank code, or None if the text ends in a quoted span."""
    last = None
    for kind, start, end in lex(sql, dialect):
        if kind == "quoted" or (kind == "code" and sql[start:end].strip()):
            last = kind, start, end
    if last is None or last[0] != "code":
        return None
    _, start, end = last
    return start + len(sql[start:end].rstrip())


def _special_at(sql: str, i: int, dialect: str) -> tuple[Kind, int] | None:
    c, nxt = sql[i], sql[i + 1 : i + 2]
    if c == "-" and nxt == "-":
        if dialect != "mysql" or sql[i + 2 : i + 3] in _MYSQL_DASH_FOLLOWERS:
            return "comment", _line_end(sql, i)
    elif c == "#" and dialect == "mysql":
        return "comment", _line_end(sql, i)
    elif c == "/" and nxt == "*":
        return "comment", _block_comment_end(sql, i, nested=dialect == "mssql")
    elif c in "qQ" and nxt == "'" and dialect == "oracle" and _starts_q_quote(sql, i):
        return "quoted", _q_quote_end(sql, i)
    elif c in "'\"":
        return "quoted", _quote_end(sql, i, c, backslash=dialect == "mysql")
    elif c == "`" and dialect == "mysql":
        return "quoted", _quote_end(sql, i, "`", backslash=False)
    elif c == "[" and dialect == "mssql":
        return "quoted", _quote_end(sql, i, "]", backslash=False)
    return None


def _line_end(sql: str, i: int) -> int:
    end = sql.find("\n", i)
    return len(sql) if end == -1 else end


_COMMENT_MARKS = re.compile(r"/\*|\*/")


def _block_comment_end(sql: str, i: int, *, nested: bool) -> int:
    if not nested:
        end = sql.find("*/", i + 2)
        return len(sql) if end == -1 else end + 2
    depth = 0
    for mark in _COMMENT_MARKS.finditer(sql, i):
        depth += 1 if mark[0] == "/*" else -1
        if depth == 0:
            return mark.end()
    return len(sql)


def _quote_end(sql: str, i: int, close: str, *, backslash: bool) -> int:
    # A doubled closing character ('' or ]] or ``) is an escaped one.
    stop = re.compile(r"\\.|" + re.escape(close), re.DOTALL) if backslash else None
    j = i + 1
    while True:
        if stop:
            match = stop.search(sql, j)
            j = match.start() if match else -1
            if match and match[0] != close:
                j = match.end()
                continue
        else:
            j = sql.find(close, j)
        if j == -1:
            return len(sql)
        if sql[j + 1 : j + 2] != close:
            return j + 1
        j += 2


def _starts_q_quote(sql: str, i: int) -> bool:
    # q'...' or nq'...', but not an identifier ending in q (e.g. "seq'").
    j = i - 1
    if j >= 0 and sql[j] in "nN":
        j -= 1
    return j < 0 or not (sql[j].isalnum() or sql[j] in "_$#")


def _q_quote_end(sql: str, i: int) -> int:
    opener = sql[i + 2 : i + 3]
    if not opener:
        return len(sql)
    end = sql.find(_Q_CLOSERS.get(opener, opener) + "'", i + 3)
    return len(sql) if end == -1 else end + 2
