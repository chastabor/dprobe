"""Shared behavior for the driver-specific connectors."""

from abc import ABC, abstractmethod
from typing import Any, Self

from dprobe.config import ConnectionConfig, parse_host_url
from dprobe.errors import ConfigError, ConnectError


class Connector(ABC):
    """A single database connection, opened by entering the context manager.

    Closing without a commit rolls back any open transaction on all three drivers.
    """

    def __init__(self, config: ConnectionConfig, password: str | None) -> None:
        self.config = config
        self.password = password
        self.conn: Any = None

    @abstractmethod
    def _connect(self) -> Any:
        """Return a new DB-API connection, raising ConnectError on driver errors."""

    @abstractmethod
    def server_version(self) -> str:
        """Product name and version as reported by the server."""

    def connect(self) -> None:
        # Drivers are imported lazily, so a broken install only affects its own labels.
        try:
            self.conn = self._connect()
        except ImportError as e:
            raise ConnectError(f"{self.config.label}: {self.config.driver} driver failed to load: {e}") from e
        except ConnectError as e:
            raise ConnectError(f"{self.config.label}: {e}") from e.__cause__

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _connect_args(self, **core: Any) -> dict[str, Any]:
        """Merge config options over core connect() arguments; None values are dropped.

        Options can fill in a core argument left unset (e.g. port) but can't
        override one dprobe already sets from url, user or password.
        """
        core = {k: v for k, v in core.items() if v is not None}
        clash = sorted(core.keys() & self.config.options.keys())
        if clash:
            raise ConfigError(
                f"connections.{self.config.label}.options: {', '.join(clash)} "
                "is already set from url/user/password"
            )
        return {**core, **self.config.options}

    def _host_url(self) -> tuple[str, int | None, str | None]:
        try:
            return parse_host_url(self.config.url)
        except ValueError as e:
            raise ConfigError(f"connections.{self.config.label}.url: {e}") from None
