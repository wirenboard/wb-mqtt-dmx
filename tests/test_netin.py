from dataclasses import dataclass

from wb.mqtt_dmx.netin import (
    ARTNET_PRIORITY,
    SACN_OPT_PREVIEW,
    SACN_OPT_TERMINATED,
    Binding,
    Frame,
    NetReceiver,
    Sources,
    artpoll_reply,
    parse_artnet,
    parse_sacn,
)

from .packets import artdmx_packet, artpoll_packet, sacn_packet


@dataclass
class Clock:
    """
    A clock the test moves by hand.
    """

    now: float = 100.0

    def __call__(self) -> float:
        return self.now


def test_artdmx_is_parsed_into_a_frame() -> None:
    frame = parse_artnet(artdmx_packet(0x0123, bytes(range(1, 11))))

    assert isinstance(frame, Frame)
    assert (frame.proto, frame.universe, frame.slots) == ("artnet", 0x0123, bytes(range(1, 11)))
    assert frame.priority == ARTNET_PRIORITY and not frame.terminated


def test_artdmx_is_cut_at_512_slots() -> None:
    frame = parse_artnet(artdmx_packet(1, bytes(600)))

    assert isinstance(frame, Frame) and len(frame.slots) == 512


def test_artpoll_is_told_apart_from_data() -> None:
    assert parse_artnet(artpoll_packet()) == "poll"


def test_foreign_datagrams_are_ignored() -> None:
    assert parse_artnet(b"Art-Net\0\x00\x99") is None
    assert parse_artnet(b"nope") is None
    assert parse_sacn(b"\x00\x10" + bytes(200)) is None
    assert parse_sacn(b"short") is None


def test_artpoll_reply_lists_the_universes_as_output_ports() -> None:
    reply = artpoll_reply("192.168.1.10", [0x0123, 0x0124])

    assert len(reply) == 239
    assert reply[:8] == b"Art-Net\0" and reply[8:10] == b"\x00\x21"
    assert reply[10:14] == bytes([192, 168, 1, 10])
    assert (reply[18], reply[19]) == (0x01, 0x02), "net and subnet of the first universe"
    assert reply[26:44].rstrip(b"\0") == b"wb-mqtt-dmx"
    assert reply[172:174] == b"\x00\x02", "two ports"
    assert reply[174:178] == bytes([0x80, 0x80, 0, 0]) and reply[190:194] == bytes([3, 4, 0, 0])


def test_sacn_data_packet_is_parsed_into_a_frame() -> None:
    packet = sacn_packet(7, bytes([10, 20, 30]), priority=150)
    frame = parse_sacn(packet)

    assert len(packet) == 126 + 3
    assert frame is not None
    assert (frame.proto, frame.universe, frame.slots, frame.priority) == ("sacn", 7, bytes([10, 20, 30]), 150)
    assert not frame.terminated


def test_sacn_stream_terminated_is_flagged() -> None:
    frame = parse_sacn(sacn_packet(7, b"\x01", options=SACN_OPT_TERMINATED))

    assert frame is not None and frame.terminated


def test_sacn_preview_data_is_not_output() -> None:
    assert parse_sacn(sacn_packet(7, b"\x01", options=SACN_OPT_PREVIEW)) is None


def test_among_equal_priorities_the_latest_sender_wins() -> None:
    clock = Clock()
    sources = Sources(clock=clock, timeout=2.5)

    assert sources.offer(Frame("artnet", 1, b"\x01"), "10.0.0.1") == (True, True)
    clock.now += 0.1
    assert sources.offer(Frame("sacn", 1, b"\x02", priority=100), "10.0.0.2") == (True, True)
    clock.now += 0.1
    assert sources.offer(Frame("artnet", 1, b"\x03"), "10.0.0.1") == (True, True)
    assert sources.text.startswith("Art-Net 10.0.0.1")


def test_higher_sacn_priority_wins_and_keeps_winning() -> None:
    clock = Clock()
    sources = Sources(clock=clock, timeout=2.5)
    sources.offer(Frame("artnet", 1, b"\x01"), "10.0.0.1")
    clock.now += 0.1

    assert sources.offer(Frame("sacn", 1, b"\x04", priority=150), "10.0.0.3") == (True, True)
    assert sources.text == "sACN 10.0.0.3 · prio 150 · 0 fps"
    clock.now += 0.1
    assert sources.offer(Frame("artnet", 1, b"\x05"), "10.0.0.1") == (False, False)


def test_when_the_winner_goes_silent_the_next_source_takes_over() -> None:
    clock = Clock()
    sources = Sources(clock=clock, timeout=2.5)
    sources.offer(Frame("sacn", 1, b"\x04", priority=150), "10.0.0.3")
    for _ in range(6):
        clock.now += 0.5
        sources.offer(Frame("artnet", 1, b"\x06"), "10.0.0.1")

    assert sources.expire() is True
    assert sources.active is not None and sources.active.addr == "10.0.0.1"


def test_terminated_stream_releases_the_port_at_once() -> None:
    sources = Sources(clock=Clock(), timeout=2.5)
    sources.offer(Frame("artnet", 1, b"\x01"), "10.0.0.1")

    assert sources.offer(Frame("artnet", 1, b"", terminated=True), "10.0.0.1") == (False, True)
    assert sources.active is None and sources.text == ""
    assert sources.expire() is False


def test_art_net_survives_a_two_second_keepalive_and_sacn_times_out_sooner() -> None:
    clock = Clock(0.0)
    sources = Sources(clock=clock)
    sources.offer(Frame("sacn", 2, b"\x01", priority=150), "10.0.0.2")
    sources.offer(Frame("artnet", 1, b"\x01"), "10.0.0.1")

    clock.now = 3.0
    assert sources.expire() is True, "E1.31 gives a source 2.5 s"
    assert sources.active is not None and sources.active.proto == "artnet"
    clock.now = 5.5
    assert sources.expire() is True and sources.active is None


def test_frame_rate_of_a_source_is_measured() -> None:
    clock = Clock(0.0)
    sources = Sources(clock=clock)
    for _ in range(90):
        sources.offer(Frame("artnet", 1, b"\x01"), "10.0.0.1")
        clock.now += 0.025

    assert sources.active is not None and 38 <= sources.active.fps <= 42


def test_receiver_routes_a_universe_to_its_port() -> None:
    frames: list[tuple[str, bytes]] = []
    owners: list[tuple[str, str]] = []
    receiver = NetReceiver(
        [Binding("/dev/ttyRS485-1", artnet=5), Binding("/dev/ttyRS485-2", sacn=9)],
        on_frame=lambda path, slots: frames.append((path, slots)),
        on_source=lambda path, text: owners.append((path, text)),
    )

    assert receiver.feed("artnet", artdmx_packet(5, b"\x01\x02"), "10.0.0.1") is False
    assert receiver.feed("sacn", sacn_packet(9, b"\x07"), "10.0.0.2") is False
    receiver.feed("artnet", artdmx_packet(6, b"\x09\x09"), "10.0.0.1")  # nobody listens to universe 6

    assert frames == [("/dev/ttyRS485-1", b"\x01\x02"), ("/dev/ttyRS485-2", b"\x07")]
    assert [path for path, _text in owners] == ["/dev/ttyRS485-1", "/dev/ttyRS485-2"]
    assert receiver.source_text("/dev/ttyRS485-1").startswith("Art-Net 10.0.0.1")


def test_receiver_asks_for_a_reply_to_artpoll() -> None:
    receiver = NetReceiver([Binding("/dev/ttyRS485-1", artnet=5)], lambda *_: None, lambda *_: None)

    assert receiver.feed("artnet", artpoll_packet(), "10.0.0.1") is True
