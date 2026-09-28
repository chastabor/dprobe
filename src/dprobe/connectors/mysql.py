from typing import Any

from dprobe.connectors.base import Connector
from dprobe.errors import ConnectError

_UNKNOWN_COLLATION = 1273


class MysqlConnector(Connector):
    """mysql-connector-python, for MySQL and MariaDB.

    The driver asks for collation utf8mb4_0900_ai_ci, which MariaDB lacks
    (error 1273); set options.collation to utf8mb4_general_ci there.
    Without a database in url, queries need schema-qualified table names.
    """

    def _connect(self) -> Any:
        import mysql.connector

        host, port, database = self._host_url()
        args = self._connect_args(
            host=host,
            port=port,
            database=database,
            user=self.config.user,
            password=self.password,
        )
        try:
            return mysql.connector.connect(**args)
        except mysql.connector.Error as e:
            message = str(e)
            if e.errno == _UNKNOWN_COLLATION and "collation" not in self.config.options:
                message += " (MariaDB? set options.collation: utf8mb4_general_ci)"
            raise ConnectError(message) from e

    def server_version(self) -> str:
        with self.conn.cursor() as cursor:
            # version_comment tells MySQL and MariaDB builds apart.
            cursor.execute("SELECT @@version_comment, @@version")
            comment, version = cursor.fetchone()
        return f"{comment} {version}"
