import re
from types import ModuleType
from typing import Any

from dprobe.connectors.base import (
    ColumnInfo, Connector, Found, IndexInfo, Result, TableInfo, group_indexes,
)
from dprobe.errors import UsageError

_UNKNOWN_COLLATION = 1273
_NUMERIC = re.compile(r"(tiny|small|medium|big)?int|decimal|numeric|float|double|bit|year", re.IGNORECASE)

# MariaDB lists system-versioned tables separately from base tables.
_TABLES = """
SELECT table_schema, table_name,
       CASE WHEN table_type = 'VIEW' THEN 'VIEW' ELSE 'TABLE' END,
       CASE WHEN table_type = 'VIEW' THEN NULL ELSE NULLIF(table_comment, '') END
FROM information_schema.tables
WHERE table_schema = %(schema)s AND table_type IN ({types})
  AND (%(pattern)s IS NULL OR UPPER(table_name) LIKE UPPER(%(pattern)s))
ORDER BY table_name
"""
# One row per index column; the LEFT JOIN keeps a row for a table without indexes.
_INDEXES = """
SELECT s.index_name, s.non_unique, s.index_type, s.column_name, s.collation, s.sub_part,
       {expression}
FROM information_schema.tables t
LEFT JOIN information_schema.statistics s ON s.table_schema = %(schema)s AND s.table_name = %(name)s
WHERE t.table_schema = %(schema)s AND t.table_name = %(name)s
ORDER BY s.index_name, s.seq_in_index
"""
# The constant schema and table in the statistics ON clause let MariaDB and MySQL
# 5.7 read one table's statistics instead of the whole server's.
_COLUMNS = """
SELECT c.ordinal_position, c.column_name, c.column_type, c.is_nullable, c.column_default,
       s.seq_in_index, c.extra, c.column_comment
FROM information_schema.columns c
LEFT JOIN information_schema.statistics s
  ON s.table_schema = %(schema)s AND s.table_name = %(name)s
 AND s.column_name = c.column_name AND s.index_name = 'PRIMARY'
WHERE c.table_schema = %(schema)s AND c.table_name = %(name)s
ORDER BY c.ordinal_position
"""


class MysqlConnector(Connector):
    """mysql-connector-python, for MySQL and MariaDB.

    The driver asks for collation utf8mb4_0900_ai_ci, which MariaDB lacks
    (error 1273); set options.collation to utf8mb4_general_ci there.
    Without a database in url, queries need schema-qualified table names.

    readonly uses START TRANSACTION READ ONLY, which blocks DML only: DDL
    commits implicitly and still runs.
    """

    dialect = "mysql"
    ddl_autocommits = True
    # A user only sees schemas it has privileges on; information_schema is always there.
    _SCHEMAS = """
SELECT schema_name, schema_name = DATABASE(),
       schema_name IN ('information_schema', 'mysql', 'performance_schema', 'sys')
FROM information_schema.schemata ORDER BY schema_name
"""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._killer: Any = None  # a second connection, opened on first need, for KILL QUERY

    def _import_driver(self) -> ModuleType:
        import mysql.connector

        return mysql.connector

    def _connect_args(self) -> dict[str, Any]:
        host, port, database = self._host_url()
        return self._merge_options(
            host=host,
            port=port,
            database=database,
            user=self.config.user,
            password=self.password,
            # MySQL's "utf8" is a 3-byte subset that can't store emoji.
            charset="utf8mb4",
        )

    def server_version(self) -> str:
        with self.conn.cursor() as cursor:
            # version_comment tells MySQL and MariaDB builds apart.
            cursor.execute("SELECT @@version_comment, @@version")
            comment, version = cursor.fetchone()
        return f"{comment} {version}"

    def list_tables(self, schema: str | None, like: str | None, views: bool) -> list[TableInfo]:
        """schema defaults to the database in url."""
        types = "'BASE TABLE', 'SYSTEM VERSIONED'" + (", 'VIEW'" if views else "")
        params = {"schema": schema or self._database(), "pattern": like}
        return [TableInfo(*row) for row in self._catalog(_TABLES.format(types=types), params)]

    def describe(self, schema: str | None, name: str) -> Found[ColumnInfo] | None:
        """Names match exactly: on Linux, MySQL table names are case-sensitive."""
        schema = schema or self._database()
        columns = []
        for position, column, type_, nullable, default, pk, extra, comment in self._catalog(
                _COLUMNS, {"schema": schema, "name": name}):
            columns.append(ColumnInfo(
                position=int(position),
                name=column,
                type=type_,
                nullable=nullable == "YES",
                default=mysql_default(default, type_, extra, mariadb=self._mariadb),
                pk=int(pk) if pk is not None else None,
                # DEFAULT_GENERATED only flags an expression default.
                extra=extra.replace("DEFAULT_GENERATED", "").strip() or None,
                comment=comment or None,
            ))
        return Found(schema, name, columns) if columns else None

    def indexes(self, schema: str | None, name: str) -> Found[IndexInfo] | None:
        schema = schema or self._database()
        # MariaDB has no statistics.expression (MySQL 8.0.13+ functional key parts).
        expression = "NULL" if self._mariadb else "s.expression"
        rows = self._catalog(_INDEXES.format(expression=expression), {"schema": schema, "name": name})
        if not rows:
            return None
        indexes = group_indexes(
            (index, not non_unique, index == "PRIMARY", index_type,
             _index_column(column, sub_part, expr), collation == "D", False)
            for index, non_unique, index_type, column, collation, sub_part, expr in rows
        )
        return Found(schema, name, indexes)

    def _type_name(self, type_code: Any) -> str:
        return self.driver.FieldType.get_info(type_code)

    def raw_columns(self, table: Found) -> Result:
        sql = ("SELECT * FROM information_schema.columns "
               "WHERE table_schema = %(schema)s AND table_name = %(name)s ORDER BY ordinal_position")
        return self.execute(sql, {"schema": table.schema, "name": table.name})

    def _database(self) -> str:
        # dprobe never runs USE, so the database is the one it connected with.
        if database := self._host_url()[2] or self.config.options.get("database"):
            return database
        raise UsageError(f"{self.config.label} has no database in its url, so give a schema")

    @property
    def _mariadb(self) -> bool:
        return "MariaDB" in self.conn.server_info

    def _start_readonly(self) -> None:
        self.conn.start_transaction(readonly=True)

    def _error_hint(self, error: Exception, sql: str | None) -> str | None:
        if sql is None and getattr(error, "errno", None) == _UNKNOWN_COLLATION and "collation" not in self.config.options:
            return "MariaDB? set options.collation: utf8mb4_general_ci"
        return super()._error_hint(error, sql)

    def commit(self) -> None:
        if self.conn.unread_result:
            # Let the statement run to completion before committing its work.
            self.conn.consume_results()
        super().commit()

    def close(self) -> None:
        self._kill_unread()
        if self._killer is not None:
            killer, self._killer = self._killer, None
            killer.close()
        super().close()

    def _discard(self, cursor: Any) -> None:
        self._kill_unread()
        super()._discard(cursor)

    def _kill_unread(self) -> None:
        """End a statement whose rows weren't all read (output stopped at max_rows).

        close() and commit() first read every remaining row, which
        took ~30 s for 3M rows on MySQL 8.4. KILL QUERY from a second connection
        ends it at once and leaves this connection usable. (shutdown() would
        drop the socket, but the C extension doesn't implement it.)
        """
        if self.conn is None or not self.conn.unread_result:
            return
        try:
            # Kept open for the next cut-short result, e.g. in a --script.
            if self._killer is None:
                self._killer = self.driver.connect(**self._connect_args())
            with self._killer.cursor() as cursor:
                cursor.execute(f"KILL QUERY {self.conn.connection_id}")
            self.conn.consume_results()
        except self.driver.Error:
            pass  # close() still works, just slowly.


def mysql_default(default: str | None, column_type: str, extra: str, *, mariadb: bool) -> str | None:
    """A column default as SQL text, like the other databases report it.

    MySQL stores a literal default as its bare value (none rather than
    'none'), so text-like literals are quoted here; numbers and expressions
    (flagged DEFAULT_GENERATED, or CURRENT_TIMESTAMP on older servers) are
    already valid SQL. MariaDB quotes literals itself and writes NULL as the
    text NULL.
    """
    if default is None or (mariadb and default == "NULL"):
        return None
    if mariadb or "DEFAULT_GENERATED" in extra or _NUMERIC.match(column_type):
        return default
    if default.upper().startswith("CURRENT_TIMESTAMP"):
        return default
    return "'" + default.replace("'", "''") + "'"


def _index_column(column: str | None, sub_part: int | None, expression: str | None) -> str | None:
    """Key part as written in DDL: name(10) for a prefix, (expr) for a functional part."""
    if column is None and expression is None:
        return None
    return f"({expression})" if expression else f"{column}({sub_part})" if sub_part else column
