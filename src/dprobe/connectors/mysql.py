from contextlib import closing
from types import ModuleType
from typing import Any

from dprobe.connectors.base import Connector

_UNKNOWN_COLLATION = 1273


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
        )

    def server_version(self) -> str:
        with self.conn.cursor() as cursor:
            # version_comment tells MySQL and MariaDB builds apart.
            cursor.execute("SELECT @@version_comment, @@version")
            comment, version = cursor.fetchone()
        return f"{comment} {version}"

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
