import pytest

from wb.mqtt_dmx import dmx_port as dp
from wb.mqtt_dmx.sender import FRAME_MARGIN, FrameSender, Universe

from .fakes import PORT_PATH, FakeOps


def sender_on(ops: FakeOps, universe: Universe, **port_settings: int) -> FrameSender:
    sender = FrameSender(
        universe, dp.DmxPort(PORT_PATH, ops=ops, **port_settings), refresh_hz=40, realtime=False
    )
    sender.clock, sender.sleep = ops.monotonic, ops.sleep
    return sender


def test_universe_keeps_writes_and_drops_what_is_past_the_end() -> None:
    universe = Universe(slots=4)
    universe.write(2, bytes([10, 20]))
    universe.write(511, bytes([1, 2, 3, 4]))

    assert universe.snapshot() == bytes([0, 10, 20, 0]), "a snapshot is as long as the port's frame"
    assert universe.get(512) == 2


def test_sender_streams_the_universe() -> None:
    ops = FakeOps()
    universe = Universe(slots=4)
    universe.write(2, bytes([10, 20]))
    sender = sender_on(ops, universe)
    sender.run(max_frames=3)

    assert ops.written() == [b"\x00\x00\x0a\x14\x00"] * 3
    assert sender.frames == 3 and sender.late_frames == 0
    assert not sender.running, "the port is closed when the loop ends"


def test_sender_reports_a_port_that_does_not_open() -> None:
    sender = FrameSender(Universe(), dp.DmxPort("/dev/nope", ops=FakeOps(fail_open=True)), realtime=False)
    sender.sleep = lambda _seconds: sender.stop()  # the first idle sleep after the failure ends the loop
    sender.run()

    assert sender.frames == 0 and not sender.running
    assert "No such file" in sender.error


def test_period_follows_the_refresh_rate() -> None:
    assert sender_on(FakeOps(), Universe(slots=512)).period == pytest.approx(0.025)


def test_period_stretches_when_a_frame_with_pauses_does_not_fit() -> None:
    ops = FakeOps()
    sender = sender_on(ops, Universe(slots=512), slot_gap_us=50)

    assert sender.period == pytest.approx(sender.port.frame_time_s(512) * FRAME_MARGIN)
    assert 15 < 1 / sender.period < 18


def test_period_stays_when_a_short_frame_with_pauses_fits() -> None:
    assert sender_on(FakeOps(), Universe(slots=64), slot_gap_us=50).period == pytest.approx(0.025)


def test_period_then_follows_what_a_frame_really_takes() -> None:
    ops = FakeOps()
    sender = sender_on(ops, Universe(slots=512), slot_gap_us=50)
    sender.run(max_frames=2)

    took = (176 + 20) / 1e6 + 513 * (dp.SLOT_US + 50 - dp.SLEEP_OVERHEAD_US) / 1e6
    assert sender.period == pytest.approx(took * FRAME_MARGIN, rel=1e-3)
