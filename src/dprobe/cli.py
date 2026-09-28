"""Command-line interface."""

import argparse
import io
import os
import sys
import time
import traceback
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import TextIO

from dprobe import __version__
from dprobe.binds import TYPES, bind, load_binds_file, parse_bind_arg
from dprobe.config import FORMATS, Config, ConnectionConfig, load_config, read_utf8
from dprobe.connectors import REGISTRY, create_connector
from dprobe.connectors.base import ColumnInfo, Connector, TableInfo
from dprobe.errors import DprobeError, QueryError, UsageError, redact
from dprobe.output import Table, format_table, text_value, write_result
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

    query = sub.add_parser("query", help="run one SQL statement and print the results")
    _add_global_options(query, in_subcommand=True)
    query.add_argument("label", metavar="LABEL")
    query.add_argument("sql_file", nargs="?", metavar="FILE", help='SQL file, or "-" for stdin')
    query.add_argument("-e", "--execute", metavar="SQL", help="SQL text to run instead of a file")
    _add_output_options(query)
    query.add_argument("--max-rows", type=_count, metavar="N",
                       help="stop after N rows, 0 for no limit "
                            "(default: defaults.max_rows for table, no limit otherwise)")
    query.add_argument("-b", "--bind", action="append", default=[], metavar="NAME[:TYPE]=VALUE",
                       help=f"value for :NAME, repeatable; TYPE is {', '.join(TYPES)} (default str)")
    query.add_argument("--binds", type=Path, metavar="FILE",
                       help="YAML or JSON mapping of bind names to values; -b takes priority")
    query.add_argument("--dry-run", action="store_true",
                       help="print the SQL and bind values that would be sent, without connecting")
    query.add_argument("--native", action="store_true",
                       help="send the SQL exactly as written: no binds, no removal of a trailing ; / or GO")
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


def cmd_query(args: argparse.Namespace) -> int:
    config = _load(args)
    target = config.get(args.label)
    if args.commit and target.readonly:
        raise UsageError(f"{target.label} is readonly, so --commit is not allowed")
    if args.native and (args.bind or args.binds):
        raise UsageError("--native sends the SQL as written, so it takes no -b or --binds values")
    sql, params = _read_sql(args.sql_file, args.execute), None
    if not args.native:
        sql, params = _bind(args, target, REGISTRY[target.driver].prepare(sql))
    if not sql.strip():
        raise UsageError("no SQL to run")
    if args.dry_run:
        _print_dry_run(sql, params)
        return 0
    if args.max_rows is None:
        max_rows = config.defaults.max_rows if _format(args, config) == "table" else None
    else:
        max_rows = args.max_rows or None

    hidden_results, extra = False, ""
    with create_connector(target) as connector:
        start = time.perf_counter()
        result = connector.execute(sql, params, max_rows=max_rows)
        if result.columns is None:
            status = _rowless_status(result.affected, connector.ddl_autocommits, args.commit)
        else:
            count, more = _write(args, config, result, max_rows=max_rows)
            hidden_results = result.has_more_results()
            status = f"first {_plural(count, 'row')}" if more else _plural(count, "row")
            status += "; committed" if args.commit else ""
            extra = "; --max-rows 0 shows all" if more else ""
        if args.commit:
            connector.commit()
    _status(status, start, extra)
    if hidden_results:
        print("dprobe: warning: the batch returned more result sets; only the first is shown",
              file=sys.stderr)
    return 0


def cmd_tables(args: argparse.Namespace) -> int:
    config = _load(args)
    target = config.get(args.label)
    # Names are checked before connecting, so a typo costs no password prompt.
    (schema,) = _names(target, args.schema, "one name for --schema", limit=1) if args.schema else (None,)
    with create_connector(target) as connector:
        start = time.perf_counter()
        found = connector.list_tables(schema, args.like, args.views)
    _write(args, config, Table.of(found, TableInfo))
    views = sum(t.type == "VIEW" for t in found)
    _status(_plural(len(found) - views, "table") + (f", {_plural(views, 'view')}" if args.views else ""), start)
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    config = _load(args)
    target = config.get(args.label)
    schema, name = (None, *_names(target, args.table, "[SCHEMA.]TABLE", limit=2))[-2:]
    with create_connector(target) as connector:
        start = time.perf_counter()
        found = connector.describe(schema, name)
        if found is None:
            raise QueryError(_not_found(connector, schema, name))
        _write(args, config, connector.raw_columns(found) if args.raw else Table.of(found.columns, ColumnInfo))
    via = f" via synonym {found.synonym}" if found.synonym else ""
    _status(f"{found.schema}.{found.name}{via}: {_plural(len(found.columns), 'column')}", start)
    return 0


def _bind(args: argparse.Namespace, target: ConnectionConfig, sql: str) -> tuple[str, dict | None]:
    given = dict(parse_bind_arg(text) for text in args.bind)
    values = {**(load_binds_file(args.binds) if args.binds else {}), **given}
    sql, params, used = bind(sql, target.driver, REGISTRY[target.driver].placeholder, values)
    if unused := sorted(given.keys() - used):
        print(f"dprobe: warning: the statement has no :{', :'.join(unused)}", file=sys.stderr)
    return sql, params


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


def _write(args: argparse.Namespace, config: Config, rows, *, max_rows: int | None = None) -> tuple[int, bool]:
    with _open_output(args.output) as out:
        return write_result(rows, _format(args, config), out, null=args.null, max_rows=max_rows,
                            max_width=args.max_width or None)


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


def _print_dry_run(sql: str, params: dict | None) -> None:
    print(sql.rstrip("\n"))
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


def _open_output(path: Path | None) -> AbstractContextManager[TextIO]:
    if path is None:
        return nullcontext(sys.stdout)
    try:
        # newline="" so the csv writer's line endings pass through unchanged.
        return open(path, "w", encoding="utf-8", newline="")
    except OSError as e:
        raise UsageError(f"cannot write {path}: {e.strerror}") from e


def _rowless_status(affected: int | None, ddl_autocommits: bool, commit: bool) -> str:
    if affected is not None:
        done, note = f"{_plural(affected, 'row')} affected", "use --commit to keep changes"
    else:
        done = "statement executed"
        note = "this database commits DDL regardless" if ddl_autocommits else "use --commit to keep changes"
    return f"{done}; committed" if commit else f"{done}; rolled back ({note})"


def _status(message: str, start: float, extra: str = "") -> None:
    # Keeps the status after the rows when stdout and stderr share a pipe.
    sys.stdout.flush()
    print(f"({message}, {_ms_since(start):.0f} ms{extra})", file=sys.stderr)


def _ms_since(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _count(text: str) -> int:
    if not text.isdigit():
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}")
    return int(text)


def _report(error: DprobeError, verbose: bool) -> None:
    if verbose:
        sys.stderr.write(redact(traceback.format_exc()))
    print(f"dprobe: error: {redact(str(error))}", file=sys.stderr)
