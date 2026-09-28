"""Render query results as table, csv, tsv, json or jsonl."""

import csv
import json
import math
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from itertools import chain, islice
from typing import Any, Protocol, TextIO

_TABLE_ESCAPES = str.maketrans({"\n": "\\n", "\r": "\\r", "\t": "\\t"})
_BINARY = (bytes, bytearray, memoryview)
_PLAIN_TEXT = (str, int, float)
# Shared, because json.dumps() builds a new encoder per call when given options.
# ensure_ascii=False keeps non-ASCII text readable; output is UTF-8.
_JSON = json.JSONEncoder(ensure_ascii=False, default=str)


class Rows(Protocol):
    columns: list[str]

    def rows(self) -> Iterator[tuple]: ...


def write_result(
    result: Rows,
    fmt: str,
    out: TextIO,
    *,
    null: str | None = None,
    max_rows: int | None = None,
    max_width: int | None = None,
) -> tuple[int, bool]:
    """Write result's rows to out; return (rows written, whether rows were left unread).

    max_rows and max_width of None mean no limit; max_width only affects
    table cells. null is the text for NULL, by default "NULL" in table and
    empty in csv/tsv; JSON always writes null.
    """
    rows = result.rows()
    limited = islice(rows, max_rows) if max_rows is not None else rows
    if fmt == "table":
        count = _write_table(result.columns, limited, out, null="NULL" if null is None else null,
                             max_width=max_width)
    elif fmt in ("csv", "tsv"):
        count = _write_delimited(result.columns, limited, out, null=null or "",
                                 delimiter="," if fmt == "csv" else "\t")
    else:
        count = _write_json(result.columns, limited, out, lines=fmt == "jsonl")
    # Peek one row past the limit to tell the caller the output was cut short.
    more = max_rows is not None and next(rows, None) is not None
    return count, more


def format_table(columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """Left-aligned columns under a dashed header rule. None renders as an empty cell."""
    cells = [["" if value is None else str(value) for value in row] for row in rows]
    return "\n".join(_table_lines(columns, cells))


def text_value(value: Any, null: str = "") -> str:
    """Plain-text form of a database value.

    bytes become 0x-prefixed hex; Decimals never use exponent notation; MySQL
    SET values join with commas; MySQL TIME and Oracle INTERVAL values
    (timedelta) print as [-]H:MM:SS[.ffffff], hours unbounded.
    """
    if value is None:
        return null
    if type(value) in _PLAIN_TEXT:
        return str(value)
    if isinstance(value, timedelta):
        return _format_timedelta(value)
    if isinstance(value, _BINARY):
        return "0x" + bytes(value).hex()
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else str(value)
    if isinstance(value, (set, frozenset)):
        return ",".join(sorted(map(str, value)))
    if isinstance(value, (dict, list)):
        return _JSON.encode(value)
    return str(value)


def json_value(value: Any) -> str:
    """Encode one value as JSON.

    Decimals are written as bare numbers with every digit; the json module
    would need them converted to float or string first. NaN and Infinity,
    which aren't valid JSON numbers, become strings.
    """
    # encode() is only fast for str; for other types it builds an encoder per call.
    if type(value) is str:
        return _JSON.encode(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    if isinstance(value, Decimal) and value.is_finite():
        return format(value, "f")
    if isinstance(value, (datetime, date, time)):
        return _JSON.encode(value.isoformat())
    if isinstance(value, (set, frozenset)):
        return _JSON.encode(sorted(map(str, value)))
    if isinstance(value, (dict, list)):
        return _JSON.encode(value)
    return _JSON.encode(text_value(value))


def json_keys(columns: Sequence[str]) -> list[str]:
    """Object keys for columns, made unique so no value is lost.

    "SELECT a.id, b.id" gives id, id_2; unnamed columns (SQL Server "SELECT 1")
    become column1, column2, ... by position.
    """
    keys: list[str] = []
    for position, name in enumerate(columns, 1):
        key = name or f"column{position}"
        candidate, n = key, 2
        while candidate in keys:
            candidate, n = f"{key}_{n}", n + 1
        keys.append(candidate)
    return keys


def _format_timedelta(value: timedelta) -> str:
    sign = "-" if value < timedelta(0) else ""
    value = abs(value)
    minutes, seconds = divmod(value.days * 86400 + value.seconds, 60)
    hours, minutes = divmod(minutes, 60)
    fraction = f".{value.microseconds:06d}" if value.microseconds else ""
    return f"{sign}{hours}:{minutes:02d}:{seconds:02d}{fraction}"


def _table_lines(columns: Sequence[str], cells: list[list[str]]) -> Iterator[str]:
    widths = [max([len(name), *(len(row[i]) for row in cells)]) for i, name in enumerate(columns)]
    for line in chain([columns, ["-" * w for w in widths]], cells):
        yield "  ".join(v.ljust(w) for v, w in zip(line, widths, strict=True)).rstrip()


def _write_table(columns, rows, out, *, null, max_width) -> int:
    cells = [[_table_cell(v, null, max_width) for v in row] for row in rows]
    out.writelines(line + "\n" for line in _table_lines(columns, cells))
    return len(cells)


def _table_cell(value: Any, null: str, max_width: int | None) -> str:
    if max_width is not None:
        # Cut before converting, so a multi-MB CLOB or BLOB isn't converted in
        # full; hex and escapes only lengthen the text.
        if isinstance(value, str):
            value = value[: max_width + 1]
        elif isinstance(value, _BINARY):
            value = value[: max_width // 2 + 1]
    # Control characters would break the alignment, so they're shown escaped.
    text = text_value(value, null).translate(_TABLE_ESCAPES)
    if max_width is not None and len(text) > max_width:
        return text[: max_width - 1] + "…"
    return text


def _write_delimited(columns, rows, out, *, null, delimiter) -> int:
    # "\n" rather than the csv module's default "\r\n".
    writer = csv.writer(out, delimiter=delimiter, lineterminator="\n")
    writer.writerow(columns)
    count = 0
    for row in rows:
        writer.writerow([text_value(v, null) for v in row])
        count += 1
    return count


def _write_json(columns, rows, out, *, lines) -> int:
    prefixes = [_JSON.encode(key) + ": " for key in json_keys(columns)]
    count = 0
    if not lines:
        out.write("[")
    for row in rows:
        obj = "{" + ", ".join([p + json_value(v) for p, v in zip(prefixes, row, strict=True)]) + "}"
        if lines:
            out.write(obj + "\n")
        else:
            out.write(("\n" if count == 0 else ",\n") + obj)
        count += 1
    if not lines:
        out.write("\n]\n" if count else "]\n")
    return count
