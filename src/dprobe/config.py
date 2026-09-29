"""Find, load and validate the YAML connection config."""

import getpass
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn

import yaml

from dprobe import tty
from dprobe.errors import ConfigError, DprobeError, register_secret

CONFIG_ENV = "DPROBE_CONFIG"
DRIVERS = ("oracle", "mssql", "mysql")
FORMATS = ("table", "csv", "tsv", "json", "jsonl")

_TOP_KEYS = ("defaults", "connections")
_DEFAULT_KEYS = ("format", "max_rows")
_CONNECTION_KEYS = ("driver", "url", "user", "password", "password_cmd", "keyring", "readonly", "options")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class ConnectionConfig:
    label: str
    driver: str
    url: str
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    password_cmd: str | None = None
    keyring: str | None = None  # service name; the password is stored under it for user
    readonly: bool = False
    options: Mapping[str, Any] = field(default_factory=dict)

    @property
    def auth_source(self) -> str:
        """Where the password comes from: env, password, command, keyring, prompt or none."""
        if self.password is not None:
            return "env" if _ENV_REF.search(self.password) else "password"
        if self.password_cmd is not None:
            return "command"
        if self.keyring is not None:
            return "keyring"
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
    override: Path | None = None  # the override file merged over path, if there was one

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


def read_utf8(path: Path, error: type[DprobeError]) -> str:
    """Read a text file as UTF-8, raising error for anything unreadable.

    utf-8-sig drops the byte-order mark that Notepad and SSMS write; json.loads
    would reject it.
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise error(f"cannot read {path}: {e.strerror}") from e
    except UnicodeDecodeError:
        raise error(f'{path} is not UTF-8; save it as UTF-8 (SSMS "Unicode" files are UTF-16)') from None


def load_config(explicit: Path | None = None) -> Config:
    """Load the config file, with its override file merged over it when there is one."""
    path = find_config(explicit)
    data = _read_yaml(path)
    override = override_path(path)
    if not override.is_file():
        return parse_config(data, path)
    changes = _read_yaml(override)
    if changes is not None and not isinstance(changes, dict):
        raise ConfigError(f"{override}: expected a mapping, like the file it overrides")
    return parse_config(merge(data, changes or {}), path, override)


def override_path(path: Path) -> Path:
    """The file merged over path, as docker compose does: dprobe.yaml -> dprobe.override.yaml."""
    return path.with_name(f"{path.stem}.override{path.suffix}")


def merge(base: Any, changes: Any) -> Any:
    """changes laid over base: mappings merge key by key, anything else replaces, and null removes the key.

    So an override can set one connection's password, or swap password_cmd
    for password with "password_cmd: null".
    """
    if not (isinstance(base, dict) and isinstance(changes, dict)):
        return changes
    merged = dict(base)
    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = merge(merged[key], value) if key in merged else value
    return merged


def parse_config(data: Any, path: Path, override: Path | None = None) -> Config:
    """Validate parsed YAML. url values holding ${VAR} are checked after expansion."""
    v = _Validator(f"{path} with {override}" if override else str(path))
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
    return Config(path=path, defaults=defaults, connections=connections, override=override)


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
    """Return the password from password, password_cmd, keyring, or a terminal prompt.

    Pass an expand_env() copy. Returns None when no user is set, leaving
    authentication to the driver options (e.g. an Oracle wallet).
    """
    where = f"connections.{conn.label}"
    if conn.password is not None:
        password = conn.password
    elif conn.password_cmd is not None:
        password = _run_password_cmd(conn.password_cmd, where)
    elif conn.keyring is not None:
        password = _with_keyring(conn, lambda keyring: keyring.get_password(conn.keyring, conn.user))
        if password is None:
            raise ConfigError(f"{where}: no password in keyring service {conn.keyring!r} for {conn.user}; "
                              f"store one with: dprobe keyring {conn.label}")
    elif conn.user is None:
        return None
    else:
        password = prompt_password(conn)
    register_secret(password)
    return password


def prompt_password(conn: ConnectionConfig) -> str:
    """Ask for conn's password on the terminal; ConfigError without one or on Ctrl-D."""
    if not tty.available():
        raise ConfigError(f"connections.{conn.label}: no password, password_cmd or keyring, "
                          "and no terminal to prompt on")
    try:
        return getpass.getpass(f"Password for {conn.user} on {conn.label}: ")
    except EOFError:
        raise ConfigError(f"{conn.label}: password prompt cancelled") from None


def store_keyring_password(conn: ConnectionConfig, password: str | None) -> None:
    """Save password in conn's keyring service under its user; None deletes it.

    Pass an expand_env() copy, so the user matches the one resolve_password() looks up.
    """
    if conn.keyring is None:
        raise ConfigError(f"connections.{conn.label}: set keyring: SERVICE to keep its password in the keyring")
    if password is None:
        _with_keyring(conn, lambda keyring: keyring.delete_password(conn.keyring, conn.user))
    else:
        _with_keyring(conn, lambda keyring: keyring.set_password(conn.keyring, conn.user, password))


def _with_keyring(conn: ConnectionConfig, action: Callable[[Any], Any]) -> Any:
    """Run action(keyring module), turning its failures into ConfigError.

    keyring is an optional dependency, and headless Linux often has no
    backend for it (Secret Service needs a desktop session's D-Bus).
    """
    where = f"connections.{conn.label}.keyring"
    try:
        import keyring
        import keyring.errors
    except ImportError:
        raise ConfigError(f"{where}: the keyring package isn't installed; install dprobe[keyring]") from None
    try:
        return action(keyring)
    except keyring.errors.PasswordDeleteError:
        raise ConfigError(f"{where}: no password stored for {conn.user}") from None
    except keyring.errors.KeyringError as e:
        raise ConfigError(f"{where}: {e} (no usable keyring backend?)") from e


class _Validator:
    def __init__(self, source: str) -> None:
        self.source = source

    def fail(self, where: str, message: str) -> NoReturn:
        raise ConfigError(f"{self.source}: {where}: {message}")

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
    keyring = v.string(data.get("keyring"), f"{where}.keyring")
    if sum(x is not None for x in (password, password_cmd, keyring)) > 1:
        v.fail(where, "set only one of password, password_cmd and keyring")
    if keyring is not None and data.get("user") is None:
        v.fail(where, "keyring needs a user to store the password under")
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
        keyring=keyring,
        readonly=readonly,
        options=options,
    )


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(read_utf8(path, ConfigError))
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e


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
    result = subprocess.run(command, shell=True, stdout=subprocess.PIPE, encoding="utf-8")
    if result.returncode != 0:
        raise ConfigError(f"{where}.password_cmd exited with status {result.returncode}")
    # First line only, so multi-line entries (e.g. from `pass show`) work.
    lines = result.stdout.splitlines()
    if not lines or not lines[0]:
        raise ConfigError(f"{where}.password_cmd printed no password")
    return lines[0]


def _search_paths() -> list[Path]:
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return [Path("dprobe.yaml"), Path(config_home) / "dprobe" / "config.yaml"]
