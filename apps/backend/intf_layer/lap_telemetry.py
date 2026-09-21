"""Serve committed live lap recordings through the backend's IPC interface."""

import asyncio

from pydantic import ValidationError

from lib.lap_telemetry.query import LapTelemetryQuery, safe_query_reference, unavailable


async def handle_lap_telemetry_request(telemetry_handler, session_state, data, logger):
    try:
        query = LapTelemetryQuery.model_validate(data)
    except ValidationError:
        return unavailable("invalid_request")
    recorder = telemetry_handler.m_lap_recorder
    if recorder is None:
        return unavailable("recording_disabled", detail="Enable Capture.telemetry_recording_enabled and restart the backend.")
    if recorder.error:
        return unavailable("recording_failed")
    reference = recorder.reference(recorder.session_uid)
    if reference is None or recorder.assembler is None:
        return unavailable("recording_not_started")
    epoch = recorder.assembler.epoch
    result = await asyncio.to_thread(
        safe_query_reference, recorder.root.parent, reference, query, logger, expected_epoch=epoch,
    )
    # Disk reads yield to packet processing. Never return a result labeled live
    # if a session clear, rewind or writer failure happened during that read.
    if recorder.error:
        return unavailable("recording_failed")
    if (recorder.reference(recorder.session_uid) != reference or recorder.assembler is None
            or recorder.assembler.epoch != epoch):
        return unavailable("recording_changed", detail="The session or timeline changed during the query. Retry.")
    return {**result, "source": "live", "connected": bool(session_state.m_connected_to_sim)}
