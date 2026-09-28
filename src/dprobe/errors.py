"""Exceptions carrying CLI exit codes, plus masking of secrets in messages."""


class DprobeError(Exception):
    exit_code = 1


class ConfigError(DprobeError):
    """Bad config file, unknown label, or missing credentials."""

    exit_code = 2


class ConnectError(DprobeError):
    """The driver could not connect or log in."""

    exit_code = 3


_secrets: set[str] = set()


def register_secret(value: str) -> None:
    """Mask value in anything passed through redact().

    Values shorter than 4 characters are skipped; masking them would garble
    unrelated text.
    """
    if len(value) >= 4:
        _secrets.add(value)


def redact(text: str) -> str:
    # Longest first, so a secret containing another is masked whole.
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text
