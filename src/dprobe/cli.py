"""Command-line interface."""

import argparse
import os
import sys
import time
import traceback
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import TextIO

from dprobe import __version__
from dprobe.config import FORMATS, Config, load_config
from dprobe.connectors import REGISTRY, create_connector
from dprobe.errors import DprobeError, UsageError, redact
from dprobe.output import format_table, write_result


def main(argv: list[str] | None = None) -> int:
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
    query.add_argument("-f", "--format", choices=FORMATS,
                       help="output format (default: defaults.format in the config, else table)")
    query.add_argument("-o", "--output", type=Path, metavar="FILE",
                       help="write results to FILE; not created if the query fails")
    query.add_argument("--max-rows", type=_count, metavar="N",
                       help="stop after N rows, 0 for no limit "
                            "(default: defaults.max_rows for table, no limit otherwise)")
    query.add_argument("--max-width", type=_count, default=80, metavar="N",
                       help="cut table cells to N characters, 0 for no limit (default: 80)")
    query.add_argument("--null", metavar="TEXT",
                       help='text for NULL (default: "NULL" in table, empty in csv/tsv; JSON uses null)')
    query.add_argument("--native", action="store_true",
                       help="send the SQL exactly as written (no removal of a trailing ; / or GO)")
    query.add_argument("--commit", action="store_true",
                       help="commit instead of rolling back; DDL on Oracle and MySQL commits regardless")
    query.set_defaults(func=cmd_query)
    return parser


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
    sql = _read_sql(args.sql_file, args.execute)
    if not args.native:
        sql = REGISTRY[target.driver].prepare(sql)
    if not sql.strip():
        raise UsageError("no SQL to run")
    fmt = args.format or config.defaults.format
    if args.max_rows is None:
        max_rows = config.defaults.max_rows if fmt == "table" else None
    else:
        max_rows = args.max_rows or None

    hidden_results, extra = False, ""
    with create_connector(target) as connector:
        start = time.perf_counter()
        result = connector.execute(sql, max_rows=max_rows)
        if result.columns is None:
            status = _rowless_status(result.affected, connector.ddl_autocommits, args.commit)
        else:
            with _open_output(args.output) as out:
                count, more = write_result(result, fmt, out, null=args.null, max_rows=max_rows,
                                           max_width=args.max_width or None)
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


def _read_sql(path: str | None, inline: str | None) -> str:
    if (path is None) == (inline is None):
        raise UsageError('give exactly one of a SQL file, "-" for stdin, or -e SQL')
    if inline is not None:
        return inline
    if path == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            raise UsageError(f"cannot read {path}: {e.strerror}") from e
        except UnicodeDecodeError:
            raise UsageError(
                f'{path} is not UTF-8 (SSMS "Unicode" files are UTF-16; save as UTF-8)'
            ) from None
    # Drop the byte-order mark that SSMS and Notepad write.
    return text.removeprefix("\ufeff")


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
