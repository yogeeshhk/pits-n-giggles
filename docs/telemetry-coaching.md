# Telemetry coaching and race histories

Enable `Capture.telemetry_recording_enabled` and restart the backend before
driving. Full setup capture also requires `Privacy.process_car_setup`.
See [recorder settings](lap-telemetry-recorder.md). These tools use new recordings;
older JSON summaries cannot recover inputs that were never captured.

## MCP tools

Every tool takes a zero-based `driver_index` (0–23). Omit `session_slug` for
the live recording or supply a slug from `list_saved_sessions`.

| Tool | Arguments beyond driver/session | Result |
| --- | --- | --- |
| `get_lap_telemetry` | `lap_num`, optional channels, distance window, `max_samples` | Aligned inputs, temperatures, pressures, wear, wheel speed/slip, gaps, ERS, aero and world position |
| `compare_laps` | `reference_lap`, `comparison_lap`, `max_samples=200` | Distance-aligned elapsed-time delta, disjoint segment losses, driving observations and recorded setup context |
| `get_corner_analysis` | `lap_num` | Mapped or detected corners, braking/minimum speed/gear/exit throttle, temperatures and slip candidates |
| `get_time_loss_analysis` | `lap_num`, optional `reference_lap`, `max_samples=200` | Segment attribution against an explicit or representative same-stint lap |
| `get_race_pace_breakdown` | `start_lap=1`, `end_lap=255` | Lap exclusions, mean/median pace, stint/compound breakdown and observed tyre-age trends |
| `get_driver_telemetry_history` | `history_kind`, lap range, `offset=0`, `limit=50` | Paginated setup/tyre/timing/position/event/session snapshots or lap summaries |
| `get_pit_timing_analysis` | Lap range, optional `reference_lap` | Entry, limiter observations, stationary timer, exit and optional estimated pit loss |
| `get_start_analysis` | `max_samples=200` | Lights-out, player reaction time, launch inputs, slip candidates and position change at the first corner |

Examples:

```json
{"driver_index": 0, "reference_lap": 9, "comparison_lap": 12, "max_samples": 200}
```

```json
{"driver_index": 0, "history_kind": "lap_summary", "start_lap": 9, "end_lap": 12, "limit": 4}
```

For `get_driver_telemetry_history`, kinds are `all`, `setup`, `tyre_sets`,
`lap_timing`, `lap_position`, `event`, `session`, and `lap_summary`.
`all` includes packet snapshots; request `lap_summary` separately for derived
gap/position, ERS, aero, Overtake and wheel-terrain intervals. Follow `next_offset`
to retrieve another page. Snapshot payloads preserve the parser's field names.
Tyre-set snapshots include available/fitted status, wear and usable life, so
strategy consumers can check actual inventory. They do not assume unused tyres.

Additional trace groups: `wheel_speed`, `wheel_slip_ratio`, `wheel_slip_angle`,
`tyre_wear`. Individual channels include `gap_front_ms`, `gap_leader_ms`,
`pit_lane_active`, `pit_lane_time_ms`, `pit_stop_time_ms`, `pit_limiter`, `clutch`,
`fuel_kg`, `tyre_compound`, `tyre_age_laps`, and `front_brake_bias`.
Wheel speed is preserved in game units because the parser specification does
not identify its unit. Slip detection uses the supplied slip ratio directly.

## Interpretation and limits

- Analyses use original recorded samples before response downsampling. Lap
  comparison interpolates lap clocks only between nearby samples at common
  distances. It rejects distance/clock reversals, disjoint coverage and gaps;
  it never extrapolates missing lap boundaries.
- Positive deltas mean the comparison lap was slower. Segment deltas sum to
  `covered_time_delta_s`, the difference accumulated over the shared distance.
  `delta_at_end_s` includes any difference already present at the start of that
  window. Partial coverage is explicit; neither value necessarily equals the
  official complete-lap difference.
- Known tracks use bundled track segments. The fallback groups steering or
  lateral-G activity by lap distance, labels it “Detected corner”, and does
  not assign an official turn number. Brake onset uses 10% input, full throttle
  95%, and wheelspin/lockup candidates use slip ratios above +0.15/below -0.15
  while accelerating/braking. These are heuristics with sampling uncertainty.
- Temperature, wear, gear, ERS and setup differences are observations. They do
  not prove causation or assign independent seconds lost to overlapping causes.
  Low-energy/full-throttle samples are candidates for deployment limitation,
  not proof of clipping. Recorded setup context is the latest earlier snapshot
  with its age, not an interpolated setup.
- Representative reference selection requires at least three other eligible
  same-compound, same-stop-count laps. It selects the lap nearest their median.
  Otherwise pass `reference_lap` explicitly.
- Mean race pace requires three eligible complete laps. Start laps, invalid or
  incomplete laps, pit transitions, yellow/SC/VSC laps, missing flag coverage,
  and robust per-stint outliers are excluded. Timing must contain all three
  sectors and reconcile with the total. Tyre-age trends are observations that
  also reflect fuel, traffic, weather and driving.
- Gap values include both the game's minute and millisecond fields. Sector
  entry and finish values are observations at recorded sample times, not exact
  reconstructed timing-line crossings. Lap Positions packets provide positions
  at the start of each lap, returned with one-based lap numbers.
- Pit entry/exit and limiter activity are sampled transitions. Limiter state
  does not identify a surveyed limiter line. Lane/stationary timer maxima may
  fall short by a sampling interval. Optional pit loss compares lane traversal
  with a clean reference at the same reported distances; pit-lane/racing-line
  distance correspondence is approximate. Without suitable coverage/baseline,
  the estimate is null. Penalties and traffic remain included.
- Wheel speed/slip and game reaction time are player-only. Other drivers retain
  null wheel data. Setup, tyre inventory and restricted status/wear values
  require public telemetry. Nonfinite history values become null. Zero remains
  a valid measurement when the protocol allows it.
- Start analysis needs the lights-out event for its launch window. Game reaction
  time and observed time to 5 km/h are separate quantities. Collision snapshots
  preserve the game's severity classification without inventing impact forces.
- Missing data stays unavailable. Old Parquet chunks remain readable with null
  values for newly introduced columns. A final lap without a following recorded
  lap remains conservatively partial.

## Storage and query bounds

Slow-changing packets and events use compressed `history-*.parquet` chunks under
the same per-recording disk cap. Identical setup, tyre, session, lap-timing and
lap-position snapshots are deduplicated. Histories and traces share the bounded
writer queue; history drops have a separate counter. Rewinds invalidate both.
Control messages retain reserved capacity and a lost rewind fails recording.

Queries run off the packet event loop. An analysis reads one committed manifest
snapshot across all its laps and history. Live responses recheck identity and
timeline after the read. Output is capped at 128 KiB of compact JSON; history
pages shrink to fit, lap analyses bound their curves, and oversized reports ask
for a narrower range. Candidate reads are capped at 100,000 rows per lap/history
and two million lap rows per analysis. State summaries retain at most 100
intervals per channel and report omissions.

## Validation

```powershell
.venv/Scripts/python.exe -m pytest tests/tests_telemetry_analysis.py tests/tests_lap_telemetry.py tests/tests_lap_telemetry_mcp.py -o addopts='' -q
```

Tests cover known delta sign/sums, mapped and detected corners, complete-lap pace,
minute gaps, player-only wheels, privacy, flashback invalidation, storage limits,
older Parquet schemas, pit/start calculations, raw UDP/backend recording, and all
seven analysis tools through FastMCP in both live and saved modes.
Real-game validation still requires a new capture with recording enabled.


A paced synthetic 24-car, 30-second stream at 60 Hz input was also replayed:

| Recorder rate | Retained samples | Dropped / unaligned | Peak queued bytes | Compressed trace + history bytes |
| --- | ---: | ---: | ---: | ---: |
| 20 Hz | 14,400 | 0 | 122,779 | 2,114,355 |
| 60 Hz | 43,200 | 0 | 417,557 | 5,729,612 |

Two-lap analysis completed in approximately 0.6-1.0 seconds in this synthetic
run. This validates the local recording/query path, not real-game delivery or
full-session throughput on other machines.
