"""Single-writer immutable Parquet chunks with an atomic manifest."""

import json
from pathlib import Path

from .channels import SCHEMA_VERSION, UNITS, arrow_schema


class SessionStore:
    """Only accessed by the recorder worker. Each instance owns a unique directory."""

    def __init__(self, directory, metadata, disk_limit_bytes, chunk_rows=2048):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.disk_limit = disk_limit_bytes
        self.chunk_rows = chunk_rows
        self.rows = []
        self.schema = arrow_schema()
        self.manifest = {
            "schema_version": SCHEMA_VERSION, **metadata, "units": UNITS,
            "state": "recording", "chunks": [], "rewinds": [],
            "parquet_bytes": 0, "written_samples": 0, "discarded_samples": 0,
            "counters": {},
        }
        self.checkpoint()

    def append(self, rows):
        if self.manifest["state"] != "recording":
            self.manifest["discarded_samples"] += len(rows)
            return
        self.rows.extend(rows)
        if len(self.rows) >= self.chunk_rows:
            self.flush()

    def rewind(self, event):
        # Commit invalidation before subsequent samples become visible. Old
        # chunks remain immutable; readers apply all later rewind cutoffs.
        self.flush()
        self.manifest["rewinds"].append(event)
        self.checkpoint()

    def flush(self):
        if not self.rows:
            return
        import pyarrow as pa
        import pyarrow.parquet as pq

        rows, self.rows = self.rows, []
        rows.sort(key=lambda row: (row["driver_index"], row["lap_num"], row["session_time_s"]))
        table = pa.Table.from_pylist(rows, schema=self.schema)
        name = f"chunk-{len(self.manifest['chunks']):06d}.parquet"
        temporary = self.directory / (name + ".tmp")
        try:
            pq.write_table(table, temporary, compression="zstd", row_group_size=256)
            size = temporary.stat().st_size
            metadata_size = len(json.dumps(self.manifest).encode()) + 4096
            if self.manifest["parquet_bytes"] + size + metadata_size > self.disk_limit:
                self.manifest["state"] = "disk_limit"
                self.manifest["discarded_samples"] += len(rows)
                self.checkpoint()
                return
            temporary.replace(self.directory / name)
        finally:
            temporary.unlink(missing_ok=True)
        self.manifest["chunks"].append({
            "file": name, "rows": len(rows), "bytes": size,
            "drivers": sorted({row["driver_index"] for row in rows}),
            "laps": sorted({row["lap_num"] for row in rows}),
        })
        self.manifest["parquet_bytes"] += size
        self.manifest["written_samples"] += len(rows)
        self.checkpoint()

    def checkpoint(self):
        temporary = self.directory / "manifest.json.tmp"
        content = json.dumps(self.manifest, separators=(",", ":"))
        if len(content.encode()) + self.manifest["parquet_bytes"] > self.disk_limit:
            # Even rewind metadata must stay bounded. Fail closed rather than
            # omit an invalidation and accidentally expose superseded samples.
            self.manifest.update(state="failed", error="metadata_disk_limit", chunks=[], rewinds=[])
            content = json.dumps(self.manifest, separators=(",", ":"))
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(self.directory / "manifest.json")

    def close(self, counters, error=None):
        if not error:
            self.flush()
        self.rows.clear()
        self.manifest["counters"] = counters
        if error:
            self.manifest.update(state="failed", error=error)
        elif self.manifest["state"] == "recording":
            self.manifest["state"] = "closed"
        self.checkpoint()


def read_lap(directory, driver_index, lap_num, *, columns=None, start_m=None, end_m=None):
    """Read a lap from committed chunks; exclude superseded timeline rows.

    This is an internal storage API, not an MCP response. An active recording
    exposes committed chunks only. Coverage is conservative; no reconstructed
    or interpolated samples are returned.
    """
    import pyarrow.parquet as pq

    if not 0 <= driver_index < 24 or lap_num < 1:
        raise ValueError("Invalid driver index or lap number")
    if start_m is not None and end_m is not None and start_m > end_m:
        raise ValueError("Invalid distance window")
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported telemetry schema")
    if manifest["state"] == "failed":
        raise ValueError(f"Recording failed: {manifest.get('error', 'unknown')}")
    requested = list(columns) if columns is not None else None
    if requested is not None and not set(requested) <= set(arrow_schema().names):
        raise ValueError("Unknown telemetry column")
    required = {"epoch", "session_time_s", "distance_m", "lap_num", "driver_index"}
    selected = sorted(set(requested) | required) if requested is not None else None
    rows = []
    for chunk in manifest["chunks"]:
        if driver_index not in chunk["drivers"] or not {lap_num, lap_num + 1}.intersection(chunk["laps"]):
            continue
        path = (directory / chunk["file"]).resolve()
        if path.parent != directory.resolve():
            raise ValueError("Invalid chunk path")
        rows.extend(pq.read_table(path, columns=selected, filters=[
            ("driver_index", "=", driver_index), ("lap_num", "in", [lap_num, lap_num + 1]),
        ]).to_pylist())
    for event in manifest["rewinds"]:
        rows = [row for row in rows if row["epoch"] >= event["epoch"]
                or row["session_time_s"] < event["target_time_s"]]
    next_times = [row["session_time_s"] for row in rows if row["lap_num"] == lap_num + 1]
    following = bool(next_times)
    rows = sorted((row for row in rows if row["lap_num"] == lap_num), key=lambda row: row["session_time_s"])
    distances = [row["distance_m"] for row in rows if row["distance_m"] is not None]
    has_loss = manifest["discarded_samples"] > 0 or any(manifest["counters"].values())
    gaps = [b["session_time_s"] - a["session_time_s"] for a, b in zip(rows, rows[1:])]
    if rows and next_times:
        gaps.append(min(next_times) - rows[-1]["session_time_s"])
    complete = bool(distances and 0 <= distances[0] <= 25 and following and not has_loss
                    and max(gaps, default=0) <= 3 / manifest["sample_hz"])
    coverage = {"start_m": min(distances) if distances else None,
                "end_m": max(distances) if distances else None,
                "partial": not complete, "following_lap_observed": following}
    rows = [row for row in rows if (start_m is None or row["distance_m"] is not None and row["distance_m"] >= start_m)
            and (end_m is None or row["distance_m"] is not None and row["distance_m"] <= end_m)]
    if requested is not None:
        rows = [{key: row[key] for key in requested} for row in rows]
    return {"schema_version": SCHEMA_VERSION, "rows": rows, "coverage": coverage,
            "state": manifest["state"], "units": manifest["units"],
            "counters": manifest["counters"], "discarded_samples": manifest["discarded_samples"]}
