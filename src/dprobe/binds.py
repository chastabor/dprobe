"""Bind variables: find :name placeholders, rewrite them per driver, and parse values."""

import csv
import json
import re
from dataclasses import dataclass, field
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from dprobe import yaml12
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


_DECLARATION = re.compile(
    r"--\s*@bind\s+(?P<name>\S+)(?:\s+(?P<kind>[A-Za-z]*\[\]|[A-Za-z]+))?\s*(?:=\s*(?P<default>.*?))?\s*"
)


@dataclass(frozen=True)
class Declared:
    """A "-- @bind NAME [TYPE] [= DEFAULT]" comment in the SQL."""

    kind: str | None
    default: str | None
    line: str


@dataclass(frozen=True)
class BindValues:
    """Where bind values come from, highest priority first.

    given holds -b values over --binds ones, already converted (see
    parse_bind_arg); a declared default fills a gap; ask (a prompt, None
    without a terminal) fills the rest.
    """

    given: Mapping[str, Any] = field(default_factory=dict)
    declared: Mapping[str, Declared] = field(default_factory=dict)
    ask: Callable[[str], str] | None = None

    def resolve(self, names: list[str]) -> dict[str, Any]:
        values, missing = {}, []
        for name in names:
            declared = self.declared.get(name)
            if name in self.given:
                values[name] = self.given[name]
            elif declared and declared.default is not None:
                values[name] = convert(declared.default, declared.kind or "str", declared.line)
            else:
                missing.append(name)
        if missing and self.ask is None:
            listed = ", ".join(f":{name}" for name in missing)
            raise UsageError(f"no value for {listed}; give one with -b NAME=VALUE or --binds FILE")
        for name in missing:
            values[name] = self._prompt(name)
        return values

    def _prompt(self, name: str) -> Any:
        declared = self.declared.get(name)
        kind = (declared.kind if declared else None) or "str"
        problem = ""
        while True:
            try:
                answer = self.ask(f"{problem}:{name} ({kind}): ")
            except EOFError:
                raise UsageError(f"no value for :{name} (prompt cancelled)") from None
            try:
                return convert(answer, kind, f":{name}")
            except UsageError as e:
                problem = f"{e}\n"


def declarations(sql: str, dialect: str) -> dict[str, Declared]:
    """The -- @bind lines in the SQL's comments, by lower-cased name."""
    found = {}
    for kind, start, end in lex(sql, dialect):
        text = sql[start:end]
        if kind != "comment" or not re.match(r"--\s*@bind\b", text):
            continue
        match = _DECLARATION.fullmatch(text.rstrip())
        if not match:
            raise UsageError(f"{text.strip()!r}: expected -- @bind NAME [TYPE] [= DEFAULT]")
        where = text.strip()
        kind_name = match["kind"]
        if kind_name:
            _check_kind(kind_name, where)
        default = match["default"]
        # Quotes keep leading or trailing spaces: -- @bind pad str = ' x '
        if default and len(default) >= 2 and default[0] == default[-1] and default[0] in "'\"":
            default = default[1:-1]
        found[_name(match["name"], where)] = Declared(kind_name, default, where)
    return found


def bind(sql: str, dialect: str, placeholder: Callable[[str], tuple[str, str]],
         values: Mapping[str, Any]) -> tuple[str, dict | None]:
    """Return the SQL to send and its parameters, None when it has no placeholders.

    placeholder(name) gives the text that replaces :name and its params key,
    e.g. ("%(id)s", "id"). A list value becomes one placeholder per element,
    :ids -> :ids__0, :ids__1, ..., for IN (:ids). Only names the statement
    uses are passed, because oracledb rejects extras.
    """
    return _rewrite(sql, placeholders(sql, dialect), placeholder, values)


def bind_script(sqls: list[str], dialect: str, placeholder: Callable[[str], tuple[str, str]],
                values: BindValues) -> tuple[list[tuple[str, dict | None]], set[str]]:
    """bind() each statement, resolving every name once; also return the names used.

    Resolving up front asks for a name used twice only once, and reports
    every missing name in one error.
    """
    found = [placeholders(sql, dialect) for sql in sqls]
    names = list(dict.fromkeys(name for spots in found for name, _, _ in spots))
    resolved = values.resolve(names)
    return [_rewrite(sql, spots, placeholder, resolved) for sql, spots in zip(sqls, found, strict=True)], set(names)


def parse_bind_arg(text: str, declared: Mapping[str, Declared] | None = None) -> tuple[str, Any]:
    """Parse -b NAME[:TYPE]=VALUE, converting by TYPE, else the declared type, else keeping text."""
    key, sep, raw = text.partition("=")
    if not sep:
        raise UsageError(f"-b {text!r}: expected NAME[:TYPE]=VALUE")
    name, _, kind = key.partition(":")
    name = _name(name, f"-b {text}")
    return name, convert(raw, kind or _declared_kind(declared, name), f"-b {text}")


def load_binds_file(path: Path, declared: Mapping[str, Declared] | None = None) -> dict[str, Any]:
    """Read a YAML or JSON (by .json suffix) mapping of NAME[:TYPE] to value.

    YAML follows the 1.2 core schema (see yaml12), so dates stay text: give
    them a type, e.g. "hired:date": 2024-01-02. With a :TYPE suffix the value
    is converted from its text, e.g. "amount:decimal": "12.50" keeps both
    digits; a declared type converts string values only.
    """
    text = read_utf8(path, UsageError)
    is_json = path.suffix.lower() == ".json"
    try:
        data = json.loads(text) if is_json else yaml12.load(text)
    except (json.JSONDecodeError, yaml12.YAMLError) as e:
        raise UsageError(f"{path}: invalid {'JSON' if is_json else 'YAML'}: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise UsageError(f"{path}: expected a mapping of bind names to values")
    values = {}
    for key, value in data.items():
        where = f"{path}: {key}"
        name, _, kind = str(key).partition(":")
        name = _name(name, where)
        if isinstance(value, dict) or (isinstance(value, list) and any(isinstance(v, (dict, list)) for v in value)):
            raise UsageError(f"{where}: only values and lists of values can be bound")
        if isinstance(value, list) and kind:
            if not kind.endswith("[]"):
                raise UsageError(f"{where}: the value is a list, so give a list type like {kind}[]")
            value = [convert("" if v is None else str(v), kind[:-2] or "str", where) for v in value]
        elif kind:
            value = convert("" if value is None else str(value), kind, where)
        elif isinstance(value, str):
            value = convert(value, _declared_kind(declared, name), where)
        values[name] = value
    return values


def convert(raw: str, kind: str, where: str) -> Any:
    """Convert text to TYPE. bool takes true/false/1/0; null takes no text.

    TYPE[] (or [] for text) makes a list from comma-separated text, e.g.
    1,2,3; quote an element that holds a comma: "a,b",c.
    """
    _check_kind(kind, where)
    if kind.endswith("[]"):
        return [convert(part, kind[:-2] or "str", where) for part in next(csv.reader([raw], skipinitialspace=True), [])]
    if kind == "null":
        if raw:
            raise UsageError(f"{where}: null takes no value, e.g. -b name:null=")
        return None
    try:
        return _CONVERTERS[kind](raw)
    except (ValueError, InvalidOperation, KeyError):
        raise UsageError(f"{where}: {raw!r} is not a valid {kind}") from None


def _declared_kind(declared: Mapping[str, Declared] | None, name: str) -> str:
    found = (declared or {}).get(name)
    return (found.kind if found else None) or "str"


def _rewrite(sql: str, found: list[tuple[str, int, int]], placeholder: Callable[[str], tuple[str, str]],
             values: Mapping[str, Any]) -> tuple[str, dict | None]:
    names = list(dict.fromkeys(name for name, _, _ in found))
    if not names:
        return sql, None
    texts, params = {}, {}
    for name in names:
        value = values[name]
        if not isinstance(value, list):
            text, key = placeholder(name)
            texts[name], params[key] = text, value
            continue
        if not value:
            raise UsageError(f":{name} is an empty list, and IN () isn't valid SQL")
        elements = [f"{name}__{i}" for i in range(len(value))]
        if clash := set(elements) & set(names):
            raise UsageError(f":{name} is a list, and its elements' names clash with :{', :'.join(sorted(clash))}")
        texts[name] = ", ".join(placeholder(element)[0] for element in elements)
        params.update((placeholder(element)[1], item) for element, item in zip(elements, value, strict=True))
    parts, pos = [], 0
    for name, start, end in found:
        parts += [sql[pos:start], texts[name]]
        pos = end
    parts.append(sql[pos:])
    return "".join(parts), params


def _check_kind(kind: str, where: str) -> None:
    if kind not in ("null", "[]") and kind.removesuffix("[]") not in _CONVERTERS:
        raise UsageError(f"{where}: unknown type {kind!r} (use {', '.join(TYPES)}, or TYPE[] for a list)")


def _name(name: str, where: str) -> str:
    if not _NAME.fullmatch(name):
        raise UsageError(f"{where}: invalid bind name {name!r}")
    return name.lower()
