import struct

import pytest

from wb.mqtt_dmx import dmx_port as dp

from .fakes import FakeOps, fake_port


def test_open_sets_the_line_to_250000_8n2() -> None:
    ops = FakeOps()
    fake_port(ops).open()

    cflag, ospeed = (
        struct.unpack(dp.TERMIOS2_FMT, ops.termios)[2],
        struct.unpack(dp.TERMIOS2_FMT, ops.termios)[7],
    )
    assert ospeed == 250000
    assert cflag & dp.BOTHER and cflag & dp.CSTOPB and cflag & dp.CS8 == dp.CS8
    assert not cflag & dp.PARENB and not cflag & dp.CRTSCTS


def test_open_takes_the_transmitter_from_the_kernel_and_holds_it() -> None:
    ops = FakeOps()
    fake_port(ops).open()

    assert struct.unpack("I", ops.rs485[:4])[0] & dp.SER_RS485_ENABLED == 0
    assert ("ioctl", dp.TIOCMBIS, struct.pack("i", dp.TIOCM_RTS)) in ops.calls


def test_close_gives_the_port_back_as_it_was() -> None:
    ops = FakeOps()
    port = fake_port(ops)
    port.open()
    port.close()

    assert ("ioctl", dp.TIOCMBIC, struct.pack("i", dp.TIOCM_RTS)) in ops.calls
    assert struct.unpack("I", ops.rs485[:4])[0] & dp.SER_RS485_ENABLED
    assert ops.rs485[12:] == bytes(20), "the kernel rejects junk in the padding"
    assert struct.unpack(dp.TERMIOS2_FMT, ops.termios)[7] == 9600
    assert ops.calls[-1] == ("close",)


def test_open_failure_is_a_port_error() -> None:
    with pytest.raises(dp.DmxPortError, match="No such file"):
        fake_port(FakeOps(fail_open=True), "/dev/nope").open()


def test_frame_is_break_mab_start_code_and_slots() -> None:
    ops = FakeOps()
    port = fake_port(ops, break_us=176, mab_us=20)
    port.open()
    ops.calls.clear()
    port.send_frame(bytes([1, 2, 3]))

    assert ops.calls == [
        ("ioctl", dp.TIOCSBRK, b""),
        ("spin_us", 176),
        ("ioctl", dp.TIOCCBRK, b""),
        ("spin_us", 20),
        ("write", b"\x00\x01\x02\x03"),
    ]


def test_next_frame_waits_for_the_previous_one_to_leave_the_uart() -> None:
    ops = FakeOps()
    port = fake_port(ops)
    port.open()
    port.send_frame(bytes([1]))
    ops.calls.clear()
    port.send_frame(bytes([9]))

    assert ops.requests()[:2] == [dp.TIOCOUTQ, dp.TIOCSERGETLSR]
    assert ops.written() == [b"\x00\x09"]
    assert port.frames == 2


def test_timings_are_clamped_to_the_standard_minimum() -> None:
    port = fake_port(FakeOps(), break_us=10, mab_us=1)

    assert (port.break_us, port.mab_us) == (dp.MIN_BREAK_US, dp.MIN_MAB_US)


@pytest.mark.parametrize("asked, used", [(0, 0), (50, 50), (99999, dp.MAX_SLOT_GAP_US), (-5, 0)])
def test_pause_between_slots_is_clamped(asked: int, used: int) -> None:
    assert fake_port(FakeOps(), slot_gap_us=asked).slot_gap_us == used


def test_with_a_pause_the_frame_goes_out_byte_by_byte() -> None:
    ops = FakeOps()
    port = fake_port(ops, slot_gap_us=50)
    port.open()
    ops.calls.clear()
    port.send_frame(bytes([1, 2]))

    pause = (dp.SLOT_US + 50 - dp.SLEEP_OVERHEAD_US) / 1e6
    assert ops.calls[4:] == [
        ("write", b"\x00"),
        ("sleep", pause),
        ("write", b"\x01"),
        ("sleep", pause),
        ("write", b"\x02"),
        ("sleep", pause),
    ], "a real sleep after every byte: a busy-wait would starve the other ports"


def test_frame_time_grows_with_the_pause() -> None:
    plain = fake_port(FakeOps())
    slow = fake_port(FakeOps(), slot_gap_us=50)

    assert plain.frame_time_s(512) == pytest.approx((176 + 20 + 513 * 44) / 1e6)
    assert slow.frame_time_s(512) == pytest.approx((176 + 20 + 513 * (44 + 50 + dp.SLEEP_JITTER_US)) / 1e6)
