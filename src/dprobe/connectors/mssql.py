import re
from collections.abc import Iterator
from types import ModuleType
from typing import Any

from dprobe.connectors.base import Connector
from dprobe.sqltext import remove_trailing_line

_DBLIB_HEADER = re.compile(r"DB-Lib error message \d+, severity \d+:")


class MssqlConnector(Connector):
    """pymssql, which bundles FreeTDS, so no ODBC driver is needed.

    Useful options: encryption ("off", "request", "require"), tds_version,
    login_timeout (default 60 seconds, slow to fail on a dead host).

    SQL Server has no read-only transaction, so readonly only blocks --commit;
    DML still runs and holds locks until the rollback.
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

    def _error_text(self, error: Exception) -> str:
        return error_text(error)

    def _iter_rows(self, cursor: Any) -> Iterator[tuple]:
        # fetchmany() and later fetchone() calls run on into the batch's next
        # result set; fetchone() returns None once at the boundary, so stop there.
        return iter(cursor.fetchone, None)

    def _has_more_results(self, cursor: Any) -> bool:
        while cursor.nextset():
            if cursor.description is not None:
                return True
        return False


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
