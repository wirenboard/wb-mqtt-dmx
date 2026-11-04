#!/usr/bin/env python3
"""Send a test universe to a controller over Art-Net or sACN, or poll it for ArtPollReply.

    python scripts/net_send.py HOST [--proto artnet|sacn] [--universe N] [--pattern ramp|blink|chase|fill]
                                    [--fps 40] [--seconds 10] [--priority 100] [--terminate]
    python scripts/net_send.py HOST --poll

Run from any machine that can reach the controller (python3, no dependencies).
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wb.mqtt_dmx.netin import (ARTNET_PORT, SACN_OPT_TERMINATED, SACN_PORT, artdmx_packet, artpoll_packet,  # noqa: E402
                               parse_artnet, sacn_packet)


def frame(pattern: str, slots: int, n: int, fps: int) -> bytes:
    if pattern == "ramp":
        return bytes((ch + n) % 256 for ch in range(slots))
    if pattern == "blink":
        return bytes([255 if (n // fps) % 2 == 0 else 0]) * slots
    if pattern == "chase":
        pos = (n // max(1, fps // 10)) % slots
        return bytes(255 if ch == pos else 0 for ch in range(slots))
    return bytes([255]) * slots


def poll(host: str) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", ARTNET_PORT))
    s.settimeout(3.0)
    s.sendto(artpoll_packet(), (host, ARTNET_PORT))
    while True:
        try:
            data, addr = s.recvfrom(2048)
        except socket.timeout:
            print("no ArtPollReply")
            return 1
        if data[:8] == b"Art-Net\0" and struct.unpack_from("<H", data, 8)[0] == 0x2100:
            short, long_ = data[26:44].rstrip(b"\0"), data[44:108].rstrip(b"\0")
            ports = struct.unpack_from(">H", data, 172)[0]
            universes = [(data[18] << 8) | (data[19] << 4) | data[190 + i] for i in range(ports)]
            print(f"{addr[0]}: {short.decode()} - {long_.decode()}; universes {universes}")
            return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("host")
    ap.add_argument("--proto", choices=["artnet", "sacn"], default="artnet")
    ap.add_argument("--universe", type=int, default=1)
    ap.add_argument("--pattern", choices=["ramp", "blink", "chase", "fill"], default="ramp")
    ap.add_argument("--slots", type=int, default=64)
    ap.add_argument("--fps", type=int, default=40)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--priority", type=int, default=100)
    ap.add_argument("--terminate", action="store_true", help="sACN: say goodbye at the end (instant release)")
    ap.add_argument("--poll", action="store_true", help="send ArtPoll and print the reply")
    args = ap.parse_args()
    if args.poll:
        return poll(args.host)
    port = ARTNET_PORT if args.proto == "artnet" else SACN_PORT
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    period = 1.0 / args.fps
    total = int(args.seconds * args.fps)
    t0 = time.monotonic()
    for n in range(total):
        slots = frame(args.pattern, args.slots, n, args.fps)
        if args.proto == "artnet":
            pkt = artdmx_packet(args.universe, slots, sequence=n % 256)
        else:
            pkt = sacn_packet(args.universe, slots, priority=args.priority, sequence=n % 256)
        s.sendto(pkt, (args.host, port))
        time.sleep(max(0.0, t0 + (n + 1) * period - time.monotonic()))
    if args.terminate and args.proto == "sacn":
        for _ in range(3):
            s.sendto(sacn_packet(args.universe, bytes(args.slots), priority=args.priority,
                                 options=SACN_OPT_TERMINATED), (args.host, port))
    print(f"sent {total} {args.proto} frames to {args.host} universe {args.universe}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
