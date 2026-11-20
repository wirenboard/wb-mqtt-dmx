"""
Art-Net and sACN packets the way a lighting console builds them.

The service only receives, so the builders live with the tests.
"""

import struct
import uuid

from wb.mqtt_dmx.netin import (
    ACN_ID,
    ARTNET_ID,
    ARTNET_MAX_UNIVERSE,
    MAX_SLOTS,
    OP_DMX,
    OP_POLL,
    VECTOR_DMP_SET,
    VECTOR_FRAMING_DATA,
    VECTOR_ROOT_DATA,
)


def artdmx_packet(universe: int, slots: bytes, sequence: int = 0) -> bytes:
    """
    An ArtDmx packet as a console would send it.
    """
    data = bytes(slots[:MAX_SLOTS])
    if len(data) % 2:
        data += b"\0"  # the spec wants an even length
    data = data or b"\0\0"
    return (
        ARTNET_ID
        + struct.pack("<H", OP_DMX)
        + struct.pack(">H", 14)
        + bytes([sequence & 0xFF, 0])
        + struct.pack("<H", universe & ARTNET_MAX_UNIVERSE)
        + struct.pack(">H", len(data))
        + data
    )


def artpoll_packet() -> bytes:
    return ARTNET_ID + struct.pack("<H", OP_POLL) + struct.pack(">H", 14) + bytes([0, 0])


def sacn_packet(universe: int, slots: bytes, priority: int = 100, options: int = 0) -> bytes:
    """
    An E1.31 data packet as a console would send it.
    """
    source_name = "console"
    sequence = 0
    data = bytes([0]) + bytes(slots[:MAX_SLOTS])  # start code 0 + slots
    cid = uuid.uuid5(uuid.NAMESPACE_DNS, source_name).bytes
    dmp = (
        struct.pack(">H", 0x7000 | (10 + len(data)))
        + bytes([VECTOR_DMP_SET, 0xA1])
        + struct.pack(">HHH", 0, 1, len(data))
        + data
    )
    framing = (
        struct.pack(">H", 0x7000 | (77 + len(dmp)))
        + struct.pack(">I", VECTOR_FRAMING_DATA)
        + source_name.encode()[:63].ljust(64, b"\0")
        + bytes([priority & 0xFF])
        + struct.pack(">H", 0)
        + bytes([sequence & 0xFF, options & 0xFF])
        + struct.pack(">H", universe)
    )
    root = (
        struct.pack(">HH", 0x0010, 0)
        + ACN_ID
        + struct.pack(">H", 0x7000 | (22 + len(framing)))
        + struct.pack(">I", VECTOR_ROOT_DATA)
        + cid
    )
    return root + framing + dmp
