"""Art-Net and sACN (E1.31) input: a lighting console on the network takes over a port's universe.

While a source sends, its frames go straight into the port's buffer and win over MQTT writes; after
SOURCE_TIMEOUT_S of silence (or an sACN stream-terminated packet) the port goes back to its own
values. Among several sources the highest priority wins: sACN carries one (0..200, default 100),
Art-Net has none and counts as ARTNET_PRIORITY; equal priorities — the latest sender wins (LTP).

Both protocols are plain UDP frames, one universe per packet:

* Art-Net (port 6454): ArtDmx carries 2..512 slots; ArtPoll is answered with ArtPollReply so a
  console lists the controller as a node. The 15-bit port address is net << 8 | subnet << 4 | universe.
* sACN (port 5568, unicast or multicast 239.255.<hi>.<lo>): E1.31 data packets, universe 1..63999.

The packet builders are here too: tests and scripts/net_send.py use them.
"""

from __future__ import annotations

import logging
import select
import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

LOG = logging.getLogger("wb-mqtt-dmx")

ARTNET_PORT = 6454
SACN_PORT = 5568
ARTNET_ID = b"Art-Net\0"
OP_POLL, OP_POLL_REPLY, OP_DMX = 0x2000, 0x2100, 0x5000
ARTNET_PRIORITY = 100
ARTNET_MAX_UNIVERSE = 0x7FFF
SACN_MAX_UNIVERSE = 63999
ACN_ID = b"ASC-E1.17\0\0\0"
VECTOR_ROOT_DATA, VECTOR_FRAMING_DATA, VECTOR_DMP_SET = 4, 2, 2
SACN_OPT_TERMINATED = 0x40
SACN_OPT_PREVIEW = 0x80
SOURCE_TIMEOUT_S = 2.5
FPS_WINDOW_S = 2.0
POLL_S = 0.5
MAX_SLOTS = 512
NODE_SHORT_NAME = b"wb-mqtt-dmx"
NODE_LONG_NAME = b"Wiren Board DMX512 output (wb-mqtt-dmx)"
NODE_ESTA = 0x7FF0          # prototype code, not a registered manufacturer id
NODE_OEM = 0x00FF           # OemUnknown


@dataclass
class Frame:
    proto: str                  # "artnet" | "sacn"
    universe: int
    slots: bytes
    priority: int = ARTNET_PRIORITY
    terminated: bool = False


# ---- packets ---------------------------------------------------------------------------------

def artdmx_packet(universe: int, slots: bytes, sequence: int = 0) -> bytes:
    data = bytes(slots[:MAX_SLOTS])
    if len(data) % 2:
        data += b"\0"              # the spec wants an even length
    data = data or b"\0\0"
    return (ARTNET_ID + struct.pack("<H", OP_DMX) + struct.pack(">H", 14) + bytes([sequence & 0xFF, 0])
            + struct.pack("<H", universe & ARTNET_MAX_UNIVERSE) + struct.pack(">H", len(data)) + data)


def artpoll_packet() -> bytes:
    return ARTNET_ID + struct.pack("<H", OP_POLL) + struct.pack(">H", 14) + bytes([0, 0])


def artpoll_reply(ip: str, universes: list[int], bind_index: int = 1) -> bytes:
    """One ArtPollReply listing up to four output universes (a console shows them as node ports)."""
    outs = universes[:4]
    net = (outs[0] >> 8) & 0x7F if outs else 0
    sub = (outs[0] >> 4) & 0x0F if outs else 0
    ports = len(outs)
    sw_out = bytes([(u & 0x0F) for u in outs] + [0] * (4 - ports))
    report = f"#0001 [0000] {len(universes)} universe(s)".encode()[:63]
    return b"".join([
        ARTNET_ID, struct.pack("<H", OP_POLL_REPLY),
        socket.inet_aton(ip), struct.pack("<H", ARTNET_PORT),
        bytes([0, 1]),                                     # firmware version
        bytes([net, sub]),
        struct.pack(">H", NODE_OEM), b"\0",                # OEM, UBEA
        bytes([0xD0]),                                     # Status1: RDM off, indicators normal, port addr by network
        struct.pack("<H", NODE_ESTA),
        NODE_SHORT_NAME.ljust(18, b"\0")[:18],
        NODE_LONG_NAME.ljust(64, b"\0")[:64],
        report.ljust(64, b"\0"),
        struct.pack(">H", ports),
        bytes([0x80 if i < ports else 0 for i in range(4)]),   # PortTypes: output DMX512
        bytes(4),                                              # GoodInput
        bytes([0x80 if i < ports else 0 for i in range(4)]),   # GoodOutput: data being transmitted
        bytes(4), sw_out,                                      # SwIn, SwOut
        bytes(3), bytes(3),                                    # SwVideo/SwMacro/SwRemote, spare
        bytes([0]),                                            # Style: StNode
        bytes(6),                                              # MAC unknown
        socket.inet_aton(ip), bytes([bind_index]),
        bytes([0x08]),                                         # Status2: 15-bit port addresses (Art-Net 3+)
        bytes(26),
    ])


def parse_artnet(data: bytes) -> Union[Frame, str, None]:
    """A Frame for ArtDmx, "poll" for ArtPoll, None for anything else."""
    if len(data) < 12 or data[:8] != ARTNET_ID:
        return None
    opcode = struct.unpack_from("<H", data, 8)[0]
    if opcode == OP_POLL:
        return "poll"
    if opcode != OP_DMX or len(data) < 18:
        return None
    universe = struct.unpack_from("<H", data, 14)[0] & ARTNET_MAX_UNIVERSE
    length = struct.unpack_from(">H", data, 16)[0]
    slots = data[18:18 + min(length, MAX_SLOTS)]
    if not slots:
        return None
    return Frame("artnet", universe, bytes(slots))


def sacn_packet(universe: int, slots: bytes, priority: int = 100, sequence: int = 0, cid: bytes = b"",
                source_name: str = "wb-mqtt-dmx", options: int = 0) -> bytes:
    data = bytes([0]) + bytes(slots[:MAX_SLOTS])          # start code 0 + slots
    cid = (cid or uuid.uuid5(uuid.NAMESPACE_DNS, source_name).bytes)[:16].ljust(16, b"\0")
    dmp = (struct.pack(">H", 0x7000 | (10 + len(data))) + bytes([VECTOR_DMP_SET, 0xA1])
           + struct.pack(">HHH", 0, 1, len(data)) + data)
    framing = (struct.pack(">H", 0x7000 | (77 + len(dmp))) + struct.pack(">I", VECTOR_FRAMING_DATA)
               + source_name.encode()[:63].ljust(64, b"\0") + bytes([priority & 0xFF])
               + struct.pack(">H", 0) + bytes([sequence & 0xFF, options & 0xFF]) + struct.pack(">H", universe))
    root = (struct.pack(">HH", 0x0010, 0) + ACN_ID + struct.pack(">H", 0x7000 | (22 + len(framing)))
            + struct.pack(">I", VECTOR_ROOT_DATA) + cid)
    return root + framing + dmp


def parse_sacn(data: bytes) -> Optional[Frame]:
    if len(data) < 126 or data[4:16] != ACN_ID or struct.unpack_from(">I", data, 18)[0] != VECTOR_ROOT_DATA:
        return None
    if struct.unpack_from(">I", data, 40)[0] != VECTOR_FRAMING_DATA or data[117] != VECTOR_DMP_SET:
        return None
    priority = data[108]
    options = data[112]
    universe = struct.unpack_from(">H", data, 113)[0]
    count = struct.unpack_from(">H", data, 123)[0]          # start code + slots
    if data[125] != 0 or options & SACN_OPT_PREVIEW:        # not a dimmer frame, or preview data
        return None
    slots = data[126:126 + min(count - 1, MAX_SLOTS)]
    return Frame("sacn", universe, bytes(slots), priority, bool(options & SACN_OPT_TERMINATED))


# ---- which source owns a universe --------------------------------------------------------------

@dataclass
class Source:
    proto: str
    addr: str
    priority: int
    last_seen: float
    frames: int = 0
    window_start: float = 0.0
    window_frames: int = 0
    fps: float = 0.0

    @property
    def label(self) -> str:
        name = "Art-Net" if self.proto == "artnet" else "sACN"
        prio = f" · prio {self.priority}" if self.proto == "sacn" else ""
        return f"{name} {self.addr}{prio} · {self.fps:.0f} fps"


class Sources:
    """The network sources seen on one port's universe and which of them is in charge."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, timeout: float = SOURCE_TIMEOUT_S) -> None:
        self._clock = clock
        self._timeout = timeout
        self._sources: dict[tuple[str, str], Source] = {}
        self.active: Optional[Source] = None

    def offer(self, frame: Frame, addr: str) -> tuple[bool, bool]:
        """(output this frame?, did the source in charge change?)."""
        now = self._clock()
        key = (frame.proto, addr)
        if frame.terminated:
            self._sources.pop(key, None)
            return False, self._elect()
        src = self._sources.get(key)
        if src is None:
            src = self._sources[key] = Source(frame.proto, addr, frame.priority, now, window_start=now)
        src.priority, src.last_seen = frame.priority, now
        src.frames += 1
        src.window_frames += 1
        if now - src.window_start >= FPS_WINDOW_S:
            src.fps = src.window_frames / (now - src.window_start)
            src.window_start, src.window_frames = now, 0
        changed = self._elect()
        return self.active is src, changed

    def expire(self) -> bool:
        """Drop silent sources; True when the source in charge changed (usually: released)."""
        now = self._clock()
        for key, src in list(self._sources.items()):
            if now - src.last_seen > self._timeout:
                del self._sources[key]
        return self._elect()

    def _elect(self) -> bool:
        best = max(self._sources.values(), key=lambda s: (s.priority, s.last_seen), default=None)
        changed = best is not self.active
        self.active = best
        return changed

    @property
    def text(self) -> str:
        return self.active.label if self.active else ""


# ---- the receiver thread -------------------------------------------------------------------------

@dataclass
class Binding:
    path: str                              # serial port the universe is output on
    artnet: Optional[int] = None           # Art-Net port address, None = off
    sacn: Optional[int] = None             # sACN universe, None = off


class NetReceiver:
    """Listens on the Art-Net and sACN ports; frames of bound universes go to on_frame(path, slots),
    on_source(path, text) reports who is in charge ("" when nobody: back to MQTT)."""

    def __init__(self, bindings: list[Binding], on_frame: Callable[[str, bytes], None],
                 on_source: Callable[[str, str], None], clock: Callable[[], float] = time.monotonic) -> None:
        self._bindings = bindings
        self._on_frame = on_frame
        self._on_source = on_source
        self._sources = {b.path: Sources(clock) for b in bindings}
        self._artnet: dict[int, list[Binding]] = {}
        self._sacn: dict[int, list[Binding]] = {}
        for b in bindings:
            if b.artnet is not None:
                self._artnet.setdefault(b.artnet, []).append(b)
            if b.sacn is not None:
                self._sacn.setdefault(b.sacn, []).append(b)
        self._socks: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.error = ""

    def source_text(self, path: str) -> str:
        s = self._sources.get(path)
        return s.text if s else ""

    def start(self) -> None:
        try:
            if self._artnet:
                self._socks.append(self._udp(ARTNET_PORT, broadcast=True))
            if self._sacn:
                s = self._udp(SACN_PORT)
                for universe in self._sacn:
                    group = socket.inet_aton(f"239.255.{universe >> 8}.{universe & 0xFF}")
                    try:
                        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group + socket.inet_aton("0.0.0.0"))
                    except OSError as e:
                        LOG.warning("sACN universe %d: multicast join failed (%s), unicast still works", universe, e)
                self._socks.append(s)
        except OSError as e:
            self.error = f"network input: {e}"
            LOG.error("%s", self.error)
            return
        LOG.info("network input: Art-Net %s, sACN %s", sorted(self._artnet) or "off", sorted(self._sacn) or "off")
        self._thread = threading.Thread(target=self._loop, name="dmx-netin", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        for s in self._socks:
            s.close()
        self._socks = []

    @staticmethod
    def _udp(port: int, broadcast: bool = False) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if broadcast:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", port))
        s.setblocking(False)
        return s

    def _loop(self) -> None:
        last_expire = 0.0
        while not self._stop.is_set():
            ready, _, _ = select.select(self._socks, [], [], POLL_S)
            for s in ready:
                try:
                    data, addr = s.recvfrom(2048)
                except OSError:
                    continue
                self._handle(s, data, addr)
            now = time.monotonic()
            if now - last_expire >= POLL_S:
                last_expire = now
                for path, sources in self._sources.items():
                    if sources.expire():
                        self._on_source(path, sources.text)

    def _handle(self, sock: socket.socket, data: bytes, addr: tuple[str, int]) -> None:
        if sock.getsockname()[1] == ARTNET_PORT:
            parsed = parse_artnet(data)
            if parsed == "poll":
                self._reply_poll(sock, addr)
                return
            bindings = self._artnet.get(parsed.universe, []) if isinstance(parsed, Frame) else []
        else:
            parsed = parse_sacn(data)
            bindings = self._sacn.get(parsed.universe, []) if parsed else []
        if not isinstance(parsed, Frame):
            return
        for b in bindings:
            sources = self._sources[b.path]
            output, changed = sources.offer(parsed, addr[0])
            if output:
                self._on_frame(b.path, parsed.slots)
            if changed:
                self._on_source(b.path, sources.text)

    def _reply_poll(self, sock: socket.socket, addr: tuple[str, int]) -> None:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect((addr[0], ARTNET_PORT))
            ip = probe.getsockname()[0]
            probe.close()
            sock.sendto(artpoll_reply(ip, sorted(self._artnet)), (addr[0], ARTNET_PORT))
        except OSError as e:
            LOG.debug("ArtPollReply to %s failed: %s", addr[0], e)
