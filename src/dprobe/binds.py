"""Bind variables: find :name placeholders, rewrite them per driver, and parse values."""

import json
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from dprobe.config import read_utf8
from dprobe.errors import UsageError
from dprobe.sqltext import first_keyword, lex

_BOOL = {"true": True, "1": True, "false": False, "0": False}
_CONVERTERS: dict[str, Callable[[str], Any]] = {
    "str": str,
    "int": int,
    "float": float,
    "decimal": Decimal,
    "date": date.fromisoformat,
    "datetime": datetime.fromisoformat,
    "bool": lambda raw: _BOOL[raw.lower()],
}
TYPES = (*_CONVERTERS, "null")

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Not after a word character or another colon, so 12:30 and ::fn aren't binds;
# := never matches because "=" can't start a name.
_PLACEHOLDER = re.compile(rf"(?<![\w:]):({_NAME.pattern})")
# DDL takes no bind variables on any of these databases, and Oracle trigger
# bodies use :NEW and :OLD, which aren't binds.
_DDL = {"CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "REVOKE", "COMMENT", "RENAME"}


def placeholders(sql: str, dialect: str) -> list[tuple[str, int, int]]:
    """(name, start, end) for each :name outside strings and comments.

    Names are lower-cased: they match values case-insensitively, as Oracle does.
    """
    if first_keyword(sql, dialect) in _DDL:
        return []
    found = []
    for kind, start, end in lex(sql, dialect):
        if kind == "code":
            found += [(m[1].lower(), m.start(), m.end()) for m in _PLACEHOLDER.finditer(sql, start, end)]
    return found


def bind(
    sql: str,
    dialect: str,
    placeholder: Callable[[str], tuple[str, str]],
    values: Mapping[str, Any],
) -> tuple[str, dict | None, set[str]]:
    """Return the SQL to send, its parameters (None without placeholders) and the names used.

    placeholder(name) gives the text that replaces :name and its params key,
    e.g. ("%(id)s", "id"). Only names the statement uses are passed, because
    oracledb rejects extras. Every missing name is reported at once.
    """
    found = placeholders(sql, dialect)
    names = list(dict.fromkeys(name for name, _, _ in found))
    if not names:
        return sql, None, set()
    if missing := [name for name in names if name not in values]:
        listed = ", ".join(f":{name}" for name in missing)
        raise UsageError(f"no value for {listed}; give one with -b NAME=VALUE or --binds FILE")
    parts, pos = [], 0
    for name, start, end in found:
        parts += [sql[pos:start], placeholder(name)[0]]
        pos = end
    parts.append(sql[pos:])
    return "".join(parts), {placeholder(name)[1]: values[name] for name in names}, set(names)


def parse_bind_arg(text: str) -> tuple[str, Any]:
    """Parse -b NAME[:TYPE]=VALUE. Without a TYPE the value stays a string."""
    key, sep, raw = text.partition("=")
    if not sep:
        raise UsageError(f"-b {text!r}: expected NAME[:TYPE]=VALUE")
    name, _, kind = key.partition(":")
    return _name(name, f"-b {text}"), convert(raw, kind or "str", f"-b {text}")


def load_binds_file(path: Path) -> dict[str, Any]:
    """Read a YAML or JSON (by .json suffix) mapping of NAME[:TYPE] to value.

    YAML 1.1 reads unquoted 01234 as octal 668 and yes/no as booleans, so
    quote such strings. With a :TYPE suffix the value is converted from its
    text, e.g. "amount:decimal": "12.50" keeps both digits.
    """
    text = read_utf8(path, UsageError)
    is_json = path.suffix.lower() == ".json"
    try:
        data = json.loads(text) if is_json else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        raise UsageError(f"{path}: invalid {'JSON' if is_json else 'YAML'}: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise UsageError(f"{path}: expected a mapping of bind names to values")
    values = {}
    for key, value in data.items():
        where = f"{path}: {key}"
        name, _, kind = str(key).partition(":")
        if isinstance(value, (dict, list)):
            raise UsageError(f"{where}: lists and mappings can't be bound")
        if kind:
            value = convert("" if value is None else str(value), kind, where)
        values[_name(name, where)] = value
    return values


def convert(raw: str, kind: str, where: str) -> Any:
    """Convert text to TYPE. bool takes true/false/1/0; null takes no text."""
    if kind not in TYPES:
        raise UsageError(f"{where}: unknown type {kind!r} (use {', '.join(TYPES)})")
    if kind == "null":
        if raw:
            raise UsageError(f"{where}: null takes no value, e.g. -b name:null=")
        return None
    try:
        return _CONVERTERS[kind](raw)
    except (ValueError, InvalidOperation, KeyError):
        raise UsageError(f"{where}: {raw!r} is not a valid {kind}") from None


def _name(name: str, where: str) -> str:
    if not _NAME.fullmatch(name):
        raise UsageError(f"{where}: invalid bind name {name!r}")
    return name.lower()
