"""Call state follows the APK's message meanings, not receipt acknowledgments."""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def coordinator(media_module, monkeypatch, tmp_path):
    for name in ("paho", "paho.mqtt", "paho.mqtt.client"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["paho"].mqtt = sys.modules["paho.mqtt"]
    sys.modules["paho.mqtt"].client = sys.modules["paho.mqtt.client"]
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda func: func
    config = types.ModuleType("homeassistant.config_entries")
    config.ConfigEntry = object
    monkeypatch.setitem(sys.modules, core.__name__, core)
    monkeypatch.setitem(sys.modules, config.__name__, config)
    path = Path(__file__).parents[1] / "custom_components/aiphone/coordinator.py"
    spec = importlib.util.spec_from_file_location("aiphone_test.coordinator", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    tasks = []

    def create_task(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    hass = types.SimpleNamespace(loop=asyncio.get_running_loop(), async_create_task=create_task,
                                 config=types.SimpleNamespace(path=lambda sub: str(tmp_path / sub)))
    entry = types.SimpleNamespace(data={"cliid":"self", "unit_mac":"unit", "sndtpc":"unit/send", "rcvtpc":"unit/receive",
                                         "cert_pem":"placeholder", "seckey_pem":"placeholder"})
    coord = module.AiphoneCoordinator(hass, entry)
    messages = []
    coord.publish = lambda msg: messages.append(msg) or True
    coord.video.passive_capture = AsyncMock()
    coord.video.async_hang_up = AsyncMock()
    return coord, messages, tasks


@pytest.mark.asyncio
async def test_receipt_is_not_answer_and_duplicate_ring_starts_once(coordinator):
    coord, messages, tasks = coordinator
    ring = {"HEADER":{"MID":"23001", "CID":"ring", "CKIND":1, "CLIID":"door"}}
    coord._dispatch(ring)
    coord._dispatch(ring)
    await asyncio.gather(*tasks)
    coord._dispatch({"HEADER":{"MID":"24000", "CID":"ring", "RSLT":200}})
    assert coord.doorbell_state == "ringing"
    assert [m["HEADER"]["MID"] for m in messages] == ["24000"]
    coord.video.passive_capture.assert_awaited_once()
    coord._cancel_timer()


@pytest.mark.asyncio
async def test_own_answer_is_not_call_end_other_app_answer_releases_only_our_media(coordinator):
    coord, _, tasks = coordinator
    coord._active_cid = coord.video._passive_cid = "ring"
    coord.doorbell_state = "ringing"
    coord._dispatch({"HEADER":{"MID":"24002", "CID":"ring", "CLIID":"self", "RSLT":200}})
    assert not tasks
    assert coord.active_call_id == "ring"
    coord._dispatch({"HEADER":{"MID":"24002", "CID":"ring", "CLIID":"app", "RSLT":200}})
    await asyncio.gather(*tasks)
    coord.video.async_hang_up.assert_awaited_once_with(remote=True)


@pytest.mark.asyncio
async def test_remote_disconnect_is_session_bound_and_own_echo_is_ignored(coordinator):
    coord, messages, tasks = coordinator
    coord.video._mon_cid = "monitor"
    for cid, sender in (("other", "door"), ("monitor", "self")):
        coord._dispatch({"HEADER":{"MID":"23003", "CID":cid, "CLIID":sender}})
    assert not tasks and not messages
    coord._dispatch({"HEADER":{"MID":"23003", "CID":"monitor", "CLIID":"door"}})
    await asyncio.gather(*tasks)
    assert messages[0]["HEADER"]["MID"] == "24003"
    coord.video.async_hang_up.assert_awaited_once_with(remote=True)


@pytest.mark.asyncio
async def test_old_session_cleanup_cannot_clear_new_call(coordinator):
    coord, _, _ = coordinator
    coord._active_cid = "new-ring"
    coord.doorbell_state = "ringing"
    coord.call_ended("old-monitor")
    assert coord.active_call_id == "new-ring"
    assert coord.doorbell_state == "ringing"


@pytest.mark.asyncio
@pytest.mark.parametrize("head, answered, own", [
    ({"NCID": "self"}, False, True),     # app rule: NCID == own client id
    ({"NCID": "app"}, True, False),      # another terminal answered
    ({}, True, True),                    # no NCID: we already sent 24002
    ({}, False, False),                  # no NCID: genuine cancel while ringing
])
async def test_call_cancel_for_our_own_answer_keeps_the_call(coordinator, head, answered, own):
    coord, messages, tasks = coordinator
    coord._active_cid = coord.video._passive_cid = "ring"
    coord.video._answer_requested = answered
    coord._dispatch({"HEADER": {"MID": "23004", "CID": "ring", "CLIID": "door", **head}})
    await asyncio.gather(*tasks)
    if own:
        assert not messages
        coord.video.async_hang_up.assert_not_called()
    else:
        assert messages[0]["HEADER"]["MID"] == "24004"
        coord.video.async_hang_up.assert_awaited_once_with(remote=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mid", ["23003", "23004", "24002"])
async def test_cloud_end_clears_ring_before_media_exists(coordinator, mid):
    coord, _, tasks = coordinator
    coord._active_cid, coord.doorbell_state = "ring", "ringing"
    coord._dispatch({"HEADER":{"MID":mid, "CID":"ring", "CLIID":"other-app", "RSLT":200}})
    assert coord.doorbell_state == "idle"
    assert coord.active_call_id is None
    await asyncio.gather(*tasks)
    coord.video.async_hang_up.assert_awaited_once_with(remote=True)


@pytest.mark.asyncio
async def test_replayed_cloud_ring_cannot_restart_an_ended_call(coordinator):
    coord, messages, tasks = coordinator
    ring = {"HEADER":{"MID":"23001", "CID":"ring", "CLIID":"door"}}
    coord._dispatch(ring)
    await asyncio.gather(*tasks)
    coord.call_ended("ring")
    coord._dispatch(ring)
    assert coord.doorbell_state == "idle"
    assert len(messages) == 1
    coord.video.passive_capture.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_unlock_cannot_publish_even_if_old_entity_is_called(coordinator):
    coord, messages, _ = coordinator
    coord.entry.options = {"enable_unlock":False}
    with pytest.raises(RuntimeError, match="not enabled"):
        coord.async_unlock_door()
    assert not messages


@pytest.mark.asyncio
async def test_quick_reply_background_request_is_coalesced_and_can_be_stopped(coordinator):
    coord, _, _ = coordinator
    coord._client = types.SimpleNamespace(is_connected=lambda: True)
    coord.entry.options = {"enable_reply":True}
    waiting = asyncio.Event()
    async def play():
        waiting.set()
        await asyncio.Future()
    coord.async_play_reply = AsyncMock(side_effect=play)
    coord.start_reply()
    coord.start_reply()
    await waiting.wait()
    assert coord.reply_active
    assert coord.async_play_reply.await_count == 1
    await coord.async_stop_reply()
    assert not coord.reply_active
    assert coord.doorbell_state == "idle"
