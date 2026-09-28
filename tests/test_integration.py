"""Real connections. Set DPROBE_IT_CONFIG to a config with labels it-oracle,
it-mssql and/or it-mysql; missing labels are skipped."""

import os
from pathlib import Path

import pytest

from dprobe.config import DRIVERS, load_config
from dprobe.connectors import create_connector

# Resolved at import: the autouse fixture changes the working directory.
IT_CONFIG = Path(os.environ["DPROBE_IT_CONFIG"]).resolve() if os.environ.get("DPROBE_IT_CONFIG") else None

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("driver", DRIVERS)
def test_ping(driver):
    if IT_CONFIG is None:
        pytest.skip("DPROBE_IT_CONFIG not set")
    config = load_config(IT_CONFIG)
    label = f"it-{driver}"
    if label not in config.connections:
        pytest.skip(f"{label} not in {IT_CONFIG}")
    with create_connector(config.get(label)) as db:
        assert db.server_version()
