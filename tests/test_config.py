from pathlib import Path

import pytest

from dprobe.config import ConnectionConfig, find_config, load_config, parse_host_url, resolve_password
from dprobe.errors import ConfigError, redact

BASIC = """
    connections:
      web:
        driver: mysql
        url: web-db:3306/webapp
        user: app_ro
        password: ${WEB_PASS}
"""


def test_load_basic(write_config):
    config = load_config(write_config(BASIC))
    web = config.get("web")
    assert (web.driver, web.url, web.user, web.readonly) == ("mysql", "web-db:3306/webapp", "app_ro", False)
    assert web.password == "${WEB_PASS}"
    assert config.defaults.format == "table"
    assert config.warnings == ()


def test_search_order(write_config, tmp_path, monkeypatch):
    xdg = write_config(BASIC, name="xdg/dprobe/config.yaml")
    assert find_config() == xdg
    local = write_config(BASIC)
    assert find_config() == Path("dprobe.yaml")
    env = write_config(BASIC, name="env.yaml")
    monkeypatch.setenv("DPROBE_CONFIG", str(env))
    assert find_config() == env
    explicit = write_config(BASIC, name="explicit.yaml")
    assert find_config(explicit) == explicit
    assert local.exists()


def test_missing_env_config_does_not_fall_through(write_config, monkeypatch):
    write_config(BASIC)
    monkeypatch.setenv("DPROBE_CONFIG", "nope.yaml")
    with pytest.raises(ConfigError, match=r"nope\.yaml \(from \$DPROBE_CONFIG\)"):
        find_config()


def test_no_config_found():
    with pytest.raises(ConfigError, match="no config file found"):
        find_config()


def test_unknown_label(write_config):
    config = load_config(write_config(BASIC))
    with pytest.raises(ConfigError, match=r"unknown label 'nope' \(configured: web\)"):
        config.get("nope")


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("driver: mysql\n    url: h/d\n    pasword: x", r"connections\.web: unknown key 'pasword'"),
        ("driver: postgres\n    url: h/d", r"connections\.web\.driver: must be one of oracle, mssql, mysql"),
        ("driver: mysql", r"connections\.web\.url: is required"),
        ("driver: mysql\n    url: h/d\n    password: 12345", r"password: must be a string; put 12345 in quotes"),
        ("driver: mysql\n    url: mysql://h/d", r"url: expected host\[:port\]/database without a scheme"),
        ("driver: mssql\n    url: h:99999/d", r"url: invalid port '99999'"),
        ("driver: mysql\n    url: h/d\n    password: a\n    password_cmd: b", "set only one of password"),
        ("driver: mysql\n    url: h/d\n    readonly: maybe", r"readonly: must be true or false"),
        ("driver: mysql\n    url: h/d\n    options: [1]", r"options: expected a mapping"),
    ],
)
def test_invalid_connection(write_config, entry, message):
    path = write_config(f"connections:\n  web:\n    {entry}\n")
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_invalid_top_level(write_config):
    with pytest.raises(ConfigError, match="at least one connection is required"):
        load_config(write_config(""))
    with pytest.raises(ConfigError, match=r"defaults\.max_rows: must be a positive integer"):
        load_config(write_config("defaults: {max_rows: true}\n" + "connections: {a: {driver: oracle, url: x}}"))
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(write_config("connections: [\n"))


def test_url_with_env_ref_is_not_checked_at_load(write_config):
    load_config(write_config("connections:\n  a:\n    driver: mysql\n    url: ${DB_URL}\n"))


@pytest.mark.parametrize(
    ("url", "parts"),
    [
        ("web-db", ("web-db", None, None)),
        ("web-db:3306/webapp", ("web-db", 3306, "webapp")),
        ("[::1]:3306/db", ("::1", 3306, "db")),
        ("sqlsrv01\\SQLEXPRESS/sis", ("sqlsrv01\\SQLEXPRESS", None, "sis")),
        ("host:/db", ("host", None, "db")),
    ],
)
def test_parse_host_url(url, parts):
    assert parse_host_url(url) == parts


def test_plaintext_password_warning(write_config):
    text = "connections:\n  a:\n    driver: oracle\n    url: x\n    user: u\n    password: hunter22\n"
    assert load_config(write_config(text, mode=0o600)).warnings == ()
    (warning,) = load_config(write_config(text, mode=0o644)).warnings
    assert "chmod 600" in warning
    # Env references aren't plaintext.
    assert load_config(write_config(BASIC, mode=0o644)).warnings == ()


def test_config_must_be_utf8(tmp_path):
    path = tmp_path / "latin1.yaml"
    path.write_bytes("connections:\n  a: {driver: mysql, url: h, user: jos\xe9}\n".encode("latin-1"))
    with pytest.raises(ConfigError, match="is not UTF-8"):
        load_config(path)


def test_expand_env(monkeypatch):
    monkeypatch.setenv("HOST", "db1")
    monkeypatch.setenv("PASS", "s3cret")
    conn = ConnectionConfig(
        label="a", driver="mysql", url="${HOST}:3306/app", user="u", password="${PASS}",
        password_cmd="echo $PASS", options={"ssl_ca": "${HOST}.pem", "n": 1},
    ).expand_env()
    assert (conn.url, conn.password, conn.password_cmd) == ("db1:3306/app", "s3cret", "echo $PASS")
    assert conn.options == {"ssl_ca": "db1.pem", "n": 1}


def test_expand_env_unset(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    conn = ConnectionConfig(label="a", driver="mysql", url="h", password="${NOPE}")
    with pytest.raises(ConfigError, match=r"connections\.a\.password: environment variable NOPE is not set"):
        conn.expand_env()


def _conn(**kwargs) -> ConnectionConfig:
    return ConnectionConfig(label="a", driver="mysql", url="h", **kwargs)


def test_resolve_password_sources(monkeypatch):
    assert resolve_password(_conn(user="u", password="plain-pw")) == "plain-pw"
    assert redact("login plain-pw failed") == "login *** failed"
    assert resolve_password(_conn(user="u", password_cmd="printf 'from-cmd\\nurl: x\\n'")) == "from-cmd"
    assert resolve_password(_conn()) is None
    assert _conn(user="u").auth_source == "prompt"


def test_resolve_password_cmd_failures():
    with pytest.raises(ConfigError, match="password_cmd exited with status 3"):
        resolve_password(_conn(user="u", password_cmd="exit 3"))
    with pytest.raises(ConfigError, match="password_cmd printed no password"):
        resolve_password(_conn(user="u", password_cmd="true"))


def test_resolve_password_without_terminal():
    with pytest.raises(ConfigError, match="no terminal to prompt on"):
        resolve_password(_conn(user="u"))


def test_resolve_password_prompts(monkeypatch):
    from dprobe import config

    monkeypatch.setattr(config.tty, "available", lambda: True)
    monkeypatch.setattr(config.getpass, "getpass", lambda prompt: "typed-pw")
    assert resolve_password(_conn(user="u")) == "typed-pw"


def test_keyring_password(memory_keyring):
    from dprobe.config import store_keyring_password

    conn = _conn(user="u", keyring="dprobe")
    assert conn.auth_source == "keyring"
    with pytest.raises(ConfigError, match="no password in keyring service 'dprobe' for u; store one with: dprobe keyring a"):
        resolve_password(conn)
    store_keyring_password(conn, "kr-secret")
    assert resolve_password(conn) == "kr-secret"
    assert redact("x kr-secret y") == "x *** y"
    store_keyring_password(conn, None)
    with pytest.raises(ConfigError, match="no password stored for u"):
        store_keyring_password(conn, None)


def test_keyring_without_backend(monkeypatch):
    import keyring
    import keyring.backends.fail

    monkeypatch.setattr(keyring, "get_password", keyring.backends.fail.Keyring().get_password)
    with pytest.raises(ConfigError, match="no usable keyring backend"):
        resolve_password(_conn(user="u", keyring="dprobe"))


@pytest.mark.parametrize(
    ("entry", "message"),
    [("driver: mysql\n    url: h/d\n    user: u\n    keyring: s\n    password: p", "set only one of password"),
     ("driver: mysql\n    url: h/d\n    keyring: s", "keyring needs a user")],
)
def test_keyring_config_errors(write_config, entry, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(f"connections:\n  web:\n    {entry}\n"))
