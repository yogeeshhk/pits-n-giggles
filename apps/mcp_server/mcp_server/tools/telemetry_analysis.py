"""Public coaching tools sharing one bounded recorder query service."""

from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from apps.mcp_server.mcp_server.tools.get_lap_telemetry import LAP_TELEMETRY_OUTPUT_SCHEMA, get_telemetry_analysis

Driver = Annotated[int, Field(ge=0, le=23, strict=True)]
Lap = Annotated[int, Field(ge=1, le=255, strict=True)]
Slug = Annotated[str | None, Field(min_length=1, max_length=256)]
Samples = Annotated[int, Field(ge=2, le=1000, strict=True)]


def register_analysis_tools(bridge):
    def tool(name, description):
        return bridge._tool(name=name, description=description +
                            " Omit session_slug for live, or use a saved-session slug. Recording must have been enabled. "
                            "Uses committed full-resolution samples; returns explicit missing-data errors and coverage.",
                            tags={"telemetry", "coaching", "saved"}, output_schema=LAP_TELEMETRY_OUTPUT_SCHEMA,
                            annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))

    async def request(operation, session_slug, **kwargs):
        return await get_telemetry_analysis(bridge.dealer, bridge.logger, bridge.session_dir, bridge.version,
                                            session_slug=session_slug, operation=operation, **kwargs)

    @tool("compare_laps", "Compare two laps from one driver at common distances. Positive delta means comparison slower. "
          "Returns disjoint segment losses, braking/minimum-speed/gear/throttle/ERS observations and a bounded delta curve.")
    async def compare_laps(driver_index: Driver, reference_lap: Lap, comparison_lap: Lap,
                           session_slug: Slug = None, max_samples: Samples = 200) -> dict:
        return await request("compare_laps", session_slug, driver_index=driver_index, reference_lap=reference_lap,
                             comparison_lap=comparison_lap, max_samples=max_samples)

    @tool("get_corner_analysis", "Find mapped corners or automatically detect turning regions. Reports braking, minimum speed, "
          "exit throttle, tyre/brake temperature, pressures and player-only wheelspin/lockup candidates.")
    async def get_corner_analysis(driver_index: Driver, lap_num: Lap, session_slug: Slug = None) -> dict:
        return await request("get_corner_analysis", session_slug, driver_index=driver_index, lap_num=lap_num)

    @tool("get_time_loss_analysis", "Partition covered time loss into nonoverlapping distance segments. Explicit reference_lap is "
          "recommended; otherwise selects a median representative from at least three clean same-stint laps. "
          "Driving observations are evidence, not proven causal allocations.")
    async def get_time_loss_analysis(driver_index: Driver, lap_num: Lap,
                                    reference_lap: Annotated[int | None, Field(ge=1, le=255, strict=True)] = None,
                                    session_slug: Slug = None, max_samples: Samples = 200) -> dict:
        return await request("get_time_loss_analysis", session_slug, driver_index=driver_index, lap_num=lap_num,
                             reference_lap=reference_lap, max_samples=max_samples)

    @tool("get_race_pace_breakdown", "Summarize clean race pace and compound/stint trends. Excludes incomplete/invalid, start, "
          "pit-transition, yellow/SC/VSC and per-stint outlier laps. Requires three eligible laps for mean race pace.")
    async def get_race_pace_breakdown(driver_index: Driver, session_slug: Slug = None,
                                     start_lap: Lap = 1, end_lap: Lap = 255) -> dict:
        return await request("get_race_pace_breakdown", session_slug, driver_index=driver_index,
                             start_lap=start_lap, end_lap=end_lap)

    @tool("get_driver_telemetry_history", "Page through full setup changes, available/used tyre sets, exact lap/sector timing, "
          "lap-start positions, collision severity and session reaction-time history. lap_summary returns sampled "
          "sector gap/position, ERS/aero/overtake and terrain summaries. Offsets support bounded responses.")
    async def get_driver_telemetry_history(
        driver_index: Driver, session_slug: Slug = None,
        history_kind: Literal["all", "setup", "tyre_sets", "lap_timing", "lap_position", "event", "session", "lap_summary"] = "all",
        start_lap: Lap = 1, end_lap: Lap = 255,
        offset: Annotated[int, Field(ge=0, le=100000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=200, strict=True)] = 50,
    ) -> dict:
        return await request("get_driver_telemetry_history", session_slug, driver_index=driver_index,
                             history_kind=history_kind, start_lap=start_lap, end_lap=end_lap, offset=offset, limit=limit)

    @tool("get_pit_timing_analysis", "Separate sampled pit entry, limiter activity, stationary timer and exit. "
          "Optional reference_lap estimates racing time lost over the same distance; timings carry coverage limits.")
    async def get_pit_timing_analysis(driver_index: Driver, session_slug: Slug = None,
                                     start_lap: Lap = 1, end_lap: Lap = 255,
                                     reference_lap: Annotated[int | None, Field(ge=1, le=255, strict=True)] = None) -> dict:
        return await request("get_pit_timing_analysis", session_slug, driver_index=driver_index,
                             start_lap=start_lap, end_lap=end_lap, reference_lap=reference_lap)

    @tool("get_start_analysis", "Analyze lights-out, player reaction time, clutch/throttle/gear launch trace, "
          "wheelspin candidates and position change by the first corner. Missing lights-out remains unavailable.")
    async def get_start_analysis(driver_index: Driver, session_slug: Slug = None,
                                max_samples: Samples = 200) -> dict:
        return await request("get_start_analysis", session_slug, driver_index=driver_index, max_samples=max_samples)
