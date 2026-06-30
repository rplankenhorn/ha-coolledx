import importlib.util
import json
from pathlib import Path

import pytest

_DEVICE_PATH = Path(__file__).resolve().parent.parent / "custom_components" / "coolledx" / "device.py"
_UX_PROTOCOL_PATH = Path(__file__).resolve().parent.parent / "custom_components" / "coolledx" / "ux_protocol.py"
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def device_module():
    spec = importlib.util.spec_from_file_location("coolledx_device", _DEVICE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def ux_module():
    spec = importlib.util.spec_from_file_location("coolledx_ux_protocol", _UX_PROTOCOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_fixture(name: str):
    """Load a JSON golden-vector fixture from tests/fixtures/ by filename."""
    with open(_FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)
