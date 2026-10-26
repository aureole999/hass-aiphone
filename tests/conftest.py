"""Load the media modules without booting Home Assistant or installing its shim."""

import sys
import types
from pathlib import Path

package = types.ModuleType("aiphone_test")
package.__path__ = [str(Path(__file__).parents[1] / "custom_components" / "aiphone")]
sys.modules[package.__name__] = package


import importlib.util
import pytest


@pytest.fixture
def media_module(monkeypatch):
    """Stub only HA host plumbing and the auto-install shim, not the media code."""
    deps = types.ModuleType("aiphone_test._deps")
    deps.ensure_runtime_deps = lambda: None
    dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    dispatcher.async_dispatcher_send = lambda *args: None
    monkeypatch.setitem(sys.modules, deps.__name__, deps)
    monkeypatch.setitem(sys.modules, dispatcher.__name__, dispatcher)
    path = Path(__file__).parents[1] / "custom_components/aiphone/media.py"
    spec = importlib.util.spec_from_file_location("aiphone_test.media", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module
