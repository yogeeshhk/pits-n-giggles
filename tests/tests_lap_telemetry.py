"""Recorder correctness tests using deterministic packet streams and real Parquet."""

import asyncio
import json
import logging
from types import SimpleNamespace as Obj

import pytest

from lib.config import CaptureSettings
from lib.f1_types import F1PacketType as P, PacketEventData
from lib.lap_telemetry.assembler import SampleAssembler
from lib.lap_telemetry.recorder import TelemetryRecorder
from lib.lap_telemetry.storage import SessionStore, read_lap


def packet(kind, frame=1, time=0.0, uid=123, **kwargs):
    return Obj(m_header=Obj(m_packetId=kind, m_sessionUID=uid, m_packetFormat=2026,
                           m_overallFrameIdentifier=frame, m_frameIdentifier=frame,
                           m_sessionTime=time, m_playerCarIndex=0), **kwargs)


def participants(count=1, **kwargs):
    return packet(P.PARTICIPANTS, m_numActiveCars=count,
                  m_participants=[Obj(m_yourTelemetry=1) for _ in range(count)], **kwargs)


def frame_packets(frame=1, time=0.0, lap=1, distance=0, count=1, uid=123):
    args = dict(frame=frame, time=time, uid=uid)
    return [
        packet(P.LAP_DATA, **args, m_lapData=[Obj(
            m_currentLapNum=lap, m_lapDistance=distance, m_currentLapTimeInMS=round(time * 1000),
            m_carPosition=i+1, m_sector=0, m_currentLapInvalid=0, m_pitStatus=0, m_resultStatus=2,
        ) for i in range(count)]),
        packet(P.CAR_TELEMETRY, **args, m_carTelemetryData=[Obj(
            m_speed=100+i, m_throttle=0.0, m_brake=0.0, m_steer=-0.25, m_gear=-1, m_engineRPM=12000,
            m_tyresSurfaceTemperature=[80, 81, 82, 83], m_tyresInnerTemperature=[90, 91, 92, 93],
            m_brakesTemperature=[400, 401, 402, 403], m_tyresPressure=[20, 21, 22, 23],
            m_surfaceType=[0, 0, 1, 1],
        ) for i in range(count)]),
        packet(P.CAR_STATUS, **args, m_carStatusData=[Obj(
            m_ersStoreEnergy=0.0, m_ersDeployMode=1, m_ersDeployedThisLap=100,
            m_ersHarvestedThisLapMGUK=200, m_ersHarvestedThisLapMGUH=0,
            m_ersHarvestedLimitPerLap=8_500_000,
        ) for _ in range(count)]),
        packet(P.MOTION, **args, m_carMotionData=[Obj(
            m_worldPositionX=1.0, m_worldPositionY=2.0, m_worldPositionZ=3.0,
            m_gForceLateral=0.5, m_gForceLongitudinal=0.0, m_gForceVertical=1.0, m_yaw=0.2,
        ) for _ in range(count)]),
        packet(P.CAR_TELEMETRY_2, **args, m_carTelemetry2Data=[Obj(
            m_activeAeroMode=0, m_activeAeroAvailable=False, m_activeAeroActivationDistance=0,
            m_overtakeAvailable=True, m_overtakeActive=False, m_overtakeActivationDistance=0,
            m_2026Regulations=True,
        ) for _ in range(count)]),
    ]


def flashback(frame, time, target):
    return packet(P.EVENT, frame=frame, time=time,
                  m_eventCode=PacketEventData.EventPacketType.FLASHBACK,
                  mEventDetails=Obj(flashbackSessionTime=target))


def assemble(packets):
    events = []
    assembler = SampleAssembler(lambda op, data: events.append((op, data)))
    for pkt in packets:
        assembler.feed(pkt)
    assembler.flush()
    return assembler, events


def rows(events):
    return [row for op, data in events if op == "rows" for row in data]


@pytest.mark.parametrize("reverse", [False, True])
def test_frame_alignment_wheels_zeroes_and_missing_values(reverse):
    packets = frame_packets()
    if reverse:
        packets.reverse()
    _, events = assemble([participants(), *packets])
    row = rows(events)[0]
    assert row["throttle"] == row["brake"] == row["ers_percent"] == 0
    assert row["gear"] == -1
    assert row["tyre_surface_temp_fl"] == 82
    assert row["tyre_surface_temp_rr"] == 81
    assert row["tyre_pressure_fr"] == 23
    assert row["overtake_active"] is False
    assert not any(row[f"{group}_stale"] for group in ("lap", "telemetry", "status", "motion", "telemetry2"))


def test_stale_sources_missing_packets_and_restricted_status():
    packets = frame_packets()
    packets += frame_packets(frame=2, time=1.0)[0:2]
    _, events = assemble([participants(), *packets])
    old, new = rows(events)
    assert old["ers_percent"] == 0
    assert new["ers_percent"] is None
    assert new["status_stale"] and new["status_source_time_s"] == 0
    assert new["status_age_s"] == 1
    assert new["world_position_x"] is None
    _, events = assemble(frame_packets(count=2))
    assert rows(events)[1]["ers_percent"] is None
    assert rows(events)[1]["tyre_surface_temp_fl"] == 82


def test_lap_boundaries_never_use_previous_frame_lap_number():
    _, events = assemble([*frame_packets(), frame_packets(frame=2, time=0.1, lap=2)[1],
                          *frame_packets(frame=3, time=0.2, lap=2)])
    assert [row["lap_num"] for row in rows(events)] == [1, 2]
    assert [row["frame_id"] for row in rows(events)] == [1, 3]


def test_frequency_duplicate_flush_and_packet_reordering():
    packets = [pkt for frame in range(61) for pkt in frame_packets(frame=frame+1, time=frame/60)]
    assembler, events = assemble(packets)
    assembler.flush()
    assembler.feed(frame_packets(frame=1, time=0)[0])
    assert len(rows(events)) == 21
    assert len({row["session_time_s"] for row in rows(events)}) == 21


@pytest.mark.parametrize("explicit", [True, False])
def test_flashback_invalidates_cache_and_emits_new_epoch(explicit):
    packets = [participants(), *frame_packets(), *frame_packets(frame=2, time=1)]
    if explicit:
        packets += [flashback(3, 1, 0.1)]
    packets += frame_packets(frame=4, time=0.1)
    _, events = assemble(packets)
    assert len([op for op, _ in events if op == "rewind"]) == 1
    assert rows(events)[-1]["epoch"] == 1
    assert rows(events)[-1]["session_time_s"] == 0.1


def test_parquet_roundtrip_and_persisted_multiple_rewinds(tmp_path):
    _, events = assemble([participants(), *frame_packets(), *frame_packets(frame=2, time=1),
                          *frame_packets(frame=3, time=2, lap=2)])
    store = SessionStore(tmp_path / "recording", {"sample_hz": 20}, 10**7, chunk_rows=1)
    store.append(rows(events))
    store.rewind({"epoch": 1, "target_time_s": 0.5})
    replacement = dict(rows(events)[1], epoch=1, session_time_s=0.5, speed_kph=200)
    store.append([replacement])
    store.rewind({"epoch": 2, "target_time_s": 0.25})
    store.append([dict(replacement, epoch=2, session_time_s=0.25, speed_kph=250)])
    store.close({})
    result = read_lap(store.directory, 0, 1, columns=["session_time_s", "speed_kph", "tyre_surface_temp_fl"])
    assert result["rows"] == [
        {"session_time_s": 0.0, "speed_kph": 100.0, "tyre_surface_temp_fl": 82.0},
        {"session_time_s": 0.25, "speed_kph": 250.0, "tyre_surface_temp_fl": 82.0},
    ]
    assert result["coverage"]["partial"]
    assert read_lap(store.directory, 0, 2)["rows"] == []


def test_window_query_driver_filter_and_invalid_columns(tmp_path):
    _, events = assemble([participants(count=2), *frame_packets(count=2, distance=10),
                          *frame_packets(frame=2, time=0.1, count=2, distance=30)])
    store = SessionStore(tmp_path / "recording", {"sample_hz": 20}, 10**7)
    store.append(rows(events))
    store.close({})
    result = read_lap(store.directory, 1, 1, start_m=20, end_m=40, columns=["speed_kph"])
    assert result["rows"] == [{"speed_kph": 101.0}]
    with pytest.raises(ValueError, match="Unknown"):
        read_lap(store.directory, 0, 1, columns=["invented"])
    with pytest.raises(ValueError):
        read_lap(store.directory, 24, 1)


def test_disk_cap_discards_whole_chunk_and_keeps_readable_manifest(tmp_path):
    _, events = assemble(frame_packets())
    store = SessionStore(tmp_path / "recording", {"sample_hz": 20}, 4096)
    store.append(rows(events))
    store.close({})
    assert store.manifest["state"] == "disk_limit"
    assert store.manifest["discarded_samples"] == 1
    assert not list(store.directory.glob("*.parquet"))
    assert not list(store.directory.glob("*.tmp"))


async def test_recorder_session_switch_shutdown_and_manifest_link(tmp_path):
    recorder = TelemetryRecorder(tmp_path / "telemetry", logging.getLogger(__name__))
    recorder.start()
    for uid in (123, 456):
        recorder.observe(participants(uid=uid))
        for pkt in frame_packets(uid=uid):
            recorder.observe(pkt)
    await recorder.stop()
    assert recorder.error is None
    assert recorder.queued_bytes == 0
    for uid in (123, 456):
        reference = recorder.reference(uid)
        path = tmp_path / reference["manifest"]
        assert path.exists()
        result = read_lap(path.parent, 0, 1)
        assert result["state"] == "closed"
        assert result["rows"][0]["session_uid"] == uid
        assert result["coverage"]["partial"]


async def test_queue_saturation_is_bounded_and_persisted(tmp_path):
    recorder = TelemetryRecorder(tmp_path / "telemetry", logging.getLogger(__name__), buffer_bytes=128*1024)
    # Hold worker off while packet producer outruns it.
    for frame in range(100):
        for pkt in frame_packets(frame=frame+1, time=frame/20, count=24):
            recorder.observe(pkt)
    assert recorder.dropped_samples > 0
    assert recorder.peak_queued_bytes <= recorder.buffer_bytes
    await recorder.stop()
    assert recorder.error is None
    manifest = json.loads((tmp_path / recorder.reference(123)["manifest"]).read_text())
    assert manifest["counters"]["queue_dropped_samples"] > 0


async def test_writer_failure_does_not_escape_packet_processing(tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("simulated full disk")
    monkeypatch.setattr(SessionStore, "append", fail)
    recorder = TelemetryRecorder(tmp_path / "telemetry", logging.getLogger(__name__))
    for pkt in frame_packets():
        recorder.observe(pkt)
    await recorder.stop()
    assert "simulated full disk" in recorder.error
    with pytest.raises(ValueError, match="Recording failed"):
        read_lap((tmp_path / recorder.reference(123)["manifest"]).parent, 0, 1)


@pytest.mark.parametrize("setting", [
    {"telemetry_sample_hz": 0}, {"telemetry_sample_hz": 61},
    {"telemetry_buffer_mib": 0}, {"telemetry_session_limit_mib": 0},
])
def test_config_rejects_invalid_resource_limits(setting):
    with pytest.raises(ValueError):
        CaptureSettings(**setting)


async def test_binary_packets_through_backend_callbacks(tmp_path):
    from unittest.mock import Mock
    from apps.backend.telemetry_layer.telemetry_handler import F1TelemetryHandler
    from lib.config import PngSettings
    from lib.f1_types import PacketHeader, PacketLapData, LapData, CarTelemetryData, ResultStatus
    from lib.telemetry_manager.factory import PacketParserFactory

    settings = PngSettings(Capture=CaptureSettings(
        telemetry_recording_enabled=True, session_dir=str(tmp_path)))
    state = Mock()
    state.m_pkt_count = 0
    logger = Mock()
    handler = F1TelemetryHandler(settings, logger, state)
    handler.m_lap_recorder.start()
    factory = PacketParserFactory(set(handler.m_manager.m_callbacks), logger)

    def header(kind):
        return PacketHeader.from_values(2026, 26, 1, 0, 1, kind, 123, 1.0, 60, 60, 0, 255)

    lap_header = header(P.LAP_DATA)
    lap = PacketLapData(lap_header, bytes(LapData.PACKET_LEN_24 * 24 + 2))
    lap.m_lapData[0].m_currentLapNum = 1
    lap.m_lapData[0].m_resultStatus = ResultStatus.ACTIVE
    lap.m_lapData[0].m_lapDistance = 12.5
    telemetry_header = header(P.CAR_TELEMETRY)
    telemetry_bytes = telemetry_header.to_bytes() + bytes(CarTelemetryData.PACKET_LEN_2026 * 24 + 3)
    # Feed bytes through the same parser, frame gate and callbacks used by UDP.
    await handler.m_manager._processPacket(factory, telemetry_bytes)
    await handler.m_manager._processPacket(factory, lap.to_bytes())
    await handler.stop()
    result = read_lap((tmp_path / handler.m_lap_recorder.reference(123)["manifest"]).parent, 0, 1)
    assert len(result["rows"]) == 1
    assert result["rows"][0]["distance_m"] == 12.5
    assert result["rows"][0]["throttle"] == 0
    assert state.m_telemetry_recording_ref["session_uid"] == "123"
    state.processLapDataUpdate.assert_called_once()
    state.processCarTelemetryUpdate.assert_called_once()


def test_late_resumption_after_flashback_and_same_frame_duplicates():
    _, events = assemble([*frame_packets(time=1), *frame_packets(time=1),
                          flashback(2, 1, 0.1), *frame_packets(frame=3, time=0.8)])
    assert [(r["epoch"], r["session_time_s"]) for r in rows(events)] == [(0, 1), (1, 0.8)]


async def test_control_overflow_fails_closed(tmp_path):
    recorder = TelemetryRecorder(tmp_path / "telemetry", logging.getLogger(__name__), buffer_bytes=128*1024)
    for pkt in frame_packets():
        recorder.observe(pkt)
    # A lost rewind must fail the recording, not expose the superseded timeline.
    recorder._enqueue("rewind", {"oversized": "x" * recorder.buffer_bytes})
    assert recorder.error == "control_queue_overflow"
    await recorder.stop()
    assert recorder.get_stats()["state"] == "failed"


async def test_live_failure_manifest_is_marked_before_shutdown(tmp_path, monkeypatch):
    original = SessionStore.append
    recorder = TelemetryRecorder(tmp_path / "telemetry", logging.getLogger(__name__))
    recorder.start()
    for pkt in frame_packets():
        recorder.observe(pkt)
    for pkt in frame_packets(frame=2, time=0.1):
        recorder.observe(pkt)
    for _ in range(200):
        if recorder.queue.empty() and recorder.queued_bytes == 0:
            break
        await asyncio.sleep(0.01)
    def fail(*_args):
        raise OSError("writer disconnected")
    monkeypatch.setattr(SessionStore, "append", fail)
    for pkt in frame_packets(frame=3, time=0.2):
        recorder.observe(pkt)
    path = tmp_path / recorder.reference(123)["manifest"]
    for _ in range(200):
        if path.exists() and json.loads(path.read_text())["state"] == "failed":
            break
        await asyncio.sleep(0.01)
    assert json.loads(path.read_text())["state"] == "failed"
    monkeypatch.setattr(SessionStore, "append", original)
    await recorder.stop()


@pytest.mark.parametrize("hz", [20, 60])
def test_float32_timestamps_after_one_hour_preserve_sample_rate(hz):
    import struct
    events = []
    assembler = SampleAssembler(lambda op, data: events.append((op, data)), sample_hz=hz)
    for frame in range(1800):
        timestamp = struct.unpack("f", struct.pack("f", 3600 + frame / 60))[0]
        for pkt in frame_packets(frame=frame+1, time=timestamp):
            assembler.feed(pkt)
    assembler.flush()
    assert len(rows(events)) == 30 * hz


async def test_same_uid_clear_starts_a_separate_recording(tmp_path):
    recorder = TelemetryRecorder(tmp_path / "telemetry", logging.getLogger(__name__))
    for pkt in frame_packets():
        recorder.observe(pkt)
    first_reference = recorder.reference(123)
    recorder.reset_session()
    for pkt in frame_packets(frame=2, time=1):
        recorder.observe(pkt)
    second_reference = recorder.reference(123)
    await recorder.stop()
    assert first_reference != second_reference
    for reference in (first_reference, second_reference):
        result = read_lap((tmp_path / reference["manifest"]).parent, 0, 1)
        assert result["state"] == "closed"
        assert len(result["rows"]) == 1



def test_native_types_and_metadata_limit_fail_closed(tmp_path):
    _, events = assemble([participants(), *frame_packets()])
    store = SessionStore(tmp_path / "recording", {"sample_hz": 20}, 10**7)
    store.append(rows(events))
    store.close({})
    row = read_lap(store.directory, 0, 1)["rows"][0]
    assert row["overtake_active"] is False
    assert row["lap_invalid"] is False
    assert isinstance(row["gear"], int)
    # Force a metadata-only overflow and verify no truncated rewind list leaks.
    store.disk_limit = store.manifest["parquet_bytes"] + 4096
    store.rewind({"epoch": 1, "target_time_s": 0, "padding": "x" * 8192})
    assert store.manifest["state"] == "failed"
    with pytest.raises(ValueError, match="Recording failed"):
        read_lap(store.directory, 0, 1)
    assert sum(path.stat().st_size for path in store.directory.iterdir()) <= store.disk_limit
