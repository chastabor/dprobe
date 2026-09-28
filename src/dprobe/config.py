"""Find, load and validate the YAML connection config."""

import getpass
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn

import yaml

from dprobe.errors import ConfigError, ConnectError, register_secret

CONFIG_ENV = "DPROBE_CONFIG"
DRIVERS = ("oracle", "mssql", "mysql")
FORMATS = ("table", "csv", "tsv", "json", "jsonl")

_TOP_KEYS = ("defaults", "connections")
_DEFAULT_KEYS = ("format", "max_rows")
_CONNECTION_KEYS = ("driver", "url", "user", "password", "password_cmd", "readonly", "options")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class ConnectionConfig:
    label: str
    driver: str
    url: str
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    password_cmd: str | None = None
    readonly: bool = False
    options: Mapping[str, Any] = field(default_factory=dict)

    @property
    def auth_source(self) -> str:
        """Where the password comes from: env, password, command, prompt or none."""
        if self.password is not None:
            return "env" if _ENV_REF.search(self.password) else "password"
        if self.password_cmd is not None:
            return "command"
        return "prompt" if self.user is not None else "none"

    def expand_env(self) -> "ConnectionConfig":
        """Return a copy with ${VAR} replaced in url, user, password and options.

        password_cmd is left as-is because the shell expands variables in it.
        """
        where = f"connections.{self.label}"
        return replace(
            self,
            url=_expand(self.url, f"{where}.url"),
            user=_expand(self.user, f"{where}.user"),
            password=_expand(self.password, f"{where}.password"),
            options=_expand(dict(self.options), f"{where}.options"),
        )


@dataclass(frozen=True)
class Defaults:
    format: str = "table"
    max_rows: int = 1000


@dataclass(frozen=True)
class Config:
    path: Path
    defaults: Defaults
    connections: Mapping[str, ConnectionConfig]
    warnings: tuple[str, ...] = ()

    def get(self, label: str) -> ConnectionConfig:
        try:
            return self.connections[label]
        except KeyError:
            known = ", ".join(self.connections)
            raise ConfigError(f"unknown label {label!r} (configured: {known})") from None


def find_config(explicit: Path | None = None) -> Path:
    """Return the first of: explicit, $DPROBE_CONFIG, ./dprobe.yaml, ~/.config/dprobe/config.yaml.

    A missing explicit or $DPROBE_CONFIG path is an error, not a fall-through.
    """
    source = "--config"
    if explicit is None and (env := os.environ.get(CONFIG_ENV)):
        explicit, source = Path(env).expanduser(), f"${CONFIG_ENV}"
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"config file not found: {explicit} (from {source})")
        return explicit
    candidates = _search_paths()
    for path in candidates:
        if path.is_file():
            return path
    looked = ", ".join(str(p) for p in candidates)
    raise ConfigError(f"no config file found (looked for {looked}); use --config or ${CONFIG_ENV}")


def load_config(explicit: Path | None = None) -> Config:
    path = find_config(explicit)
    try:
        data = yaml.safe_load(path.read_text())
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e
    config = parse_config(data, path)
    if _readable_by_others(path) and any(
        c.auth_source == "password" for c in config.connections.values()
    ):
        warning = (
            f"{path} contains plaintext passwords and is readable by other users; "
            f"run: chmod 600 {path}"
        )
        config = replace(config, warnings=(*config.warnings, warning))
    return config


def parse_config(data: Any, path: Path) -> Config:
    """Validate parsed YAML. url values holding ${VAR} are checked after expansion."""
    v = _Validator(path)
    top = v.mapping(data, "top level", _TOP_KEYS)
    defaults = _parse_defaults(v, top.get("defaults"))
    raw_connections = v.mapping(top.get("connections"), "connections")
    if not raw_connections:
        v.fail("connections", "at least one connection is required")
    connections = {}
    for label, entry in raw_connections.items():
        if not isinstance(label, str) or not label:
            v.fail("connections", f"label {label!r} must be a non-empty string")
        connections[label] = _parse_connection(v, label, entry)
    return Config(path=path, defaults=defaults, connections=connections)


def parse_host_url(url: str) -> tuple[str, int | None, str | None]:
    """Split "host[:port][/database]" into (host, port, database).

    IPv6 hosts need brackets ("[::1]:3306/db"). A SQL Server named instance
    goes in the host ("sqlsrv01\\SQLEXPRESS/sis"), normally without a port.
    """
    if "://" in url:
        raise ValueError('expected host[:port]/database without a scheme such as "mysql://"')
    hostport, _, database = url.partition("/")
    if hostport.startswith("["):
        host, closed, rest = hostport[1:].partition("]")
        if not closed or (rest and not rest.startswith(":")):
            raise ValueError("expected [ipv6-address]:port")
        port_text = rest[1:]
    else:
        host, _, port_text = hostport.partition(":")
    if not host:
        raise ValueError("missing host")
    port = None
    if port_text:
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise ValueError(f"invalid port {port_text!r}")
        port = int(port_text)
    return host, port, database or None


def resolve_password(conn: ConnectionConfig) -> str | None:
    """Return the password from password, password_cmd, or a terminal prompt.

    Pass an expand_env() copy. Returns None when no user is set, leaving
    authentication to the driver options (e.g. an Oracle wallet).
    """
    where = f"connections.{conn.label}"
    if conn.password is not None:
        password = conn.password
    elif conn.password_cmd is not None:
        password = _run_password_cmd(conn.password_cmd, where)
    elif conn.user is None:
        return None
    elif _can_prompt():
        try:
            password = getpass.getpass(f"Password for {conn.user} on {conn.label}: ")
        except EOFError:
            raise ConnectError(f"{conn.label}: password prompt cancelled") from None
    else:
        raise ConfigError(f"{where}: no password or password_cmd, and no terminal to prompt on")
    register_secret(password)
    return password


class _Validator:
    def __init__(self, path: Path) -> None:
        self.path = path

    def fail(self, where: str, message: str) -> NoReturn:
        raise ConfigError(f"{self.path}: {where}: {message}")

    def mapping(self, value: Any, where: str, allowed: tuple[str, ...] | None = None) -> dict:
        if value is None:
            value = {}
        if not isinstance(value, dict):
            self.fail(where, "expected a mapping")
        if allowed is not None:
            for key in value:
                if key not in allowed:
                    self.fail(where, f"unknown key {key!r} (allowed: {', '.join(allowed)})")
        return value

    def string(self, value: Any, where: str, *, required: bool = False) -> str | None:
        if value is None:
            if required:
                self.fail(where, "is required")
            return None
        if not isinstance(value, str):
            # YAML turns unquoted 1234, yes, 2024-01-01 into non-strings.
            self.fail(where, f"must be a string; put {value!r} in quotes")
        return value


def _parse_defaults(v: _Validator, raw: Any) -> Defaults:
    data = v.mapping(raw, "defaults", _DEFAULT_KEYS)
    defaults = Defaults()
    if "format" in data:
        fmt = v.string(data["format"], "defaults.format", required=True)
        if fmt not in FORMATS:
            v.fail("defaults.format", f"must be one of {', '.join(FORMATS)}")
        defaults = replace(defaults, format=fmt)
    if "max_rows" in data:
        max_rows = data["max_rows"]
        # bool is a subclass of int.
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
            v.fail("defaults.max_rows", "must be a positive integer")
        defaults = replace(defaults, max_rows=max_rows)
    return defaults


def _parse_connection(v: _Validator, label: str, raw: Any) -> ConnectionConfig:
    where = f"connections.{label}"
    data = v.mapping(raw, where, _CONNECTION_KEYS)
    driver = v.string(data.get("driver"), f"{where}.driver", required=True)
    if driver not in DRIVERS:
        v.fail(f"{where}.driver", f"must be one of {', '.join(DRIVERS)}")
    url = v.string(data.get("url"), f"{where}.url", required=True)
    if driver != "oracle" and not _ENV_REF.search(url):
        try:
            parse_host_url(url)
        except ValueError as e:
            v.fail(f"{where}.url", str(e))
    password = v.string(data.get("password"), f"{where}.password")
    password_cmd = v.string(data.get("password_cmd"), f"{where}.password_cmd")
    if password is not None and password_cmd is not None:
        v.fail(where, "set password or password_cmd, not both")
    readonly = data.get("readonly", False)
    if not isinstance(readonly, bool):
        v.fail(f"{where}.readonly", "must be true or false")
    options = v.mapping(data.get("options"), f"{where}.options")
    for key in options:
        if not isinstance(key, str):
            v.fail(f"{where}.options", f"key {key!r} must be a string")
    return ConnectionConfig(
        label=label,
        driver=driver,
        url=url,
        user=v.string(data.get("user"), f"{where}.user"),
        password=password,
        password_cmd=password_cmd,
        readonly=readonly,
        options=options,
    )


def _expand(value: Any, where: str) -> Any:
    if isinstance(value, str):

        def lookup(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(f"{where}: environment variable {name} is not set")
            return os.environ[name]

        return _ENV_REF.sub(lookup, value)
    if isinstance(value, dict):
        return {k: _expand(item, f"{where}.{k}") for k, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, f"{where}[{i}]") for i, item in enumerate(value)]
    return value


def _run_password_cmd(command: str, where: str) -> str:
    # Runs through the shell so pipes and $VARS work. stderr is not captured,
    # so prompts from tools like gpg or op still reach the user.
    result = subprocess.run(command, shell=True, stdout=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise ConfigError(f"{where}.password_cmd exited with status {result.returncode}")
    # First line only, so multi-line entries (e.g. from `pass show`) work.
    lines = result.stdout.splitlines()
    if not lines or not lines[0]:
        raise ConfigError(f"{where}.password_cmd printed no password")
    return lines[0]


def _can_prompt() -> bool:
    # getpass reads /dev/tty, so prompting works even when stdin is a pipe.
    try:
        with open("/dev/tty"):
            return True
    except OSError:
        return False


def _readable_by_others(path: Path) -> bool:
    return bool(path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH))


def _search_paths() -> list[Path]:
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return [Path("dprobe.yaml"), Path(config_home) / "dprobe" / "config.yaml"]
