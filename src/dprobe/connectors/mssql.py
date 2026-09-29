import re
from collections.abc import Iterator, Mapping
from types import ModuleType
from typing import Any

from dprobe.connectors.base import (
    ColumnInfo, Connector, Found, IndexInfo, Result, ResultColumn, TableInfo, group_indexes,
)
from dprobe.sqltext import lex, remove_trailing_line

_DBLIB_HEADER = re.compile(r"DB-Lib error message \d+, severity \d+:")

_TABLES = """
SELECT s.name, o.name, CASE o.type WHEN 'V' THEN 'VIEW' ELSE 'TABLE' END,
       CAST(ep.value AS nvarchar(max))
FROM sys.objects o
JOIN sys.schemas s ON s.schema_id = o.schema_id
LEFT JOIN sys.extended_properties ep
  ON ep.class = 1 AND ep.major_id = o.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description'
WHERE o.type IN ({types}) AND o.is_ms_shipped = 0
  AND (%(schema)s IS NULL OR s.name = %(schema)s)
  AND (%(pattern)s IS NULL OR UPPER(o.name) LIKE UPPER(%(pattern)s))
ORDER BY s.name, o.name
"""
# OBJECT_ID resolves an unqualified name as SQL Server does: default schema, then dbo.
_COLUMNS = """
SELECT s.name, o.name, c.column_id, c.name, TYPE_NAME(c.user_type_id), c.max_length,
       c.precision, c.scale, c.is_nullable, dc.definition, ic.key_ordinal, c.is_identity,
       c.is_computed, CAST(ep.value AS nvarchar(max))
FROM sys.objects o
JOIN sys.schemas s ON s.schema_id = o.schema_id
JOIN sys.columns c ON c.object_id = o.object_id
LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
LEFT JOIN sys.indexes i ON i.object_id = c.object_id AND i.is_primary_key = 1
LEFT JOIN sys.index_columns ic
  ON ic.object_id = c.object_id AND ic.index_id = i.index_id AND ic.column_id = c.column_id
LEFT JOIN sys.extended_properties ep
  ON ep.class = 1 AND ep.major_id = c.object_id AND ep.minor_id = c.column_id
 AND ep.name = 'MS_Description'
WHERE o.object_id = OBJECT_ID(%(name)s) AND o.type IN ('U', 'V')
ORDER BY c.column_id
"""


# One row per index column, included columns last; the LEFT JOINs keep a row for a
# table without indexes. index_id 0 is the heap, not an index.
_INDEXES = """
SELECT s.name, o.name, i.name, i.is_unique, i.is_primary_key, i.type_desc, c.name,
       ic.is_included_column, ic.is_descending_key
FROM sys.objects o
JOIN sys.schemas s ON s.schema_id = o.schema_id
LEFT JOIN sys.indexes i ON i.object_id = o.object_id AND i.index_id > 0 AND i.is_hypothetical = 0
LEFT JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
LEFT JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE o.object_id = OBJECT_ID(%(name)s) AND o.type IN ('U', 'V')
ORDER BY i.name, ic.is_included_column, ic.key_ordinal, ic.index_column_id
"""


class MssqlConnector(Connector):
    """pymssql, which bundles FreeTDS, so no ODBC driver is needed.

    Useful options: encryption ("off", "request", "require"), tds_version,
    login_timeout (default 60 seconds, slow to fail on a dead host).

    SQL Server has no read-only transaction, so readonly only blocks --commit;
    DML still runs and holds locks until the rollback.
    """

    dialect = "mssql"
    # Schemas of the current database only; 16384-16399 belong to the fixed
    # database roles (db_owner, ...).
    _SCHEMAS = """
SELECT name, CASE WHEN name = SCHEMA_NAME() THEN 1 ELSE 0 END,
       CASE WHEN name IN ('sys', 'INFORMATION_SCHEMA', 'guest') OR schema_id BETWEEN 16384 AND 16399
            THEN 1 ELSE 0 END
FROM sys.schemas ORDER BY name
"""

    def _import_driver(self) -> ModuleType:
        import pymssql

        return pymssql

    def _connect_args(self) -> dict[str, Any]:
        host, port, database = self._host_url()
        return self._merge_options(
            server=host,
            # pymssql expects the port as a string.
            port=str(port) if port else None,
            database=database,
            user=self.config.user,
            password=self.password,
            # FreeTDS converts from the server's code page to this.
            charset="UTF-8",
        )

    def server_version(self) -> str:
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT @@VERSION")
            (version,) = cursor.fetchone()
        # @@VERSION spans several lines (copyright, OS); the first has product and build.
        return version.splitlines()[0].strip()

    @classmethod
    def prepare(cls, sql: str) -> str:
        """Drop a trailing GO line, which SQL Server would otherwise read as an alias.

        A trailing ";" stays: SQL Server accepts it and MERGE requires it.
        """
        return remove_trailing_line(sql, "GO", "mssql")

    def list_tables(self, schema: str | None, like: str | None, views: bool) -> list[TableInfo]:
        """Without a schema, lists every schema in the current database."""
        types = "'U'" + (", 'V'" if views else "")
        rows = self._catalog(_TABLES.format(types=types), {"schema": schema, "pattern": like})
        return [TableInfo(*row) for row in rows]

    def describe(self, schema: str | None, name: str) -> Found[ColumnInfo] | None:
        rows = self._catalog(_COLUMNS, {"name": _qualified(schema, name)})
        if not rows:
            return None
        columns = [
            ColumnInfo(
                position=position,
                name=column,
                type=mssql_type(type_name, max_length, precision, scale),
                nullable=bool(nullable),
                default=_unwrap(default) if default else None,
                pk=pk,
                extra="identity" if identity else "computed" if computed else None,
                comment=comment,
            )
            for (_, _, position, column, type_name, max_length, precision, scale, nullable,
                 default, pk, identity, computed, comment) in rows
        ]
        # The schema and name as resolved, e.g. dbo for an unqualified name.
        return Found(rows[0][0], rows[0][1], columns)

    def indexes(self, schema: str | None, name: str) -> Found[IndexInfo] | None:
        rows = self._catalog(_INDEXES, {"name": _qualified(schema, name)})
        if not rows:
            return None
        indexes = group_indexes(
            (index, unique, primary, type_desc, column, descending, included)
            for _, _, index, unique, primary, type_desc, column, included, descending in rows
        )
        return Found(rows[0][0], rows[0][1], indexes)

    def describe_result(self, sql: str, params: Mapping[str, Any] | None = None) -> list[ResultColumn]:
        """sp_describe_first_result_set reads the columns without running the query.

        pymssql fills in bind values on the client, so the same substitution
        turns the statement into the literal text SQL Server describes.
        """
        text = sql if params is None else self.driver._mssql.substitute_params(sql, params).decode()
        rows = self._catalog("EXEC sp_describe_first_result_set @tsql = %(tsql)s", {"tsql": text})
        # is_hidden, column_ordinal, name, is_nullable, system_type_id, system_type_name,
        # max_length, precision, scale, ...
        return [
            ResultColumn(row[1], row[2] or "", row[5], bool(row[3]), row[6], row[7], row[8])
            for row in rows if not row[0]
        ]

    def raw_columns(self, table: Found) -> Result:
        sql = ("SELECT c.*, TYPE_NAME(c.user_type_id) AS type_name FROM sys.columns c "
               "WHERE c.object_id = OBJECT_ID(%(name)s) ORDER BY c.column_id")
        return self.execute(sql, {"name": _qualified(table.schema, table.name)})

    def _error_text(self, error: Exception) -> str:
        return error_text(error)

    def _iter_rows(self, cursor: Any) -> Iterator[tuple]:
        # fetchmany() and later fetchone() calls run on into the batch's next
        # result set; fetchone() returns None once at the boundary, so stop there.
        return iter(cursor.fetchone, None)

    def _result_sets(self, cursor: Any) -> Iterator[Any]:
        # nextset() moves the same cursor on, skipping results without columns.
        yield from super()._result_sets(cursor)
        while cursor.nextset():
            if cursor.description is not None:
                yield cursor


def error_text(error: Exception) -> str:
    """Flatten pymssql's (code, b"...") error args into one readable line.

    FreeTDS repeats each message and prefixes it with a DB-Lib header,
    sometimes with no newline before the header.
    """
    args = error.args
    if len(args) == 1 and isinstance(args[0], tuple):
        args = args[0]
    if len(args) != 2 or not isinstance(args[1], bytes):
        return str(error)
    code, raw = args
    text = _DBLIB_HEADER.sub("\n", raw.decode(errors="replace"))
    lines = dict.fromkeys(line.strip() for line in text.splitlines() if line.strip())
    return f"{code}: {'; '.join(lines)}"


def mssql_type(name: str, max_length: int, precision: int, scale: int) -> str:
    """Write a column type the way DDL would, e.g. nvarchar(20), varchar(max), decimal(10,2).

    max_length is in bytes, so nchar/nvarchar lengths are halved; -1 means max.
    """
    if name in ("varchar", "char", "varbinary", "binary"):
        return f"{name}({'max' if max_length == -1 else max_length})"
    if name in ("nvarchar", "nchar"):
        return f"{name}({'max' if max_length == -1 else max_length // 2})"
    if name in ("decimal", "numeric"):
        return f"{name}({precision},{scale})"
    if name in ("datetime2", "time", "datetimeoffset"):
        return f"{name}({scale})"
    return name


def _qualified(schema: str | None, name: str) -> str:
    # Brackets keep dots and spaces in names from being read as separators.
    def quote(part: str) -> str:
        return "[" + part.replace("]", "]]") + "]"

    return f"{quote(schema)}.{quote(name)}" if schema else quote(name)


def _unwrap(definition: str) -> str:
    """Drop the parentheses SQL Server wraps defaults in: ((0)) -> 0, ('x') -> 'x'."""
    text = definition.strip()
    while text.startswith("(") and text.endswith(")") and _balanced(text[1:-1]):
        text = text[1:-1].strip()
    return text


def _balanced(text: str) -> bool:
    # Parentheses inside string literals, as in ('a)b'), don't count.
    depth = 0
    for kind, start, end in lex(text, "mssql"):
        if kind != "code":
            continue
        for ch in text[start:end]:
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if depth < 0:
                return False
    return depth == 0
