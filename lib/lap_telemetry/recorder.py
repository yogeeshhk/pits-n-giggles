"""Nonblocking packet-side recorder and a bounded background storage worker."""

import asyncio
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import time
from uuid import uuid4

import msgpack

from .assembler import SampleAssembler
from .storage import SessionStore


class TelemetryRecorder:
    """One producer on the backend event loop; one sequential I/O worker.

    Serialized queue payloads have a strict byte limit. Control commands have
    reserved capacity; if even that fills, recording fails closed. It never
    blocks packet processing or silently loses a timeline invalidation.
    """

    def __init__(self, root, logger, *, sample_hz=20, buffer_bytes=64 * 1024**2,
                 disk_limit_bytes=1024**3, max_age_s=0.25):
        if buffer_bytes < 128 * 1024 or disk_limit_bytes < 1:
            raise ValueError("Invalid recorder resource limits")
        self.root = Path(root)
        self.logger = logger
        self.sample_hz = sample_hz
        self.buffer_bytes = buffer_bytes
        self.disk_limit_bytes = disk_limit_bytes
        self.max_age_s = max_age_s
        self.queue = asyncio.Queue(maxsize=4096)
        self.queued_bytes = 0
        self.peak_queued_bytes = 0
        self.dropped_samples = 0
        self.error = None
        self.storage_state = "idle"
        self.session_uid = None
        self.assembler = None
        self.task = None
        self.closed = False
        self.references = deque(maxlen=2)
        self._store = None  # Worker-owned; packet callbacks never access it.

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name="Lap telemetry writer")

    def reference(self, session_uid):
        return next((dict(ref) for ref in reversed(self.references)
                     if ref["session_uid"] == str(session_uid)), None)

    def observe(self, packet):
        if self.closed or self.error:
            return
        uid = packet.m_header.m_sessionUID
        if uid == 0:
            return
        if uid != self.session_uid:
            if self.assembler:
                self.assembler.flush()
                self._enqueue("close", self._counters())
            self.session_uid = uid
            self.dropped_samples = 0
            recording_id = f"{uid}-{uuid4().hex}"
            reference = {"schema_version": 1, "session_uid": str(uid),
                         "manifest": f"telemetry/{recording_id}/manifest.json"}
            self.references.append(reference)
            self._enqueue("begin", {"recording_id": recording_id, "session_uid": str(uid),
                                    "sample_hz": self.sample_hz, "max_source_age_s": self.max_age_s,
                                    "started_at": datetime.now(timezone.utc).isoformat()})
            self.assembler = SampleAssembler(self._enqueue, self.sample_hz, self.max_age_s)
        try:
            self.assembler.feed(packet)
        except Exception as exc:  # Recording must not take down the live backend.
            self._fail(f"sample_assembly_failed: {exc}")

    def reset_session(self):
        """Close a recording when the backend clears a session or formation lap."""
        if self.assembler and not self.error:
            self.assembler.flush()
            self._enqueue("close", self._counters())
        self.assembler = None
        self.session_uid = None

    def _counters(self):
        return {"queue_dropped_samples": self.dropped_samples,
                "unaligned_samples": self.assembler.dropped_unaligned if self.assembler else 0}

    def _fail(self, reason):
        if self.error is None:
            self.error = reason
            self.logger.error("Lap telemetry recording stopped: %s", reason)

    def _enqueue(self, operation, data):
        if self.error:
            return
        payload = msgpack.packb(data, use_bin_type=True)
        charge = len(payload) + 128
        reserve = 64 * 1024 if operation == "rows" else 0
        full = self.queued_bytes + charge > self.buffer_bytes - reserve
        # Reserve 32 queue slots as well as bytes for lifecycle commands.
        full = full or self.queue.qsize() >= (4064 if operation == "rows" else 4096)
        if full:
            if operation == "rows":
                self.dropped_samples += len(data)
            else:
                self._fail("control_queue_overflow")
            return
        self.queue.put_nowait((operation, payload, charge))
        self.queued_bytes += charge
        self.peak_queued_bytes = max(self.peak_queued_bytes, self.queued_bytes)

    def _process(self, operation, payload):
        data = msgpack.unpackb(payload, raw=False)
        if operation == "begin":
            self._store = SessionStore(self.root / data["recording_id"], data, self.disk_limit_bytes)
        elif self._store is not None:
            if operation == "rows":
                self._store.append(data)
            elif operation == "rewind":
                self._store.rewind(data)
            elif operation == "close":
                self._store.close(data)
                self._store = None
        return self._store.manifest["state"] if self._store else "idle"

    def _checkpoint(self, counters):
        if self._store:
            self._store.manifest["counters"] = counters
            self._store.flush()
            self._store.checkpoint()
            return self._store.manifest["state"]
        return "idle"

    async def _run(self):
        last_checkpoint = time.monotonic()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    item = None
                if item is not None:
                    operation, payload, charge = item
                    if operation == "stop":
                        break
                    try:
                        if not self.error:
                            previous_state = self.storage_state
                            self.storage_state = await asyncio.to_thread(self._process, operation, payload)
                            if self.storage_state == "failed":
                                self._fail("recording_metadata_limit")
                            if self.storage_state == "disk_limit" and previous_state != "disk_limit":
                                self.logger.warning("Lap telemetry recording reached its session disk limit")
                    except Exception as exc:
                        self._fail(f"storage_failed: {exc}")
                    finally:
                        self.queued_bytes -= charge
                if self.error and self._store is not None:
                    try:
                        await asyncio.to_thread(self._store.close, self._counters(), self.error)
                    except Exception:
                        self.logger.exception("Could not persist telemetry failure status")
                    self._store = None
                if time.monotonic() - last_checkpoint >= 1.0:
                    if not self.error and self.queue.empty():
                        try:
                            self.storage_state = await asyncio.to_thread(self._checkpoint, self._counters())
                        except Exception as exc:
                            self._fail(f"storage_failed: {exc}")
                    last_checkpoint = time.monotonic()
        finally:
            if self._store is not None:
                try:
                    await asyncio.to_thread(self._store.close, self._counters(), self.error)
                    self.storage_state = self._store.manifest["state"]
                except Exception as exc:
                    self._fail(f"storage_failed: {exc}")

    async def stop(self):
        if self.closed:
            if self.task:
                await self.task
            return
        if self.assembler and not self.error:
            self.assembler.flush()
        self.closed = True
        self.start()
        # Only shutdown may await capacity; the producer has already stopped.
        await self.queue.put(("stop", b"", 0))
        await self.task

    def get_stats(self):
        return {"enabled": True, "session_uid": self.session_uid,
                "state": "failed" if self.error else self.storage_state,
                "error": self.error, "queued_bytes": self.queued_bytes,
                "peak_queued_bytes": self.peak_queued_bytes, **self._counters(),
                "recording": self.reference(self.session_uid)}
