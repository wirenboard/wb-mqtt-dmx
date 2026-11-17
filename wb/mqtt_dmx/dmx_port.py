"""DMX512 frames on a Linux serial port (the controller's RS-485 or a WBE2 RS-485 module).

One DMX frame is: BREAK (line low ≥ 88 µs) → MAB (line high ≥ 8 µs) → start code 0x00 → up to
512 slots, every byte at 250 kbit/s, 8 data bits, no parity, 2 stop bits. The kernel gives all of it:

* 250000 baud is not a POSIX constant → `TCSETS2` with the `BOTHER` flag;
* BREAK → `TIOCSBRK` … busy-wait … `TIOCCBRK` (`time.sleep` wakes up 120-150 µs late on the
  controller, so the short BREAK and MAB intervals are spun on `perf_counter` instead);
* the RS-485 transmitter enable (DE) must stay on for the whole frame, BREAK included. The kernel's
  automatic RS-485 direction control turns DE on only around the bytes it writes, not around a
  BREAK, so it is switched off (`TIOCSRS485`) and DE is held through the RTS line (`TIOCMBIS`).
  The previous RS-485 settings are restored on close, so wb-mqtt-serial gets the port back as is;
* before the next BREAK the previous frame must have left the UART FIFO: poll `TIOCOUTQ` == 0 and
  the TEMT bit of `TIOCSERGETLSR`. `tcdrain` is the fallback (some drivers sleep whole ms in it).

ioctl numbers are the generic Linux ones, identical on armhf (WB6/WB7) and arm64 (WB8). The module
imports on Windows too; the Linux calls go through `PortOps`, which tests replace with a fake.
"""

from __future__ import annotations

import logging
import os
import struct
import time
from typing import Optional

LOG = logging.getLogger("wb-mqtt-dmx")

try:  # Linux only
    import fcntl
    import termios
except ImportError:  # pragma: no cover
    fcntl = None   # type: ignore[assignment]
    termios = None   # type: ignore[assignment]

DMX_BAUD = 250000
MIN_BREAK_US = 88
MIN_MAB_US = 8
SLOT_US = 44               # one slot on the wire: start bit, 8 data bits, 2 stop bits at 4 µs each
MAX_SLOT_GAP_US = 1000
WRITE_US = 8               # what a one-byte write() adds to a slot on the controller (measured 4–8 µs)
DRAIN_TIMEOUT_S = 0.1      # a 512-slot frame is 22.6 ms on the wire
DRAIN_POLL_S = 0.0002

# <asm-generic/ioctls.h>, <asm-generic/termbits.h>, <linux/serial.h>
TCGETS2 = 0x802C542A
TCSETS2 = 0x402C542B
BOTHER = 0o010000
CBAUD = 0o010017
CSIZE, CS8, CSTOPB, PARENB, CREAD, CLOCAL, CRTSCTS = 0o60, 0o60, 0o100, 0o400, 0o200, 0o4000, 0o20000000000
TIOCOUTQ = 0x5411
TIOCMBIS = 0x5416
TIOCMBIC = 0x5417
TIOCSBRK = 0x5427
TIOCCBRK = 0x5428
TIOCGRS485 = 0x542E
TIOCSRS485 = 0x542F
TIOCSERGETLSR = 0x5459
TIOCSER_TEMT = 0x01
TIOCM_RTS = 0x004
SER_RS485_ENABLED = 1 << 0
RS485_STRUCT_SIZE = 32
# struct termios2 on arm/arm64/x86: 4 flag words, c_line, c_cc[19], ispeed, ospeed = 44 bytes
TERMIOS2_FMT = "@IIIIB19sII"


class DmxPortError(Exception):
    """The port cannot be opened, configured or written."""


class PortOps:
    """The system calls the port needs, in one place so tests can replace them."""

    def open(self, path: str) -> int:
        return os.open(path, os.O_RDWR | os.O_NOCTTY)

    def close(self, fd: int) -> None:
        os.close(fd)

    def write(self, fd: int, data: bytes) -> int:
        return os.write(fd, data)

    def ioctl(self, fd: int, request: int, arg: bytes = b"") -> bytes:
        """ioctl with a buffer argument; returns the (possibly updated) buffer."""
        if fcntl is None:
            raise DmxPortError("serial DMX output needs Linux")
        buf = bytearray(arg) if arg else None
        if buf is None:
            fcntl.ioctl(fd, request)
            return b""
        fcntl.ioctl(fd, request, buf, True)
        return bytes(buf)

    def tcdrain(self, fd: int) -> None:
        if termios is not None:
            termios.tcdrain(fd)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def spin_us(self, micros: int) -> None:
        """Busy-wait for a few hundred microseconds: time.sleep wakes up ~120-150 us late on the
        controller, which stretched BREAK and MAB; spinning costs well under 1 % CPU at 40 Hz."""
        end = time.perf_counter() + micros / 1e6
        while time.perf_counter() < end:
            pass

    def monotonic(self) -> float:
        return time.monotonic()


class DmxPort:
    """One DMX universe on one serial port. Not thread-safe: one sender per port."""

    def __init__(self, path: str, break_us: int = 176, mab_us: int = 20, de_via_rts: bool = True,
                 ops: Optional[PortOps] = None, slot_gap_us: int = 0) -> None:
        self.path = path
        self.break_us = max(MIN_BREAK_US, int(break_us))
        self.mab_us = max(MIN_MAB_US, int(mab_us))
        # A pause after every slot, for receivers that cannot keep up with back-to-back bytes. The UART
        # cannot do it by itself, so the frame goes out byte by byte with a busy-wait in between.
        self.slot_gap_us = max(0, min(MAX_SLOT_GAP_US, int(slot_gap_us)))
        self.de_via_rts = de_via_rts
        self.ops = ops or PortOps()
        self.fd: Optional[int] = None
        self.frames = 0
        self.drain_fallbacks = 0
        self._saved_rs485: Optional[bytes] = None
        self._saved_termios: Optional[bytes] = None

    # ---- open / close ----------------------------------------------------------------------

    def open(self) -> None:
        if self.fd is not None:
            return
        try:
            fd = self.ops.open(self.path)
        except OSError as e:
            raise DmxPortError(f"{self.path}: {e.strerror or e}") from e
        try:
            self._set_line(fd)
            if self.de_via_rts:
                self._hold_de(fd)
        except OSError as e:
            self.ops.close(fd)
            raise DmxPortError(f"{self.path}: cannot configure: {e}") from e
        self.fd = fd

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        for request, arg in ((TIOCCBRK, b""), (TIOCMBIC, struct.pack("i", TIOCM_RTS))):
            try:
                self.ops.ioctl(fd, request, arg)
            except (OSError, DmxPortError):
                pass
        if self._saved_rs485 is not None:
            # Only flags and the two delays are restored; the kernel may reject non-zero padding.
            restore = self._saved_rs485[:12] + bytes(RS485_STRUCT_SIZE - 12)
            try:
                self.ops.ioctl(fd, TIOCSRS485, restore)
            except (OSError, DmxPortError) as e:
                LOG.warning("%s: cannot restore the RS-485 mode: %s", self.path, e)
            self._saved_rs485 = None
        if self._saved_termios is not None:
            # Leave the line as we found it (speed, framing), the way wb-mqtt-serial does.
            try:
                self.ops.ioctl(fd, TCSETS2, self._saved_termios)
            except (OSError, DmxPortError) as e:
                LOG.warning("%s: cannot restore the line settings: %s", self.path, e)
            self._saved_termios = None
        self.ops.close(fd)

    def _set_line(self, fd: int) -> None:
        """250000 8N2, raw, no flow control, via termios2."""
        raw = self.ops.ioctl(fd, TCGETS2, bytes(struct.calcsize(TERMIOS2_FMT)))
        self._saved_termios = raw
        _iflag, _oflag, cflag, _lflag, line, cc, _ispeed, _ospeed = struct.unpack(TERMIOS2_FMT, raw)
        cflag &= ~(CBAUD | CSIZE | PARENB | CRTSCTS)
        cflag |= BOTHER | CS8 | CSTOPB | CREAD | CLOCAL
        self.ops.ioctl(fd, TCSETS2, struct.pack(TERMIOS2_FMT, 0, 0, cflag, 0, line, cc, DMX_BAUD, DMX_BAUD))
        check = self.ops.ioctl(fd, TCGETS2, bytes(struct.calcsize(TERMIOS2_FMT)))
        ospeed = struct.unpack(TERMIOS2_FMT, check)[7]
        if ospeed != DMX_BAUD:
            raise DmxPortError(f"{self.path}: driver did not accept {DMX_BAUD} baud (got {ospeed})")

    def _hold_de(self, fd: int) -> None:
        """Disable the kernel's automatic RS-485 direction and keep DE on through RTS."""
        try:
            saved = self.ops.ioctl(fd, TIOCGRS485, bytes(RS485_STRUCT_SIZE))
        except OSError:
            saved = None   # no rs485 support in this driver (USB adapter): nothing to switch off
        if saved is not None:
            flags = struct.unpack("I", saved[:4])[0]
            if flags & SER_RS485_ENABLED:
                self.ops.ioctl(fd, TIOCSRS485, struct.pack("I", flags & ~SER_RS485_ENABLED) + saved[4:])
                self._saved_rs485 = saved
        self.ops.ioctl(fd, TIOCMBIS, struct.pack("i", TIOCM_RTS))

    # ---- frames ----------------------------------------------------------------------------

    def frame_time_s(self, slot_count: int) -> float:
        """How long a frame of that many slots stays on the wire."""
        slot_us = SLOT_US + (self.slot_gap_us + WRITE_US if self.slot_gap_us else 0)
        return (self.break_us + self.mab_us + (slot_count + 1) * slot_us) / 1e6

    def send_frame(self, slots: bytes) -> None:
        """BREAK, MAB, start code and the slots; waits for the previous frame to leave the UART."""
        fd = self.fd
        if fd is None:
            raise DmxPortError(f"{self.path}: port is not open")
        try:
            if self.frames:
                self._wait_transmitter_empty(fd)
            self.ops.ioctl(fd, TIOCSBRK)
            self.ops.spin_us(self.break_us)
            self.ops.ioctl(fd, TIOCCBRK)
            self.ops.spin_us(self.mab_us)
            data = b"\x00" + bytes(slots)
            if self.slot_gap_us:
                wait_us = SLOT_US + self.slot_gap_us       # the byte itself, then the pause
                write, spin = self.ops.write, self.ops.spin_us
                for i in range(len(data)):
                    write(fd, data[i:i + 1])
                    spin(wait_us)
            else:
                written = 0
                while written < len(data):
                    written += self.ops.write(fd, data[written:])
        except OSError as e:
            raise DmxPortError(f"{self.path}: {e.strerror or e}") from e
        self.frames += 1

    def _wait_transmitter_empty(self, fd: int) -> None:
        deadline = self.ops.monotonic() + DRAIN_TIMEOUT_S
        while True:
            queued = struct.unpack("i", self.ops.ioctl(fd, TIOCOUTQ, bytes(4)))[0]
            if queued == 0:
                try:
                    lsr = struct.unpack("i", self.ops.ioctl(fd, TIOCSERGETLSR, bytes(4)))[0]
                except OSError:
                    lsr = TIOCSER_TEMT   # driver without LSR support: the queue count is all we have
                if lsr & TIOCSER_TEMT:
                    return
            if self.ops.monotonic() >= deadline:
                self.drain_fallbacks += 1
                self.ops.tcdrain(fd)
                return
            self.ops.sleep(DRAIN_POLL_S)
