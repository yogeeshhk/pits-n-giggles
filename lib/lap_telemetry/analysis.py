"""Distance-aligned coaching computed from committed, full-resolution samples.

Timing attribution partitions distance exactly once. Driving observations are
evidence, not additive causal estimates. Missing inputs never become zeroes.
"""

from bisect import bisect_left
from functools import lru_cache
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .history import read_history
from .query import MAX_READ_ROWS, MAX_RESPONSE_BYTES, resolve_recording, unavailable
from .storage import read_lap


class AnalysisQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["compare_laps", "get_corner_analysis", "get_time_loss_analysis",
                       "get_race_pace_breakdown", "get_driver_telemetry_history",
                       "get_pit_timing_analysis", "get_start_analysis"]
    driver_index: int = Field(ge=0, le=23)
    lap_num: int | None = Field(default=None, ge=1, le=255)
    reference_lap: int | None = Field(default=None, ge=1, le=255)
    comparison_lap: int | None = Field(default=None, ge=1, le=255)
    start_lap: int = Field(default=1, ge=1, le=255)
    end_lap: int = Field(default=255, ge=1, le=255)
    max_samples: int = Field(default=200, ge=2, le=1000)
    history_kind: Literal["all", "setup", "tyre_sets", "lap_timing", "lap_position", "event", "session", "lap_summary"] = "all"
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=50, ge=1, le=200)

    @model_validator(mode="after")
    def check_operation(self):
        if self.end_lap < self.start_lap:
            raise ValueError("Invalid lap range")
        if self.operation == "compare_laps" and (self.reference_lap is None or self.comparison_lap is None):
            raise ValueError("Two lap numbers are required")
        if self.operation in ("get_corner_analysis", "get_time_loss_analysis") and self.lap_num is None:
            raise ValueError("A lap number is required")
        return self


def values(rows, name):
    return [r[name] for r in rows if r.get(name) is not None]


def average(rows, name):
    data = values(rows, name)
    return mean(data) if data else None


def difference(a, b):
    return b - a if a is not None and b is not None else None


class Trace:
    def __init__(self, result):
        self.result = result
        self.rows = []
        for row in result["rows"]:
            if row.get("distance_m") is None or row.get("lap_time_ms") is None or row["distance_m"] < 0:
                continue
            if self.rows:
                previous = self.rows[-1]
                if row["distance_m"] < previous["distance_m"] - 1 or row["lap_time_ms"] < previous["lap_time_ms"]:
                    raise ValueError("Nonmonotonic lap distance or clock")
                if row["distance_m"] <= previous["distance_m"]:
                    continue
            self.rows.append(row)
        self.distances = [r["distance_m"] for r in self.rows]
        self.max_gap_s = max(0.25, 3 / result["recording"]["sample_hz"])

    def clock(self, distance):
        i = bisect_left(self.distances, distance)
        if i < len(self.rows) and self.distances[i] == distance:
            return self.rows[i]["lap_time_ms"] / 1000
        if i == 0 or i == len(self.rows):
            return None
        a, b = self.rows[i - 1:i + 1]
        if b["session_time_s"] - a["session_time_s"] > self.max_gap_s:
            return None
        fraction = (distance - a["distance_m"]) / (b["distance_m"] - a["distance_m"])
        return (a["lap_time_ms"] + fraction * (b["lap_time_ms"] - a["lap_time_ms"])) / 1000

    def window(self, start, end):
        return [r for r in self.rows if start <= r["distance_m"] <= end]


@lru_cache(maxsize=1)
def track_database():
    from lib.track_segment_info.database import TrackSegmentsDatabase
    return TrackSegmentsDatabase(Path(__file__).resolve().parents[2] / "assets" / "track-segments")


def segments(trace):
    """Prefer track assets; otherwise detect turning regions without official T labels."""
    if not trace.rows:
        return []
    track_ids = values(trace.rows, "track_id")
    db = track_database()
    track = int(track_ids[-1]) if track_ids else -1
    mapped = {}
    for row in trace.rows:
        segment = db.get_segment_info(track, row["distance_m"])
        if segment:
            data = segment.model_dump()
            key = (data["start_m"], data["end_m"])
            mapped[key] = {**data, "source": "track_map"}
    if mapped:
        return sorted(mapped.values(), key=lambda item: item["start_m"])
    regions = []
    for row in trace.rows:
        turning = abs(row.get("steering") or 0) >= 0.07 or abs(row.get("g_lateral") or 0) >= 0.4
        if not turning:
            continue
        distance = row["distance_m"]
        if regions and distance - regions[-1][1] <= 60:
            regions[-1][1] = distance
        else:
            regions.append([distance, distance])
    return [{"name": f"Detected corner {i + 1}", "type": "corner", "start_m": start,
             "end_m": end, "source": "steering_or_lateral_g_heuristic"}
            for i, (start, end) in enumerate(regions) if end - start >= 10]


def driving_metrics(rows):
    speed_rows = [r for r in rows if r.get("speed_kph") is not None]
    minimum = min(speed_rows, key=lambda r: r["speed_kph"]) if speed_rows else {}
    braking = [r for r in rows if r.get("brake") is not None and r["brake"] >= 0.1]
    apex = minimum.get("distance_m")
    exit_rows = [r for r in rows if apex is not None and r["distance_m"] >= apex]
    throttle = [r for r in exit_rows if r.get("throttle") is not None]
    full = next((r["distance_m"] for r in throttle if r["throttle"] >= 0.95), None)
    metrics = {
        "brake_onset_m": braking[0]["distance_m"] if braking else None,
        "minimum_speed_kph": minimum.get("speed_kph"), "minimum_speed_distance_m": apex,
        "gear_at_minimum_speed": minimum.get("gear"), "full_throttle_after_apex_m": full,
        "exit_below_90pct_throttle_fraction": mean(r["throttle"] < 0.9 for r in throttle) if throttle else None,
        "mean_ers_percent": average(rows, "ers_percent"),
        "low_energy_full_throttle_candidates": sum((r.get("throttle") or 0) >= 0.95 and r.get("ers_percent") is not None
                                                   and r["ers_percent"] < 1 for r in rows),
        "sample_count": len(rows),
    }
    for prefix in ("tyre_surface_temp", "tyre_inner_temp", "tyre_wear", "brake_temp", "tyre_pressure"):
        metrics[f"mean_{prefix}"] = {w: average(rows, f"{prefix}_{w}") for w in ("fl", "fr", "rl", "rr")}
    for name, condition in (
        ("wheelspin_candidates", lambda r, slip: (r.get("throttle") or 0) > 0.2 and slip > 0.15),
        ("lockup_candidates", lambda r, slip: (r.get("brake") or 0) > 0.1 and slip < -0.15),
    ):
        eligible = [r for r in rows if any(r.get(f"wheel_slip_ratio_{w}") is not None for w in ("fl", "fr", "rl", "rr"))]
        metrics[name] = {"samples": sum(any(r.get(f"wheel_slip_ratio_{w}") is not None
                                             and condition(r, r[f"wheel_slip_ratio_{w}"])
                                             for w in ("fl", "fr", "rl", "rr")) for r in eligible),
                         "observed_samples": len(eligible)}
    return metrics


def corner_analysis(trace):
    corners = []
    for segment in segments(trace):
        if segment["type"] == "straight":
            continue
        # Include braking approach and throttle exit around the mapped corner.
        rows = trace.window(max(trace.distances[0], segment["start_m"] - 120),
                            min(trace.distances[-1], segment["end_m"] + 80))
        corners.append({**segment, "metrics": driving_metrics(rows)})
    return {"corners": corners, "coverage": trace.result["coverage"],
            "method": "Full recorded samples; brake >= 0.1, full throttle >= 0.95, slip candidate threshold +/-0.15.",
            "limitations": ["Sampled onset distances have recording-resolution uncertainty.",
                            "Slip flags are candidates; temperatures and gear differences do not establish causation."]}


def compare(reference, comparison, max_samples):
    if len(reference.rows) < 2 or len(comparison.rows) < 2:
        return None
    start = max(reference.distances[0], comparison.distances[0])
    end = min(reference.distances[-1], comparison.distances[-1])
    if end <= start:
        return None
    # Refuse to bridge telemetry outages even when grid points miss the gap.
    for trace in (reference, comparison):
        for a, b in zip(trace.rows, trace.rows[1:]):
            if a["distance_m"] < end and b["distance_m"] > start and b["session_time_s"] - a["session_time_s"] > trace.max_gap_s:
                return None
    delta = lambda d: difference(reference.clock(d), comparison.clock(d))
    start_delta, end_delta = delta(start), delta(end)
    if start_delta is None or end_delta is None:
        return None
    # Every boundary belongs to exactly one interval, including unmapped straights.
    mapped = segments(reference)
    boundaries = sorted({start, end, *(max(start, min(end, s[k])) for s in mapped for k in ("start_m", "end_m"))})
    partitions = []
    for a, b in zip(boundaries, boundaries[1:]):
        middle = (a + b) / 2
        segment = next((s for s in mapped if s["start_m"] <= middle < s["end_m"]), None)
        da, db = delta(a), delta(b)
        if da is None or db is None:
            return None
        ref_metrics = driving_metrics(reference.window(a, b))
        cmp_metrics = driving_metrics(comparison.window(a, b))
        partitions.append({"name": segment["name"] if segment else "Other / unmapped",
                           "type": segment["type"] if segment else "unmapped", "start_m": a, "end_m": b,
                           "time_delta_s": db - da,
                           "observations": {
                               "braking_earlier_m": difference(cmp_metrics["brake_onset_m"], ref_metrics["brake_onset_m"]),
                               "minimum_speed_delta_kph": difference(ref_metrics["minimum_speed_kph"], cmp_metrics["minimum_speed_kph"]),
                               "exit_throttle_hesitation_fraction_delta": difference(ref_metrics["exit_below_90pct_throttle_fraction"], cmp_metrics["exit_below_90pct_throttle_fraction"]),
                               "gear_at_minimum_speed": {"reference": ref_metrics["gear_at_minimum_speed"], "comparison": cmp_metrics["gear_at_minimum_speed"]},
                               "mean_ers_percent_delta": difference(ref_metrics["mean_ers_percent"], cmp_metrics["mean_ers_percent"]),
                               "mean_rear_surface_temp_delta_c": {w: difference(ref_metrics["mean_tyre_surface_temp"][w], cmp_metrics["mean_tyre_surface_temp"][w]) for w in ("rl", "rr")},
                               "mean_rear_wear_delta_pct": {w: difference(ref_metrics["mean_tyre_wear"][w], cmp_metrics["mean_tyre_wear"][w]) for w in ("rl", "rr")},
                               "wheelspin_candidates": {"reference": ref_metrics["wheelspin_candidates"], "comparison": cmp_metrics["wheelspin_candidates"]},
                               "low_energy_full_throttle_candidates": {"reference": ref_metrics["low_energy_full_throttle_candidates"], "comparison": cmp_metrics["low_energy_full_throttle_candidates"]},
                           }})
    grid = [start + (end - start) * i / (max_samples - 1) for i in range(max_samples)]
    return {"distance_m": grid, "elapsed_delta_s": [delta(d) for d in grid],
            "covered_time_delta_s": end_delta - start_delta, "delta_at_start_s": start_delta,
            "delta_at_end_s": end_delta, "segments": partitions,
            "coverage": {"start_m": start, "end_m": end, "reference": reference.result["coverage"],
                         "comparison": comparison.result["coverage"]},
            "method": "Linear interpolation of lap clocks at common distances; positive means comparison slower.",
            "limitations": ["Covered time delta excludes unrecorded lap boundaries.",
                            "Segment deltas partition time once; observations are not causal time allocations."]}


class RecordingAnalysis:
    def __init__(self, directory, manifest, query):
        self.directory, self.manifest, self.query = directory, manifest, query
        self.history = read_history(directory, manifest, query.driver_index)
        self.read_rows = 0

    def lap(self, number, columns=None):
        result = read_lap(self.directory, self.query.driver_index, number, columns=columns,
                          max_rows=MAX_READ_ROWS, manifest=self.manifest)
        self.read_rows += len(result["rows"])
        if self.read_rows > 2_000_000:
            raise ValueError("Analysis read budget exceeded; narrow lap range")
        return result

    def setup_at(self, session_time):
        records = [r for r in self.history if r["kind"] == "setup" and r["session_time_s"] <= session_time]
        if not records:
            return None
        latest = records[-1]
        return {"session_time_s": latest["session_time_s"], "age_s": session_time - latest["session_time_s"],
                "setup": latest["data"]}

    def lap_numbers(self):
        return sorted({n for c in self.manifest["chunks"] if self.query.driver_index in c["drivers"]
                       for n in c["laps"] if self.query.start_lap <= n <= self.query.end_lap})

    def pace(self):
        timing = {r["lap_num"]: r["data"] for r in self.history if r["kind"] == "lap_timing"}
        laps = []
        columns = ["distance_m", "session_time_s", "lap_time_ms", "lap_invalid", "pit_status", "pit_lane_active",
                   "safety_car_status", "fia_flag", "tyre_compound", "tyre_age_laps", "num_pit_stops", "last_lap_time_ms",
                   "gap_front_ms", "fuel_kg"]
        previous_pit = False
        for number in self.lap_numbers():
            result = self.lap(number, columns)
            rows = result["rows"]
            if not rows:
                continue
            summary = timing.get(number, {})
            sectors = [summary.get(f"sector-{i}-time-in-ms", 0) + 60000 * summary.get(f"sector-{i}-time-minutes", 0) for i in (1, 2, 3)]
            total = summary.get("lap-time-in-ms")
            complete_timing = total and all(sectors) and abs(sum(sectors) - total) <= 5
            reasons = []
            pit = any((r.get("pit_status") or 0) != 0 or r.get("pit_lane_active") for r in rows)
            if number == 1:
                reasons.append("start_lap")
            if not complete_timing or result["coverage"]["partial"]:
                reasons.append("incomplete")
            if not (summary.get("lap-valid-bit-flags", 0) & 1) or any(r.get("lap_invalid") for r in rows):
                reasons.append("invalid")
            if pit or previous_pit or number > 1 and 0 in values(rows, "tyre_age_laps"):
                reasons.append("pit_transition")
            previous_pit = pit
            if any(r.get("safety_car_status") not in (None, 0) or r.get("fia_flag") == 3 for r in rows):
                reasons.append("yellow_or_safety_car")
            if any(r.get("safety_car_status") is None or r.get("fia_flag") not in (0, 1, 2, 3) for r in rows):
                reasons.append("unknown_flag_coverage")
            compounds = values(rows, "tyre_compound")
            ages = values(rows, "tyre_age_laps")
            laps.append({"lap_num": number, "lap_time_s": total / 1000 if complete_timing else None,
                         "compound": compounds[-1] if compounds else None,
                         "tyre_age_laps": ages[-1] if ages else None,
                         "pit_stops": max(values(rows, "num_pit_stops"), default=None),
                         "mean_fuel_kg": average(rows, "fuel_kg"),
                         "traffic_observed": any(0 < x < 1000 for x in values(rows, "gap_front_ms")),
                         "excluded_reasons": reasons})
        stints = []
        for lap in laps:
            key = (lap["compound"], lap["pit_stops"])
            age = lap["tyre_age_laps"]
            if (not stints or stints[-1]["key"] != key or age is not None
                    and stints[-1]["laps"][-1]["tyre_age_laps"] is not None
                    and age < stints[-1]["laps"][-1]["tyre_age_laps"]):
                stints.append({"key": key, "laps": []})
            stints[-1]["laps"].append(lap)
        breakdown = []
        for stint in stints:
            eligible = [lap for lap in stint["laps"] if not lap["excluded_reasons"]]
            if len(eligible) >= 3:
                mid = median(l["lap_time_s"] for l in eligible)
                mad = median(abs(l["lap_time_s"] - mid) for l in eligible)
                for lap in eligible:
                    if abs(lap["lap_time_s"] - mid) > max(2.0, 3 * 1.4826 * mad):
                        lap["excluded_reasons"].append("stint_outlier")
            eligible = [lap for lap in eligible if not lap["excluded_reasons"]]
            times = [lap["lap_time_s"] for lap in eligible]
            trend = None
            ages = [lap["tyre_age_laps"] for lap in eligible]
            if len(times) >= 3 and all(age is not None for age in ages) and len(set(ages)) > 1:
                x, y = mean(ages), mean(times)
                trend = sum((a - x) * (t - y) for a, t in zip(ages, times)) / sum((a - x) ** 2 for a in ages)
            breakdown.append({"compound": stint["key"][0], "start_lap": stint["laps"][0]["lap_num"],
                              "end_lap": stint["laps"][-1]["lap_num"], "eligible_samples": len(times),
                              "mean_pace_s": mean(times) if len(times) >= 3 else None,
                              "median_pace_s": median(times) if times else None,
                              "observed_age_trend_s_per_lap": trend})
        eligible = [lap for lap in laps if not lap["excluded_reasons"]]
        times = [lap["lap_time_s"] for lap in eligible]
        return {"laps": laps, "stints": breakdown, "eligible_samples": len(times),
                "mean_race_pace_s": mean(times) if len(times) >= 3 else None,
                "median_representative_pace_s": median(times) if times else None,
                "limitations": ["Observed age trends also include fuel, traffic, weather and driving changes.",
                                "Three eligible laps are required for mean race pace; missing flag data excludes a lap."]}

    def lap_summaries(self):
        numbers = self.lap_numbers()
        selected = numbers[self.query.offset:self.query.offset + self.query.limit]
        summaries = []
        for number in selected:
            result = self.lap(number)
            rows = result["rows"]
            if not rows:
                continue
            sector_rows = []
            previous = None
            for row in rows:
                if row.get("sector") != previous:
                    sector_rows.append({k: row.get(k) for k in ("sector", "session_time_s", "distance_m", "position", "gap_front_ms", "gap_leader_ms")})
                    previous = row.get("sector")
            modes = {}
            for channel in ("ers_deploy_mode", "overtake_active", "active_aero_mode", "surface_type_fl", "surface_type_fr", "surface_type_rl", "surface_type_rr"):
                runs = []
                for row in rows:
                    value = row.get(channel)
                    if not runs or value != runs[-1]["value"]:
                        runs.append({"value": value, "start_m": row["distance_m"], "end_m": row["distance_m"],
                                     "start_session_time_s": row["session_time_s"], "end_session_time_s": row["session_time_s"]})
                    else:
                        runs[-1].update(end_m=row["distance_m"], end_session_time_s=row["session_time_s"])
                modes[channel] = {"intervals": runs[:100], "omitted_intervals": max(0, len(runs) - 100)}
            ers = {}
            for channel in ("ers_percent", "ers_store_j", "ers_deployed_j", "ers_harvested_mguk_j", "ers_harvested_mguh_j", "ers_harvest_limit_j"):
                observed = values(rows, channel)
                ers[channel] = {"start": observed[0] if observed else None, "end": observed[-1] if observed else None,
                                "min": min(observed) if observed else None, "max": max(observed) if observed else None}
            summaries.append({"lap_num": number, "coverage": result["coverage"], "sector_entry_observations": sector_rows,
                              "finish_observation": {k: rows[-1].get(k) for k in ("session_time_s", "distance_m", "position", "gap_front_ms", "gap_leader_ms")},
                              "ers": ers, "state_intervals": modes})
            if len(json.dumps(summaries).encode()) > MAX_RESPONSE_BYTES - 8192:
                summaries.pop()
                break
        return {"records": summaries, "offset": self.query.offset, "total_records": len(numbers),
                "next_offset": self.query.offset + len(summaries) if self.query.offset + len(summaries) < len(numbers) else None,
                "limitations": ["Sector entry and finish gaps are packet observations, not reconstructed timing-line crossings.",
                                "ERS maxima are observed counters; missing lap boundaries can hide final energy totals.",
                                "State intervals with null values indicate unavailable data; at most 100 intervals per channel."]}

    def histories(self):
        if self.query.history_kind == "lap_summary":
            return self.lap_summaries()
        rows = [r for r in self.history if (self.query.history_kind == "all" or r["kind"] == self.query.history_kind)
                and (not r["lap_num"] or self.query.start_lap <= r["lap_num"] <= self.query.end_lap)]
        # Keep session-wide events for context but restrict collisions to the requested car.
        rows = [r for r in rows if r["kind"] != "event" or self._relevant_event(r)]
        page = rows[self.query.offset:self.query.offset + self.query.limit]
        while page and len(json.dumps(page).encode()) > MAX_RESPONSE_BYTES - 8192:
            page.pop()
        return {"records": page, "total_records": len(rows), "offset": self.query.offset,
                "next_offset": self.query.offset + len(page) if self.query.offset + len(page) < len(rows) else None,
                "available_kinds": sorted({r["kind"] for r in self.history}),
                "limitations": ["Snapshots retain their original packet fields and units.",
                                "Lap positions refer to the start of the one-based lap; gaps remain sampled trace channels.",
                                "Setup capture requires Privacy.process_car_setup and public telemetry; tyre sets require public telemetry."]}

    def _relevant_event(self, row):
        details = row["data"].get("event-details") or {}
        vehicles = [v for k, v in details.items() if k in ("vehicle-index", "vehicle-1-index", "vehicle-2-index")]
        return not vehicles or self.query.driver_index in vehicles

    def pits(self):
        rows = []
        for lap in self.lap_numbers():
            rows.extend(self.lap(lap, ["lap_num", "session_time_s", "distance_m", "pit_lane_active", "pit_lane_time_ms",
                                       "pit_stop_time_ms", "pit_limiter", "pit_status", "speed_kph"]) ["rows"])
        baseline = Trace(self.lap(self.query.reference_lap)) if self.query.reference_lap else None
        if baseline and (baseline.result["coverage"]["partial"] or any(
                r.get("lap_invalid") is not False or r.get("pit_status") != 0 for r in baseline.rows)):
            baseline = None
        stops = []
        current: dict | None = None
        previous = None
        for row in rows:
            active = row.get("pit_lane_active")
            if active is None:
                previous = row
                continue
            if active:
                if current is None:
                    current = {"lap_num": row["lap_num"], "entry_distance_m": row["distance_m"], "entry_session_time_s": row["session_time_s"],
                               "entry_observed": previous is not None and previous.get("pit_lane_active") is False
                                                 and row["session_time_s"] - previous["session_time_s"] <= max(0.25, 3 / self.manifest["sample_hz"]),
                               "samples": []}
                current["samples"].append(row)
            elif current:
                current.update(exit_session_time_s=row["session_time_s"], exit_distance_m=row["distance_m"],
                               exit_lap_num=row["lap_num"])
                stops.append(current)
                current = None
            previous = row
        if current:
            stops.append(current)
        output = []
        for stop in stops:
            samples = stop.pop("samples")
            limiter = [r["session_time_s"] for r in samples if r.get("pit_limiter")]
            measured = max(values(samples, "pit_lane_time_ms"), default=None)
            stationary = max(values(samples, "pit_stop_time_ms"), default=None)
            stopped = [r["session_time_s"] for r in samples if r.get("speed_kph") is not None and r["speed_kph"] < 1]
            maximum_gap = max(0.25, 3 / self.manifest["sample_hz"])
            exit_gap = stop.get("exit_session_time_s", samples[-1]["session_time_s"]) - samples[-1]["session_time_s"]
            has_gap = exit_gap > maximum_gap or any(
                b["session_time_s"] - a["session_time_s"] > maximum_gap
                for a, b in zip(samples, samples[1:]))
            estimated_loss = None
            if baseline and not has_gap and stop["entry_observed"] and "exit_session_time_s" in stop:
                entry_clock = baseline.clock(stop["entry_distance_m"])
                exit_clock = baseline.clock(stop["exit_distance_m"])
                duration = stop["exit_session_time_s"] - stop["entry_session_time_s"]
                if entry_clock is not None and exit_clock is not None:
                    racing_duration = exit_clock - entry_clock
                    if stop["exit_lap_num"] == stop["lap_num"] + 1:
                        timing = next((h["data"] for h in reversed(self.history)
                                       if h["kind"] == "lap_timing" and h["lap_num"] == self.query.reference_lap), {})
                        lap_time = timing.get("lap-time-in-ms")
                        sectors = [timing.get(f"sector-{i}-time-in-ms", 0) + 60000 * timing.get(f"sector-{i}-time-minutes", 0) for i in (1, 2, 3)]
                        if not lap_time or not all(sectors) or abs(sum(sectors) - lap_time) > 5:
                            lap_time = None
                        racing_duration = racing_duration + lap_time / 1000 if lap_time else None
                    elif stop["exit_lap_num"] != stop["lap_num"]:
                        racing_duration = None
                    if racing_duration is not None and racing_duration > 0:
                        estimated_loss = duration - racing_duration
            output.append({**stop, "observed_lane_duration_s": measured / 1000 if measured is not None else None,
                           "observed_stationary_time_s": stationary / 1000 if stationary is not None else None,
                           "first_limiter_on_session_time_s": limiter[0] if limiter else None,
                           "last_limiter_on_session_time_s": limiter[-1] if limiter else None,
                           "stationary_first_observed_session_time_s": stopped[0] if stopped else None,
                           "stationary_last_observed_session_time_s": stopped[-1] if stopped else None,
                           "entry_to_stationary_s": stopped[0] - stop["entry_session_time_s"] if stopped else None,
                           "stationary_to_exit_s": stop["exit_session_time_s"] - stopped[-1] if stopped and "exit_session_time_s" in stop else None,
                           "partial": has_gap or not stop["entry_observed"] or "exit_session_time_s" not in stop,
                           "estimated_total_pit_loss_s": estimated_loss, "reference_lap": self.query.reference_lap})
        return {"stops": output, "limitations": [
            "Entry/exit and limiter transitions are sampled observations, not surveyed limiter lines.",
            "Lane and stationary timers report maxima observed before reset, potentially short by a sample interval.",
            "Estimated pit loss compares elapsed lane traversal with reference-lap clocks at the observed entry/exit distances.",
            "Distance correspondence between pit lane and racing line is approximate; penalties and traffic remain included."]}

    def start(self):
        result = self.lap(1)
        trace = Trace(result)
        events = [r for r in self.history if r["kind"] == "event" and r["data"].get("event-string-code") == "LGOT"]
        lights = events[0]["session_time_s"] if events else None
        rows = [r for r in result["rows"] if lights is not None and lights <= r["session_time_s"] <= lights + 10]
        reaction = next((r["data"].get("reaction_time_s") for r in reversed(self.history)
                         if r["kind"] == "session" and r["data"].get("player_index") == self.query.driver_index
                         and (r["data"].get("reaction_time_s") or 0) > 0), None)
        moving = next((r for r in rows if (r.get("speed_kph") or 0) >= 5), None)
        first_corner = next((s for s in segments(trace) if s["type"] != "straight"), None)
        before = [r for r in result["rows"] if first_corner is not None and r["distance_m"] <= first_corner["end_m"]]
        covered_start = (rows and rows[0]["session_time_s"] - lights <= 0.25
                         and first_corner is not None and trace.distances[-1] >= first_corner["end_m"])
        positions = values(before, "position") if covered_start else []
        return {"lights_out_session_time_s": lights, "game_reaction_time_s": reaction,
                "observed_time_to_5kph_s": moving["session_time_s"] - lights if moving else None,
                "positions_gained_by_first_corner": positions[0] - positions[-1] if positions else None,
                "first_corner": first_corner, "launch_metrics": driving_metrics(rows),
                "launch_trace": [{k: r.get(k) for k in ("session_time_s", "speed_kph", "throttle", "clutch", "gear")}
                                 for r in rows[::max(1, math.ceil(len(rows) / self.query.max_samples))]],
                "limitations": ["Reaction time is game-reported and player-only; acceleration delay is a separate observation.",
                                "Lights-out event and lap-one coverage are required for a launch trace."]}


def analyze_reference(session_dir, reference, query, logger, *, expected_epoch=None):
    """One immutable manifest snapshot across all laps and history in a query."""
    try:
        directory = resolve_recording(session_dir, reference)
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if str(manifest.get("session_uid")) != str(reference["session_uid"]):
            return unavailable("recording_session_mismatch")
        if manifest.get("schema_version") != reference["schema_version"]:
            return unavailable("recording_unreadable")
        if manifest.get("state") == "failed":
            return unavailable("recording_failed")
        epoch = max((r["epoch"] for r in manifest["rewinds"]), default=0)
        if expected_epoch is not None and epoch != expected_epoch:
            return unavailable("telemetry_pending")
        reader = RecordingAnalysis(directory, manifest, query)
        if query.operation == "get_race_pace_breakdown":
            data = reader.pace()
        elif query.operation == "get_driver_telemetry_history":
            data = reader.histories()
        elif query.operation == "get_pit_timing_analysis":
            data = reader.pits()
        elif query.operation == "get_start_analysis":
            data = reader.start()
        elif query.operation == "get_corner_analysis":
            trace = Trace(reader.lap(query.lap_num))
            if not trace.rows:
                return unavailable("lap_not_recorded")
            data = corner_analysis(trace)
            data["setup_at_lap_start"] = reader.setup_at(trace.rows[0]["session_time_s"])
        else:
            comparison_lap = query.comparison_lap if query.operation == "compare_laps" else query.lap_num
            reference_lap = query.reference_lap
            automatic = reference_lap is None
            if automatic:
                pace = reader.pace()
                target = next((lap for lap in pace["laps"] if lap["lap_num"] == comparison_lap), None)
                candidates = [lap for lap in pace["laps"] if not lap["excluded_reasons"] and lap["lap_num"] != comparison_lap
                              and target and lap["compound"] is not None and lap["compound"] == target["compound"]
                              and lap["pit_stops"] == target["pit_stops"]]
                if len(candidates) < 3:
                    return unavailable("reference_required", detail="Provide reference_lap; three clean same-stint laps are needed for automatic selection.")
                center = median(lap["lap_time_s"] for lap in candidates)
                reference_lap = min(candidates, key=lambda lap: abs(lap["lap_time_s"] - center))["lap_num"]
            ref_trace, cmp_trace = Trace(reader.lap(reference_lap)), Trace(reader.lap(comparison_lap))
            data = compare(ref_trace, cmp_trace, query.max_samples)
            if data is None:
                return unavailable("insufficient_contiguous_telemetry")
            ref_setup = reader.setup_at(ref_trace.rows[0]["session_time_s"])
            cmp_setup = reader.setup_at(cmp_trace.rows[0]["session_time_s"])
            data["setup_context"] = {"reference": ref_setup, "comparison": cmp_setup}
            data.update(reference_lap=reference_lap, comparison_lap=comparison_lap,
                        reference_selection="same_stint_median" if automatic else "explicit")
        result = {"ok": True, "available": True, "error": None, "data": data,
                  "driver_index": query.driver_index, "session_uid": str(reference["session_uid"]),
                  "recording": {"epoch": epoch, "state": manifest["state"], "committed_only": True, "flashback_count": len(manifest["rewinds"])},
                  "quality": {"capture_counters": manifest["counters"], "discarded_samples": manifest["discarded_samples"],
                              "discarded_history": manifest.get("discarded_history", 0)}}
        if len(json.dumps(result, allow_nan=False, separators=(",", ":")).encode()) > MAX_RESPONSE_BYTES - 2048:
            return unavailable("response_limit_exceeded", detail="Narrow the lap range or reduce the page size.")
        return result
    except FileNotFoundError:
        return unavailable("recording_unavailable")
    except (ValueError, KeyError, TypeError):
        logger.exception("Telemetry analysis rejected incomplete or unreadable data")
        return unavailable("analysis_unavailable")
    except Exception:
        logger.exception("Telemetry analysis failed")
        return unavailable("analysis_unavailable")
