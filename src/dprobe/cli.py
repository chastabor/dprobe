"""Command-line interface."""

import argparse
import io
import os
import sys
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

from dprobe import __version__, tty
from dprobe.binds import TYPES, BindValues, bind_script, declarations, load_binds_file, parse_bind_arg
from dprobe.config import (
    FORMATS, Config, ConnectionConfig, load_config, prompt_password, read_utf8, store_keyring_password,
)
from dprobe.connectors import REGISTRY, create_connector
from dprobe.connectors.base import ColumnInfo, Connector, Found, IndexInfo, ResultColumn, SchemaInfo, TableInfo
from dprobe.errors import DprobeError, QueryError, UsageError, redact
from dprobe.output import ResultWriter, Table, format_table, text_value
from dprobe.sqltext import split_name


def main(argv: list[str] | None = None) -> int:
    _use_utf8()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DprobeError as e:
        _report(e, args.verbose)
        return e.exit_code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # Output went to something like `head` that exited early. Point stdout at
        # devnull so the interpreter's final flush doesn't fail again.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1


def _use_utf8() -> None:
    # Python picks the stdio encoding from the locale, which is ASCII under e.g.
    # LC_ALL=C in cron; dprobe always reads and writes UTF-8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dprobe", description="Run SQL and inspect tables on Oracle, SQL Server and MySQL."
    )
    _add_global_options(parser, in_subcommand=False)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    labels = sub.add_parser("labels", help="list configured connections")
    _add_global_options(labels, in_subcommand=True)
    labels.set_defaults(func=cmd_labels)

    ping = sub.add_parser("ping", help="connect and show the server version and connect time")
    _add_global_options(ping, in_subcommand=True)
    ping.add_argument("labels", nargs="+", metavar="LABEL")
    ping.set_defaults(func=cmd_ping)

    keyring = sub.add_parser("keyring", help="store a connection's password in its keyring service")
    _add_global_options(keyring, in_subcommand=True)
    keyring.add_argument("label", metavar="LABEL")
    keyring.add_argument("--delete", action="store_true", help="remove the stored password instead")
    keyring.set_defaults(func=cmd_keyring)

    query = sub.add_parser("query", help="run SQL and print the results")
    _add_global_options(query, in_subcommand=True)
    query.add_argument("label", metavar="LABEL")
    query.add_argument("sql_file", nargs="?", metavar="FILE", help='SQL file, or "-" for stdin')
    query.add_argument("-e", "--execute", metavar="SQL", help="SQL text to run instead of a file")
    _add_output_options(query)
    query.add_argument("--max-rows", type=_count, metavar="N",
                       help="stop after N rows, 0 for no limit "
                            "(default: defaults.max_rows for table, no limit otherwise)")
    query.add_argument("-b", "--bind", action="append", default=[], metavar="NAME[:TYPE]=VALUE",
                       help=f"value for :NAME, repeatable; TYPE is {', '.join(TYPES)} (default str), "
                            "or TYPE[] for a comma-separated list, e.g. -b ids:int[]=1,2,3 for IN (:ids)")
    query.add_argument("--binds", type=Path, metavar="FILE",
                       help="YAML or JSON mapping of bind names to values; -b takes priority")
    query.add_argument("--dry-run", action="store_true",
                       help="print the SQL and bind values that would be sent, without connecting")
    query.add_argument("--meta", action="store_true",
                       help="show the result columns instead of rows (Oracle and SQL Server don't run the "
                            "query; MySQL does, so it takes only queries)")
    query.add_argument("--native", action="store_true",
                       help="send the SQL exactly as written: no binds, no removal of a trailing ; / or GO")
    query.add_argument("--script", action="store_true",
                       help="run every statement in the file in order, in one transaction, stopping at "
                            "the first error (split on ; or, for SQL Server, GO lines)")
    query.add_argument("--commit", action="store_true",
                       help="commit instead of rolling back; DDL on Oracle and MySQL commits regardless")
    query.set_defaults(func=cmd_query)

    tables = sub.add_parser("tables", help="list tables, and optionally views")
    _add_global_options(tables, in_subcommand=True)
    tables.add_argument("label", metavar="LABEL")
    tables.add_argument("--schema", metavar="SCHEMA",
                        help="default: the current schema; on SQL Server, every schema in the database")
    tables.add_argument("--like", metavar="PATTERN",
                        help="SQL LIKE pattern for the name, any case, e.g. 'emp%%'")
    tables.add_argument("--views", action="store_true", help="include views")
    _add_output_options(tables)
    tables.set_defaults(func=cmd_tables)

    describe = sub.add_parser("describe", help="show the columns of a table or view")
    _add_global_options(describe, in_subcommand=True)
    describe.add_argument("label", metavar="LABEL")
    describe.add_argument("table", metavar="[SCHEMA.]TABLE",
                          help='quote a part to keep its case or dots, e.g. \'"Mixed Case"\'')
    describe.add_argument("--raw", action="store_true",
                          help="the database's own catalog rows instead of the common columns")
    _add_output_options(describe)
    describe.set_defaults(func=cmd_describe)

    indexes = sub.add_parser("indexes", help="show the indexes of a table")
    _add_global_options(indexes, in_subcommand=True)
    indexes.add_argument("label", metavar="LABEL")
    indexes.add_argument("table", metavar="[SCHEMA.]TABLE",
                         help='quote a part to keep its case or dots, e.g. \'"Mixed Case"\'')
    _add_output_options(indexes)
    indexes.set_defaults(func=cmd_indexes)

    schemas = sub.add_parser("schemas", help="list schemas, marking the current one")
    _add_global_options(schemas, in_subcommand=True)
    schemas.add_argument("label", metavar="LABEL")
    schemas.add_argument("--all", action="store_true", help="include the database's own system schemas")
    _add_output_options(schemas)
    schemas.set_defaults(func=cmd_schemas)
    return parser


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-f", "--format", choices=FORMATS,
                        help="output format (default: defaults.format in the config, else table)")
    parser.add_argument("-o", "--output", type=Path, metavar="FILE",
                        help="write results to FILE; not created if the command fails")
    parser.add_argument("--max-width", type=_count, default=80, metavar="N",
                        help="cut table cells to N characters, 0 for no limit (default: 80)")
    parser.add_argument("--null", metavar="TEXT",
                        help='text for NULL (default: "NULL" in table, empty in csv/tsv; JSON uses null)')


def cmd_labels(args: argparse.Namespace) -> int:
    config = _load(args)
    rows = [
        (c.label, c.driver, c.url, c.user, c.auth_source, "yes" if c.readonly else None)
        for c in config.connections.values()
    ]
    print(format_table(("label", "driver", "url", "user", "auth", "readonly"), rows))
    return 0


def cmd_ping(args: argparse.Namespace) -> int:
    config = _load(args)
    # Look up every label first so a typo fails before any connection is attempted.
    targets = [config.get(label) for label in args.labels]
    status = 0
    for target in targets:
        try:
            connector = create_connector(target)
            start = time.perf_counter()
            with connector:
                elapsed_ms = _ms_since(start)
                version = connector.server_version()
        except DprobeError as e:
            _report(e, args.verbose)
            status = status or e.exit_code
            continue
        print(f"{target.label}: connected in {elapsed_ms:.0f} ms ({version})")
    return status


def cmd_keyring(args: argparse.Namespace) -> int:
    # Expanded like a connection, so a ${VAR} user is stored as it will be looked up.
    target = _load(args).get(args.label).expand_env()
    if args.delete:
        store_keyring_password(target, None)
        print(f"{target.label}: removed the password for {target.user} from keyring service {target.keyring}")
        return 0
    store_keyring_password(target, prompt_password(target))
    print(f"{target.label}: stored the password for {target.user} in keyring service {target.keyring}")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    config = _load(args)
    target = config.get(args.label)
    if args.commit and target.readonly:
        raise UsageError(f"{target.label} is readonly, so --commit is not allowed")
    if args.native and (args.bind or args.binds):
        raise UsageError("--native sends the SQL as written, so it takes no -b or --binds values")
    if args.script and (args.native or args.meta):
        raise UsageError("--script can't be combined with --native or --meta")
    statements = _statements(args, target, _read_sql(args.sql_file, args.execute))
    if args.dry_run:
        _print_dry_run(statements, script=args.script)
        return 0
    if args.max_rows is None:
        max_rows = config.defaults.max_rows if _format(args, config) == "table" else None
    else:
        max_rows = args.max_rows or None

    if args.meta:
        return _query_meta(args, config, target, statements[0])

    with create_connector(target) as connector, _writer(args, config) as out:
        report = _Report(time.perf_counter(), script=args.script, commit=args.commit,
                         ddl_autocommits=connector.ddl_autocommits)
        for number, (line, sql, params) in enumerate(statements, 1):
            report.statement(number, line)
            try:
                result = connector.execute(sql, params, max_rows=max_rows)
            except QueryError as e:
                if not args.script:
                    raise
                raise QueryError(f"statement {number}, line {line}: {e}") from e
            if result.columns is None:
                report.rowless(result.affected)
                continue
            while True:
                count, more = out.write(result, max_rows=max_rows)
                report.rows(count, more)
                if more:
                    # Frees the connection for the next statement; later sets aren't read.
                    result.discard()
                    break
                if not result.next_set():
                    break
                report.next_set()
        if args.commit:
            connector.commit()
    report.finish()
    return 0


def _query_meta(args: argparse.Namespace, config: Config, target: ConnectionConfig, statement: "Statement") -> int:
    _, sql, params = statement
    with create_connector(target) as connector:
        start = time.perf_counter()
        columns = connector.describe_result(sql, params)
    _write(args, config, Table.of(columns, ResultColumn))
    _status(_plural(len(columns), "result column") if columns else "no result columns", start)
    return 0


def cmd_tables(args: argparse.Namespace) -> int:
    config = _load(args)
    target = config.get(args.label)
    # Names are checked before connecting, so a typo costs no password prompt.
    schema = _names(target, args.schema, "one name for --schema", limit=1)[0] if args.schema else None
    with create_connector(target) as connector:
        start = time.perf_counter()
        found = connector.list_tables(schema, args.like, args.views)
    _write(args, config, Table.of(found, TableInfo))
    views = sum(t.type == "VIEW" for t in found)
    _status(_plural(len(found) - views, "table") + (f", {_plural(views, 'view')}" if args.views else ""), start)
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    def rows(connector: Connector, found: Found) -> Any:
        return connector.raw_columns(found) if args.raw else Table.of(found.items, ColumnInfo)

    return _show_table(args, lambda connector, schema, name: connector.describe(schema, name), rows, "column")


def cmd_indexes(args: argparse.Namespace) -> int:
    return _show_table(args, lambda connector, schema, name: connector.indexes(schema, name),
                       lambda _, found: Table.of(found.items, IndexInfo), "index", "indexes")


def _show_table(args: argparse.Namespace, lookup: Callable[[Connector, str | None, str], Found | None],
                rows: Callable[[Connector, Found], Any], word: str, plural: str | None = None) -> int:
    """Look a table up with lookup(connector, schema, name) and write rows(connector, found)."""
    config = _load(args)
    target = config.get(args.label)
    schema, name = (None, *_names(target, args.table, "[SCHEMA.]TABLE", limit=2))[-2:]
    with create_connector(target) as connector:
        start = time.perf_counter()
        found = lookup(connector, schema, name)
        if found is None:
            raise QueryError(_not_found(connector, schema, name))
        _write(args, config, rows(connector, found))
    via = f" via synonym {found.synonym}" if found.synonym else ""
    _status(f"{found.schema}.{found.name}{via}: {_plural(len(found.items), word, plural)}", start)
    return 0


def cmd_schemas(args: argparse.Namespace) -> int:
    config = _load(args)
    with create_connector(config.get(args.label)) as connector:
        start = time.perf_counter()
        found = connector.schemas()
    shown = [s for s in found if args.all or not s.system]
    _write(args, config, Table.of(shown, SchemaInfo))
    hidden = len(found) - len(shown)
    _status(_plural(len(shown), "schema"), start,
            f"; {_plural(hidden, 'system schema')} hidden, --all shows them" if hidden else "")
    return 0


Statement = tuple[int, str, dict | None]  # line number, SQL to send, params


def _statements(args: argparse.Namespace, target: ConnectionConfig, text: str) -> list[Statement]:
    """Split, clean up and bind the SQL; --native sends it whole and untouched."""
    if args.native:
        if not text.strip():
            raise UsageError("no SQL to run")
        return [(1, text, None)]
    try:
        prepared = REGISTRY[target.driver].statements(text)
    except ValueError as e:
        raise UsageError(str(e)) from None
    if not prepared:
        raise UsageError("no SQL to run")
    if len(prepared) > 1 and not args.script:
        lines = ", ".join(str(line) for line, _ in prepared)
        raise UsageError(f"the SQL holds {len(prepared)} statements (lines {lines}); "
                         "add --script to run them in order")
    return _bind(args, target, text, prepared)


def _bind(args: argparse.Namespace, target: ConnectionConfig, text: str,
          prepared: list[tuple[int, str]]) -> list[Statement]:
    connector_class = REGISTRY[target.driver]
    declared = declarations(text, connector_class.dialect)
    given = dict(parse_bind_arg(arg, declared) for arg in args.bind)
    values = BindValues(
        given={**(load_binds_file(args.binds, declared) if args.binds else {}), **given},
        declared=declared,
        ask=tty.ask if tty.available() else None,
    )
    bound, used = bind_script([sql for _, sql in prepared], connector_class.dialect,
                              connector_class.placeholder, values)
    # A -b or @bind name the SQL lacks is most likely a typo.
    if unused := sorted((given.keys() | declared.keys()) - used):
        print(f"dprobe: warning: the statement has no :{', :'.join(unused)}", file=sys.stderr)
    return [(line, *statement) for (line, _), statement in zip(prepared, bound, strict=True)]


def _names(target: ConnectionConfig, text: str, form: str, *, limit: int) -> list[str]:
    try:
        parts = split_name(text)
    except ValueError as e:
        raise UsageError(str(e)) from None
    if len(parts) > limit:
        raise UsageError(f"{text!r}: expected {form}")
    return [REGISTRY[target.driver].identifier(part, quoted) for part, quoted in parts]


def _not_found(connector: Connector, schema: str | None, name: str) -> str:
    shown = f"{schema}.{name}" if schema else name
    # Searches where list_tables() does by default: the current schema, or on
    # SQL Server every schema. Catches wrong case, e.g. on case-sensitive MySQL.
    similar = [f"{t.schema}.{t.name}" for t in connector.list_tables(schema, name, views=True)]
    hint = f"; did you mean {', '.join(similar)}?" if similar else ""
    return f"no table or view {shown}{hint}"


def _format(args: argparse.Namespace, config: Config) -> str:
    return args.format or config.defaults.format


def _write(args: argparse.Namespace, config: Config, rows: Any) -> None:
    with _writer(args, config) as out:
        out.write(rows)


@contextmanager
def _writer(args: argparse.Namespace, config: Config) -> Iterator[ResultWriter]:
    """A ResultWriter for -o FILE or stdout, closing the file afterwards."""
    writer = ResultWriter(lambda: _open_output(args.output), _format(args, config), null=args.null,
                          max_width=args.max_width or None)
    try:
        yield writer
    finally:
        if writer.out is not None and writer.out is not sys.stdout:
            writer.out.close()


class _Report:
    """Status lines on stderr for query.

    A single result gets one line, as before. With several result sets or a
    --script, each result gets a line as it finishes, then a total with the
    time and what happened to the transaction.
    """

    def __init__(self, start: float, *, script: bool, commit: bool, ddl_autocommits: bool) -> None:
        self._start = start
        self._script = script
        self._commit = commit
        self._ddl_autocommits = ddl_autocommits
        self._where = ""
        self._statements = self._set = 0
        self._pending: str | None = None  # the only result's line, until we know if more follow
        self._dml = self._ddl = self._truncated = False

    def statement(self, number: int, line: int) -> None:
        self._statements += 1
        self._set = 0
        self._where = f"statement {number}, line {line}"

    def rows(self, count: int, more: bool) -> None:
        self._truncated |= more
        self._add(f"first {_plural(count, 'row')}" if more else _plural(count, "row"))

    def rowless(self, affected: int | None) -> None:
        self._dml |= affected is not None
        self._ddl |= affected is None
        self._add(f"{_plural(affected, 'row')} affected" if affected is not None else "statement executed")

    def next_set(self) -> None:
        if self._pending is not None:
            _status(f"result 1: {self._pending}")
            self._pending = None

    def finish(self) -> None:
        if self._pending is not None:
            head = self._pending
        else:
            head = _plural(self._statements, "statement") if self._script else _plural(self._set, "result")
        _status(head + self._outcome(), self._start, "; --max-rows 0 shows all" if self._truncated else "")

    def _add(self, message: str) -> None:
        self._set += 1
        if self._script:
            _status(self._where + (f", result {self._set}" if self._set > 1 else "") + f": {message}")
        elif self._set == 1:
            self._pending = message
        else:
            _status(f"result {self._set}: {message}")

    def _outcome(self) -> str:
        if self._commit:
            return "; committed"
        if self._dml:
            return "; rolled back (use --commit to keep changes)"
        if self._ddl:
            note = "this database commits DDL regardless" if self._ddl_autocommits else "use --commit to keep changes"
            return f"; rolled back ({note})"
        return ""


def _add_global_options(parser: argparse.ArgumentParser, *, in_subcommand: bool) -> None:
    """Accept global options before or after the subcommand.

    Subcommands default to SUPPRESS so they don't overwrite a value given
    before the subcommand. Each parser needs its own actions: set_defaults()
    or parents= would share them and leak defaults across parsers.
    """
    def default(value: object) -> object:
        return argparse.SUPPRESS if in_subcommand else value

    parser.add_argument("--config", type=Path, default=default(None),
                        help="config file (default: $DPROBE_CONFIG, ./dprobe.yaml, "
                             "~/.config/dprobe/config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", default=default(False),
                        help="show tracebacks and the config file in use")


def _load(args: argparse.Namespace) -> Config:
    config = load_config(args.config)
    if args.verbose:
        print(f"dprobe: using {config.path}", file=sys.stderr)
    for warning in config.warnings:
        print(f"dprobe: warning: {warning}", file=sys.stderr)
    return config


def _print_dry_run(statements: list[Statement], *, script: bool) -> None:
    for number, (line, sql, params) in enumerate(statements, 1):
        if script:
            print(f"{chr(10) if number > 1 else ''}-- statement {number}, line {line}")
        print(sql.strip() if script else sql.rstrip("\n"))
        for name, value in (params or {}).items():
            if value is None:
                shown = "NULL"
            elif isinstance(value, str):
                shown = repr(value)
            else:
                shown = f"{text_value(value)} ({type(value).__name__})"
            print(f"-- bind {name} = {shown}")


def _read_sql(path: str | None, inline: str | None) -> str:
    if (path is None) == (inline is None):
        raise UsageError('give exactly one of a SQL file, "-" for stdin, or -e SQL')
    if inline is not None:
        return inline
    if path != "-":
        return read_utf8(Path(path), UsageError)
    try:
        # stdin is UTF-8 (see _use_utf8); drop a byte-order mark like read_utf8 does.
        return sys.stdin.read().removeprefix("\ufeff")
    except UnicodeDecodeError:
        raise UsageError("stdin is not UTF-8") from None


def _open_output(path: Path | None) -> TextIO:
    if path is None:
        return sys.stdout
    try:
        # newline="" so the csv writer's line endings pass through unchanged.
        return open(path, "w", encoding="utf-8", newline="")
    except OSError as e:
        raise UsageError(f"cannot write {path}: {e.strerror}") from e


def _status(message: str, start: float | None = None, extra: str = "") -> None:
    """Print "(message, N ms extra)" to stderr; without a start, just "(message)"."""
    # Keeps the status after the rows when stdout and stderr share a pipe.
    sys.stdout.flush()
    timing = f", {_ms_since(start):.0f} ms" if start is not None else ""
    print(f"({message}{timing}{extra})", file=sys.stderr)


def _ms_since(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {plural or word + 's'}"


def _count(text: str) -> int:
    if not text.isdigit():
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}")
    return int(text)


def _report(error: DprobeError, verbose: bool) -> None:
    if verbose:
        sys.stderr.write(redact(traceback.format_exc()))
    print(f"dprobe: error: {redact(str(error))}", file=sys.stderr)
