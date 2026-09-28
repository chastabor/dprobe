"""Lexing of SQL text into code, quoted and comment spans, and statement-ending cleanup."""

import re
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


def lex(sql: str, dialect: str) -> Iterator[tuple[Kind, int, int]]:
    """Yield (kind, start, end) spans that cover sql end to end.

    "quoted" is any string literal or quoted identifier. Dialect differences:
    oracle has q'[...]' strings; mysql has backslash escapes, # comments, and
    "--" only starts a comment when followed by whitespace; mssql has
    [identifiers] and nested /* */ comments. An unterminated quote or comment
    runs to the end of the text.
    """
    code_start = i = 0
    while i < len(sql):
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
    return _PLSQL_START.match(_without_comments(sql, "oracle")) is not None


def first_keyword(sql: str, dialect: str) -> str:
    """The first word outside comments, upper-cased; "" if there is none."""
    match = re.match(r"\s*([A-Za-z_]\w*)", _without_comments(sql, dialect))
    return match[1].upper() if match else ""


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


def _without_comments(sql: str, dialect: str) -> str:
    return "".join(" " if kind == "comment" else sql[s:e] for kind, s, e in lex(sql, dialect))


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


def _block_comment_end(sql: str, i: int, *, nested: bool) -> int:
    depth, j = 0, i
    while j < len(sql):
        pair = sql[j : j + 2]
        if pair == "/*" and (nested or depth == 0):
            depth, j = depth + 1, j + 2
        elif pair == "*/":
            depth, j = depth - 1, j + 2
            if depth == 0:
                return j
        else:
            j += 1
    return len(sql)


def _quote_end(sql: str, i: int, close: str, *, backslash: bool) -> int:
    # A doubled closing character ('' or ]] or ``) is an escaped one.
    j = i + 1
    while j < len(sql):
        ch = sql[j]
        if backslash and ch == "\\":
            j += 2
        elif ch == close:
            if sql[j + 1 : j + 2] != close:
                return j + 1
            j += 2
        else:
            j += 1
    return len(sql)


def _starts_q_quote(sql: str, i: int) -> bool:
    # q'...' or nq'...', but not an identifier ending in q (e.g. "seq'").
    before = sql[:i]
    if before[-1:] in ("n", "N"):
        before = before[:-1]
    return not (before[-1:].isalnum() or before[-1:] in ("_", "$", "#"))


def _q_quote_end(sql: str, i: int) -> int:
    opener = sql[i + 2 : i + 3]
    if not opener:
        return len(sql)
    end = sql.find(_Q_CLOSERS.get(opener, opener) + "'", i + 3)
    return len(sql) if end == -1 else end + 2
