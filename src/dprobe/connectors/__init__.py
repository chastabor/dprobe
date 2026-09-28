from dprobe.config import ConnectionConfig, resolve_password
from dprobe.connectors.base import Connector
from dprobe.connectors.mssql import MssqlConnector
from dprobe.connectors.mysql import MysqlConnector
from dprobe.connectors.oracle import OracleConnector

REGISTRY: dict[str, type[Connector]] = {
    "oracle": OracleConnector,
    "mssql": MssqlConnector,
    "mysql": MysqlConnector,
}


def create_connector(config: ConnectionConfig) -> Connector:
    """Expand ${VAR}s and resolve the password (may prompt). Connects on `with`."""
    config = config.expand_env()
    return REGISTRY[config.driver](config, resolve_password(config))
