"""Art-Net / sACN packets round-trip through the builders and parsers; source election and timeout."""

from wb.mqtt_dmx.netin import (ARTNET_PRIORITY, SACN_OPT_PREVIEW, SACN_OPT_TERMINATED, Frame, Sources, artdmx_packet,
                               artpoll_packet, artpoll_reply, parse_artnet, parse_sacn, sacn_packet)


def test_artdmx_round_trip():
    frame = parse_artnet(artdmx_packet(0x0123, bytes(range(1, 11))))
    assert isinstance(frame, Frame) and frame.proto == "artnet" and frame.universe == 0x0123
    assert frame.slots == bytes(range(1, 11)) and frame.priority == ARTNET_PRIORITY and not frame.terminated
    odd = parse_artnet(artdmx_packet(1, b"\x07\x08\x09"))
    assert odd.slots == b"\x07\x08\x09\x00"                  # padded to an even length, as the spec wants
    assert len(parse_artnet(artdmx_packet(1, bytes(600))).slots) == 512
    assert parse_artnet(artpoll_packet()) == "poll"
    assert parse_artnet(b"Art-Net\0\x00\x99") is None and parse_artnet(b"nope") is None


def test_artpoll_reply_describes_the_universes():
    reply = artpoll_reply("192.168.1.10", [0x0123, 0x0124])
    assert len(reply) == 239 and reply[:8] == b"Art-Net\0" and reply[8:10] == b"\x00\x21"
    assert reply[10:14] == bytes([192, 168, 1, 10])
    assert reply[18] == 0x01 and reply[19] == 0x02             # net, subnet of the first universe
    assert reply[26:44].rstrip(b"\0") == b"wb-mqtt-dmx"
    assert reply[172:174] == b"\x00\x02"                        # two output ports
    assert reply[174:178] == bytes([0x80, 0x80, 0, 0]) and reply[190:194] == bytes([3, 4, 0, 0])


def test_sacn_round_trip():
    pkt = sacn_packet(7, bytes([10, 20, 30]), priority=150, sequence=5, source_name="console")
    assert len(pkt) == 126 + 3
    frame = parse_sacn(pkt)
    assert frame.proto == "sacn" and frame.universe == 7 and frame.slots == bytes([10, 20, 30])
    assert frame.priority == 150 and not frame.terminated
    assert parse_sacn(sacn_packet(7, b"\x01", options=SACN_OPT_TERMINATED)).terminated
    assert parse_sacn(sacn_packet(7, b"\x01", options=SACN_OPT_PREVIEW)) is None   # preview data is not output
    assert parse_sacn(b"\x00\x10" + bytes(200)) is None and parse_sacn(b"short") is None


def test_sources_priority_timeout_and_termination():
    now = [100.0]
    s = Sources(clock=lambda: now[0], timeout=2.5)
    assert s.offer(Frame("artnet", 1, b"\x01"), "10.0.0.1") == (True, True)
    assert s.text.startswith("Art-Net 10.0.0.1")
    # equal priority: the latest sender wins (LTP)
    now[0] += 0.1
    assert s.offer(Frame("sacn", 1, b"\x02", priority=100), "10.0.0.2") == (True, True)
    now[0] += 0.1
    assert s.offer(Frame("artnet", 1, b"\x03"), "10.0.0.1") == (True, True)
    # a higher sACN priority wins and keeps winning
    now[0] += 0.1
    assert s.offer(Frame("sacn", 1, b"\x04", priority=150), "10.0.0.3") == (True, True)
    assert s.text == "sACN 10.0.0.3 · prio 150 · 0 fps"
    now[0] += 0.1
    assert s.offer(Frame("artnet", 1, b"\x05"), "10.0.0.1") == (False, False)
    # the winner goes silent: after the timeout the others are back in charge
    for _ in range(5):
        now[0] += 0.5
        s.offer(Frame("artnet", 1, b"\x06"), "10.0.0.1")
    assert s.expire() is True and s.active.addr == "10.0.0.1"
    # stream terminated: released at once
    assert s.offer(Frame("artnet", 1, b"", terminated=True), "10.0.0.1") == (False, True)
    assert s.active is None and s.text == ""
    assert s.expire() is False


def test_sources_measure_fps():
    now = [0.0]
    s = Sources(clock=lambda: now[0])
    for _ in range(90):
        s.offer(Frame("artnet", 1, b"\x01"), "10.0.0.1")
        now[0] += 0.025
    assert 38 <= s.active.fps <= 42
