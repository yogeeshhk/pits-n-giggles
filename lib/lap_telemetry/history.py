"""Changed packet snapshots and events, stored separately from sampled traces."""

import json
import math

from lib.f1_types import F1PacketType as P
from .channels import value

HISTORY_PACKETS = {P.SESSION, P.EVENT, P.CAR_SETUPS, P.TYRE_SETS, P.SESSION_HISTORY, P.LAP_POSITIONS}


def finite_json(data):
    if isinstance(data, float) and not math.isfinite(data):
        return None
    if isinstance(data, dict):
        return {key: finite_json(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [finite_json(value) for value in data]
    return data


class HistoryCollector:
    def __init__(self, emit):
        self.emit = emit
        self.previous = {}

    def clear(self):
        self.previous.clear()

    def feed(self, packet, epoch, public):
        header = packet.m_header
        kind = header.m_packetId
        entries = []
        if kind == P.SESSION:
            entries.append(("session", -1, 0, {
                "track_id": value(packet, "m_trackId"),
                "track_length_m": value(packet, "m_trackLength"),
                "safety_car_status": value(packet, "m_safetyCarStatus"),
                "session_type": value(packet, "m_sessionType"),
                "player_index": header.m_playerCarIndex,
                "reaction_time_s": value(packet, "m_startReactionTime") if header.m_packetFormat >= 2026 else None,
            }))
        elif kind == P.CAR_SETUPS:
            for index, setup in enumerate(packet.m_carSetups):
                if public.get(index) is True:
                    entries.append(("setup", index, 0, setup.toJSON()))
        elif kind == P.TYRE_SETS:
            if public.get(packet.m_carIdx) is True:
                entries.append(("tyre_sets", packet.m_carIdx, 0, packet.toJSON()))
        elif kind == P.SESSION_HISTORY:
            for index, lap in enumerate(packet.m_lapHistoryData):
                if lap.m_lapTimeInMS > 0:
                    entries.append(("lap_timing", packet.m_carIdx, index + 1, lap.toJSON()))
        elif kind == P.LAP_POSITIONS:
            for offset, positions in enumerate(packet.m_lapPositions):
                for index, position in enumerate(positions):
                    if position:
                        entries.append(("lap_position", index, packet.m_lapStart + offset + 1,
                                        {"position": position}))
        elif kind == P.EVENT and hasattr(packet, "toJSON"):
            # Button packets can arrive at input frequency and add no coaching evidence.
            if value(packet, "m_eventCode") != "BUTN":
                entries.append(("event", -1, 0, packet.toJSON()))
        rows = []
        for name, driver, lap, data in entries:
            encoded = json.dumps(finite_json(data), allow_nan=False, separators=(",", ":"))
            key = (name, driver, lap)
            if name != "event" and self.previous.get(key) == encoded:
                continue
            if name != "event":
                self.previous[key] = encoded
            rows.append({"kind": name, "driver_index": driver, "lap_num": lap, "epoch": epoch,
                         "session_time_s": header.m_sessionTime, "frame_id": header.m_frameIdentifier,
                         "payload": encoded})
        if rows:
            self.emit("history", rows)


def flush_history(store):
    """Called by the single storage writer; shares the session disk budget."""
    if not store.history_rows:
        return
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows, store.history_rows = store.history_rows, []
    if store.manifest["state"] != "recording":
        store.manifest["discarded_history"] += len(rows)
        return
    schema = pa.schema([("kind", pa.string()), ("driver_index", pa.int16()), ("lap_num", pa.int16()),
                        ("epoch", pa.uint32()), ("session_time_s", pa.float64()),
                        ("frame_id", pa.uint32()), ("payload", pa.string())])
    name = f"history-{len(store.manifest['history_chunks']):06d}.parquet"
    temporary = store.directory / (name + ".tmp")
    try:
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), temporary, compression="zstd")
        size = temporary.stat().st_size
        if size + store.manifest["parquet_bytes"] + len(json.dumps(store.manifest).encode()) + 4096 > store.disk_limit:
            store.manifest["state"] = "disk_limit"
            store.manifest["discarded_history"] += len(rows)
            return
        temporary.replace(store.directory / name)
        store.manifest["history_chunks"].append({"file": name, "rows": len(rows), "bytes": size})
        store.manifest["parquet_bytes"] += size
    finally:
        temporary.unlink(missing_ok=True)


def read_history(directory, manifest, driver_index, *, max_rows=100000):
    import pyarrow.parquet as pq

    rows = []
    for chunk in manifest.get("history_chunks", []):
        path = (directory / chunk["file"]).resolve()
        if path.parent != directory.resolve():
            raise ValueError("Invalid history chunk path")
        table = pq.read_table(path, filters=[("driver_index", "in", [-1, driver_index])])
        if len(rows) + table.num_rows > max_rows:
            raise ValueError("History read limit exceeded")
        rows.extend(table.to_pylist())
    for rewind in manifest["rewinds"]:
        rows = [row for row in rows if row["epoch"] >= rewind["epoch"]
                or row["session_time_s"] < rewind["target_time_s"]]
    for row in rows:
        row["data"] = json.loads(row.pop("payload"))
    return sorted(rows, key=lambda row: (row["session_time_s"], row["frame_id"]))
