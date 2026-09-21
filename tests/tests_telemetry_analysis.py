"""Known-answer coaching, history persistence, privacy and MCP integration."""

import json
import logging
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from apps.backend.intf_layer.lap_telemetry import handle_lap_telemetry_request
from apps.mcp_server.mcp_server.mcp_server import MCPBridge
from lib.f1_types import F1PacketType as P
from lib.lap_telemetry.analysis import AnalysisQuery, Trace, analyze_reference, compare, corner_analysis
from lib.lap_telemetry.history import HistoryCollector, read_history
from lib.lap_telemetry.query import LapTelemetryQuery, query_lap
from lib.lap_telemetry.recorder import TelemetryRecorder
from lib.lap_telemetry.storage import SessionStore, read_lap
from tests.tests_lap_telemetry import assemble, flashback, frame_packets, packet, participants, rows
from tests.tests_lap_telemetry_mcp import backend, sample, save_session

LOGGER = logging.getLogger(__name__)


def fixture_recording(tmp_path, laps=7):
    reference = {"schema_version": 1, "session_uid": "123", "manifest": "telemetry/123-analysis/manifest.json"}
    store = SessionStore(tmp_path / "telemetry" / "123-analysis", {"session_uid": "123", "sample_hz": 20}, 100_000_000)
    data, history = [], []
    for lap in range(1, laps + 1):
        for i in range(200):
            row = sample(i, lap=lap)
            row.update(session_time_s=(lap - 1) * 10 + i / 20, lap_time_ms=i * 50,
                       distance_m=i * 5, track_id=-1, steering=0.2 if 20 <= i <= 60 else 0,
                       brake=0.5 if 18 <= i <= 30 else 0, throttle=0.4 if 20 <= i <= 50 else 1,
                       speed_kph=100 + abs(i - 40), tyre_compound=16, tyre_age_laps=lap - 1,
                       num_pit_stops=0, pit_status=0, pit_lane_active=False, lap_invalid=False,
                       fia_flag=0, safety_car_status=0, gap_front_ms=1500, gap_leader_ms=2500,
                       sector=i // 70, ers_percent=50, ers_deployed_j=i * 1000)
            data.append(row)
        history.append(history_row("lap_timing", lap * 10, {
            "lap-time-in-ms": 10000, "sector-1-time-in-ms": 3000, "sector-2-time-in-ms": 3000,
            "sector-3-time-in-ms": 4000, "lap-valid-bit-flags": 15}, lap=lap))
    store.append(data)
    store.append_history(history)
    store.close({})
    return store, reference


def history_row(kind, time, data, driver=0, lap=0, epoch=0):
    return dict(kind=kind, session_time_s=time, driver_index=driver, lap_num=lap, epoch=epoch,
                frame_id=round(time * 60), payload=json.dumps(data))


def analyze(tmp_path, reference, operation, **kwargs):
    return analyze_reference(tmp_path, reference, AnalysisQuery(operation=operation, driver_index=0, **kwargs), LOGGER)


def trace(factor=1, offset=0):
    data = []
    for i in range(201):
        row = sample(i)
        row.update(distance_m=i * 5, lap_time_ms=i * 50 * factor + offset,
                   steering=0.2 if 30 <= i <= 60 else 0)
        data.append(row)
    return Trace({"rows": data, "recording": {"sample_hz": 20}, "coverage": {"partial": False}})


def test_comparison_known_delta_sign_and_partition_reconciliation():
    result = compare(trace(), trace(1.1, 300), 17)
    assert result["delta_at_start_s"] == pytest.approx(0.3)
    assert result["delta_at_end_s"] == pytest.approx(1.3)
    assert result["covered_time_delta_s"] == pytest.approx(1)
    assert sum(s["time_delta_s"] for s in result["segments"]) == pytest.approx(1)
    assert result["distance_m"][0] == 0 and result["distance_m"][-1] == 1000
    assert len(result["elapsed_delta_s"]) == 17
    assert compare(trace(1.1), trace(), 3)["covered_time_delta_s"] == pytest.approx(-1)


def test_comparison_rejects_gaps_reverse_distance_and_disjoint_laps():
    broken = trace()
    broken.rows[100]["session_time_s"] += 1
    assert compare(trace(), broken, 10) is None
    result = trace().result
    result["rows"][100]["distance_m"] = 5
    with pytest.raises(ValueError, match="Nonmonotonic"):
        Trace(result)
    other = trace()
    for row in other.result["rows"]:
        row["distance_m"] += 2000
    assert compare(trace(), Trace(other.result), 5) is None


def test_corner_detection_and_missing_slip_are_not_zero_evidence():
    result = corner_analysis(trace())
    assert len(result["corners"]) == 1
    corner = result["corners"][0]
    assert corner["name"] == "Detected corner 1"
    assert corner["source"] == "steering_or_lateral_g_heuristic"
    assert corner["metrics"]["wheelspin_candidates"]["observed_samples"] == 0
    assert corner["metrics"]["mean_tyre_inner_temp"]["fl"] is None


def test_track_map_uses_real_corner_names():
    t = trace()
    for row in t.rows:
        row["track_id"] = 3  # Bahrain
    result = corner_analysis(t)
    assert result["corners"]
    assert all(c["source"] == "track_map" for c in result["corners"])


def test_minute_gaps_player_only_motion_and_restricted_wear():
    packets = frame_packets(count=2)
    packets[0].m_lapData[0].m_deltaToRaceLeaderInMS = 1234
    packets[0].m_lapData[0].m_deltaToRaceLeaderMinutes = 2
    extra = packet(P.MOTION_EX, m_wheelSpeed=[10, 11, 12, 13], m_wheelSlipRatio=[0.3, 0, 0, 0], m_wheelSlipAngle=[0] * 4)
    damage = packet(P.CAR_DAMAGE, m_carDamageData=[Obj(m_tyresWear=[1, 2, 3, 4])] * 2)
    _, events = assemble([participants(count=2), *packets, extra, damage])
    result = rows(events)
    assert result[0]["gap_leader_ms"] == 121234
    assert result[0]["wheel_speed_fl"] == 12
    assert result[1]["wheel_speed_fl"] is None
    assert result[1]["motion_ex_stale"]
    _, restricted = assemble([*packets, damage])
    assert rows(restricted)[0]["tyre_wear_fl"] is None


def test_history_deduplicates_privacy_and_zero_based_laps():
    emitted = []
    collector = HistoryCollector(lambda op, data: emitted.extend(data))
    setup = packet(P.CAR_SETUPS, m_carSetups=[Obj(toJSON=lambda: {"front-wing": 30})] * 2)
    collector.feed(setup, 0, {0: True, 1: False})
    collector.feed(setup, 0, {0: True, 1: False})
    assert len(emitted) == 1 and emitted[0]["driver_index"] == 0
    collector.feed(packet(P.LAP_POSITIONS, m_lapStart=0, m_lapPositions=[[2, 1], [1, 2]]), 0, {})
    assert [r["lap_num"] for r in emitted[1:]] == [1, 1, 2, 2]
    collector.clear()
    collector.feed(setup, 1, {0: True})
    assert emitted[-1]["epoch"] == 1


async def test_history_recorder_round_trip_and_rewind(tmp_path):
    recorder = TelemetryRecorder(tmp_path / "telemetry", LOGGER)
    recorder.observe(participants())
    for p in frame_packets():
        recorder.observe(p)
    recorder.observe(packet(P.CAR_SETUPS, m_carSetups=[Obj(toJSON=lambda: {"front-wing": 20})]))
    recorder.observe(packet(P.CAR_SETUPS, frame=2, time=1, m_carSetups=[Obj(toJSON=lambda: {"front-wing": 25})]))
    recorder.observe(flashback(3, 1.1, 0.5))
    recorder.observe(packet(P.CAR_SETUPS, frame=4, time=0.6, m_carSetups=[Obj(toJSON=lambda: {"front-wing": 30})]))
    await recorder.stop()
    assert recorder.error is None
    directory = (tmp_path / recorder.reference(123)["manifest"]).parent
    manifest = json.loads((directory / "manifest.json").read_text())
    history = read_history(directory, manifest, 0)
    assert [r["data"]["front-wing"] for r in history] == [20, 30]
    assert manifest["history_chunks"]
    assert sum(p.stat().st_size for p in directory.iterdir()) <= recorder.disk_limit_bytes


def test_old_parquet_columns_are_null_filled(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    store, _ = fixture_recording(tmp_path, laps=1)
    path = store.directory / store.manifest["chunks"][0]["file"]
    table = pq.read_table(path)
    columns = [name for name in table.column_names if not name.startswith(("wheel_", "motion_ex_"))]
    pq.write_table(pa.table({name: table[name] for name in columns}), path)
    result = query_lap(store.directory, LapTelemetryQuery(driver_index=0, lap_num=1, channels=["wheel_speed"]), expected_uid="123")
    assert result["ok"]
    assert result["data"]["wheel_speed"]["fl"] == [None] * 200


def test_race_pace_uses_complete_green_laps_and_explicit_exclusions(tmp_path):
    store, reference = fixture_recording(tmp_path)
    result = analyze(tmp_path, reference, "get_race_pace_breakdown")
    assert result["ok"]
    assert result["data"]["eligible_samples"] == 5
    assert result["data"]["mean_race_pace_s"] == 10
    assert "start_lap" in result["data"]["laps"][0]["excluded_reasons"]
    assert "incomplete" in result["data"]["laps"][-1]["excluded_reasons"]
    # An incomplete sector fragment cannot anchor pace even with valid bit flags.
    store.manifest["state"] = "recording"
    store.append_history([history_row("lap_timing", 100, {"lap-time-in-ms": 3000,
                            "sector-1-time-in-ms": 3000, "lap-valid-bit-flags": 15}, lap=2)])
    store.close({})
    result = analyze(tmp_path, reference, "get_race_pace_breakdown")
    assert "incomplete" in result["data"]["laps"][1]["excluded_reasons"]


def test_representative_reference_and_bounded_energy_gap_history(tmp_path):
    _, reference = fixture_recording(tmp_path)
    result = analyze(tmp_path, reference, "get_time_loss_analysis", lap_num=6)
    assert result["ok"] and result["data"]["reference_selection"] == "same_stint_median"
    assert result["data"]["covered_time_delta_s"] == 0
    result = analyze(tmp_path, reference, "get_driver_telemetry_history", history_kind="lap_summary", limit=1)
    assert result["ok"] and result["data"]["next_offset"] == 1
    lap = result["data"]["records"][0]
    assert lap["sector_entry_observations"][0]["gap_leader_ms"] == 2500
    assert lap["ers"]["ers_deployed_j"]["max"] == 199000


def test_history_pagination_and_collision_relevance(tmp_path):
    store, reference = fixture_recording(tmp_path, laps=1)
    store.manifest["state"] = "recording"
    store.append_history([history_row("event", 15, {"event-string-code": "COLL", "event-details": {
        "vehicle-1-index": 0, "vehicle-2-index": 1, "severity": "Heavy"}}, driver=-1),
        history_row("event", 16, {"event-string-code": "COLL", "event-details": {
            "vehicle-1-index": 2, "vehicle-2-index": 3, "severity": "Light"}}, driver=-1)])
    store.close({})
    result = analyze(tmp_path, reference, "get_driver_telemetry_history", history_kind="event")
    assert len(result["data"]["records"]) == 1
    assert result["data"]["records"][0]["data"]["event-details"]["severity"] == "Heavy"


def test_pit_entry_stationary_exit_and_loss_estimate(tmp_path):
    store, reference = fixture_recording(tmp_path, laps=2)
    import pyarrow as pa
    import pyarrow.parquet as pq
    path = store.directory / store.manifest["chunks"][0]["file"]
    data = pq.read_table(path).to_pylist()
    for row in data:
        if row["lap_num"] == 2 and 100 <= row["distance_m"] < 300:
            row.update(pit_lane_active=True, pit_limiter=True, pit_lane_time_ms=(row["distance_m"] - 100) * 10,
                       pit_stop_time_ms=500)
    pq.write_table(pa.Table.from_pylist(data, schema=store.schema), path)
    result = analyze(tmp_path, reference, "get_pit_timing_analysis", reference_lap=1)
    stop = result["data"]["stops"][0]
    assert not stop["partial"]
    assert stop["entry_session_time_s"] == 11
    assert stop["exit_session_time_s"] == 13
    assert stop["observed_stationary_time_s"] == 0.5
    assert stop["estimated_total_pit_loss_s"] == 0


def test_launch_keeps_negative_grid_distance_and_reaction_scope(tmp_path):
    store, reference = fixture_recording(tmp_path, laps=1)
    import pyarrow as pa
    import pyarrow.parquet as pq
    path = store.directory / store.manifest["chunks"][0]["file"]
    data = pq.read_table(path).to_pylist()
    for row in data:
        row["distance_m"] -= 50
        row["speed_kph"] = 0 if row["session_time_s"] < 0.2 else 10
    pq.write_table(pa.Table.from_pylist(data, schema=store.schema), path)
    store.manifest["state"] = "recording"
    store.append_history([history_row("event", 0, {"event-string-code": "LGOT"}, driver=-1),
                          history_row("session", 1, {"player_index": 0, "reaction_time_s": 0.17}, driver=-1)])
    store.close({})
    result = analyze(tmp_path, reference, "get_start_analysis")
    assert result["data"]["game_reaction_time_s"] == 0.17
    assert result["data"]["observed_time_to_5kph_s"] == pytest.approx(0.2)
    assert result["data"]["launch_trace"][0]["session_time_s"] == 0


@pytest.mark.parametrize("operation,args", [
    ("compare_laps", {"reference_lap": 1, "comparison_lap": 2}),
    ("get_corner_analysis", {"lap_num": 1}),
    ("get_time_loss_analysis", {"lap_num": 2, "reference_lap": 1}),
    ("get_race_pace_breakdown", {}),
    ("get_driver_telemetry_history", {}),
    ("get_pit_timing_analysis", {}),
    ("get_start_analysis", {}),
])
@pytest.mark.parametrize("saved", [False, True])
async def test_all_tools_through_real_mcp(tmp_path, operation, args, saved):
    _, reference = fixture_recording(tmp_path)
    handler, state = backend(tmp_path, reference)
    dealer = AsyncMock()
    async def request(_destination, topic, data):
        assert topic == "telemetry-analysis-request"
        return await handle_lap_telemetry_request(handler, state, data, LOGGER, analysis=True)
    dealer.request.side_effect = request
    bridge = MCPBridge(dealer, LOGGER, "test", tmp_path)
    if saved:
        args = {**args, "session_slug": save_session(tmp_path, reference)}
    async with Client(bridge.mcp) as client:
        result = await client.call_tool(operation, {"driver_index": 0, **args})
        assert result.structured_content["ok"], result.structured_content
        assert result.structured_content["source"] == ("saved" if saved else "live")
        bad = await client.call_tool(operation, {"driver_index": 24, **args}, raise_on_error=False)
        assert bad.is_error
    if saved:
        dealer.request.assert_not_called()


def test_pending_rewind_identity_and_path_safety(tmp_path):
    _, reference = fixture_recording(tmp_path)
    query = AnalysisQuery(operation="get_corner_analysis", driver_index=0, lap_num=1)
    assert analyze_reference(tmp_path, reference, query, LOGGER, expected_epoch=1)["error"] == "telemetry_pending"
    assert analyze_reference(tmp_path, {**reference, "session_uid": "999"}, query, LOGGER)["error"] == "recording_session_mismatch"
    assert not analyze_reference(tmp_path, {**reference, "manifest": "../manifest.json"}, query, LOGGER)["ok"]


async def test_new_binary_packets_reach_backend_recorder(tmp_path):
    from unittest.mock import Mock
    from apps.backend.telemetry_layer.telemetry_handler import F1TelemetryHandler
    from lib.config import CaptureSettings, PngSettings
    from lib.f1_types import (PacketHeader, PacketLapData, LapData, CarTelemetryData,
                              PacketMotionExData, PacketLapPositionsData, ResultStatus)
    from lib.telemetry_manager.factory import PacketParserFactory
    settings = PngSettings(Capture=CaptureSettings(telemetry_recording_enabled=True, session_dir=str(tmp_path)))
    state = Mock()
    state.m_pkt_count = 0
    handler = F1TelemetryHandler(settings, Mock(), state)
    factory = PacketParserFactory(set(handler.m_manager.m_callbacks), Mock())
    def header(kind):
        return PacketHeader.from_values(2026, 26, 1, 0, 1, kind, 123, 1.0, 60, 60, 0, 255)
    lap = PacketLapData(header(P.LAP_DATA), bytes(LapData.PACKET_LEN_24 * 24 + 2))
    lap.m_lapData[0].m_currentLapNum = 1
    lap.m_lapData[0].m_resultStatus = ResultStatus.ACTIVE
    import struct
    motion = bytearray(PacketMotionExData.PACKET_LEN_25)
    struct.pack_into("<4f", motion, 16 * 4, 0.2, 0.3, 0.4, 0.5)
    positions = PacketLapPositionsData.from_values(header(P.LAP_POSITIONS), 1, 0, [[3] + [0] * 23])
    for payload in (lap.to_bytes(), header(P.CAR_TELEMETRY).to_bytes() + bytes(CarTelemetryData.PACKET_LEN_2026 * 24 + 3),
                    header(P.MOTION_EX).to_bytes() + motion, positions.to_bytes()):
        await handler.m_manager._processPacket(factory, payload)
    await handler.stop()
    assert handler.m_lap_recorder.error is None
    directory = (tmp_path / handler.m_lap_recorder.reference(123)["manifest"]).parent
    assert read_lap(directory, 0, 1)["rows"][0]["wheel_slip_ratio_fl"] == pytest.approx(0.4)
    manifest = json.loads((directory / "manifest.json").read_text())
    history = read_history(directory, manifest, 0)
    assert history[0]["data"]["position"] == 3 and history[0]["lap_num"] == 1


def test_history_disk_cap_and_path_traversal(tmp_path):
    store = SessionStore(tmp_path / "cap", {"session_uid": "123", "sample_hz": 20}, 9000)
    store.append_history([history_row("setup", 0, {str(i): i for i in range(10000)})])
    store.close({})
    assert store.manifest["state"] == "disk_limit"
    assert store.manifest["discarded_history"] == 1
    assert sum(p.stat().st_size for p in store.directory.iterdir()) <= 9000
    store.manifest["history_chunks"] = [{"file": "../outside.parquet"}]
    with pytest.raises(ValueError, match="Invalid history"):
        read_history(store.directory, store.manifest, 0)


async def test_analysis_rechecks_live_epoch_after_disk_read(tmp_path, monkeypatch):
    import apps.backend.intf_layer.lap_telemetry as module
    _, reference = fixture_recording(tmp_path)
    handler, state = backend(tmp_path, reference)
    original = module.analyze_reference
    def change_epoch(*args, **kwargs):
        result = original(*args, **kwargs)
        handler.m_lap_recorder.assembler.epoch = 1
        return result
    monkeypatch.setattr(module, "analyze_reference", change_epoch)
    result = await handle_lap_telemetry_request(handler, state,
               {"operation": "get_corner_analysis", "driver_index": 0, "lap_num": 1}, LOGGER, analysis=True)
    assert result["error"] == "recording_changed"


def test_flashback_discards_trailing_old_frame_history():
    from lib.lap_telemetry.assembler import SampleAssembler
    emitted = []
    assembler = SampleAssembler(lambda op, data: emitted.append((op, data)))
    assembler.feed(participants())
    assembler.feed(flashback(3, 1.1, 0.5))
    assembler.feed(packet(P.CAR_SETUPS, frame=3, time=1.1,
                          m_carSetups=[Obj(toJSON=lambda: {"front-wing": 99})]))
    assembler.feed(packet(P.CAR_SETUPS, frame=4, time=0.6,
                          m_carSetups=[Obj(toJSON=lambda: {"front-wing": 30})]))
    history = [row for op, data in emitted if op == "history" for row in data]
    assert len(history) == 1
    assert json.loads(history[0]["payload"])["front-wing"] == 30
