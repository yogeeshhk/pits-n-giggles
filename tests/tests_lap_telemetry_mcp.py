"""Bounded trace retrieval, historical discovery, live consistency and MCP wiring."""

import importlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import Client
from jsonschema import validate

from apps.backend.intf_layer.lap_telemetry import handle_lap_telemetry_request
from apps.mcp_server.mcp_server.mcp_server import MCPBridge
from apps.mcp_server.mcp_server.tools.get_lap_telemetry import (
    LAP_TELEMETRY_OUTPUT_SCHEMA, get_lap_telemetry,
)
from apps.web.session_discovery import find_json_files
from lib.lap_telemetry.channels import CHANNELS
from lib.lap_telemetry.query import (
    MAX_CELLS, MAX_RESPONSE_BYTES, SELECTORS, LapTelemetryQuery, query_lap, safe_query_reference,
)
from lib.lap_telemetry.storage import SessionStore

LOGGER = logging.getLogger(__name__)


def sample(index, driver=0, lap=1, epoch=0):
    row = {
        "session_uid": 123, "driver_index": driver, "lap_num": lap, "epoch": epoch,
        "session_time_s": index / 20, "lap_time_ms": index * 50,
        "distance_m": index * 5, "speed_kph": 100 + index % 200,
        "throttle": (index % 10) / 10, "brake": 0.0, "steering": -0.25,
        "gear": 0, "rpm": 12000, "ers_percent": None, "telemetry_public": True,
        "overtake_active": index % 2 == 0, "active_aero_mode": 0,
        "tyre_surface_temp_fl": 82, "tyre_surface_temp_fr": 83,
        "tyre_surface_temp_rl": 80, "tyre_surface_temp_rr": 81,
        "world_position_x": index, "world_position_y": 2, "world_position_z": 3,
    }
    for group in CHANNELS:
        row[f"{group}_age_s"] = 0.05 if group == "status" else 0.0
        row[f"{group}_stale"] = group == "status"
    return row


def recording(tmp_path, count=20, *, closed=True, extra=None):
    reference = {"schema_version": 1, "session_uid": "123", "manifest": "telemetry/123-test/manifest.json"}
    directory = tmp_path / "telemetry" / "123-test"
    store = SessionStore(directory, {"session_uid": "123", "sample_hz": 20}, 100 * 1024**2)
    store.append([sample(index) for index in range(count)] + (extra or []))
    if closed:
        store.close({})
    else:
        store.flush()
    return store, reference


def backend(tmp_path, reference, epoch=0):
    recorder = Obj(root=tmp_path / "telemetry", session_uid=123, error=None,
                   assembler=Obj(epoch=epoch), reference=lambda uid: reference if uid == 123 else None)
    return Obj(m_lap_recorder=recorder), Obj(m_connected_to_sim=True)


def save_session(tmp_path, reference=None):
    path = tmp_path / "Race_Bahrain_2026_09_17_12_00_00.json"
    data = {"session-info": {"track-id": "Bahrain", "session-type": "Race"},
            "classification-data": [], "debug": {"session-uid": 123}}
    if reference is not None:
        data["telemetry-recording"] = reference
    path.write_text(json.dumps(data))
    return path.stem.lower().replace("_", "-")


def test_aligned_arrays_nested_wheels_units_and_nulls(tmp_path):
    store, _ = recording(tmp_path)
    query = LapTelemetryQuery(driver_index=0, lap_num=1, max_samples=3,
                              channels=["speed_kph", "brake", "gear", "overtake_active", "ers_percent",
                                        "tyre_surface_temp", "world_position"])
    result = query_lap(store.directory, query, expected_uid="123")
    validate(result, LAP_TELEMETRY_OUTPUT_SCHEMA)
    assert result["data"]["distance_m"] == [0, 45, 95]
    assert result["data"]["speed_kph"] == [100, 109, 119]
    assert result["data"]["brake"] == [0, 0, 0]
    assert result["data"]["gear"] == [0, 0, 0]
    assert result["data"]["overtake_active"] == [True, False, False]
    assert result["data"]["ers_percent"] == [None] * 3
    assert result["data"]["tyre_surface_temp"]["fl"] == [82] * 3
    assert result["data"]["world_position"]["x"] == [0, 9, 19]
    assert result["units"]["session_time_s"] == "s"
    assert result["units"]["tyre_surface_temp"]["fl"] == "degC"
    assert result["quality"]["missing_channels"] == ["ers_percent"]
    assert result["quality"]["source_groups"]["status"]["unavailable_or_stale_samples"] == 20
    assert result["downsampling"]["source_samples"] == 20
    assert result["downsampling"]["returned_samples"] == 3
    assert result["coverage"]["partial"]


def test_distance_window_precedes_sampling_and_preserves_full_lap_coverage(tmp_path):
    store, _ = recording(tmp_path)
    result = query_lap(store.directory, LapTelemetryQuery(driver_index=0, lap_num=1,
                        start_m=20, end_m=40, max_samples=3, channels=["speed_kph"]), expected_uid="123")
    assert result["data"]["distance_m"] == [20, 30, 40]
    assert result["downsampling"]["source_samples"] == 5
    assert result["coverage"]["end_m"] == 95
    assert result["coverage"]["window_end_m"] == 40


@pytest.mark.parametrize("change", [
    {"driver_index": -1}, {"driver_index": 24}, {"driver_index": True}, {"driver_index": "0"},
    {"lap_num": 0}, {"lap_num": 256}, {"max_samples": 1}, {"max_samples": 2001},
    {"channels": []}, {"channels": ["unknown"]}, {"start_m": float("nan")},
    {"end_m": float("inf")}, {"start_m": 5, "end_m": 2}, {"unexpected": 1},
])
def test_invalid_requests_are_rejected(change):
    with pytest.raises(ValueError):
        LapTelemetryQuery.model_validate({"driver_index": 0, "lap_num": 1, **change})


def test_cell_and_byte_budgets_apply_to_large_channel_selections(tmp_path):
    store, _ = recording(tmp_path, count=3000)
    result = query_lap(store.directory, LapTelemetryQuery(driver_index=0, lap_num=1,
                        max_samples=2000, channels=list(SELECTORS)), expected_uid="123")
    def cells(values):
        return sum(cells(value) if isinstance(value, dict) else len(value) for value in values.values())
    assert result["ok"]
    assert cells(result["data"]) <= MAX_CELLS
    assert len(json.dumps(result).encode()) <= MAX_RESPONSE_BYTES
    assert result["data"]["distance_m"][0] == 0
    assert result["data"]["distance_m"][-1] == 2999 * 5


@pytest.mark.parametrize("driver,lap,start,error", [
    (1, 1, None, "lap_not_recorded"), (0, 2, None, "lap_not_recorded"), (0, 1, 1000, "window_empty"),
])
def test_absent_driver_lap_and_empty_windows(tmp_path, driver, lap, start, error):
    store, _ = recording(tmp_path)
    result = query_lap(store.directory, LapTelemetryQuery(driver_index=driver, lap_num=lap, start_m=start), expected_uid="123")
    assert not result["available"] and result["error"] == error


@pytest.mark.parametrize("path", ["../outside/manifest.json", "telemetry/../other/manifest.json",
                                  "elsewhere/manifest.json", "telemetry/manifest.json", "C:/private/manifest.json"])
def test_saved_references_cannot_escape_telemetry_directory(tmp_path, path):
    query = LapTelemetryQuery(driver_index=0, lap_num=1)
    result = safe_query_reference(tmp_path, {"schema_version": 1, "session_uid": "123", "manifest": path}, query, LOGGER)
    assert result["error"] == "invalid_recording_reference"


async def test_real_saved_lookup_and_internal_manifests_excluded(tmp_path):
    _, reference = recording(tmp_path)
    slug = save_session(tmp_path, reference)
    assert find_json_files(tmp_path) == [Path("Race_Bahrain_2026_09_17_12_00_00.json")]
    dealer = AsyncMock()
    result = await get_lap_telemetry(dealer, LOGGER, tmp_path, "test", driver_index=0, lap_num=1, session_slug=slug)
    assert result["ok"], result
    assert result["source"] == "saved" and result["session_slug"] == slug
    assert result["connected"] is None
    dealer.request.assert_not_called()


async def test_old_saved_session_has_no_fabricated_trace(tmp_path):
    slug = save_session(tmp_path)
    result = await get_lap_telemetry(AsyncMock(), LOGGER, tmp_path, "test", driver_index=0, lap_num=1, session_slug=slug)
    assert result["error"] == "telemetry_not_recorded"


async def test_live_response_and_uncommitted_rewind(tmp_path):
    store, reference = recording(tmp_path, closed=False)
    handler, state = backend(tmp_path, reference)
    query = {"driver_index": 0, "lap_num": 1}
    result = await handle_lap_telemetry_request(handler, state, query, LOGGER)
    assert result["ok"] and result["source"] == "live" and result["connected"]
    handler.m_lap_recorder.assembler.epoch = 1
    pending = await handle_lap_telemetry_request(handler, state, query, LOGGER)
    assert pending["error"] == "telemetry_pending"
    assert pending["data"] is None
    store.rewind({"epoch": 1, "target_time_s": 0.5})
    store.append([sample(10, epoch=1)])
    store.flush()
    result = await handle_lap_telemetry_request(handler, state, query, LOGGER)
    assert result["ok"] and len(result["data"]["distance_m"]) == 11
    assert result["recording"]["epoch"] == 1


@pytest.mark.parametrize("change", ["rewind", "session", "failure"])
async def test_live_changes_during_disk_read_do_not_return_old_data(tmp_path, monkeypatch, change):
    _, reference = recording(tmp_path)
    handler, state = backend(tmp_path, reference)
    module = importlib.import_module("apps.backend.intf_layer.lap_telemetry")
    real_read = module.safe_query_reference
    def changed(*args, **kwargs):
        result = real_read(*args, **kwargs)
        if change == "rewind":
            handler.m_lap_recorder.assembler.epoch += 1
        elif change == "session":
            handler.m_lap_recorder.session_uid = 456
        else:
            handler.m_lap_recorder.error = "disk_failure"
        return result
    monkeypatch.setattr(module, "safe_query_reference", changed)
    result = await handle_lap_telemetry_request(handler, state, {"driver_index": 0, "lap_num": 1}, LOGGER)
    assert result["error"] == ("recording_failed" if change == "failure" else "recording_changed")
    assert result["data"] is None


async def test_disabled_recording_and_backend_timeout(tmp_path):
    result = await handle_lap_telemetry_request(Obj(m_lap_recorder=None), Obj(), {"driver_index": 0, "lap_num": 1}, LOGGER)
    assert result["error"] == "recording_disabled"
    dealer = AsyncMock()
    dealer.request.return_value = {"status": "error", "reason": "request timeout"}
    result = await get_lap_telemetry(dealer, LOGGER, tmp_path, "test", driver_index=0, lap_num=1)
    assert result["error"] == "core_server_timeout"


async def test_fastmcp_registration_validation_and_end_to_end_call(tmp_path):
    _, reference = recording(tmp_path)
    handler, state = backend(tmp_path, reference)
    dealer = AsyncMock()
    async def request(destination, topic, data):
        assert topic == "lap-telemetry-request"
        return await handle_lap_telemetry_request(handler, state, data, LOGGER)
    dealer.request.side_effect = request
    bridge = MCPBridge(dealer, LOGGER, "test", tmp_path)
    async with Client(bridge.mcp) as client:
        tools = await client.list_tools()
        tool = next(tool for tool in tools if tool.name == "get_lap_telemetry")
        assert tool.annotations.readOnlyHint
        assert tool.inputSchema["properties"]["driver_index"]["maximum"] == 23
        result = await client.call_tool("get_lap_telemetry", {"driver_index": 0, "lap_num": 1, "max_samples": 3})
        assert result.structured_content["ok"]
        assert result.structured_content["downsampling"]["returned_samples"] == 3
        invalid = await client.call_tool("get_lap_telemetry", {"driver_index": 24, "lap_num": 1}, raise_on_error=False)
        assert invalid.is_error



async def test_backend_ipc_route_is_registered(tmp_path, monkeypatch):
    module = importlib.import_module("apps.backend.intf_layer.telemetry_ui_tasks")
    class Dealer:
        def __init__(self, **_kwargs):
            self.routes = {}
        def route(self, name):
            def register(function):
                self.routes[name] = function
                return function
            return register
    monkeypatch.setattr(module, "IpcDealerAsync", Dealer)
    _, reference = recording(tmp_path)
    handler, state = backend(tmp_path, reference)
    settings = Obj(Network=Obj(broker_router_port=1))
    dealer = module._initDealer(settings, LOGGER, state, handler)
    result = await dealer.routes["lap-telemetry-request"]({"driver_index": 0, "lap_num": 1}, "mcp")
    assert result["ok"]
    assert result["downsampling"]["source_samples"] == 20


async def test_saved_reference_reload_and_identity_check(tmp_path):
    _, reference = recording(tmp_path)
    slug = save_session(tmp_path, reference)
    arguments = dict(driver_index=0, lap_num=1, session_slug=slug)
    first = await get_lap_telemetry(AsyncMock(), LOGGER, tmp_path, "test", **arguments)
    assert first["ok"]
    # The filename is unchanged, but the newly saved reference must be re-read.
    save_session(tmp_path, {**reference, "session_uid": "456"})
    second = await get_lap_telemetry(AsyncMock(), LOGGER, tmp_path, "test", **arguments)
    assert second["error"] == "recording_session_mismatch"


async def test_saved_fastmcp_call_needs_no_live_backend(tmp_path):
    _, reference = recording(tmp_path)
    slug = save_session(tmp_path, reference)
    dealer = AsyncMock()
    bridge = MCPBridge(dealer, LOGGER, "test", tmp_path)
    async with Client(bridge.mcp) as client:
        result = await client.call_tool("get_lap_telemetry", {
            "driver_index": 0, "lap_num": 1, "session_slug": slug, "channels": ["speed_kph"],
        })
        assert result.structured_content["ok"]
        assert result.structured_content["source"] == "saved"
        assert result.structured_content["downsampling"]["method"] == "none"
    dealer.request.assert_not_called()


def test_missing_failed_mismatched_and_excessive_recordings(tmp_path, monkeypatch):
    module = importlib.import_module("lib.lap_telemetry.query")
    store, reference = recording(tmp_path)
    query = LapTelemetryQuery(driver_index=0, lap_num=1)
    assert query_lap(store.directory, query, expected_uid="456")["error"] == "recording_session_mismatch"
    monkeypatch.setattr(module, "MAX_READ_ROWS", 10)
    assert safe_query_reference(tmp_path, reference, query, LOGGER)["error"] == "recording_unreadable"
    store.close({}, error="test failure")
    assert safe_query_reference(tmp_path, reference, query, LOGGER)["error"] == "recording_unreadable"
    missing = {**reference, "manifest": "telemetry/missing/manifest.json"}
    assert safe_query_reference(tmp_path, missing, query, LOGGER)["error"] == "recording_unavailable"


def test_complete_and_partial_coverage(tmp_path):
    store, _ = recording(tmp_path, extra=[sample(20, lap=2)])
    query = LapTelemetryQuery(driver_index=0, lap_num=1)
    assert query_lap(store.directory, query, expected_uid="123")["coverage"]["partial"] is False
    store.manifest["counters"] = {"queue_dropped_samples": 1}
    store.checkpoint()
    assert query_lap(store.directory, query, expected_uid="123")["coverage"]["partial"] is True


def test_atomic_byte_budget_can_reduce_requested_sample_count(tmp_path, monkeypatch):
    module = importlib.import_module("lib.lap_telemetry.query")
    store, _ = recording(tmp_path, count=1000)
    # Force the byte limiter to act independently of the cell limiter.
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 6000)
    query = LapTelemetryQuery(driver_index=0, lap_num=1, max_samples=1000, channels=["speed_kph"])
    result = query_lap(store.directory, query, expected_uid="123")
    assert result["ok"]
    assert result["downsampling"]["returned_samples"] < 1000
    assert len(json.dumps(result, separators=(",", ":")).encode()) < 6000
    assert result["data"]["distance_m"][-1] == 4995
