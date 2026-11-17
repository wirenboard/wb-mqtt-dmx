"""A pause between slots: the frame goes out byte by byte and the refresh rate follows the frame length."""

import pytest

from test_dmx_port import FakeOps

from wb.mqtt_dmx import dmx_port as dp
from wb.mqtt_dmx import templates
from wb.mqtt_dmx.config import ConfigError, load_config
from wb.mqtt_dmx.sender import FrameSender, Universe

TEMPLATES = templates.load_templates([])


def test_frame_goes_out_byte_by_byte_with_a_pause_after_each():
    ops = FakeOps()
    port = dp.DmxPort("/dev/ttyRS485-2", break_us=176, mab_us=20, ops=ops, slot_gap_us=50)
    port.open()
    ops.calls.clear()
    port.send_frame(bytes([1, 2]))
    wait = dp.SLOT_US + 50
    assert ops.calls == [
        ("ioctl", dp.TIOCSBRK, b""), ("spin_us", 176), ("ioctl", dp.TIOCCBRK, b""), ("spin_us", 20),
        ("write", b"\x00"), ("spin_us", wait), ("write", b"\x01"), ("spin_us", wait), ("write", b"\x02"),
        ("spin_us", wait),
    ]


def test_frame_time_and_clamping():
    plain = dp.DmxPort("/dev/x", ops=FakeOps())
    assert plain.slot_gap_us == 0
    assert plain.frame_time_s(512) == pytest.approx((176 + 20 + 513 * 44) / 1e6)       # 22.8 ms back to back
    slow = dp.DmxPort("/dev/x", ops=FakeOps(), slot_gap_us=50)
    assert slow.frame_time_s(512) == pytest.approx((176 + 20 + 513 * (44 + 50 + dp.WRITE_US)) / 1e6)
    assert dp.DmxPort("/dev/x", ops=FakeOps(), slot_gap_us=99999).slot_gap_us == dp.MAX_SLOT_GAP_US
    assert dp.DmxPort("/dev/x", ops=FakeOps(), slot_gap_us=-5).slot_gap_us == 0


def test_sender_stretches_the_period_to_the_frame():
    ops = FakeOps()
    fast = FrameSender(Universe(slots=512), dp.DmxPort("/dev/x", ops=ops), refresh_hz=40, realtime=False)
    assert fast.period == pytest.approx(0.025)
    slow_port = dp.DmxPort("/dev/x", ops=ops, slot_gap_us=50)
    slow = FrameSender(Universe(slots=512), slow_port, refresh_hz=40, realtime=False)
    assert slow.period == pytest.approx(slow_port.frame_time_s(512) * 1.05) and 17 < 1 / slow.period < 19
    short = FrameSender(Universe(slots=64), dp.DmxPort("/dev/x", ops=ops, slot_gap_us=50), refresh_hz=40,
                        realtime=False)
    assert short.period == pytest.approx(0.025)          # 65 slots with pauses still fit 40 Hz


def test_config_reads_the_pause():
    def cfg(**advanced):
        return {"ports": [{"settings": {"path": "/dev/ttyRS485-2", "advanced": advanced}, "devices": []}]}

    (port,) = load_config(cfg(), TEMPLATES)
    assert port.slot_gap_us == 0
    (port,) = load_config(cfg(slot_gap_us=50), TEMPLATES)
    assert port.slot_gap_us == 50
    with pytest.raises(ConfigError, match="slot_gap_us"):
        load_config(cfg(slot_gap_us=5000), TEMPLATES)
