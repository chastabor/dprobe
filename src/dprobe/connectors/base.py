"""Shared behavior for the driver-specific connectors."""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from types import ModuleType
from typing import Any, ClassVar, Self

from dprobe.config import ConnectionConfig, parse_host_url
from dprobe.errors import ConfigError, ConnectError, QueryError, UsageError
from dprobe.sqltext import first_keyword, has_code, split_statements

# Rows per fetchmany() round trip; pymssql and mysql-connector default to 1.
FETCH_SIZE = 500

_DML = {"INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE"}
_READS = {"SELECT", "WITH", "VALUES", "TABLE", "SHOW", "DESC", "DESCRIBE", "EXPLAIN"}


@dataclass(frozen=True)
class TableInfo:
    schema: str
    name: str
    type: str  # TABLE or VIEW
    comment: str | None


@dataclass(frozen=True)
class ColumnInfo:
    position: int
    name: str
    type: str  # as the database would write it, e.g. VARCHAR2(20 CHAR), decimal(10,2)
    nullable: bool
    default: str | None  # the default's SQL text, not its value
    pk: int | None  # position within the primary key
    extra: str | None  # identity, auto_increment, computed, ...
    comment: str | None


@dataclass(frozen=True)
class IndexInfo:
    name: str
    columns: str  # in key order, e.g. "id, created DESC"; expressions and prefix lengths as written
    unique: bool
    primary: bool
    type: str | None  # NORMAL, BITMAP, BTREE, CLUSTERED, ...
    include: str | None  # SQL Server included (non-key) columns


@dataclass(frozen=True)
class SchemaInfo:
    name: str
    current: bool
    system: bool  # created and maintained by the database itself


@dataclass(frozen=True)
class ResultColumn:
    position: int
    name: str
    type: str  # the driver's type name; SQL Server gives the full SQL type
    nullable: bool | None  # None when the driver doesn't say
    size: int | None
    precision: int | None
    scale: int | None


@dataclass(frozen=True)
class Found[T]:
    """Rows about one table, e.g. its columns, as found by name."""

    schema: str
    name: str
    items: list[T]
    synonym: str | None = None  # the name given, when it was an Oracle synonym


class Connector(ABC):
    """A single database connection, opened by entering the context manager.

    Autocommit is off, and closing without commit() rolls back: the drivers
    and servers all discard an open transaction on disconnect.
    """

    # SQL lexing rules: "oracle", "mssql" or "mysql".
    dialect: ClassVar[str]
    # DDL that commits implicitly can't be undone by the rollback.
    ddl_autocommits: ClassVar[bool] = False
    # (name, current, system) per schema, for schemas().
    _SCHEMAS: ClassVar[str]

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

    @abstractmethod
    def list_tables(self, schema: str | None, like: str | None, views: bool) -> list[TableInfo]:
        """Tables (and views) in schema, or the connection's default; like is a LIKE pattern, any case."""

    @abstractmethod
    def describe(self, schema: str | None, name: str) -> Found[ColumnInfo] | None:
        """Columns of a table or view, resolving name as the database would; None if not found."""

    @abstractmethod
    def raw_columns(self, table: Found) -> "Result":
        """The catalog's own column rows for a table found by describe()."""

    @abstractmethod
    def indexes(self, schema: str | None, name: str) -> Found[IndexInfo] | None:
        """Indexes of a table, found as describe() finds it; None if there's no such table."""

    def schemas(self) -> list[SchemaInfo]:
        """Every schema the user can see, system ones included and flagged."""
        return [SchemaInfo(name, bool(current), bool(system)) for name, current, system in self._catalog(self._SCHEMAS, {})]

    def describe_result(self, sql: str, params: Mapping[str, Any] | None = None) -> list[ResultColumn]:
        """The columns a query returns.

        This default runs the statement and reads cursor.description without
        fetching, so it refuses anything but reads: DML would run, and DDL
        would commit. Oracle and SQL Server override it to describe without
        running.
        """
        if first_keyword(sql, self.dialect) not in _READS:
            raise UsageError(f"--meta runs the statement on {self.config.driver}, so it only takes a query")
        cursor = self._new_cursor(1)
        self._run(cursor, sql, params)
        return [
            ResultColumn(position, d[0], self._type_name(d[1]), d[6], d[3], d[4], d[5])
            for position, d in enumerate(cursor.description or (), 1)
        ]

    def _type_name(self, type_code: Any) -> str:
        """Readable name for a cursor.description type code."""
        return str(type_code)


    @classmethod
    def prepare(cls, sql: str) -> str:
        """Adjust a statement's text for this database; --native skips this."""
        return sql

    @classmethod
    def statements(cls, text: str) -> list[tuple[int, str]]:
        """(line, SQL) for each statement, after prepare(); ones left empty are dropped.

        Raises ValueError for text the splitter can't take (MySQL DELIMITER).
        """
        return [(line, sql) for line, piece in split_statements(text, cls.dialect)
                if has_code(sql := cls.prepare(piece), cls.dialect)]

    @classmethod
    def placeholder(cls, name: str) -> tuple[str, str]:
        """How a :name bind is sent: the text in the SQL and the params key."""
        return f"%({name})s", name

    @classmethod
    def identifier(cls, text: str, quoted: bool) -> str:
        """A name as the catalog stores it. Oracle overrides this to upper-case unquoted names."""
        return text

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

    def _result_sets(self, cursor: Any) -> Iterator[Any]:
        """Cursors for each result set with columns, the statement's own first.

        Each is asked for only once the rows before it have been read or discarded.
        """
        if cursor.description is not None:
            yield cursor

    def _discard(self, cursor: Any) -> None:
        """Drop unread rows so the connection can run the next statement."""
        cursor.close()

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
        fetch_size: int = FETCH_SIZE,
    ) -> "Result":
        """Run one statement.

        With params None the drivers skip placeholder parsing, so "%" and ":"
        in the text reach the server untouched. max_rows only sizes fetches,
        so the row after it arrives in the same round trip as the rest.
        """
        cursor = self._new_cursor(fetch_size if max_rows is None else min(max_rows + 1, 10 * FETCH_SIZE))
        self._run(cursor, sql, params)
        return Result(self, cursor, sql)

    def _run(self, cursor: Any, sql: str, params: Mapping[str, Any] | Sequence[Any] | None) -> None:
        try:
            if params is None:
                cursor.execute(sql)
            else:
                cursor.execute(sql, params)
        except self.driver.Error as e:
            raise QueryError(self._describe(e, sql)) from e

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

    def _catalog(self, sql: str, params: Mapping[str, Any]) -> list[tuple]:
        """Rows of a catalog query written in the driver's own placeholder style."""
        # Catalog rows are small and all kept, so large batches save round trips.
        return list(self.execute(sql, params, fetch_size=10 * FETCH_SIZE).rows())

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
                f"connections.{self.config.label}.options: dprobe sets {', '.join(clash)} itself "
                "(from url, user and password, or always, like charset)"
            )
        return {**core, **self.config.options}

    def _host_url(self) -> tuple[str, int | None, str | None]:
        try:
            return parse_host_url(self.config.url)
        except ValueError as e:
            raise ConfigError(f"connections.{self.config.label}.url: {e}") from None


def group_indexes(
    rows: Iterable[tuple[str, bool, bool, str | None, str | None, bool, bool]],
) -> list[IndexInfo]:
    """Build IndexInfo from one row per index column, in key order.

    Each row is (index name, unique, primary, type, column text, descending,
    included); a row with no index name (a table without indexes) is
    skipped. The primary key comes first, then the rest by name.
    """
    grouped: dict[str, tuple[IndexInfo, list[str], list[str]]] = {}
    for name, unique, primary, type_, column, descending, included in rows:
        if name is None:
            continue
        if name not in grouped:
            grouped[name] = (IndexInfo(name, "", bool(unique), bool(primary), type_, None), [], [])
        if column is not None:
            grouped[name][2 if included else 1].append(f"{column} DESC" if descending else column)
    indexes = [
        replace(info, columns=", ".join(keys), include=", ".join(included) or None)
        for info, keys, included in grouped.values()
    ]
    return sorted(indexes, key=lambda i: (not i.primary, i.name))


class Result:
    """Output of one statement: one or more result sets, or none (columns is None).

    affected is the number of changed rows, or None when rowcount doesn't
    mean that (e.g. DDL, which Oracle and MySQL report as 0). Values are the
    driver's Python types, so digits beyond microseconds (DATETIME2,
    TIMESTAMP(9)) are lost.
    """

    def __init__(self, connector: Connector, cursor: Any, sql: str) -> None:
        self._connector = connector
        self._sql = sql
        self._cursor = cursor
        # Read first: moving on to further result sets changes it.
        rowcount = cursor.rowcount
        self._sets = connector._result_sets(cursor)
        self.columns: list[str] | None = None
        self.affected: int | None = None
        if not self.next_set() and (
            rowcount > 0 or (rowcount == 0 and first_keyword(sql, connector.dialect) in _DML)
        ):
            self.affected = rowcount

    def next_set(self) -> bool:
        """Move to the next result set with columns, once this one is read or discarded."""
        cursor = next(self._sets, None)
        if cursor is None:
            return False
        self._cursor = cursor
        self.columns = [d[0] for d in cursor.description]
        return True

    def discard(self) -> None:
        """Drop this set's unread rows, e.g. after --max-rows, so the connection is free again."""
        self._connector._discard(self._cursor)

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
