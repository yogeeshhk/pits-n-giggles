# MCP Server

Exposes live F1 telemetry from Pits n' Giggles as MCP tools.

## Transports

| Mode | Transport | When |
|------|-----------|------|
| Standalone / inspector | **stdio** | launched directly (no `--managed` flag) |
| Managed | **HTTP (SSE)** | launched by the Launcher with `--managed` |

## Testing with MCP Inspector

### Prerequisites

- Node.js installed (`node --version` to verify)
- `png_config.json` present in the repo root (run the Launcher once to generate it)
- Poetry environment set up (`poetry install`)

---

### stdio transport

#### Run the inspector

From the repo root:

```bash
npx @modelcontextprotocol/inspector poetry run python -m apps.mcp_server
```

This launches MCP Inspector in your browser and spawns the MCP server as a stdio subprocess. The inspector proxies JSON-RPC over the subprocess's stdin/stdout.

#### Optional flags

```bash
# Enable debug logging (written to png_mcp_stdio.log by default)
npx @modelcontextprotocol/inspector poetry run python -m apps.mcp_server -- --debug

# Custom log file
npx @modelcontextprotocol/inspector poetry run python -m apps.mcp_server -- --log-file my_debug.log

# Custom config file
npx @modelcontextprotocol/inspector poetry run python -m apps.mcp_server -- --config-file path/to/png_config.json
```

> **Note:** Arguments after `--` are passed to the MCP server process, not to the inspector.

#### Logs

stdio-mode logs go to `png_mcp_stdio.log` in the working directory (unless `--log-file` overrides it). Tail this file in a second terminal to watch tool calls in real time:

```bash
# Linux/macOS
tail -f png_mcp_stdio.log

# Windows (PowerShell)
Get-Content png_mcp_stdio.log -Wait
```

---

### HTTP transport

The HTTP server is the transport used when the MCP server is managed by the Launcher (`--managed`). You can also start it manually for testing without the full Launcher stack.

#### Start the server manually

```bash
poetry run python -m apps.mcp_server --managed --debug
```

The `--managed` flag switches the transport to HTTP. The server binds on `127.0.0.1:<mcp_http_port>` (default **4770**, configured in `png_config.json` under `MCP.mcp_http_port`).

> **Note:** `--managed` also tries to notify a parent process via IPC. When run standalone this IPC call is a no-op, so it is safe to ignore any IPC-related log lines.

#### Verify the server is up

```bash
curl http://localhost:4770/test
```

Expected response:

```json
{"message": "Pits n' Giggles MCP Server v<version>"}
```

#### Connect MCP Inspector to the running HTTP server

Open MCP Inspector and point it at the running server's MCP endpoint:

```bash
npx @modelcontextprotocol/inspector
```

When the inspector UI opens, switch the transport to **SSE** and enter:

```
http://localhost:4770/mcp
```

Click **Connect**. The inspector will negotiate the MCP session over SSE and list all available tools.

#### Logs

HTTP-mode logs (when `--managed`) are written as JSONL to stdout, captured by the parent process. When running manually for testing, redirect stdout to a file:

```bash
poetry run python -m apps.mcp_server --managed --debug 2>&1 | tee mcp_http.log
```

---

### What to expect (both transports)

1. The inspector opens at `http://localhost:5173` (default port).
2. Under **Tools**, you will see all registered tools:
   - `get_session_info`
   - `get_f1_setup_guide`
   - `get_race_table`
   - `get_drivers_list`
   - `get_driver_lap_times`
   - `get_session_events_for_driver`
   - `get_player_driver_info`
   - `get_car_damage`
   - `list_saved_sessions`
   - `get_saved_session_summary`
   - `get_saved_session_driver_info`
3. Tools that require a live session return `"available": false` when no telemetry is active. This is expected — no errors will be shown.
4. Tools that hit the core backend (`get_driver_lap_times`, `get_session_events_for_driver`, `get_car_damage`) additionally call the backend REST API on `localhost:<server_port>`. These return `"ok": false` with an appropriate error if the backend is not running.
5. Saved-session tools read JSON files from `Capture.session_dir` in `png_config.json`, matching the save viewer. Use `list_saved_sessions` to find a session slug before requesting a saved summary or saved driver detail.

## Architecture notes

- **`mcp_main.py`** — entry point; parses args, selects transport (`stdio` when unmanaged, `http` when `--managed`).
- **`mcp_server/mcp_server.py`** — `MCPBridge`: owns the `FastMCP` instance, registers tools, runs the server loop.
- **`mcp_server/tools/`** — one file per tool; pure functions that read from `apps.mcp_server.state` or call the backend REST API.
- **`subscriber.py`** — subscribes to the ZeroMQ broker and feeds live telemetry into the shared state store.
- **`state.py`** — in-process key/value store for telemetry snapshots published by the subscriber.


## Lap telemetry

`get_lap_telemetry` returns recorded driving inputs and car telemetry for one
lap. Recording must have been enabled when driving; old summary-only JSON saves
cannot supply missing traces. See [recorder setup](../../docs/lap-telemetry-recorder.md).

Example live request:

```json
{
  "driver_index": 0,
  "lap_num": 12,
  "channels": ["speed_kph", "throttle", "brake", "gear", "tyre_surface_temp"],
  "start_m": 1500,
  "end_m": 2200,
  "max_samples": 200
}
```

For a saved recording, add `session_slug` using a slug returned by
`list_saved_sessions`. Saved queries work without a running telemetry backend.
No arbitrary file paths are accepted.

| Parameter | Behavior |
| --- | --- |
| `driver_index` | Required, 0?23 within that session. |
| `lap_num` | Required, one-based lap number, 1?255. |
| `session_slug` | Optional saved-session slug; omit for live. |
| `channels` | Optional channel/group names; defaults to driving inputs, ERS percentage, aero/overtake states, tyre/brake temperatures and world position. The tool schema lists accepted names. |
| `start_m`, `end_m` | Optional inclusive lap-distance window in metres. |
| `max_samples` | Maximum points per array, 2?2,000; default 200. Other response budgets may reduce this. |

`data` always includes `distance_m`, `session_time_s` and `lap_time_ms`.
Requested scalar channels are parallel arrays. Wheel groups such as
`tyre_surface_temp`, `tyre_inner_temp`, `brake_temp`, `tyre_pressure` and
`surface_type` contain `fl`, `fr`, `rl`, `rr` arrays. `world_position` contains
`x`, `y`, `z` arrays. All arrays use the same selected sample indices. `units`
mirrors this structure. Session time is explicitly in seconds; lap time is in
milliseconds.

The response also includes:

- `coverage`: whole-lap distance coverage, a conservative partial flag and the
  requested distance window. This does not certify a valid or clean lap.
- `quality`: missing-channel names and null counts for the selected window
  before downsampling, source age/staleness summaries, telemetry-sharing state,
  and session-wide capture-loss counters. Valid zero inputs remain zero.
- `downsampling`: original and returned sample counts, requested limit, and the
  selection method. Uniform index selection preserves the first and last sample,
  uses no interpolation and can omit brief events or extrema. Narrow the window
  before drawing precise conclusions about braking points or input hesitation.
- `recording`: identity, committed timeline epoch, recording rate, state and last
  sample session time for the lap. Live responses expose committed disk samples;
  buffered samples appear after a writer checkpoint. `connected=false` means the
  simulator is disconnected even if recorded data remains readable.

Responses enforce 12,000 array cells and a 128 KiB compact JSON payload budget
(in addition to the sample limit). MCP transport framing adds overhead. Queries
reject an excessive candidate-row read rather than loading an unbounded trace.

Unavailable data returns `ok=false`, `available=false`, `data=null`, and an
explicit error. Examples include `recording_disabled`, `telemetry_not_recorded`,
`lap_not_recorded`, `window_empty`, `recording_unavailable` and
`core_server_timeout`. Invalid inputs are rejected at the MCP schema or returned
as `invalid_request` at the shared query boundary.

Live queries run through backend IPC, with disk reads off the packet event loop.
A pending flashback returns `telemetry_pending`; a session/recording/epoch change
during a query returns `recording_changed`. Retry after the recorder catches up.
Saved queries resolve the linked manifest within `Capture.session_dir/telemetry`
and read its latest committed state. Manual saves link to that recording; they
do not freeze a separate copy of its trace. Internal manifests are excluded from
saved-session discovery.

### Validation

```powershell
.venv/Scripts/python.exe -m pytest tests/tests_lap_telemetry_mcp.py tests/tests_lap_telemetry.py tests/tests_session_discovery.py -o addopts='' -q
```

These tests cover real Parquet queries, live and saved calls through FastMCP,
IPC route registration, response limits, source identity, path validation,
flashbacks, partial coverage and unavailable recordings. Lap comparison and
corner/time-loss analysis are separate milestones.
