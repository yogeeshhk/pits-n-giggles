"""Versioned flat storage columns. UDP wheel order is RL, RR, FL, FR."""

from enum import Enum
import math

WHEELS = ("rl", "rr", "fl", "fr")
SCHEMA_VERSION = 1

# output column -> (packet attribute, unit)
CHANNELS = {
    "lap": {
        "lap_num": ("m_currentLapNum", "lap"),
        "distance_m": ("m_lapDistance", "m"),
        "lap_time_ms": ("m_currentLapTimeInMS", "ms"),
        "position": ("m_carPosition", "position"),
        "sector": ("m_sector", "enum"),
        "lap_invalid": ("m_currentLapInvalid", "bool"),
        "pit_status": ("m_pitStatus", "enum"),
    },
    "telemetry": {
        "speed_kph": ("m_speed", "km/h"),
        "throttle": ("m_throttle", "fraction"),
        "brake": ("m_brake", "fraction"),
        "steering": ("m_steer", "signed fraction"),
        "gear": ("m_gear", "gear"),
        "rpm": ("m_engineRPM", "rpm"),
    },
    "status": {
        "ers_store_j": ("m_ersStoreEnergy", "J"),
        "ers_deploy_mode": ("m_ersDeployMode", "enum"),
        "ers_deployed_j": ("m_ersDeployedThisLap", "J"),
        "ers_harvested_mguk_j": ("m_ersHarvestedThisLapMGUK", "J"),
        "ers_harvested_mguh_j": ("m_ersHarvestedThisLapMGUH", "J"),
        "ers_harvest_limit_j": ("m_ersHarvestedLimitPerLap", "J"),
    },
    "motion": {
        "world_position_x": ("m_worldPositionX", "m"),
        "world_position_y": ("m_worldPositionY", "m"),
        "world_position_z": ("m_worldPositionZ", "m"),
        "g_lateral": ("m_gForceLateral", "g"),
        "g_longitudinal": ("m_gForceLongitudinal", "g"),
        "g_vertical": ("m_gForceVertical", "g"),
        "yaw": ("m_yaw", "rad"),
    },
    "telemetry2": {
        "active_aero_mode": ("m_activeAeroMode", "enum"),
        "active_aero_available": ("m_activeAeroAvailable", "bool"),
        "active_aero_activation_distance_m": ("m_activeAeroActivationDistance", "m"),
        "overtake_available": ("m_overtakeAvailable", "bool"),
        "overtake_active": ("m_overtakeActive", "bool"),
        "overtake_activation_distance_m": ("m_overtakeActivationDistance", "m"),
        "regulations_2026": ("m_2026Regulations", "bool"),
    },
}
for prefix, attribute, unit in (
    ("tyre_surface_temp", "m_tyresSurfaceTemperature", "degC"),
    ("tyre_inner_temp", "m_tyresInnerTemperature", "degC"),
    ("brake_temp", "m_brakesTemperature", "degC"),
    ("tyre_pressure", "m_tyresPressure", "psi"),
    ("surface_type", "m_surfaceType", "enum"),
):
    for index, wheel in enumerate(WHEELS):
        CHANNELS["telemetry"][f"{prefix}_{wheel}"] = ((attribute, index), unit)

# Additive schema evolution: readers null-fill columns absent from older chunks.
CHANNELS["lap"].update({
    "gap_front_ms_part": ("m_deltaToCarInFrontInMS", "ms"),
    "gap_front_minutes": ("m_deltaToCarInFrontMinutes", "min"),
    "gap_leader_ms_part": ("m_deltaToRaceLeaderInMS", "ms"),
    "gap_leader_minutes": ("m_deltaToRaceLeaderMinutes", "min"),
    "pit_lane_active": ("m_pitLaneTimerActive", "bool"),
    "pit_lane_time_ms": ("m_pitLaneTimeInLaneInMS", "ms"),
    "pit_stop_time_ms": ("m_pitStopTimerInMS", "ms"),
    "num_pit_stops": ("m_numPitStops", "enum"),
    "last_lap_time_ms": ("m_lastLapTimeInMS", "ms"),
})
CHANNELS["telemetry"]["clutch"] = ("m_clutch", "%")
CHANNELS["status"].update({
    "pit_limiter": ("m_pitLimiterStatus", "bool"),
    "tyre_compound": ("m_actualTyreCompound", "enum"),
    "tyre_age_laps": ("m_tyresAgeLaps", "lap"),
    "fuel_kg": ("m_fuelInTank", "kg"),
    "front_brake_bias": ("m_frontBrakeBias", "%"),
    "fia_flag": ("m_vehicleFiaFlags", "enum"),
    "engine_power_mguk_w": ("m_enginePowerMGUK", "W"),
})
CHANNELS["motion_ex"] = {}
CHANNELS["damage"] = {}
CHANNELS["session"] = {
    "track_id": ("m_trackId", "enum"),
    "safety_car_status": ("m_safetyCarStatus", "enum"),
}
for prefix, attribute, unit in (
    ("wheel_speed", "m_wheelSpeed", "game units (unspecified)"),
    ("wheel_slip_ratio", "m_wheelSlipRatio", "ratio"),
    ("wheel_slip_angle", "m_wheelSlipAngle", "rad"),
):
    for index, wheel in enumerate(WHEELS):
        CHANNELS["motion_ex"][f"{prefix}_{wheel}"] = ((attribute, index), unit)
for index, wheel in enumerate(WHEELS):
    CHANNELS["damage"][f"tyre_wear_{wheel}"] = (("m_tyresWear", index), "%")

UNITS = {name: unit for group in CHANNELS.values() for name, (_, unit) in group.items()}
UNITS["ers_percent"] = "%"
UNITS.update(gap_front_ms="ms", gap_leader_ms="ms")


def value(car, attribute):
    """Preserve valid zeroes; store absent/nonfinite values as null."""
    if isinstance(attribute, tuple):
        array = getattr(car, attribute[0], None)
        result = array[attribute[1]] if array is not None and len(array) > attribute[1] else None
    else:
        result = getattr(car, attribute, None)
    if isinstance(result, Enum):
        result = result.value
    if isinstance(result, float) and not math.isfinite(result):
        return None
    return result


def arrow_schema():
    """Import Arrow only in the storage worker or reader."""
    import json
    import pyarrow as pa

    fields = [
        ("session_uid", pa.uint64()), ("driver_index", pa.uint8()),
        ("epoch", pa.uint32()), ("frame_id", pa.uint32()),
        ("overall_frame_id", pa.uint32()), ("session_time_s", pa.float64()),
        ("packet_format", pa.uint16()), ("telemetry_public", pa.bool_()),
    ]
    for name, unit in UNITS.items():
        dtype = pa.float32()
        if unit == "bool":
            dtype = pa.bool_()
        elif unit in ("enum", "gear", "lap", "position", "rpm"):
            dtype = pa.int32()
        fields.append((name, dtype))
    for group in CHANNELS:
        fields.extend([
            (f"{group}_source_time_s", pa.float64()),
            (f"{group}_source_frame_id", pa.uint32()),
            (f"{group}_age_s", pa.float32()),
            (f"{group}_stale", pa.bool_()),
        ])
    return pa.schema(fields, metadata={
        b"schema_version": str(SCHEMA_VERSION).encode(),
        b"units": json.dumps(UNITS, sort_keys=True).encode(),
    })
