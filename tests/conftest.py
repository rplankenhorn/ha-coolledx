import importlib.util
from pathlib import Path

import pytest

_DEVICE_PATH = Path(__file__).resolve().parent.parent / "custom_components" / "coolledx" / "device.py"


@pytest.fixture(scope="session")
def device_module():
    spec = importlib.util.spec_from_file_location("coolledx_device", _DEVICE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
