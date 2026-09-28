"""Shared behavior for the driver-specific connectors."""

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from types import ModuleType
from typing import Any, ClassVar, Self

from dprobe.config import ConnectionConfig, parse_host_url
from dprobe.errors import ConfigError, ConnectError, QueryError
from dprobe.sqltext import first_keyword

# Rows per fetchmany() round trip; pymssql and mysql-connector default to 1.
FETCH_SIZE = 500

_DML = {"INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE"}


class Connector(ABC):
    """A single database connection, opened by entering the context manager.

    Autocommit is off, and closing without commit() rolls back: the drivers
    and servers all discard an open transaction on disconnect.
    """

    # DDL that commits implicitly can't be undone by the rollback.
    ddl_autocommits: ClassVar[bool] = False

    def __init__(self, config: ConnectionConfig, password: str | None) -> None:
        self.config = config
        self.password = password
        self.driver: Any = None
        self.conn: Any = None

    @abstractmethod
    def _import_driver(self) -> ModuleType:
        """Import the DB-API module. Lazy, so a broken driver only affects its own labels."""

    @abstractmethod
    def _connect_args(self) -> dict[str, Any]:
        """Keyword arguments for the driver's connect()."""

    @abstractmethod
    def server_version(self) -> str:
        """Product name and version as reported by the server."""

    @classmethod
    def prepare(cls, sql: str) -> str:
        """Adjust a statement's text for this database; --native skips this."""
        return sql

    def _start_readonly(self) -> None:
        """Begin a read-only transaction; called after connect when readonly is set."""

    def _error_text(self, error: Exception) -> str:
        return str(error)

    def _error_hint(self, error: Exception, sql: str | None) -> str | None:
        """Advice appended to an error message; sql is None for connect errors."""
        # prepare() would change the text only when --native sent it as written.
        if sql is not None and self.prepare(sql) != sql:
            return "without --native, dprobe removes a trailing ';', '/' or 'GO'"
        return None

    def _new_cursor(self, arraysize: int) -> Any:
        cursor = self.conn.cursor()
        cursor.arraysize = arraysize
        return cursor

    def _iter_rows(self, cursor: Any) -> Iterator[tuple]:
        while batch := cursor.fetchmany():
            yield from batch

    def _has_more_results(self, cursor: Any) -> bool:
        """Whether another result set with columns follows the current one."""
        return False

    def connect(self) -> None:
        label = self.config.label
        try:
            self.driver = self._import_driver()
        except ImportError as e:
            raise ConnectError(f"{label}: {self.config.driver} driver failed to load: {e}") from e
        args = self._connect_args()
        try:
            self.conn = self.driver.connect(**args)
            if self.config.readonly:
                self._start_readonly()
        except self.driver.Error as e:
            raise ConnectError(f"{label}: {self._describe(e, None)}") from e

    def execute(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] | None = None,
        *,
        max_rows: int | None = None,
    ) -> "Result":
        """Run one statement.

        With params None the drivers skip placeholder parsing, so "%" and ":"
        in the text reach the server untouched. max_rows only sizes fetches,
        so the row after it arrives in the same round trip as the rest.
        """
        size = FETCH_SIZE if max_rows is None else min(max_rows + 1, 10 * FETCH_SIZE)
        cursor = self._new_cursor(size)
        try:
            if params is None:
                cursor.execute(sql)
            else:
                cursor.execute(sql, params)
        except self.driver.Error as e:
            raise QueryError(self._describe(e, sql)) from e
        return Result(self, cursor, sql)

    def commit(self) -> None:
        try:
            self.conn.commit()
        except self.driver.Error as e:
            raise QueryError(f"commit failed: {self._error_text(e)}") from e

    def close(self) -> None:
        if self.conn is None:
            return
        conn, self.conn = self.conn, None
        try:
            conn.close()
        except self.driver.Error:
            pass  # The server rolls back on disconnect either way.

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _describe(self, error: Exception, sql: str | None) -> str:
        message = self._error_text(error)
        if hint := self._error_hint(error, sql):
            message += f" ({hint})"
        return message

    def _merge_options(self, **core: Any) -> dict[str, Any]:
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


class Result:
    """Output of one statement. columns is None when it returned no rows.

    affected is the number of changed rows, or None when rowcount doesn't
    mean that (e.g. DDL, which Oracle and MySQL report as 0). Values are the
    driver's Python types, so digits beyond microseconds (DATETIME2,
    TIMESTAMP(9)) are lost.
    """

    def __init__(self, connector: Connector, cursor: Any, sql: str) -> None:
        self._connector = connector
        self._cursor = cursor
        self._sql = sql
        self._exhausted = False
        description = cursor.description
        self.columns: list[str] | None = [d[0] for d in description] if description else None
        rowcount = cursor.rowcount
        counts_rows = rowcount > 0 or (rowcount == 0 and first_keyword(sql, connector.config.driver) in _DML)
        self.affected: int | None = rowcount if self.columns is None and counts_rows else None

    def rows(self) -> Iterator[tuple]:
        """Yield rows, fetching in batches.

        Some errors (e.g. Oracle ORA-01722 on a bad row) only surface while fetching.
        """
        if self.columns is None:
            return
        try:
            yield from self._connector._iter_rows(self._cursor)
        except self._connector.driver.Error as e:
            raise QueryError(self._connector._describe(e, self._sql)) from e
        self._exhausted = True

    def has_more_results(self) -> bool:
        """Whether more result sets with columns follow; False until every row is read."""
        return self._exhausted and self._connector._has_more_results(self._cursor)
