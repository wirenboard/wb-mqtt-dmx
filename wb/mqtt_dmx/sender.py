"""The frame loop of one universe: a 512-byte buffer streamed to a DmxPort at a fixed rate.

Frames sit on an absolute time grid (t0 + k·period). A late frame is sent at once and counted,
never "caught up" by sending two: receivers want a steady stream. On a port error the port is
closed and reopened every RETRY_S while the grid keeps running.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .dmx_port import DmxPort, DmxPortError

LOG = logging.getLogger("wb-mqtt-dmx")

UNIVERSE_SIZE = 512
MAX_REFRESH_HZ = 44        # 513 bytes × 44 µs + BREAK + MAB ≈ 22.8 ms: the DMX maximum for 512 slots
RETRY_S = 5.0


class Universe:
    """512 slots; writers set bytes, the sender takes snapshots."""

    def __init__(self, slots: int = UNIVERSE_SIZE) -> None:
        self.slots = max(1, min(UNIVERSE_SIZE, int(slots)))
        self._buf = bytearray(UNIVERSE_SIZE)
        self._lock = threading.Lock()

    def write(self, start: int, data: bytes) -> None:
        """Set slots start..start+len-1 (1-based); bytes past 512 are dropped."""
        first = max(1, int(start)) - 1
        end = min(UNIVERSE_SIZE, first + len(data))
        with self._lock:
            self._buf[first:end] = data[: end - first]

    def get(self, channel: int) -> int:
        with self._lock:
            return self._buf[channel - 1]

    def blackout(self) -> None:
        with self._lock:
            self._buf[:] = bytes(UNIVERSE_SIZE)

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._buf[: self.slots])


class FrameSender:
    def __init__(self, universe: Universe, port: DmxPort, refresh_hz: int = 40,
                 on_state: Optional[Callable[["FrameSender"], None]] = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.universe = universe
        self.port = port
        self.refresh_hz = max(1, min(MAX_REFRESH_HZ, int(refresh_hz)))
        self.period = 1.0 / self.refresh_hz
        self.on_state = on_state
        self.clock = clock
        self.sleep = sleep
        self.running = False
        self.error = ""
        self.frames = 0
        self.late_frames = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name=f"dmx-{self.port.path}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _state(self, running: bool, error: str = "") -> None:
        if (running, error) == (self.running, self.error):
            return
        self.running, self.error = running, error
        (LOG.info if running else LOG.warning)("%s: %s", self.port.path, "running" if running else error)
        if self.on_state is not None:
            self.on_state(self)

    def run(self, max_frames: Optional[int] = None) -> None:
        """Stream until stop() (or max_frames, for test-port and tests)."""
        next_retry = 0.0
        next_frame = self.clock()
        while not self._stop.is_set() and (max_frames is None or self.frames < max_frames):
            now = self.clock()
            if now < next_frame:
                self.sleep(next_frame - now)
                now = self.clock()
            elif self.running and now - next_frame > self.period / 2:
                self.late_frames += 1
            next_frame += self.period
            if now - next_frame > self.period:          # far behind (suspend, retry): restart the grid
                next_frame = now + self.period
            if not self.running:
                if now < next_retry:
                    continue
                try:
                    self.port.open()
                except DmxPortError as e:
                    next_retry = now + RETRY_S
                    self._state(False, str(e))
                    continue
                self._state(True)
            try:
                self.port.send_frame(self.universe.snapshot())
                self.frames += 1
            except DmxPortError as e:
                self.port.close()
                next_retry = now + RETRY_S
                self._state(False, str(e))
        self.port.close()
        if self.running:
            self.running = False
            LOG.info("%s: stopped after %d frames, %d late", self.port.path, self.frames, self.late_frames)
            if self.on_state is not None:
                self.on_state(self)
