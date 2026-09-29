"""Render query results as table, csv, tsv, json or jsonl."""

import csv
import json
import math
import shutil
import sys
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import astuple, dataclass, fields
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import lru_cache
from itertools import islice
from typing import Any, Protocol, TextIO

_TABLE_ESCAPES = str.maketrans({"\n": "\\n", "\r": "\\r", "\t": "\\t"})
# Fitting a table to the terminal never makes a column narrower than this.
_MIN_COLUMN = 6
_BINARY = (bytes, bytearray, memoryview)
_PLAIN_TEXT = (str, int, float)
# Shared, because json.dumps() builds a new encoder per call when given options.
# ensure_ascii=False writes characters as UTF-8 instead of \u escapes.
_JSON = json.JSONEncoder(ensure_ascii=False, default=str)


class Rows(Protocol):
    columns: list[str]

    def rows(self) -> Iterator[tuple]: ...


@dataclass(frozen=True)
class Table:
    """Rows already in memory, such as catalog records."""

    columns: list[str]
    data: list[tuple]

    def rows(self) -> Iterator[tuple]:
        return iter(self.data)

    @classmethod
    def of(cls, records: Iterable[Any], record_type: type) -> "Table":
        """One column per dataclass field; record_type still names them when records is empty."""
        return cls([f.name for f in fields(record_type)], [astuple(r) for r in records])


def write_result(
    result: Rows,
    fmt: str,
    out: TextIO,
    *,
    null: str | None = None,
    max_rows: int | None = None,
    max_width: int | None = None,
    fit: int | None = None,
) -> tuple[int, bool]:
    """Write result's rows to out; return (rows written, whether rows were left unread).

    max_rows and max_width of None mean no limit; max_width only affects
    table cells, and fit narrows a table's widest columns until a row takes
    at most that many terminal columns. null is the text for NULL, by
    default "NULL" in table and empty in csv/tsv; JSON always writes null.
    """
    rows = result.rows()
    limited = islice(rows, max_rows) if max_rows is not None else rows
    if fmt == "table":
        count = _write_table(result.columns, limited, out, null="NULL" if null is None else null,
                             max_width=max_width, fit=fit)
    elif fmt in ("csv", "tsv"):
        count = _write_delimited(result.columns, limited, out, null=null or "",
                                 delimiter="," if fmt == "csv" else "\t")
    else:
        count = _write_json(result.columns, limited, out, lines=fmt == "jsonl")
    # Peek one row past the limit to tell the caller the output was cut short.
    more = max_rows is not None and next(rows, None) is not None
    return count, more


class ResultWriter:
    """Writes result sets one after another, opening the output on the first one.

    Opening late means a failed query never creates or empties an output file.
    table, csv and tsv put a blank line between sets; json writes one array
    per set (a stream jq reads); jsonl carries on with the rows.
    """

    def __init__(self, open_output: Callable[[], TextIO], fmt: str, *, null: str | None = None,
                 max_width: int | None = None) -> None:
        self._open = open_output
        self._format = fmt
        self._options = {"null": null, "max_width": max_width, "fit": None}
        self.out: TextIO | None = None

    def write(self, result: Rows, *, max_rows: int | None = None) -> tuple[int, bool]:
        """As write_result(): (rows written, whether rows were left unread)."""
        if self.out is None:
            self.out = self._open()
            # A table shown on a terminal is fitted to its width; pipes and files get every column.
            if self.out.isatty():
                self._options["fit"] = shutil.get_terminal_size().columns
        elif self._format in ("table", "csv", "tsv"):
            self.out.write("\n")
        return write_result(result, self._format, self.out, max_rows=max_rows, **self._options)


def display_width(text: str) -> int:
    """Terminal columns text takes: two for wide East Asian characters and emoji, none for combining marks."""
    if _narrow(text):
        return len(text)
    return sum(map(_char_width, text))


def _narrow(text: str) -> bool:
    # Everything below U+0300 (ASCII, Latin-1, Latin Extended) is one column wide.
    return text.isascii() or max(text) < "\u0300"


@lru_cache(maxsize=None)
def _char_width(c: str) -> int:
    return 0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1


def text_value(value: Any, null: str = "") -> str:
    """Plain-text form of a database value.

    bools become true/false; bytes become 0x-prefixed hex; Decimals never use exponent notation; MySQL
    SET values join with commas; MySQL TIME and Oracle INTERVAL values
    (timedelta) print as [-]H:MM:SS[.ffffff], hours unbounded.
    """
    if value is None:
        return null
    if type(value) in _PLAIN_TEXT:
        return str(value)
    if isinstance(value, bool):
        # Lower-case, matching JSON, rather than Python's True/False.
        return "true" if value else "false"
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


def _table_lines(columns: Sequence[str], cells: list[list[str]], *, right: Sequence[bool],
                 max_width: int | None = None, fit: int | None = None) -> Iterator[str]:
    """Rows of aligned cells under a dashed rule; right[i] right-aligns column i.

    Cells are cut to max_width, headers aren't. fit then narrows the widest
    columns until a row takes at most that many terminal columns.
    """
    cap = max_width or sys.maxsize
    widths = [max([display_width(name), *(min(display_width(row[i]), cap) for row in cells)])
              for i, name in enumerate(columns)]
    if fit is not None:
        widths = _fit(widths, fit)
    yield _table_line(columns, widths, widths, right)
    yield "  ".join("-" * w for w in widths).rstrip()
    caps = [min(w, cap) for w in widths]
    for row in cells:
        yield _table_line(row, widths, caps, right)


def _table_line(texts: Sequence[str], widths: list[int], caps: list[int], right: Sequence[bool]) -> str:
    parts = []
    for text, width, cap, flush_right in zip(texts, widths, caps, right, strict=True):
        text, used = _clip(text, cap)
        pad = " " * (width - used)
        parts.append(pad + text if flush_right else text + pad)
    return "  ".join(parts).rstrip()


def _fit(widths: list[int], limit: int) -> list[int]:
    """Cap the widest columns so a row fits in limit terminal columns, keeping each at least _MIN_COLUMN."""
    gaps = 2 * (len(widths) - 1)
    cap = min(max(widths), max(limit, _MIN_COLUMN))
    # If even _MIN_COLUMN doesn't fit, the terminal wraps.
    while cap > _MIN_COLUMN and sum(min(w, cap) for w in widths) + gaps > limit:
        cap -= 1
    return [min(w, cap) for w in widths]


def _clip(text: str, width: int) -> tuple[str, int]:
    """text cut to fit width terminal columns, ending in "…" when cut, and the columns it takes."""
    if _narrow(text):
        return (text, len(text)) if len(text) <= width else (text[: width - 1] + "…", width)
    used = sum(map(_char_width, text))
    if used <= width:
        return text, used
    kept, used = [], 0
    for c in text:
        if used + _char_width(c) > width - 1:
            break
        kept.append(c)
        used += _char_width(c)
    return "".join(kept) + "…", used + 1


def _write_table(columns, rows, out, *, null, max_width, fit) -> int:
    # No column ends up wider than this, so longer values can be cut before converting.
    limit = min(max_width or sys.maxsize, max(fit, _MIN_COLUMN) if fit is not None else sys.maxsize)
    cells = []
    # A column is right-aligned when it holds values and every one is a number.
    numeric: list[bool | None] = [None] * len(columns)
    for row in rows:
        cells.append([_table_cell(v, null, limit) for v in row])
        for i, value in enumerate(row):
            if value is not None:
                numeric[i] = numeric[i] is not False and _is_number(value)
    right = [bool(n) for n in numeric]
    out.writelines(line + "\n" for line in _table_lines(columns, cells, right=right, max_width=max_width, fit=fit))
    return len(cells)


def _head(text: str, columns: int) -> str:
    """The shortest start of text wider than columns, or all of it, so a cut still shows as cut.

    Counts terminal columns, not characters: combining marks take none.
    """
    start = text[: columns + 1]
    if _narrow(start):
        return start
    used = 0
    for i, c in enumerate(text):
        used += _char_width(c)
        if used > columns:
            return text[: i + 1]
    return text


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _table_cell(value: Any, null: str, limit: int) -> str:
    # Cut before converting, so a multi-MB CLOB or BLOB isn't converted in full;
    # hex and escapes only lengthen the text.
    if isinstance(value, str):
        value = _head(value, limit)
    elif isinstance(value, _BINARY):
        value = value[: limit // 2 + 1]
    # Control characters would break the alignment, so they're shown escaped.
    return text_value(value, null).translate(_TABLE_ESCAPES)


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
