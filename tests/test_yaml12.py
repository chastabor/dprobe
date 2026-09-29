import math

import pytest

from dprobe import yaml12


@pytest.mark.parametrize(
    ("text", "value"),
    [
        # Only true/false are booleans; YAML 1.1 also took on/off/yes/no/y/n.
        ("true", True), ("False", False), ("TRUE", True),
        ("on", "on"), ("off", "off"), ("yes", "yes"), ("No", "No"), ("y", "y"),
        ("~", None), ("null", None), ("NULL", None), ("", None),
        # Leading zeros are decimal (1.1 read 01234 as octal 668); 0o and 0x prefix other bases.
        ("12", 12), ("-7", -7), ("+3", 3), ("01234", 1234), ("0o17", 15), ("0x1F", 31),
        ("1_000", "1_000"),
        ("1.5", 1.5), (".5", 0.5), ("1e3", 1000.0), ("-2.5E-2", -0.025), (".inf", math.inf), ("-.Inf", -math.inf),
        # Dates, times and base-60 numbers stay text, as in JSON.
        ("2024-01-02", "2024-01-02"), ("2024-01-02 03:04:05", "2024-01-02 03:04:05"), ("1:30", "1:30"),
        ("7.4.1", "7.4.1"), ("=", "="), ("'off'", "off"), ("!!str 12", "12"),
    ],
)
def test_scalars(text, value):
    assert yaml12.load(f"v: {text}")["v"] == value


def test_nan_and_merge_keys():
    assert math.isnan(yaml12.load("v: .nan")["v"])
    doc = "base: &base {a: 1, b: 2}\nmerged: {<<: *base, b: 3}\n"
    assert yaml12.load(doc)["merged"] == {"a": 1, "b": 3}
