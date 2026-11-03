"""The ioctl/write sequence of a DMX frame, checked against a fake port (no hardware)."""

import struct

import pytest

from wb.mqtt_dmx import dmx_port as dp
from wb.mqtt_dmx.sender import FrameSender, Universe


class FakeOps(dp.PortOps):
    def __init__(self, rs485_enabled=True, fail_open=False):
        self.calls = []
        self.rs485 = struct.pack("I", dp.SER_RS485_ENABLED if rs485_enabled else 0) + bytes(28)
        self.termios = struct.pack(dp.TERMIOS2_FMT, 0, 0, 0o10017, 0, 0, bytes(19), 9600, 9600)
        self.fail_open = fail_open
        self.t = 0.0

    def open(self, path):
        if self.fail_open:
            raise OSError(2, "No such file or directory")
        self.calls.append(("open", path))
        return 7

    def close(self, fd):
        self.calls.append(("close",))

    def write(self, fd, data):
        self.calls.append(("write", bytes(data)))
        return len(data)

    def ioctl(self, fd, request, arg=b""):
        self.calls.append(("ioctl", request, bytes(arg)))
        if request == dp.TCGETS2:
            return self.termios
        if request == dp.TCSETS2:
            self.termios = bytes(arg)
            return b""
        if request == dp.TIOCGRS485:
            return self.rs485
        if request == dp.TIOCSRS485:
            self.rs485 = bytes(arg)
            return b""
        if request == dp.TIOCOUTQ:
            return struct.pack("i", 0)
        if request == dp.TIOCSERGETLSR:
            return struct.pack("i", dp.TIOCSER_TEMT)
        return b""

    def sleep(self, seconds):
        self.calls.append(("sleep", seconds))
        self.t += seconds

    def monotonic(self):
        return self.t

    def tcdrain(self, fd):
        self.calls.append(("tcdrain",))

    def requests(self):
        return [c[1] for c in self.calls if c[0] == "ioctl"]


def test_open_sets_250000_8n2_disables_auto_rs485_and_holds_rts():
    ops = FakeOps()
    port = dp.DmxPort("/dev/ttyRS485-2", ops=ops)
    port.open()
    cflag, ospeed = struct.unpack(dp.TERMIOS2_FMT, ops.termios)[2], struct.unpack(dp.TERMIOS2_FMT, ops.termios)[7]
    assert ospeed == 250000 and cflag & dp.BOTHER and cflag & dp.CSTOPB and cflag & dp.CS8 == dp.CS8
    assert not cflag & dp.PARENB and not cflag & dp.CRTSCTS
    assert struct.unpack("I", ops.rs485[:4])[0] & dp.SER_RS485_ENABLED == 0
    assert ("ioctl", dp.TIOCMBIS, struct.pack("i", dp.TIOCM_RTS)) in ops.calls
    port.close()
    # close: RTS released, original rs485 config restored, fd closed
    assert ("ioctl", dp.TIOCMBIC, struct.pack("i", dp.TIOCM_RTS)) in ops.calls
    assert struct.unpack("I", ops.rs485[:4])[0] & dp.SER_RS485_ENABLED
    assert ops.calls[-1] == ("close",)


def test_frame_is_break_mab_start_code_slots():
    ops = FakeOps()
    port = dp.DmxPort("/dev/ttyRS485-2", break_us=176, mab_us=20, ops=ops)
    port.open()
    ops.calls.clear()
    port.send_frame(bytes([1, 2, 3]))
    assert ops.calls == [
        ("ioctl", dp.TIOCSBRK, b""), ("sleep", 176e-6), ("ioctl", dp.TIOCCBRK, b""), ("sleep", 20e-6),
        ("write", b"\x00\x01\x02\x03"),
    ]
    ops.calls.clear()
    port.send_frame(bytes([9]))      # second frame waits for the UART to empty first
    assert ops.requests()[:2] == [dp.TIOCOUTQ, dp.TIOCSERGETLSR]
    assert ("write", b"\x00\x09") in ops.calls and port.frames == 2


def test_timings_are_clamped_to_the_standard_minimum():
    port = dp.DmxPort("/dev/x", break_us=10, mab_us=1, ops=FakeOps())
    assert (port.break_us, port.mab_us) == (dp.MIN_BREAK_US, dp.MIN_MAB_US)


def test_open_failure_is_a_port_error():
    with pytest.raises(dp.DmxPortError, match="No such file"):
        dp.DmxPort("/dev/nope", ops=FakeOps(fail_open=True)).open()


def test_sender_streams_the_universe_and_reports_errors():
    ops = FakeOps()
    port = dp.DmxPort("/dev/ttyRS485-2", ops=ops)
    universe = Universe(slots=4)
    universe.write(2, bytes([10, 20]))
    universe.write(511, bytes([1, 2, 3, 4]))             # past 512 is dropped, no error
    assert universe.get(512) == 2
    clock = ops.monotonic
    sender = FrameSender(universe, port, refresh_hz=40, clock=clock, sleep=ops.sleep)
    sender.run(max_frames=3)
    writes = [c for c in ops.calls if c[0] == "write"]
    assert writes == [("write", b"\x00\x00\x0a\x14\x00")] * 3
    assert sender.frames == 3 and sender.late_frames == 0 and not sender.running

    failing = FrameSender(Universe(), dp.DmxPort("/dev/nope", ops=FakeOps(fail_open=True)), clock=clock)
    failing.sleep = lambda _s: failing.stop()           # the first idle sleep after the failure ends the loop
    failing.run()
    assert failing.frames == 0 and "No such file" in failing.error and not failing.running
