"""Validated, bounded lap trace responses shared by live IPC and saved MCP tools."""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .channels import CHANNELS, SCHEMA_VERSION, UNITS
from .storage import read_lap

DEFAULT_MAX_SAMPLES = 200
MAX_SAMPLES = 2000
MAX_CELLS = 12000
MAX_RESPONSE_BYTES = 128 * 1024
MAX_READ_ROWS = 100000

GROUPS = {
    prefix: {wheel: f"{prefix}_{wheel}" for wheel in ("fl", "fr", "rl", "rr")}
    for prefix in ("tyre_surface_temp", "tyre_inner_temp", "brake_temp", "tyre_pressure", "surface_type", "wheel_speed", "wheel_slip_ratio", "wheel_slip_angle", "tyre_wear")
}
GROUPS["world_position"] = {axis: f"world_position_{axis}" for axis in ("x", "y", "z")}
AXES = ("distance_m", "session_time_s", "lap_time_ms")
COLUMN_UNITS = {**UNITS, "session_time_s": "s"}
SELECTORS = {name: {None: name} for name in COLUMN_UNITS if name != "lap_num"}
SELECTORS.update(GROUPS)
DEFAULT_CHANNELS = ["speed_kph", "throttle", "brake", "steering", "gear", "rpm", "ers_percent",
                    "overtake_active", "active_aero_mode", "tyre_surface_temp", "tyre_inner_temp",
                    "brake_temp", "world_position"]


class LapTelemetryQuery(BaseModel):
    """Strict validation is shared across MCP and the backend IPC boundary."""

    model_config = ConfigDict(extra="forbid", strict=True)
    driver_index: int = Field(ge=0, le=23)
    lap_num: int = Field(ge=1, le=255)
    channels: list[str] | None = Field(default=None, min_length=1, max_length=len(SELECTORS))
    start_m: float | None = Field(default=None, allow_inf_nan=False)
    end_m: float | None = Field(default=None, allow_inf_nan=False)
    max_samples: int = Field(default=DEFAULT_MAX_SAMPLES, ge=2, le=MAX_SAMPLES)

    @model_validator(mode="after")
    def validate_selection(self):
        if self.start_m is not None and self.end_m is not None and self.start_m > self.end_m:
            raise ValueError("start_m must not exceed end_m")
        if self.channels is not None and any(name not in SELECTORS for name in self.channels):
            raise ValueError("Unknown telemetry channel")
        return self

    def selection(self):
        return list(dict.fromkeys([*AXES, *(self.channels if self.channels is not None else DEFAULT_CHANNELS)]))


def unavailable(error, *, detail=None):
    result = {"ok": False, "available": False, "error": error, "data": None}
    if detail:
        result["detail"] = detail
    return result


def resolve_recording(session_dir, reference):
    """Only accept recorder manifests within the configured telemetry directory."""
    if not isinstance(reference, dict) or reference.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Invalid recording reference")
    relative = reference.get("manifest")
    if not isinstance(relative, str):
        raise ValueError("Invalid recording reference")
    path = Path(relative)
    if path.is_absolute() or path.drive or ".." in path.parts:
        raise ValueError("Invalid recording reference")
    root = (Path(session_dir) / "telemetry").resolve()
    manifest = (Path(session_dir) / path).resolve()
    if manifest.name != "manifest.json" or manifest.parent.parent != root:
        raise ValueError("Invalid recording reference")
    if not str(reference.get("session_uid", "")).isdigit():
        raise ValueError("Invalid recording session identity")
    return manifest.parent


def _project(selection, values):
    """Use the same nesting for channel arrays and their units."""
    return {
        name: values[SELECTORS[name][None]] if None in SELECTORS[name]
        else {key: values[column] for key, column in SELECTORS[name].items()}
        for name in selection
    }


def _indices(length, count):
    if length <= count:
        return range(length)
    return [i * (length - 1) // (count - 1) for i in range(count)]


def query_lap(directory, query: LapTelemetryQuery, *, expected_uid, expected_epoch=None):
    """Blocking disk query. Call in a worker, never on the packet event loop."""
    selection = query.selection()
    columns = list(dict.fromkeys(column for name in selection for column in SELECTORS[name].values()))
    quality_columns = [f"{group}_{suffix}" for group in CHANNELS for suffix in ("age_s", "stale")]
    result = read_lap(directory, query.driver_index, query.lap_num,
                      columns=list(dict.fromkeys([*columns, *quality_columns, "telemetry_public"])),
                      start_m=query.start_m, end_m=query.end_m, max_rows=MAX_READ_ROWS)
    recording = result["recording"]
    if recording["session_uid"] != str(expected_uid):
        return unavailable("recording_session_mismatch")
    if expected_epoch is not None and recording["epoch"] != expected_epoch:
        return unavailable("telemetry_pending", detail="A timeline change has not yet been committed. Retry shortly.")
    rows = result["rows"]
    if not rows:
        return unavailable("window_empty" if result["coverage"]["start_m"] is not None else "lap_not_recorded")
    count = len(rows)
    # Count actual response cells, including overlapping group/individual selectors.
    width = sum(len(SELECTORS[name]) for name in selection)
    limit = min(query.max_samples, MAX_CELLS // width, count)
    null_counts = {column: sum(row[column] is None for row in rows) for column in columns}
    source_groups = {}
    selected_columns = set(columns)
    for group, group_channels in CHANNELS.items():
        relevant = set(group_channels) & selected_columns
        if group == "status" and "ers_percent" in selected_columns:
            relevant.add("ers_percent")
        if not relevant:
            continue
        ages = [row[f"{group}_age_s"] for row in rows if row[f"{group}_age_s"] is not None]
        source_groups[group] = {
            "unavailable_or_stale_samples": sum(row[f"{group}_stale"] is not False for row in rows),
            "max_source_age_s": max(ages) if ages else None,
        }
    response = {
        "ok": True, "available": True, "error": None, "schema_version": SCHEMA_VERSION,
        "session_uid": str(expected_uid), "driver_index": query.driver_index, "lap_num": query.lap_num,
        "recording": {**recording, "state": result["state"], "committed_only": True},
        "coverage": {**result["coverage"], "window_start_m": query.start_m, "window_end_m": query.end_m},
        "units": _project(selection, COLUMN_UNITS),
        "quality": {
            "missing_channels": [column for column, nulls in null_counts.items() if nulls == count],
            "null_counts": null_counts, "source_groups": source_groups,
            "telemetry_public": rows[-1]["telemetry_public"],
            "capture_counters": result["counters"], "discarded_samples": result["discarded_samples"],
        },
    }
    while True:
        selected_rows = [rows[index] for index in _indices(count, limit)]
        response["data"] = _project(selection, {column: [row[column] for row in selected_rows] for column in columns})
        response["downsampling"] = {
            "method": "uniform_index" if limit < count else "none",
            "source_samples": count, "returned_samples": limit,
            "requested_max_samples": query.max_samples, "applied": limit < count,
            "note": "Uniform selection can omit brief events and extrema; narrow the distance window for detail."
                    if limit < count else None,
        }
        # Leave room for source/slug/connectivity metadata added by transport wrappers.
        if len(json.dumps(response, allow_nan=False, separators=(",", ":")).encode()) <= MAX_RESPONSE_BYTES - 2048:
            return response
        if limit <= 2:
            return unavailable("response_limit_exceeded")
        limit = max(2, limit // 2)


def safe_query_lap(directory, query, logger, *, expected_uid, expected_epoch=None):
    """Do not leak filesystem paths or tracebacks through MCP/IPC errors."""
    try:
        return query_lap(directory, query, expected_uid=expected_uid, expected_epoch=expected_epoch)
    except FileNotFoundError:
        return unavailable("recording_unavailable", detail="No committed recording is available yet, or files are missing.")
    except ValueError as exc:
        logger.warning("Lap telemetry query rejected: %s", exc)
        return unavailable("recording_unreadable")
    except Exception:
        logger.exception("Could not read lap telemetry")
        return unavailable("recording_unreadable")



def safe_query_reference(session_dir, reference, query, logger, *, expected_epoch=None):
    """Resolve and validate references in the same worker as the disk read."""
    try:
        directory = resolve_recording(session_dir, reference)
    except (ValueError, OSError):
        return unavailable("invalid_recording_reference")
    return safe_query_lap(directory, query, logger, expected_uid=reference["session_uid"], expected_epoch=expected_epoch)
