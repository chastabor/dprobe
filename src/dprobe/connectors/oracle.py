from types import ModuleType
from typing import Any

from dprobe.connectors.base import Connector
from dprobe.sqltext import is_plsql, remove_trailing_line, remove_trailing_semicolon


class OracleConnector(Connector):
    """python-oracledb in thin mode (no Instant Client needed).

    url goes to connect() as dsn unchanged, so it can be Easy Connect
    ("host:1521/service"), a tnsnames.ora alias (set options.config_dir or
    $TNS_ADMIN) or a full connect descriptor.

    readonly uses SET TRANSACTION READ ONLY, which blocks DML only: DDL
    commits implicitly and still runs.
    """

    ddl_autocommits = True

    def _import_driver(self) -> ModuleType:
        import oracledb

        # LOB objects need an open cursor to read; fetch CLOB/BLOB as str/bytes instead.
        oracledb.defaults.fetch_lobs = False
        # Decimal keeps every digit of NUMBER values that a float would round.
        oracledb.defaults.fetch_decimals = True
        return oracledb

    def _connect_args(self) -> dict[str, Any]:
        return self._merge_options(user=self.config.user, password=self.password, dsn=self.config.url)

    def server_version(self) -> str:
        return f"Oracle Database {self.conn.version}"

    @classmethod
    def prepare(cls, sql: str) -> str:
        """Drop a trailing SQL*Plus "/" line, then the final ";" unless it's PL/SQL.

        Oracle before 23ai rejects a ";" after plain SQL (ORA-00911 or
        ORA-00933); 23ai accepts it. PL/SQL always needs it after END.
        """
        sql = remove_trailing_line(sql, "/", "oracle")
        return sql if is_plsql(sql) else remove_trailing_semicolon(sql, "oracle")

    def _start_readonly(self) -> None:
        with self.conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")

    def _new_cursor(self, arraysize: int) -> Any:
        cursor = super()._new_cursor(arraysize)
        # The default of 2 prefetched rows costs a second round trip for the first batch.
        cursor.prefetchrows = arraysize
        return cursor
