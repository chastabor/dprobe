import re
from contextlib import closing
from types import ModuleType
from typing import Any

from dprobe.connectors.base import ColumnInfo, Connector, Described, Result, TableInfo
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

    ddl_autocommits = True

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

    def describe(self, schema: str | None, name: str) -> Described | None:
        """Names match exactly: on Linux, MySQL table names are case-sensitive."""
        schema = schema or self._database()
        mariadb = "MariaDB" in self.conn.server_info
        columns = []
        for position, column, type_, nullable, default, pk, extra, comment in self._catalog(
                _COLUMNS, {"schema": schema, "name": name}):
            columns.append(ColumnInfo(
                position=int(position),
                name=column,
                type=type_,
                nullable=nullable == "YES",
                default=mysql_default(default, type_, extra, mariadb=mariadb),
                pk=int(pk) if pk is not None else None,
                # DEFAULT_GENERATED only flags an expression default.
                extra=extra.replace("DEFAULT_GENERATED", "").strip() or None,
                comment=comment or None,
            ))
        return Described(schema, name, columns) if columns else None

    def raw_columns(self, table: Described) -> Result:
        sql = ("SELECT * FROM information_schema.columns "
               "WHERE table_schema = %(schema)s AND table_name = %(name)s ORDER BY ordinal_position")
        return self.execute(sql, {"schema": table.schema, "name": table.name})

    def _database(self) -> str:
        # dprobe never runs USE, so the database is the one it connected with.
        if database := self._connect_args().get("database"):
            return database
        raise UsageError(f"{self.config.label} has no database in its url, so give a schema")

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
        super().close()

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
            with closing(self.driver.connect(**self._connect_args())) as killer:
                with killer.cursor() as cursor:
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
