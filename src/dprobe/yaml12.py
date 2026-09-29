"""YAML with the YAML 1.2 core schema's rules for unquoted values, on top of PyYAML.

PyYAML follows YAML 1.1, where on/off/yes/no are booleans, 01234 is octal,
1:30 is base 60 and 2024-01-02 is a date. Under the 1.2 core schema, a
superset of JSON, only true/false are booleans, integers are decimal (0o
octal, 0x hex), and dates and times stay strings. Merge keys (<<) still work.
"""

import re
from typing import Any

import yaml

# Callers catch this rather than importing PyYAML, whose safe_load parses YAML 1.1.
YAMLError = yaml.YAMLError


class Loader(yaml.SafeLoader):
    yaml_implicit_resolvers = {}


# Tried in this order, so 12 resolves as an int before float can match it.
for tag, pattern in {
    "bool": r"true|True|TRUE|false|False|FALSE",
    "null": r"~|null|Null|NULL|",
    "int": r"[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+",
    "float": r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?|[-+]?\.(inf|Inf|INF)|\.(nan|NaN|NAN)",
    "merge": r"<<",
}.items():
    Loader.add_implicit_resolver(f"tag:yaml.org,2002:{tag}", re.compile(rf"(?:{pattern})$"), None)


def _int(loader: Loader, node: yaml.ScalarNode) -> int:
    # PyYAML's own constructor reads a leading 0 as octal; int(text, 0) rejects it.
    text = loader.construct_scalar(node)
    return int(text, 0) if text.startswith(("0o", "0x")) else int(text)


# Floats keep SafeLoader's constructor; the pattern already rules out its 1.1 extras (_, base 60).
Loader.add_constructor("tag:yaml.org,2002:int", _int)


def load(text: str) -> Any:
    """Parse YAML like yaml.safe_load, but with YAML 1.2 core-schema values."""
    return yaml.load(text, Loader=Loader)
