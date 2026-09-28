"""Render rows for the terminal."""

from collections.abc import Iterable, Sequence
from typing import Any


def format_table(columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """Left-aligned columns under a dashed header rule. None renders as an empty cell."""
    cells = [["" if value is None else str(value) for value in row] for row in rows]
    widths = [max([len(name), *(len(row[i]) for row in cells)]) for i, name in enumerate(columns)]
    lines = [columns, ["-" * w for w in widths], *cells]
    return "\n".join("  ".join(v.ljust(w) for v, w in zip(line, widths)).rstrip() for line in lines)
