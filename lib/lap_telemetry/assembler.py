"""CPU-only, one-frame-delayed assembly of accepted game packets."""

import math

from lib.f1_types import F1PacketType, PacketEventData
from .channels import CHANNELS, value
from .history import HISTORY_PACKETS, HistoryCollector

PACKETS = {
    F1PacketType.MOTION_EX: ("motion_ex", None),
    F1PacketType.CAR_DAMAGE: ("damage", "m_carDamageData"),
    F1PacketType.SESSION: ("session", None),
    F1PacketType.LAP_DATA: ("lap", "m_lapData"),
    F1PacketType.CAR_TELEMETRY: ("telemetry", "m_carTelemetryData"),
    F1PacketType.CAR_STATUS: ("status", "m_carStatusData"),
    F1PacketType.MOTION: ("motion", "m_carMotionData"),
    F1PacketType.CAR_TELEMETRY_2: ("telemetry2", "m_carTelemetry2Data"),
}


class SampleAssembler:
    """Emit rows/rewinds to a synchronous sink; never access the filesystem.

    Lap and telemetry must share a frame, preventing an old lap number from
    being attached to inputs just past the finish line. Other sources may be
    held briefly, with their exact age recorded. No future values are used.
    """

    def __init__(self, emit, sample_hz=20, max_age_s=0.25):
        if not 1 <= sample_hz <= 60 or not 0 < max_age_s <= 2:
            raise ValueError("Invalid recording frequency or source age")
        self.emit = emit
        self.history = HistoryCollector(emit)
        self.sample_hz = sample_hz
        self.max_age_s = max_age_s
        self.epoch = 0
        self.frame = None
        self.time = None
        self.cache = {}
        self.last_sample = None
        self.public = {}
        self.active_cars = None
        self.player_index = None
        self.dropped_unaligned = 0
        self._seen_types = set()

    def feed(self, packet):
        header = packet.m_header
        kind = header.m_packetId
        time = header.m_sessionTime
        frame = header.m_overallFrameIdentifier
        if not math.isfinite(time):
            return
        if kind == F1PacketType.EVENT and packet.m_eventCode == PacketEventData.EventPacketType.FLASHBACK:
            self.flush()
            self._rewind(packet.mEventDetails.flashbackSessionTime)
            self.frame = frame
            self._seen_types = set(PACKETS) | HISTORY_PACKETS | {F1PacketType.PARTICIPANTS}
            return
        if kind not in PACKETS and kind not in HISTORY_PACKETS and kind != F1PacketType.PARTICIPANTS:
            return
        if self.frame is not None and frame < self.frame and frame != 0:
            return  # Reordering, not a flashback (overall frame is monotonic).
        if self.time is not None and time < self.time - 0.001:
            self.flush()
            self._rewind(time)
        if frame != self.frame:
            self.flush()
            self.frame, self.time = frame, time
            self._seen_types.clear()
        if kind in self._seen_types:
            return
        self._seen_types.add(kind)
        self.player_index = header.m_playerCarIndex
        if kind == F1PacketType.PARTICIPANTS:
            self.active_cars = packet.m_numActiveCars
            self.public = {i: value(car, "m_yourTelemetry") == 1
                           for i, car in enumerate(packet.m_participants)}
        elif kind in PACKETS:
            group, attribute = PACKETS[kind]
            if kind == F1PacketType.MOTION_EX:
                cars = [None] * 24
                if 0 <= header.m_playerCarIndex < 24:
                    cars[header.m_playerCarIndex] = packet
            elif kind == F1PacketType.SESSION:
                cars = [packet] * 24
            else:
                cars = getattr(packet, attribute)
            self.cache[group] = (header, cars)
        if kind in HISTORY_PACKETS:
            self.history.feed(packet, self.epoch, self.public)

    def _rewind(self, time):
        if not math.isfinite(time):
            return
        self.epoch += 1
        self.emit("rewind", {"epoch": self.epoch, "target_time_s": time})
        self.cache.clear()
        self.history.clear()
        self.frame = self.time = self.last_sample = None
        self._seen_types.clear()

    def flush(self):
        telemetry = self.cache.get("telemetry")
        if telemetry is None or telemetry[0].m_overallFrameIdentifier != self.frame:
            return
        header, cars = telemetry
        # Remove after consuming, so stop/session switches cannot duplicate rows.
        del self.cache["telemetry"]
        time = header.m_sessionTime
        # UDP timestamps are float32: subtracting near-hour timestamps can make
        # nominal 60 Hz intervals slightly short. Use buckets with 10% timing
        # tolerance instead of progressively skipping these valid frames.
        bucket = math.floor(time * self.sample_hz + 0.1)
        if self.last_sample is not None and bucket <= self.last_sample:
            return
        lap = self.cache.get("lap")
        if (lap is None or lap[0].m_overallFrameIdentifier != self.frame
                or lap[0].m_frameIdentifier != header.m_frameIdentifier
                or abs(lap[0].m_sessionTime - time) > 1e-5):
            self.dropped_unaligned += len(cars)
            return
        self.last_sample = bucket
        sources = {**self.cache, "telemetry": telemetry}
        rows = []
        for index, car in enumerate(cars[:self.active_cars]):
            if index >= len(lap[1]):
                continue
            lap_car = lap[1][index]
            if not value(lap_car, "m_currentLapNum") or value(lap_car, "m_resultStatus") in (0, 1):
                continue
            row = {
                "session_uid": header.m_sessionUID, "driver_index": index,
                "epoch": self.epoch, "frame_id": header.m_frameIdentifier,
                "overall_frame_id": header.m_overallFrameIdentifier,
                "session_time_s": time, "packet_format": header.m_packetFormat,
                "telemetry_public": self.public.get(index),
            }
            for group, channels in CHANNELS.items():
                source = sources.get(group)
                age = time - source[0].m_sessionTime if source else None
                max_age = 2.5 if group in ("session", "damage") else self.max_age_s
                valid = (source is not None and -1e-5 <= age <= max_age
                         and index < len(source[1]) and source[1][index] is not None)
                row[f"{group}_source_time_s"] = source[0].m_sessionTime if source else None
                row[f"{group}_source_frame_id"] = source[0].m_frameIdentifier if source else None
                row[f"{group}_age_s"] = age
                row[f"{group}_stale"] = not valid
                for name, (attribute, unit) in channels.items():
                    cell = value(source[1][index], attribute) if valid else None
                    row[name] = bool(cell) if unit == "bool" and cell is not None else cell
            # Restricted status is unavailable until participants confirm public.
            # Car Telemetry has no restricted fields in the supported parser spec.
            if self.public.get(index) is not True:
                for name in (*CHANNELS["status"], *CHANNELS["damage"]):
                    if name != "ers_harvest_limit_j":
                        row[name] = None
            if header.m_packetFormat < 2026:
                row["ers_harvest_limit_j"] = None
            for gap in ("front", "leader"):
                milliseconds, minutes = row[f"gap_{gap}_ms_part"], row[f"gap_{gap}_minutes"]
                row[f"gap_{gap}_ms"] = (milliseconds + (minutes or 0) * 60000
                                             if milliseconds is not None else None)
            energy = row["ers_store_j"]
            row["ers_percent"] = energy / 4_000_000 * 100 if energy is not None else None
            rows.append(row)
        if rows:
            self.emit("rows", rows)
