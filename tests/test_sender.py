import pytest

from wb.mqtt_dmx import dmx_port as dp
from wb.mqtt_dmx.sender import FRAME_MARGIN, WARMUP_FRAMES, FrameSender, Universe

from .fakes import FakeOps, fake_port


def sender_on(ops: FakeOps, universe: Universe, **port_settings: int) -> FrameSender:
    sender = FrameSender(universe, fake_port(ops, **port_settings), refresh_hz=40)
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
    sender = FrameSender(Universe(), fake_port(FakeOps(fail_open=True), "/dev/nope"))
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


def frames_take(sender: FrameSender, ops: FakeOps, seconds_by_frame: list[float]) -> None:
    """
    Make every frame take the given time on the line instead of going through the fake port.
    """

    def send(_frame: bytes) -> None:
        ops.now += seconds_by_frame[min(sender.frames, len(seconds_by_frame) - 1)]

    sender.port.send_frame = send  # type: ignore[method-assign]


def test_frames_that_miss_the_grid_are_counted() -> None:
    ops = FakeOps()
    sender = sender_on(ops, Universe(slots=512))
    frames_take(sender, ops, [0.060])
    sender.run(max_frames=WARMUP_FRAMES + 5)

    assert sender.late_frames == 5, "every frame after the warm-up"
    assert sender.max_late_us >= 35000


def test_with_a_pause_between_slots_no_frame_is_late() -> None:
    ops = FakeOps()
    sender = sender_on(ops, Universe(slots=512), slot_gap_us=50)
    frames_take(sender, ops, [0.060] * (WARMUP_FRAMES + 2) + [0.200])
    sender.run(max_frames=WARMUP_FRAMES + 6)

    assert sender.late_frames == 0 and sender.max_late_us == 0, "there is no grid to be late against"
    assert sender.period == pytest.approx(0.200 * FRAME_MARGIN)
