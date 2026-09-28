import re
from typing import Any

from dprobe.connectors.base import Connector
from dprobe.errors import ConnectError

_DBLIB_HEADER = re.compile(r"DB-Lib error message \d+, severity \d+:")


class MssqlConnector(Connector):
    """pymssql, which bundles FreeTDS, so no ODBC driver is needed.

    Useful options: encryption ("off", "request", "require"), tds_version,
    login_timeout (default 60 seconds, slow to fail on a dead host).
    """

    def _connect(self) -> Any:
        import pymssql

        host, port, database = self._host_url()
        args = self._connect_args(
            server=host,
            # pymssql expects the port as a string.
            port=str(port) if port else None,
            database=database,
            user=self.config.user,
            password=self.password,
        )
        try:
            return pymssql.connect(**args)
        except pymssql.Error as e:
            raise ConnectError(error_text(e)) from e

    def server_version(self) -> str:
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT @@VERSION")
            (version,) = cursor.fetchone()
        # @@VERSION spans several lines (copyright, OS); the first has product and build.
        return version.splitlines()[0].strip()


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
