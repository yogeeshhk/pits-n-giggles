# MIT License
#
# Copyright (c) [2026] [Ashwin Natarajan]
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# -------------------------------------- IMPORTS -----------------------------------------------------------------------

import logging
import statistics
from typing import Any, Dict, Iterable, List, Optional

from apps.hud.common import get_ref_row
from lib.f1_types import LapHistoryData
from lib.ipc import IpcDealerAsync

from .common import _DRIVER_INFO_REQ_STATUS_SCHEMA, _get_race_table_context, fetch_driver_info
from .get_session_events_for_driver import _get_race_ctrl_msg

# -------------------------------------- CONSTANTS ---------------------------------------------------------------------

GENERIC_ANALYSIS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        **_DRIVER_INFO_REQ_STATUS_SCHEMA,
        "available": {"type": "boolean"},
        "connected": {"type": "boolean"},
        "last-update-timestamp": {"type": ["number", "null"]},
        "ok": {"type": "boolean"},
        "error": {"type": ["string", "null"]},
    },
    "additionalProperties": True,
}

PIT_STOPS_OUTPUT_SCHEMA = GENERIC_ANALYSIS_OUTPUT_SCHEMA
PACE_ANALYSIS_OUTPUT_SCHEMA = GENERIC_ANALYSIS_OUTPUT_SCHEMA
EVENT_TIMELINE_OUTPUT_SCHEMA = GENERIC_ANALYSIS_OUTPUT_SCHEMA
STRATEGY_SUMMARY_OUTPUT_SCHEMA = GENERIC_ANALYSIS_OUTPUT_SCHEMA
RIVAL_COMPARISON_OUTPUT_SCHEMA = GENERIC_ANALYSIS_OUTPUT_SCHEMA
DATA_QUALITY_REPORT_OUTPUT_SCHEMA = GENERIC_ANALYSIS_OUTPUT_SCHEMA
COACH_NOTES_OUTPUT_SCHEMA = GENERIC_ANALYSIS_OUTPUT_SCHEMA
PLAYER_RACE_SUMMARY_OUTPUT_SCHEMA = GENERIC_ANALYSIS_OUTPUT_SCHEMA

_PIT_EVENT_TYPES = {"PITTING", "TYRE_CHANGE", "WING_CHANGE", "DRIVE_THROUGH_SERVED", "STOP_GO_SERVED"}
_PENALTY_EVENT_TYPES = {"PENALTY", "DRIVE_THROUGH_SERVED", "STOP_GO_SERVED"}
_DAMAGE_EVENT_TYPES = {"CAR_DAMAGE", "WING_CHANGE", "COLLISION"}
_SAFETY_CAR_STATUSES = {"SAFETY_CAR", "VIRTUAL_SAFETY_CAR", "FORMATION_LAP"}

# -------------------------------------- PUBLIC FUNCTIONS --------------------------------------------------------------

async def get_player_race_summary(dealer: IpcDealerAsync, logger: logging.Logger) -> Dict[str, Any]:
    telemetry_update, base_rsp = _get_race_table_context(logger)
    if telemetry_update is None:
        return base_rsp

    ref_row = get_ref_row(telemetry_update)
    if not ref_row:
        return {**base_rsp, "ok": False, "error": "No reference row found in telemetry update"}

    driver_index = ref_row.get("driver-info", {}).get("index")
    if driver_index is None:
        return {**base_rsp, "ok": False, "error": "Reference driver has no index"}

    rsp = await _fetch_driver_data(dealer, logger, driver_index)
    if not rsp["status"]["ok"]:
        return rsp

    data = rsp["data"]
    pit_stops = _detect_pit_stops(data)
    pace = _build_pace_analysis(data, pit_stops)
    events = _build_event_timeline(data)
    row_payload = _race_table_row_summary(ref_row)
    final_position = _final_position(data, ref_row)
    start_position = _start_position(data, ref_row)

    return {
        **base_rsp,
        "ok": True,
        "driver": _driver_identity(data, ref_row),
        "final_position": final_position,
        "start_position": start_position,
        "positions_gained_lost": _position_delta(start_position, final_position),
        "best_lap": pace.get("fastest_lap"),
        "average_race_pace_excluding_pit_laps_ms": pace.get("average_excluding_pit_laps_ms"),
        "pit_stop_laps": [stop["lap_num"] for stop in pit_stops],
        "pit_stops": pit_stops,
        "penalties": _penalties(data),
        "damage": data.get("car-damage") or row_payload.get("damage"),
        "fuel_remaining": row_payload.get("fuel_remaining"),
        "tyre_wear_summary": _tyre_wear_summary(data, ref_row),
        "key_events": events.get("key_events", []),
        "pace_analysis": pace,
        "status": rsp["status"],
    }


async def get_pit_stops(dealer: IpcDealerAsync, logger: logging.Logger, driver_index: int) -> Dict[str, Any]:
    rsp = await _fetch_driver_data(dealer, logger, driver_index)
    if not rsp["status"]["ok"]:
        return rsp
    return {
        "status": rsp["status"],
        "pit_stops": _detect_pit_stops(rsp["data"]),
    }


async def get_driver_pace_analysis(dealer: IpcDealerAsync, logger: logging.Logger, driver_index: int) -> Dict[str, Any]:
    rsp = await _fetch_driver_data(dealer, logger, driver_index)
    if not rsp["status"]["ok"]:
        return rsp
    pit_stops = _detect_pit_stops(rsp["data"])
    return {
        "status": rsp["status"],
        "pace_analysis": _build_pace_analysis(rsp["data"], pit_stops),
    }


async def get_driver_event_timeline(dealer: IpcDealerAsync, logger: logging.Logger, driver_index: int) -> Dict[str, Any]:
    rsp = await _fetch_driver_data(dealer, logger, driver_index)
    if not rsp["status"]["ok"]:
        return rsp
    return {
        "status": rsp["status"],
        "event_timeline": _build_event_timeline(rsp["data"]),
    }


async def get_strategy_summary(dealer: IpcDealerAsync, logger: logging.Logger, driver_index: int) -> Dict[str, Any]:
    rsp = await _fetch_driver_data(dealer, logger, driver_index)
    if not rsp["status"]["ok"]:
        return rsp
    pit_stops = _detect_pit_stops(rsp["data"])
    pace = _build_pace_analysis(rsp["data"], pit_stops)
    return {
        "status": rsp["status"],
        "strategy_summary": _build_strategy_summary(rsp["data"], pit_stops, pace),
    }


async def compare_driver_to_rivals(
    dealer: IpcDealerAsync,
    logger: logging.Logger,
    driver_index: int,
    rival_indices: Optional[List[int]] = None,
) -> Dict[str, Any]:
    telemetry_update, base_rsp = _get_race_table_context(logger)
    if telemetry_update is None:
        return base_rsp

    target_rsp = await _fetch_driver_data(dealer, logger, driver_index)
    if not target_rsp["status"]["ok"]:
        return target_rsp

    target_row = _find_row_by_index(telemetry_update, driver_index)
    target_analysis = _comparison_basis(target_rsp["data"], target_row)
    rivals = rival_indices if rival_indices else _nearest_rival_indices(telemetry_update, driver_index)

    comparisons = []
    for rival_index in rivals:
        rival_rsp = await _fetch_driver_data(dealer, logger, rival_index)
        if not rival_rsp["status"]["ok"]:
            comparisons.append({"rival_index": rival_index, "status": rival_rsp["status"]})
            continue
        rival_row = _find_row_by_index(telemetry_update, rival_index)
        rival_analysis = _comparison_basis(rival_rsp["data"], rival_row)
        comparisons.append(_compare_basis(target_analysis, rival_analysis))

    return {
        **base_rsp,
        "ok": True,
        "driver": target_analysis,
        "comparisons": comparisons,
        "status": target_rsp["status"],
    }


async def get_data_quality_report(dealer: IpcDealerAsync, logger: logging.Logger) -> Dict[str, Any]:
    telemetry_update, base_rsp = _get_race_table_context(logger)
    if telemetry_update is None:
        return base_rsp

    flags = []
    if telemetry_update.get("race-ended") and not telemetry_update.get("table-entries"):
        flags.append(_quality_flag("session_ended_but_not_ok", "Session ended, but base response is not ok.", "high"))

    for row in telemetry_update.get("table-entries", []):
        driver = row.get("driver-info", {})
        driver_index = driver.get("index")
        lap_info = row.get("lap-info", {})
        delta_info = row.get("delta-info", {})
        if delta_info.get("delta-to-leader-ms") is None:
            flags.append(_quality_flag("null_delta_to_leader", "Delta to leader is unavailable.", "medium", driver))
        if lap_info.get("top-speed-kmph") is None and lap_info.get("speed-trap-record-kmph") is None:
            flags.append(_quality_flag("missing_top_speed_data", "Top-speed data is missing.", "low", driver))

        if driver_index is None:
            continue
        rsp = await _fetch_driver_data(dealer, logger, driver_index)
        if not rsp["status"]["ok"]:
            flags.append(_quality_flag("driver_detail_unavailable", "Driver detail request failed.", "medium", driver))
            continue

        data = rsp["data"]
        penalty_events = [
            event for event in data.get("race-control", [])
            if event.get("message-type") in _PENALTY_EVENT_TYPES
        ]
        classification_penalties = _penalties(data).get("final_classification_penalties", {})
        if penalty_events and not any(value for value in classification_penalties.values()):
            flags.append(_quality_flag(
                "race_control_penalty_no_classification_effect",
                "Race-control contains penalty events, but final classification has no obvious penalty effect.",
                "medium",
                driver,
            ))

        pit_stops = _detect_pit_stops(data)
        for stop in pit_stops:
            if "abnormal_lap_time_spike" in stop.get("reasons", []) and "pit_lane_or_tyre_change_event" not in stop.get("reasons", []):
                flags.append(_quality_flag(
                    "lap_spike_likely_pit_stop",
                    f"Lap {stop['lap_num']} has a pit-stop-like lap spike without an explicit pit event.",
                    "medium",
                    driver,
                ))

    return {
        **base_rsp,
        "ok": True,
        "flags": flags,
        "summary": {
            "flag_count": len(flags),
            "high": sum(1 for flag in flags if flag["severity"] == "high"),
            "medium": sum(1 for flag in flags if flag["severity"] == "medium"),
            "low": sum(1 for flag in flags if flag["severity"] == "low"),
        },
    }


async def get_coach_notes(dealer: IpcDealerAsync, logger: logging.Logger, driver_index: int) -> Dict[str, Any]:
    telemetry_update, base_rsp = _get_race_table_context(logger)
    row = _find_row_by_index(telemetry_update, driver_index) if telemetry_update else None

    rsp = await _fetch_driver_data(dealer, logger, driver_index)
    if not rsp["status"]["ok"]:
        return rsp

    data = rsp["data"]
    pit_stops = _detect_pit_stops(data)
    pace = _build_pace_analysis(data, pit_stops)
    events = _build_event_timeline(data)
    strategy = _build_strategy_summary(data, pit_stops, pace)
    notes = _build_coach_notes(data, row, pace, pit_stops, events, strategy)

    return {
        **base_rsp,
        "ok": True,
        "driver": _driver_identity(data, row),
        "coach_notes": notes,
        "status": rsp["status"],
    }

# -------------------------------------- ANALYSIS HELPERS --------------------------------------------------------------

async def _fetch_driver_data(dealer: IpcDealerAsync, logger: logging.Logger, driver_index: int) -> Dict[str, Any]:
    rsp = await fetch_driver_info(dealer=dealer, logger=logger, driver_index=driver_index)
    if not rsp["status"]["ok"]:
        return rsp
    return {"status": rsp["status"], "data": rsp.get("data") or {}}


def _detect_pit_stops(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    laps = _laps(data)
    events_by_lap = _events_by_lap(data)
    positions = _positions_by_lap(data)
    compounds = _compounds_by_lap(data)
    tyre_ages = _tyre_ages_by_lap(data)
    median_ms = _median_ms([lap["lap_time_ms"] for lap in laps if lap["lap_time_ms"]])
    clean_top_speed = _median_ms([lap["top_speed_kmph"] for lap in laps if lap["top_speed_kmph"]])

    stops: Dict[int, Dict[str, Any]] = {}
    for lap in laps:
        lap_num = lap["lap_num"]
        reasons = []
        confidence = 0.0

        event_types = {event.get("message-type") for event in events_by_lap.get(lap_num, [])}
        if event_types & _PIT_EVENT_TYPES:
            reasons.append("pit_lane_or_tyre_change_event")
            confidence += 0.55

        if median_ms and lap["lap_time_ms"] and lap["lap_time_ms"] >= median_ms * 1.10:
            reasons.append("abnormal_lap_time_spike")
            confidence += 0.25

        prev_age = tyre_ages.get(lap_num - 1)
        curr_age = tyre_ages.get(lap_num)
        if prev_age is not None and curr_age is not None and curr_age < prev_age:
            reasons.append("tyre_age_reset")
            confidence += 0.45

        prev_compound = compounds.get(lap_num - 1)
        curr_compound = compounds.get(lap_num)
        if prev_compound and curr_compound and curr_compound != prev_compound:
            reasons.append("tyre_compound_change")
            confidence += 0.35

        prev_pos = positions.get(lap_num - 1)
        curr_pos = positions.get(lap_num)
        if prev_pos and curr_pos and curr_pos > prev_pos:
            reasons.append("position_loss_sequence")
            confidence += min(0.20, (curr_pos - prev_pos) * 0.05)

        if clean_top_speed and lap["top_speed_kmph"] and lap["top_speed_kmph"] < clean_top_speed * 0.88:
            reasons.append("speed_drop")
            confidence += 0.15

        if reasons and confidence >= 0.45:
            stops[lap_num] = {
                "lap_num": lap_num,
                "lap_time_ms": lap["lap_time_ms"],
                "confidence": round(min(confidence, 1.0), 2),
                "reasons": reasons,
                "compound_before": prev_compound,
                "compound_after": curr_compound,
                "position_before": prev_pos,
                "position_after": curr_pos,
            }

    return list(stops.values())


def _build_pace_analysis(data: Dict[str, Any], pit_stops: List[Dict[str, Any]]) -> Dict[str, Any]:
    laps = _laps(data)
    valid_laps = [lap for lap in laps if lap["lap_valid"] and lap["lap_time_ms"]]
    pit_lap_nums = {stop["lap_num"] for stop in pit_stops}
    safety_car_laps = _safety_car_laps(data)
    clean_laps = [
        lap for lap in valid_laps
        if lap["lap_num"] not in pit_lap_nums and lap["lap_num"] not in safety_car_laps
    ]
    if clean_laps:
        median_clean = statistics.median(lap["lap_time_ms"] for lap in clean_laps)
        clean_laps = [lap for lap in clean_laps if lap["lap_time_ms"] <= median_clean * 1.20]

    valid_times = [lap["lap_time_ms"] for lap in valid_laps]
    clean_times = [lap["lap_time_ms"] for lap in clean_laps]
    fastest = min(valid_laps, key=lambda lap: lap["lap_time_ms"], default=None)

    return {
        "fastest_lap": _lap_summary(fastest),
        "median_lap_ms": _median_ms(valid_times),
        "average_lap_ms": _avg_ms(valid_times),
        "average_excluding_pit_laps_ms": _avg_ms(clean_times),
        "lap_time_consistency_std_dev_ms": _std_dev_ms(clean_times),
        "stint_pace": _stint_pace(data, pit_lap_nums),
        "personal_best_sector_laps": _personal_best_sector_laps(data),
        "invalid_laps_removed": [lap["lap_num"] for lap in laps if not lap["lap_valid"]],
        "pit_laps_removed": sorted(pit_lap_nums),
        "safety_car_laps_removed": sorted(safety_car_laps),
        "clean_lap_count": len(clean_laps),
        "valid_lap_count": len(valid_laps),
    }


def _build_event_timeline(data: Dict[str, Any]) -> Dict[str, Any]:
    grouped: Dict[str, List[Dict[str, Any]]] = {
        "overtakes_made": [],
        "overtakes_lost": [],
        "pit_stop": [],
        "penalty": [],
        "fastest_lap": [],
        "damage": [],
        "safety_car": [],
        "drs_enabled": [],
        "tyre_change": [],
    }
    driver_index = data.get("index")

    for msg in data.get("race-control", []):
        event = _event_payload(msg)
        msg_type = msg.get("message-type")
        if msg_type == "OVERTAKE":
            overtaker = (msg.get("overtaker-info") or {}).get("index")
            key = "overtakes_made" if overtaker == driver_index else "overtakes_lost"
            grouped[key].append(event)
        elif msg_type in {"PITTING", "WING_CHANGE", "DRIVE_THROUGH_SERVED", "STOP_GO_SERVED"}:
            grouped["pit_stop"].append(event)
        elif msg_type == "TYRE_CHANGE":
            grouped["tyre_change"].append(event)
            grouped["pit_stop"].append(event)
        elif msg_type in _PENALTY_EVENT_TYPES:
            grouped["penalty"].append(event)
        elif msg_type == "FASTEST_LAP":
            grouped["fastest_lap"].append(event)
        elif msg_type in _DAMAGE_EVENT_TYPES:
            grouped["damage"].append(event)
        elif msg_type == "SAFETY_CAR":
            grouped["safety_car"].append(event)
        elif msg_type == "DRS_ENABLED":
            grouped["drs_enabled"].append(event)

    key_events = sorted(
        [event for events in grouped.values() for event in events],
        key=lambda event: (event.get("lap_num") is None, event.get("lap_num") or 0, event.get("timestamp") or 0),
    )
    return {"phases": grouped, "key_events": key_events}


def _build_strategy_summary(
    data: Dict[str, Any],
    pit_stops: List[Dict[str, Any]],
    pace: Dict[str, Any],
) -> Dict[str, Any]:
    stints = []
    for stint in _stint_ranges(data, {stop["lap_num"] for stop in pit_stops}):
        laps = [
            lap for lap in _laps(data)
            if stint["start_lap"] <= lap["lap_num"] <= stint["end_lap"]
            and lap["lap_valid"]
            and lap["lap_time_ms"]
            and lap["lap_num"] not in {stop["lap_num"] for stop in pit_stops}
        ]
        compound = _compound_for_stint(data, stint["start_lap"], stint["end_lap"])
        stints.append({
            "stint_number": len(stints) + 1,
            "compound": compound,
            "start_lap": stint["start_lap"],
            "end_lap": stint["end_lap"],
            "laps": max(0, stint["end_lap"] - stint["start_lap"] + 1),
            "avg_pace_ms": _avg_ms([lap["lap_time_ms"] for lap in laps]),
            "tyre_wear": _wear_for_lap_range(data, stint["start_lap"], stint["end_lap"]),
        })

    return {
        "stints": stints,
        "pit_laps": [stop["lap_num"] for stop in pit_stops],
        "undercut_overcut_effect": _undercut_overcut_effect(data, pit_stops),
        "compound_comparison": _compound_comparison(stints),
        "overall_clean_pace_ms": pace.get("average_excluding_pit_laps_ms"),
    }

# -------------------------------------- SMALL HELPERS -----------------------------------------------------------------

def _laps(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    history = data.get("lap-time-history") or data.get("session-history") or {}
    raw_laps = history.get("lap-history-data") or []
    laps = []
    for index, entry in enumerate(raw_laps):
        flags = entry.get("lap-valid-bit-flags", 0xFFFF)
        laps.append({
            "lap_num": index + 1,
            "lap_time_ms": entry.get("lap-time-in-ms"),
            "s1_time_ms": entry.get("sector-1-time-in-ms"),
            "s2_time_ms": entry.get("sector-2-time-in-ms"),
            "s3_time_ms": entry.get("sector-3-time-in-ms"),
            "top_speed_kmph": entry.get("top-speed-kmph"),
            "lap_valid": bool(flags & LapHistoryData.FULL_LAP_VALID_BIT_MASK),
            "s1_valid": bool(flags & LapHistoryData.SECTOR_1_VALID_BIT_MASK),
            "s2_valid": bool(flags & LapHistoryData.SECTOR_2_VALID_BIT_MASK),
            "s3_valid": bool(flags & LapHistoryData.SECTOR_3_VALID_BIT_MASK),
        })
    return laps


def _events_by_lap(data: Dict[str, Any]) -> Dict[int, List[Dict[str, Any]]]:
    ret: Dict[int, List[Dict[str, Any]]] = {}
    for event in data.get("race-control", []):
        lap_num = event.get("lap-number")
        if lap_num is not None:
            ret.setdefault(lap_num, []).append(event)
    return ret


def _positions_by_lap(data: Dict[str, Any]) -> Dict[int, int]:
    return {
        snapshot["lap-number"]: snapshot["track-position"]
        for snapshot in data.get("per-lap-info", [])
        if snapshot.get("lap-number") is not None and snapshot.get("track-position") is not None
    }


def _compounds_by_lap(data: Dict[str, Any]) -> Dict[int, str]:
    ret = {}
    for snapshot in data.get("per-lap-info", []):
        lap_num = snapshot.get("lap-number")
        compound = _compound_from_snapshot(snapshot)
        if lap_num is not None and compound:
            ret[lap_num] = compound
    return ret


def _tyre_ages_by_lap(data: Dict[str, Any]) -> Dict[int, int]:
    ret = {}
    for snapshot in data.get("per-lap-info", []):
        lap_num = snapshot.get("lap-number")
        status = snapshot.get("car-status-data") or {}
        age = status.get("tyres-age-laps")
        if lap_num is not None and age is not None:
            ret[lap_num] = age
    return ret


def _compound_from_snapshot(snapshot: Dict[str, Any]) -> Optional[str]:
    status = snapshot.get("car-status-data") or {}
    if status.get("visual-tyre-compound"):
        return status.get("visual-tyre-compound")
    tyre_set = ((snapshot.get("tyre-set-info") or {}).get("tyre-set") or {})
    return tyre_set.get("visual-tyre-compound")


def _safety_car_laps(data: Dict[str, Any]) -> set[int]:
    ret = set()
    for snapshot in data.get("per-lap-info", []):
        sc_status = snapshot.get("max-safety-car-status")
        if sc_status and sc_status != "NO_SAFETY_CAR" and sc_status in _SAFETY_CAR_STATUSES:
            ret.add(snapshot.get("lap-number"))
    return {lap for lap in ret if lap is not None}


def _stint_ranges(data: Dict[str, Any], pit_laps: set[int]) -> List[Dict[str, int]]:
    laps = [lap["lap_num"] for lap in _laps(data) if lap["lap_time_ms"]]
    if not laps:
        return []
    start = min(laps)
    ranges = []
    for pit_lap in sorted(pit_laps):
        if start <= pit_lap:
            ranges.append({"start_lap": start, "end_lap": pit_lap})
            start = pit_lap + 1
    if start <= max(laps):
        ranges.append({"start_lap": start, "end_lap": max(laps)})
    return ranges


def _stint_pace(data: Dict[str, Any], pit_laps: set[int]) -> List[Dict[str, Any]]:
    ret = []
    for stint in _stint_ranges(data, pit_laps):
        laps = [
            lap for lap in _laps(data)
            if stint["start_lap"] <= lap["lap_num"] <= stint["end_lap"]
            and lap["lap_valid"]
            and lap["lap_time_ms"]
            and lap["lap_num"] not in pit_laps
        ]
        ret.append({
            **stint,
            "compound": _compound_for_stint(data, stint["start_lap"], stint["end_lap"]),
            "average_lap_ms": _avg_ms([lap["lap_time_ms"] for lap in laps]),
            "median_lap_ms": _median_ms([lap["lap_time_ms"] for lap in laps]),
            "lap_count": len(laps),
        })
    return ret


def _compound_for_stint(data: Dict[str, Any], start_lap: int, end_lap: int) -> Optional[str]:
    compounds = [
        compound for lap, compound in _compounds_by_lap(data).items()
        if start_lap <= lap <= end_lap
    ]
    return statistics.mode(compounds) if compounds else None


def _personal_best_sector_laps(data: Dict[str, Any]) -> Dict[str, Optional[int]]:
    history = data.get("lap-time-history") or data.get("session-history") or {}
    return {
        "best_lap_time_lap_num": history.get("best-lap-time-lap-num"),
        "best_sector_1_lap_num": history.get("best-sector-1-lap-num"),
        "best_sector_2_lap_num": history.get("best-sector-2-lap-num"),
        "best_sector_3_lap_num": history.get("best-sector-3-lap-num"),
    }


def _penalties(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "warning_penalty_history": data.get("warning-penalty-history", []),
        "race_control_penalties": [
            _event_payload(event)
            for event in data.get("race-control", [])
            if event.get("message-type") in _PENALTY_EVENT_TYPES
        ],
        "final_classification_penalties": {
            key: value for key, value in (data.get("final-classification") or {}).items()
            if "penalt" in key.lower() or "warning" in key.lower()
        },
    }


def _tyre_wear_summary(data: Dict[str, Any], row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    current = (((row or {}).get("tyre-info") or {}).get("current-wear") or {})
    if current:
        return {
            "current_average_pct": current.get("average"),
            "current": _wear_payload(current),
            "per_stint": [
                {
                    "start_lap": stint.get("start-lap"),
                    "end_lap": stint.get("end-lap"),
                    "compound": (stint.get("tyre-set-data") or {}).get("visual-tyre-compound"),
                    "wear_history": stint.get("tyre-wear-history", []),
                }
                for stint in data.get("tyre-set-history", [])
            ],
        }

    car_damage = data.get("car-damage") or {}
    return {
        "current_average_pct": _avg_ms(car_damage.get("tyres-wear", [])),
        "current": _list_wear_payload(car_damage.get("tyres-wear", [])),
        "per_stint": data.get("tyre-set-history", []),
    }


def _wear_for_lap_range(data: Dict[str, Any], start_lap: int, end_lap: int) -> Dict[str, Any]:
    wear_points = []
    for snapshot in data.get("per-lap-info", []):
        lap_num = snapshot.get("lap-number")
        if lap_num is None or not (start_lap <= lap_num <= end_lap):
            continue
        damage = snapshot.get("car-damage-data") or {}
        wear = _list_wear_payload(damage.get("tyres-wear", []))
        if any(value is not None for value in wear.values()):
            wear_points.append({"lap_num": lap_num, **wear})
    return {
        "start": wear_points[0] if wear_points else None,
        "end": wear_points[-1] if wear_points else None,
    }


def _event_payload(msg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **_get_race_ctrl_msg(msg),
        "type": msg.get("message-type"),
    }


def _driver_identity(data: Dict[str, Any], row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    row_info = ((row or {}).get("driver-info") or {})
    return {
        "driver_index": data.get("index", row_info.get("index")),
        "name": data.get("driver-name", row_info.get("name")),
        "team": data.get("team", row_info.get("team")),
        "is_player": data.get("is-player", row_info.get("is-player")),
    }


def _race_table_row_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    fuel = row.get("fuel-info", {})
    return {
        "fuel_remaining": {
            "fuel_in_tank_kg": fuel.get("fuel-in-tank"),
            "fuel_remaining_laps": fuel.get("fuel-remaining-laps"),
            "surplus_laps_live": fuel.get("surplus-laps-png"),
            "surplus_laps_builtin": fuel.get("surplus-laps-game"),
        },
        "damage": row.get("damage-info", {}),
    }


def _final_position(data: Dict[str, Any], row: Optional[Dict[str, Any]]) -> Optional[int]:
    classification = data.get("final-classification") or {}
    return (
        classification.get("position")
        or classification.get("position-num")
        or data.get("track-position")
        or ((row or {}).get("driver-info") or {}).get("position")
    )


def _start_position(data: Dict[str, Any], row: Optional[Dict[str, Any]]) -> Optional[int]:
    lap_data = data.get("lap-data") or {}
    classification = data.get("final-classification") or {}
    row_info = ((row or {}).get("driver-info") or {})
    return lap_data.get("grid-position") or classification.get("grid-position") or row_info.get("grid-position")


def _position_delta(start_position: Optional[int], final_position: Optional[int]) -> Optional[int]:
    if start_position is None or final_position is None:
        return None
    return start_position - final_position


def _find_row_by_index(telemetry_update: Optional[Dict[str, Any]], driver_index: int) -> Optional[Dict[str, Any]]:
    if telemetry_update is None:
        return None
    for row in telemetry_update.get("table-entries", []):
        if row.get("driver-info", {}).get("index") == driver_index:
            return row
    return None


def _nearest_rival_indices(telemetry_update: Dict[str, Any], driver_index: int) -> List[int]:
    rows = telemetry_update.get("table-entries", [])
    target_pos = None
    for row in rows:
        if row.get("driver-info", {}).get("index") == driver_index:
            target_pos = row.get("driver-info", {}).get("position")
            break
    if target_pos is None:
        return [row.get("driver-info", {}).get("index") for row in rows[:3] if row.get("driver-info", {}).get("index") != driver_index]
    rivals = []
    for row in rows:
        info = row.get("driver-info", {})
        if info.get("index") != driver_index and abs((info.get("position") or 99) - target_pos) <= 2:
            rivals.append(info.get("index"))
    return [index for index in rivals if index is not None]


def _comparison_basis(data: Dict[str, Any], row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    pit_stops = _detect_pit_stops(data)
    pace = _build_pace_analysis(data, pit_stops)
    laps = _laps(data)
    best_lap = pace.get("fastest_lap") or {}
    row_lap = ((row or {}).get("lap-info") or {})
    row_tyre = ((row or {}).get("tyre-info") or {})
    row_fuel = ((row or {}).get("fuel-info") or {})
    row_ers = ((row or {}).get("ers-info") or {})
    return {
        "driver": _driver_identity(data, row),
        "best_lap_ms": best_lap.get("lap_time_ms"),
        "average_clean_pace_ms": pace.get("average_excluding_pit_laps_ms"),
        "best_sectors_ms": _best_sectors(laps),
        "top_speed_kmph": row_lap.get("top-speed-kmph") or row_lap.get("speed-trap-record-kmph") or _max_or_none(lap["top_speed_kmph"] for lap in laps),
        "tyre_wear": row_tyre.get("current-wear", {}),
        "ers_percent": row_ers.get("ers-percent-float"),
        "fuel_at_finish_kg": row_fuel.get("fuel-in-tank"),
    }


def _compare_basis(driver: Dict[str, Any], rival: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "rival": rival["driver"],
        "best_lap_gap_ms": _gap(driver.get("best_lap_ms"), rival.get("best_lap_ms")),
        "average_clean_pace_gap_ms": _gap(driver.get("average_clean_pace_ms"), rival.get("average_clean_pace_ms")),
        "sector_comparison_ms": {
            sector: _gap(driver.get("best_sectors_ms", {}).get(sector), rival.get("best_sectors_ms", {}).get(sector))
            for sector in ("s1", "s2", "s3")
        },
        "top_speed_gap_kmph": _gap(driver.get("top_speed_kmph"), rival.get("top_speed_kmph")),
        "tyre_wear_comparison": {
            "driver": driver.get("tyre_wear"),
            "rival": rival.get("tyre_wear"),
        },
        "ers_fuel_at_finish": {
            "driver": {"ers_percent": driver.get("ers_percent"), "fuel_kg": driver.get("fuel_at_finish_kg")},
            "rival": {"ers_percent": rival.get("ers_percent"), "fuel_kg": rival.get("fuel_at_finish_kg")},
        },
    }


def _build_coach_notes(
    data: Dict[str, Any],
    row: Optional[Dict[str, Any]],
    pace: Dict[str, Any],
    pit_stops: List[Dict[str, Any]],
    events: Dict[str, Any],
    strategy: Dict[str, Any],
) -> Dict[str, str]:
    fastest = pace.get("fastest_lap") or {}
    clean_pace = pace.get("average_excluding_pit_laps_ms")
    std_dev = pace.get("lap_time_consistency_std_dev_ms")
    penalties = _penalties(data)
    damage_events = events.get("phases", {}).get("damage", [])
    overtakes = events.get("phases", {}).get("overtakes_made", [])
    lost = events.get("phases", {}).get("overtakes_lost", [])

    return {
        "what_went_well": _sentence([
            f"Best lap was lap {fastest.get('lap_num')} at {fastest.get('lap_time_ms')} ms" if fastest else None,
            f"{len(overtakes)} overtakes made" if overtakes else None,
        ], "Clean laps and event data did not show a standout strength."),
        "what_cost_time": _sentence([
            f"{len(pit_stops)} pit stop lap(s) removed from clean pace" if pit_stops else None,
            f"{len(lost)} overtakes lost" if lost else None,
            f"{len(damage_events)} damage/collision event(s)" if damage_events else None,
            "Penalty events present" if penalties["race_control_penalties"] else None,
        ], "No obvious major time loss was detected."),
        "where_to_improve": "Focus on reducing lap-time spread." if std_dev and std_dev > 1000 else "Maintain the current consistency trend.",
        "one_lap_pace": f"Fastest lap {fastest.get('lap_time_ms')} ms; clean average {clean_pace} ms.",
        "race_management": f"Strategy used {len(strategy.get('stints', []))} stint(s), pit laps {strategy.get('pit_laps', [])}.",
        "next_race_focus": "Avoid pit-lap/event misclassification and compare clean pace against the nearest rivals.",
    }


def _quality_flag(flag_type: str, message: str, severity: str, driver: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "type": flag_type,
        "message": message,
        "severity": severity,
        "driver": {
            "index": (driver or {}).get("index"),
            "name": (driver or {}).get("name"),
        } if driver else None,
    }


def _undercut_overcut_effect(data: Dict[str, Any], pit_stops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    positions = _positions_by_lap(data)
    effects = []
    for stop in pit_stops:
        lap = stop["lap_num"]
        before = positions.get(lap - 1)
        after = positions.get(lap + 1) or positions.get(lap)
        effects.append({
            "pit_lap": lap,
            "position_before": before,
            "position_after": after,
            "net_positions": _position_delta(before, after),
        })
    return effects


def _compound_comparison(stints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_compound: Dict[str, List[float]] = {}
    for stint in stints:
        compound = stint.get("compound") or "Unknown"
        pace = stint.get("avg_pace_ms")
        if pace is not None:
            by_compound.setdefault(compound, []).append(pace)
    return [{"compound": compound, "avg_pace_ms": _avg_ms(paces)} for compound, paces in by_compound.items()]


def _best_sectors(laps: List[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    return {
        "s1": _min_or_none(lap["s1_time_ms"] for lap in laps if lap["s1_valid"]),
        "s2": _min_or_none(lap["s2_time_ms"] for lap in laps if lap["s2_valid"]),
        "s3": _min_or_none(lap["s3_time_ms"] for lap in laps if lap["s3_valid"]),
    }


def _lap_summary(lap: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not lap:
        return None
    return {
        "lap_num": lap["lap_num"],
        "lap_time_ms": lap["lap_time_ms"],
        "s1_time_ms": lap["s1_time_ms"],
        "s2_time_ms": lap["s2_time_ms"],
        "s3_time_ms": lap["s3_time_ms"],
        "top_speed_kmph": lap["top_speed_kmph"],
    }


def _wear_payload(wear: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fl": wear.get("front-left-wear"),
        "fr": wear.get("front-right-wear"),
        "rl": wear.get("rear-left-wear"),
        "rr": wear.get("rear-right-wear"),
    }


def _list_wear_payload(wear: List[Any]) -> Dict[str, Any]:
    return {
        "fl": wear[2] if len(wear) == 4 else None,
        "fr": wear[3] if len(wear) == 4 else None,
        "rl": wear[0] if len(wear) == 4 else None,
        "rr": wear[1] if len(wear) == 4 else None,
    }


def _avg_ms(values: Iterable[Optional[float]]) -> Optional[float]:
    clean = [value for value in values if value is not None and value > 0]
    return sum(clean) / len(clean) if clean else None


def _median_ms(values: Iterable[Optional[float]]) -> Optional[float]:
    clean = [value for value in values if value is not None and value > 0]
    return statistics.median(clean) if clean else None


def _std_dev_ms(values: Iterable[Optional[float]]) -> Optional[float]:
    clean = [value for value in values if value is not None and value > 0]
    return statistics.pstdev(clean) if len(clean) > 1 else None


def _min_or_none(values: Iterable[Optional[float]]) -> Optional[float]:
    clean = [value for value in values if value is not None and value > 0]
    return min(clean) if clean else None


def _max_or_none(values: Iterable[Optional[float]]) -> Optional[float]:
    clean = [value for value in values if value is not None and value > 0]
    return max(clean) if clean else None


def _gap(driver_value: Optional[float], rival_value: Optional[float]) -> Optional[float]:
    if driver_value is None or rival_value is None:
        return None
    return driver_value - rival_value


def _sentence(parts: List[Optional[str]], fallback: str) -> str:
    clean = [part for part in parts if part]
    return "; ".join(clean) + "." if clean else fallback
