import pytest

from dprobe.config import DRIVERS, ConnectionConfig
from dprobe.connectors import REGISTRY
from dprobe.connectors.mssql import error_text
from dprobe.errors import ConfigError


def test_registry_covers_drivers():
    assert set(REGISTRY) == set(DRIVERS)


def test_connect_args_merge_and_clash():
    cls = REGISTRY["mysql"]
    conn = ConnectionConfig(label="a", driver="mysql", url="h", options={"port": 3307, "ssl_disabled": True})
    args = cls(conn, None)._connect_args(host="h", port=None, user="u")
    assert args == {"host": "h", "user": "u", "port": 3307, "ssl_disabled": True}
    with pytest.raises(ConfigError, match=r"connections\.a\.options: host is already set"):
        cls(ConnectionConfig(label="a", driver="mysql", url="h", options={"host": "x"}), None)._connect_args(host="h")


def test_mssql_error_text_flattens_dblib_messages():
    import pymssql

    raw = (
        b"Login failed for user 'sa'.DB-Lib error message 20018, severity 14:\n"
        b"General SQL Server error: Check messages from the SQL Server\n"
        b"DB-Lib error message 20002, severity 9:\nAdaptive Server connection failed (db1)\n"
        b"DB-Lib error message 20002, severity 9:\nAdaptive Server connection failed (db1)\n"
    )
    error = pymssql.OperationalError((18456, raw))
    assert error_text(error) == (
        "18456: Login failed for user 'sa'.; "
        "General SQL Server error: Check messages from the SQL Server; "
        "Adaptive Server connection failed (db1)"
    )
    assert error_text(pymssql.OperationalError("plain")) == "plain"


def test_refused_connections_raise_connect_error():
    from dprobe.errors import ConnectError

    for driver, url in [("mysql", "127.0.0.1:1/x"), ("oracle", "127.0.0.1:1/x"), ("mssql", "127.0.0.1:1/x")]:
        conn = ConnectionConfig(label=driver, driver=driver, url=url, user="u", password="p")
        with pytest.raises(ConnectError, match=rf"^{driver}: "):
            with REGISTRY[driver](conn, "p"):
                pass
