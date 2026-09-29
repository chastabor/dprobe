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
        # YAML 1.2 reads yes as a string.
        ("driver: mysql\n    url: h/d\n    readonly: yes", r"readonly: must be true or false"),
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


def test_plaintext_password_in_a_readable_file(write_config):
    # Some setups keep passwords in the file; that's allowed, whatever the file's permissions.
    text = "connections:\n  a:\n    driver: oracle\n    url: x\n    user: u\n    password: hunter22\n"
    conn = load_config(write_config(text, mode=0o644)).get("a")
    assert (conn.auth_source, resolve_password(conn.expand_env())) == ("password", "hunter22")


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


OVERRIDDEN = """
    defaults: {max_rows: 50}
    connections:
      web:
        driver: mysql
        url: web-db:3306/webapp
        user: app_ro
        password_cmd: secret-tool lookup db web
        options: {ssl_disabled: false, connection_timeout: 5}
      old:
        driver: oracle
        url: db1:1521/ORCL
"""


def test_override_file_is_merged(write_config):
    path = write_config(OVERRIDDEN)
    override = write_config("""
        connections:
          web:
            password_cmd: null
            password: from-override
            options: {connection_timeout: 30}
          old: null
          new: {driver: mssql, url: sql1/db}
    """, name="dprobe.override.yaml")
    config = load_config(path)
    web = config.get("web")
    assert (web.password, web.password_cmd, web.user) == ("from-override", None, "app_ro")
    # Mappings merge key by key; null removes a key, here a whole connection.
    assert web.options == {"ssl_disabled": False, "connection_timeout": 30}
    assert list(config.connections) == ["web", "new"]
    assert (config.defaults.max_rows, config.override) == (50, override)


def test_override_follows_the_config_file_name(write_config):
    from dprobe.config import override_path

    assert override_path(Path("tests/it.example.yaml")) == Path("tests/it.example.override.yaml")
    assert override_path(Path("config.yml")) == Path("config.override.yml")
    path = write_config(OVERRIDDEN, name="it.example.yaml")
    write_config("connections:\n  web:\n    user: someone\n", name="it.example.override.yaml")
    assert load_config(path).get("web").user == "someone"
    # Without an override file, the config loads as it is.
    assert load_config(write_config(OVERRIDDEN, name="plain.yaml")).override is None


def test_override_errors_name_both_files(write_config):
    path = write_config(OVERRIDDEN)
    write_config("connections:\n  web:\n    password: secret\n", name="dprobe.override.yaml")
    # The base's password_cmd and the override's password clash after merging.
    with pytest.raises(ConfigError, match=r"dprobe\.yaml with .*dprobe\.override\.yaml: connections\.web: set only one"):
        load_config(path)
    write_config("- not a mapping\n", name="dprobe.override.yaml")
    with pytest.raises(ConfigError, match=r"dprobe\.override\.yaml: expected a mapping"):
        load_config(path)
    write_config("connections: [\n", name="dprobe.override.yaml")
    with pytest.raises(ConfigError, match=r"dprobe\.override\.yaml: invalid YAML"):
        load_config(path)
    write_config("", name="dprobe.override.yaml")
    assert load_config(path).get("web").password_cmd == "secret-tool lookup db web"


def test_merge():
    from dprobe.config import merge

    assert merge({"a": {"b": 1, "c": 2}, "d": [1]}, {"a": {"c": 3, "e": 4}, "d": [2]}) == {
        "a": {"b": 1, "c": 3, "e": 4}, "d": [2]}
    assert merge({"a": 1, "b": 2}, {"a": None}) == {"b": 2}
    assert merge({"a": {"b": 1}}, {"a": "x"}) == {"a": "x"}


def test_yaml_12_values(write_config):
    path = write_config("""
        connections:
          sis:
            driver: mssql
            url: h/d
            options: {encryption: off, tds_version: 7.4}
    """)
    assert load_config(path).get("sis").options == {"encryption": "off", "tds_version": 7.4}
