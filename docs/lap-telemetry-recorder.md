# Lap telemetry recorder

The backend can record lap traces to compressed Parquet files. The
`get_lap_telemetry` MCP tool reads live and saved recordings with bounded,
aligned responses; see [the MCP guide](../apps/mcp_server/README.md#lap-telemetry).

## Enable recording

Add these fields to `Capture` in `png_config.json`, then restart the backend:

```json
{
  "telemetry_recording_enabled": true,
  "telemetry_sample_hz": 20,
  "telemetry_buffer_mib": 64,
  "telemetry_session_limit_mib": 1024
}
```

Recording defaults to disabled. The sample rate accepts 1–60 Hz and cannot
exceed the received rate. The buffer setting covers serialized queued and
in-flight messages; it is not a limit on total process memory. The writer also
holds at most approximately 2,048 decoded trace rows plus a bounded history batch, Arrow conversion buffers, and
the session manifest. A single compressed chunk may temporarily occupy disk
space beyond the retained recording budget while its size is checked.

Each recording is stored under:

```text
<Capture.session_dir>/telemetry/<session-uid>-<unique-id>/
  manifest.json
  chunk-000000.parquet
  chunk-000001.parquet
```

The manifest path in saved session JSON's `telemetry-recording` field is relative
to `Capture.session_dir`. Manual, final-classification and just-in-case saves
include the reference when recording is enabled. Existing session files and
viewers remain usable. Older JSON saves cannot supply traces that were never
recorded. Recording is independent of the summary autosave switches.

## Recorded data

- Session UID, driver index, lap number, session time, lap time, lap distance,
  ordinary and overall frame identifiers, position, sector, validity and pit status.
- Speed, throttle, brake, steering, gear and RPM.
- Surface/inner tyre temperatures, pressures, brake temperatures and terrain
  per wheel, explicitly named `fl`, `fr`, `rl`, `rr`.
- ERS store/percentage, deployment mode, per-lap deployment/harvest counters,
  and the 2026 harvest limit when available.
- World XYZ position, lateral/longitudinal/vertical G and yaw.
- Active Aero and Overtake mode, availability and activation distances.

Columns and units are defined in `lib/lap_telemetry/channels.py`. Booleans and
enumerations retain their types. Missing/nonfinite values are null; valid zeros
are preserved. Restricted ERS status fields are null unless participant data
confirms public telemetry. Telemetry 2 remains null when no such packet arrives.
Motion Ex wheel speed/slip (player-only), clutch, tyre wear, minute-aware gaps,
pit timers, fuel and tyre state are also recorded. Changed setup/tyre/session/
lap-position/lap-timing snapshots and events use separate history Parquet chunks.
See [coaching and history tools](telemetry-coaching.md).

## Alignment and lifecycle

The assembler finalizes a frame when the next relevant frame arrives. Lap and
Car Telemetry packets must agree on frame identifiers and session timestamp.
An unmatched frame is discarded and counted rather than assigned to a potentially
incorrect lap. Other channels may carry a source value up to 250 ms old (2.5 seconds for the
slower Session and Car Damage packets); every
group records its source timestamp, frame ID, age and stale flag. Stale values
are null. Sampling uses time buckets with a 10% interval tolerance for the
game's float32 timestamps. Inputs are never interpolated.

The recorder receives parsed packets after the existing frame gate. Packet loss
and packets rejected by that gate cannot be recovered. Duplicate same-frame
packet types are also ignored by the assembler. Ordinary packet reordering does
not create a rewind. Flashback events and backwards session time start a new
timeline epoch, clear source caches and reset sampling. A persisted rewind
invalidates every older epoch at and after the target session time, including
rows already written to disk. Nested rewinds apply cumulatively.

UID changes, backend session clears (including formation-lap clears), and clean
shutdown flush the current recording. A fresh recording directory prevents two
captures of the same game UID from overwriting each other. A session-ending lap
without a following boundary remains conservatively marked partial.

## Resource limits and failures

The packet callback performs no disk I/O. A dedicated bounded queue feeds one
sequential background writer using `asyncio.to_thread`. This queue is separate
from the shared inter-task communicator so it can enforce a byte budget and
nonblocking admission without changing other subsystems' queue policies.

- Sample/history queue overflow drops incoming rows and counts each kind separately.
- Reserved command capacity protects session changes and rewinds. Exhausting
  even that capacity stops recording with an explicit failure.
- Hitting the per-recording disk budget discards the pending chunk and stops
  retaining new samples. Previously committed chunks remain readable.
- Storage/assembly errors stop recording and appear in backend stats and logs.
  The writer attempts to mark the manifest failed; a filesystem failure can also
  prevent that final status write.
- Backend stats expose `lap-telemetry`, including state, error, queue bytes,
  peak queue bytes, lost/unaligned sample counts and the manifest reference.

Chunks become visible only after writing and renaming; the manifest is replaced
atomically. A crash may leave an unreferenced chunk or temporary file. Readers
ignore these. An active or interrupted recording contains only its last
committed snapshot; an uncommitted flashback or sample is not yet reflected.
There is no automatic deletion of older sessions. The disk limit applies per
recording, not across the entire archive.

## Internal reader

```python
from lib.lap_telemetry.storage import read_lap

lap = read_lap(
    "data/telemetry/<recording-id>",
    driver_index=0,
    lap_num=12,
    columns=["distance_m", "session_time_s", "speed_kph", "throttle", "brake"],
    start_m=1500,
    end_m=2200,
)
```

The reader selects matching driver/lap chunks and uses Parquet filters, applies
rewind invalidations, then projects the requested columns and distance window.
It returns rows, units, recording state, capture-loss counters and coverage.
Coverage describes the whole lap before window selection. The partial flag is
conservative: it considers start coverage, a following lap, gaps in sample times,
and session-wide loss counters. It does not certify lap validity or clean pace.
The result also includes recording identity, the latest committed timeline epoch,
recording frequency and the last sample time in the requested lap. Failed
recordings and unknown schema versions are rejected.

The MCP layer adds sample, cell and byte limits with explicit downsampling
metadata. This internal reader intentionally preserves recorded samples. Its
optional `max_rows` argument bounds the number of candidate rows loaded across
the requested and following lap; MCP queries use a 100,000-row limit.

## Validation

```powershell
.venv/Scripts/python.exe -m pytest tests/tests_lap_telemetry.py tests/tests_config tests/tests_frame_gate.py tests/f1_types/tests_packet_6_car_telemetry_data.py tests/f1_types/tests_packet_16_car_telemetry2_data.py -o addopts='' -q
```

Tests exercise binary packets through backend callbacks, ordering, lap
boundaries, stale/missing/restricted channels, wheel ordering, float32 timestamps
after an hour, flashbacks, session changes, queue/disk limits and writer failures.

A paced synthetic workload of 24 cars at 60 Hz input recorded all 14,400 expected
samples at 20 Hz and all 43,200 at 60 Hz over 30 seconds each, without queue drops.
Peak serialized queue usage was under 160 KB in that run. This establishes a
local synthetic throughput check, not a real-session fidelity guarantee.

PyArrow was also validated with an isolated PyInstaller Windows executable that
wrote Zstandard-compressed Parquet and read a lap back. The full application
release bundle and real game captures still require integration validation.
