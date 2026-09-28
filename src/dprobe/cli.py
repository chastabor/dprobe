"""Command-line interface."""

import argparse
import sys
import time
import traceback
from pathlib import Path

from dprobe import __version__
from dprobe.config import Config, load_config
from dprobe.connectors import create_connector
from dprobe.errors import DprobeError, redact
from dprobe.output import format_table


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DprobeError as e:
        _report(e, args.verbose)
        return e.exit_code
    except KeyboardInterrupt:
        return 130


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
                elapsed_ms = (time.perf_counter() - start) * 1000
                version = connector.server_version()
        except DprobeError as e:
            _report(e, args.verbose)
            status = status or e.exit_code
            continue
        print(f"{target.label}: connected in {elapsed_ms:.0f} ms ({version})")
    return status


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


def _report(error: DprobeError, verbose: bool) -> None:
    if verbose:
        sys.stderr.write(redact(traceback.format_exc()))
    print(f"dprobe: error: {redact(str(error))}", file=sys.stderr)
