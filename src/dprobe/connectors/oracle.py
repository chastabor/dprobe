from typing import Any

from dprobe.connectors.base import Connector
from dprobe.errors import ConnectError


class OracleConnector(Connector):
    """python-oracledb in thin mode (no Instant Client needed).

    url goes to connect() as dsn unchanged, so it can be Easy Connect
    ("host:1521/service"), a tnsnames.ora alias (set options.config_dir or
    $TNS_ADMIN) or a full connect descriptor.
    """

    def _connect(self) -> Any:
        import oracledb

        args = self._connect_args(user=self.config.user, password=self.password, dsn=self.config.url)
        try:
            return oracledb.connect(**args)
        except oracledb.Error as e:
            raise ConnectError(str(e)) from e

    def server_version(self) -> str:
        return f"Oracle Database {self.conn.version}"
