"""
The frame loop of one universe: a 512-byte buffer streamed to a DmxPort at a fixed rate.

Frames sit on an absolute time grid (t0 + k·period). A late frame is sent at once and counted,
never "caught up" by sending two: receivers want a steady stream. On a port error the port is
closed and reopened every RETRY_S while the grid keeps running.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .dmx_port import DmxPort, DmxPortError

LOG = logging.getLogger("wb-mqtt-dmx")

UNIVERSE_SIZE = 512
MAX_REFRESH_HZ = 44  # 513 bytes × 44 µs + BREAK + MAB ≈ 22.8 ms: the DMX maximum for 512 slots
RETRY_S = 5.0
STATS_WINDOW_S = 2.0
STALL_LOG_S = 0.010  # a frame starting this late against the grid is logged
FRAME_MARGIN = 1.05  # idle line between frames when the period is set by the frame length
# Right after the port opens the first frames start late (the open itself, the first BREAK). That is not a
# fault of the line, so lateness is neither counted nor logged until this many frames have gone out.
WARMUP_FRAMES = 10


class Universe:
    """
    512 slots; writers set bytes, the sender takes snapshots.
    """

    def __init__(self, slots: int = UNIVERSE_SIZE) -> None:
        self.slots = max(1, min(UNIVERSE_SIZE, int(slots)))
        self._buf = bytearray(UNIVERSE_SIZE)
        self._lock = threading.Lock()

    def write(self, start: int, data: bytes) -> None:
        """
        Set slots start..start+len-1 (1-based); bytes past 512 are dropped.
        """
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


@dataclass
class _Grid:
    """
    Where the frame loop stands: the time grid, the retry timer and the rate window.
    """

    next_frame: float
    window_start: float
    next_retry: float = 0.0
    last_send_ms: float = 0.0
    warm_after: int = 0  # the frame count from which lateness is watched
    window_frames: int = 0


# The loop's settings, its state and the counters the service shows in the status controls live together:
# they are read from another thread while the loop runs, and one object keeps that simple.
class FrameSender:  # pylint: disable=too-many-instance-attributes
    """
    Sends the universe to the port at a steady rate from its own thread.
    """

    def __init__(
        self,
        universe: Universe,
        port: DmxPort,
        refresh_hz: int = 40,
    ) -> None:
        self.universe = universe
        self.port = port
        self.refresh_hz = max(1, min(MAX_REFRESH_HZ, int(refresh_hz)))
        self.period = 1.0 / self.refresh_hz
        # With a pause between slots a frame may not fit the period: then frames simply follow each other.
        # This is the first guess; the loop keeps the period at what a frame really takes.
        self.slot_gap = bool(getattr(port, "slot_gap_us", 0))
        if self.slot_gap:
            self.period = max(self.period, port.frame_time_s(universe.slots) * FRAME_MARGIN)
        # Called when the port starts or stops working; the service sets it to refresh the status
        self.on_state: Optional[Callable[["FrameSender"], None]] = None
        # Tests replace these two to run the loop without waiting
        self.clock: Callable[[], float] = time.monotonic
        self.sleep: Callable[[float], None] = time.sleep
        self.running = False
        self.error = ""
        self.frames = 0
        self.late_frames = 0
        self.max_late_us = 0.0  # worst lateness of a frame start against the grid
        self.measured_fps = 0.0  # frames per second over the last STATS_WINDOW_S
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
        """
        Stream frames until stop().

        Args:
            max_frames: Stop after this many frames; for test-port and tests.
        """
        if self.slot_gap:
            LOG.info(
                "%s: %d us between slots: a frame takes about %.1f ms, refresh about %.1f Hz",
                self.port.path,
                self.port.slot_gap_us,
                self.port.frame_time_s(self.universe.slots) * 1e3,
                1.0 / self.period,
            )
        now = self.clock()
        grid = _Grid(next_frame=now, window_start=now)
        while not self._stop.is_set() and (max_frames is None or self.frames < max_frames):
            now = self._wait_for_slot(grid)
            if self.running or self._open_port(grid, now):
                self._send_frame(grid, now)
        self.port.close()
        if self.running:
            self.running = False
            LOG.info("%s: stopped after %d frames, %d late", self.port.path, self.frames, self.late_frames)
            if self.on_state is not None:
                self.on_state(self)

    def _wait_for_slot(self, grid: _Grid) -> float:
        """
        Sleep until the next slot of the time grid and move the grid on.

        Returns:
            The current time.
        """
        now = self.clock()
        if now < grid.next_frame:
            self.sleep(grid.next_frame - now)
            now = self.clock()
        # With a pause between slots the period follows what a frame takes, so there is no fixed grid
        # to be late against.
        if self.running and not self.slot_gap and self.frames >= grid.warm_after:
            late = now - grid.next_frame
            self.max_late_us = max(self.max_late_us, late * 1e6)
            if late > self.period / 2:
                self.late_frames += 1
            if late > STALL_LOG_S:
                LOG.warning(
                    "%s: frame %d starts %.1f ms late (previous send took %.1f ms)",
                    self.port.path,
                    self.frames,
                    late * 1e3,
                    grid.last_send_ms,
                )
        grid.next_frame += self.period
        if now - grid.next_frame > self.period:  # far behind (suspend, retry): restart the grid
            grid.next_frame = now + self.period
        return now

    def _open_port(self, grid: _Grid, now: float) -> bool:
        """
        Try to open the port, no more often than once in RETRY_S.

        Returns:
            True when the port is open and frames can go out.
        """
        if now < grid.next_retry:
            return False
        try:
            self.port.open()
        except DmxPortError as e:
            grid.next_retry = now + RETRY_S
            self._state(False, str(e))
            return False
        self._state(True)
        grid.warm_after = self.frames + WARMUP_FRAMES
        return True

    def _send_frame(self, grid: _Grid, now: float) -> None:
        try:
            t_send = self.clock()
            self.port.send_frame(self.universe.snapshot())
            grid.last_send_ms = (self.clock() - t_send) * 1e3
            if self.slot_gap:
                self.period = max(1.0 / self.refresh_hz, grid.last_send_ms / 1e3 * FRAME_MARGIN)
            self.frames += 1
            grid.window_frames += 1
            if now - grid.window_start >= STATS_WINDOW_S:
                self.measured_fps = grid.window_frames / (now - grid.window_start)
                grid.window_start, grid.window_frames = now, 0
        except DmxPortError as e:
            self.port.close()
            grid.next_retry = now + RETRY_S
            self._state(False, str(e))
