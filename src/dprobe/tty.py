"""Questions asked on the terminal.

These use /dev/tty rather than stdin, so they work while the SQL arrives on a pipe.
"""


def available() -> bool:
    try:
        with open("/dev/tty"):
            return True
    except OSError:
        return False


def ask(prompt: str) -> str:
    """One line typed at the terminal, without its newline. EOFError on Ctrl-D."""
    with open("/dev/tty", "r+", encoding="utf-8") as tty:
        tty.write(prompt)
        tty.flush()
        line = tty.readline()
    if not line:
        raise EOFError
    return line.removesuffix("\n")
