"""MCP access to bounded live and saved lap traces."""

import asyncio

from pydantic import ValidationError

from lib.ipc import PngAppId
from lib.lap_telemetry.analysis import AnalysisQuery, analyze_reference
from lib.lap_telemetry.query import LapTelemetryQuery, safe_query_reference, unavailable
from apps.mcp_server.mcp_server.tools.get_saved_sessions import _load_saved_session


LAP_TELEMETRY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"}, "available": {"type": "boolean"},
        "error": {"type": ["string", "null"]},
        "source": {"type": "string", "enum": ["live", "saved"]},
        "session_slug": {"type": ["string", "null"]},
        "connected": {"type": ["boolean", "null"]},
        "session_uid": {"type": "string"},
        "driver_index": {"type": "integer"}, "lap_num": {"type": "integer"},
        "data": {"type": ["object", "null"], "additionalProperties": True},
        "units": {"type": "object", "additionalProperties": True},
        "coverage": {"type": "object", "additionalProperties": True},
        "quality": {"type": "object", "additionalProperties": True},
        "downsampling": {"type": "object", "additionalProperties": True},
        "recording": {"type": "object", "additionalProperties": True},
    },
    "required": ["ok", "available", "error", "data"],
    "additionalProperties": True,
}


async def get_lap_telemetry(dealer, logger, session_dir, app_version, *, driver_index, lap_num,
                            session_slug=None, channels=None, start_m=None, end_m=None, max_samples=200,
                            analysis_query=None):
    try:
        query = (AnalysisQuery.model_validate(analysis_query) if analysis_query is not None else
                 LapTelemetryQuery(driver_index=driver_index, lap_num=lap_num, channels=channels,
                                   start_m=start_m, end_m=end_m, max_samples=max_samples))
    except ValidationError:
        return unavailable("invalid_request")
    if session_slug is not None:
        if not isinstance(session_slug, str) or not 1 <= len(session_slug) <= 256:
            return unavailable("invalid_request")
        context = {"source": "saved", "session_slug": session_slug, "connected": None}
        try:
            saved = await _load_saved_session(session_dir, logger, app_version, session_slug, recompute=False)
        except Exception:
            logger.exception("Could not load saved session for lap telemetry")
            return {**unavailable("session_load_failed"), **context}
        if not saved["ok"]:
            return {**unavailable(saved.get("error", "session_not_found")), **context}
        raw_data = saved.get("raw_data")
        if not isinstance(raw_data, dict):
            return {**unavailable("session_load_failed"), **context}
        reference = raw_data.get("telemetry-recording")
        if not reference:
            return {**unavailable("telemetry_not_recorded", detail="This saved session has no recorded lap traces."), **context}
        debug = raw_data.get("debug")
        saved_uid = debug.get("session-uid") if isinstance(debug, dict) else None
        if saved_uid is not None and (not isinstance(reference, dict) or str(saved_uid) != str(reference.get("session_uid"))):
            return {**unavailable("recording_session_mismatch"), **context}
        reader = analyze_reference if analysis_query is not None else safe_query_reference
        result = await asyncio.to_thread(reader, session_dir, reference, query, logger)
        return {**result, **context}

    try:
        reply = await dealer.request(str(PngAppId.BACKEND), "telemetry-analysis-request" if analysis_query is not None else "lap-telemetry-request", query.model_dump())
    except Exception:
        logger.exception("Live lap telemetry backend request failed")
        return {**unavailable("core_server_unreachable"), "source": "live", "connected": False}
    if not isinstance(reply, dict):
        return {**unavailable("invalid_backend_response"), "source": "live"}
    if reply.get("status") == "error":
        error = "core_server_timeout" if "timeout" in str(reply.get("reason", "")).lower() else "core_server_unreachable"
        return {**unavailable(error), "source": "live", "connected": False}
    if not all(key in reply for key in ("ok", "available", "error", "data")):
        return {**unavailable("invalid_backend_response"), "source": "live"}
    return {**reply, "source": "live", "session_slug": None}


async def get_telemetry_analysis(dealer, logger, session_dir, app_version, *, session_slug=None, **request):
    """Reuse live/saved identity, path and transport validation for analysis."""
    return await get_lap_telemetry(dealer, logger, session_dir, app_version,
                                  driver_index=request.get("driver_index"), lap_num=None,
                                  session_slug=session_slug, analysis_query=request)
