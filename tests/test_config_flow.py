"""Exercise pairing with Home Assistant's Future-returning executor contract."""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


class FlowHarness:
    """Only the synchronous flow-result helpers needed by the pairing steps."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    def async_show_progress(self, **kwargs):
        return {"type": "progress", **kwargs}

    def async_show_progress_done(self, **kwargs):
        return {"type": "progress_done", **kwargs}

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}


class HassHarness:
    """Match HA: executor submission returns a Future, task creation needs a coroutine."""

    def __init__(self):
        self.jobs = []

    def async_add_executor_job(self, target, *args):
        future = asyncio.get_running_loop().create_future()
        self.jobs.append((target, args, future))
        return future

    def async_create_task(self, coroutine):
        return asyncio.create_task(coroutine)


@pytest.fixture
def flow_module(monkeypatch):
    """Import the real flow without starting HA or any network workers."""
    homeassistant = types.ModuleType("homeassistant")
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigFlow = FlowHarness
    config_entries.OptionsFlow = FlowHarness
    core = types.ModuleType("homeassistant.core")
    core.callback = lambda function: function
    data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict
    homeassistant.config_entries = config_entries
    for module in (homeassistant, config_entries, core, data_entry_flow):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    path = Path(__file__).parents[1] / "custom_components/aiphone/config_flow.py"
    spec = importlib.util.spec_from_file_location("aiphone_test.config_flow", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_pairing_progress_reuses_one_executor_job(flow_module):
    flow = flow_module.AiphoneConfigFlow()
    flow.hass = HassHarness()

    progress = await flow.async_step_user({"termname": " Hall "})
    task = progress["progress_task"]
    try:
        assert progress["type"] == "progress"
        assert isinstance(task, asyncio.Task)
        await asyncio.sleep(0)
        assert len(flow.hass.jobs) == 1
        target, args, future = flow.hass.jobs[0]
        assert target is flow_module.pair_aiphone
        assert args == ("Hall",)
        assert (await flow.async_step_pair())["progress_task"] is task
        assert len(flow.hass.jobs) == 1

        result = {"unit_mac": "test-unit", "cliid": "test-client"}
        future.set_result(result)
        await task
        done = await flow.async_step_pair()
        assert done == {"type": "progress_done", "next_step_id": "finish"}
        assert flow._result == result
        assert flow._task is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_discovery_timeout_becomes_flow_error_and_allows_retry(flow_module):
    flow = flow_module.AiphoneConfigFlow()
    flow.hass = HassHarness()
    progress = await flow.async_step_pair()
    task = progress["progress_task"]
    try:
        await asyncio.sleep(0)
        future = flow.hass.jobs[0][2]
        future.set_exception(flow_module.WPNotInPairingMode("No discovery reply"))
        await asyncio.sleep(0)
        assert task.done()

        done = await flow.async_step_pair()
        assert done == {"type": "progress_done", "next_step_id": "failed"}
        form = await flow.async_step_failed()
        assert form["errors"] == {"base": "wp_not_in_pairing_mode"}
        assert flow._task is None

        retry = await flow.async_step_failed({})
        assert retry["step_id"] == "user"
        assert flow._error_key is None
        assert flow._result is None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
