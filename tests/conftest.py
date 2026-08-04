"""Test fixtures.

api.py is imported directly from its file so the protocol tests run with
plain pytest — no Home Assistant installation required.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_API_PATH = (
    Path(__file__).resolve().parents[1] / "custom_components/revox_studioart/api.py"
)


def _load_api():
    spec = importlib.util.spec_from_file_location("revox_api", _API_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["revox_api"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def api():
    """The api.py module, loaded without the surrounding HA package."""
    return _load_api()


_CLI_PATH = Path(__file__).resolve().parents[1] / "tools/revox_cli.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("revox_cli", _CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["revox_cli"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def cli():
    """The standalone tools/revox_cli.py module."""
    return _load_cli()
