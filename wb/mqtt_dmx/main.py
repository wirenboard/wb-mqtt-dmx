"""Command line entry point of wb-mqtt-dmx.

    wb-mqtt-dmx test-port /dev/ttyRS485-2 [--seconds 10] [--pattern blink|full|ramp] [--hz 40] [--slots 512]
        stream a test pattern on a serial port; no MQTT, no config — for a scope and a decoder

The service itself (MQTT devices from a config) comes in the next stage.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time

from .dmx_port import DmxPort
from .sender import FrameSender, Universe

PATTERNS = ("blink", "full", "ramp")


def pattern_frame(pattern: str, slots: int, second: int) -> bytes:
    """blink = all channels on/off every second, full = all 255, ramp = channel number mod 256."""
    if pattern == "blink":
        return bytes([255 if second % 2 == 0 else 0]) * slots
    if pattern == "full":
        return bytes([255]) * slots
    if pattern == "ramp":
        return bytes(ch % 256 for ch in range(1, slots + 1))
    raise ValueError(f"unknown pattern {pattern!r}")


def cmd_test_port(args: argparse.Namespace) -> int:
    universe = Universe(args.slots)
    port = DmxPort(args.port, break_us=args.break_us, mab_us=args.mab_us, de_via_rts=not args.no_rts)
    sender = FrameSender(universe, port, args.hz)
    universe.write(1, pattern_frame(args.pattern, args.slots, 0))
    frames = int(args.seconds * sender.refresh_hz)
    print(f"{args.port}: {args.pattern}, {args.slots} slots, {sender.refresh_hz} Hz, {frames} frames, "
          f"BREAK {port.break_us} us, MAB {port.mab_us} us, DE via {'kernel' if args.no_rts else 'RTS'}")

    def blinker() -> None:
        second = 0
        while sender.frames < frames and not sender.error:
            time.sleep(1.0)
            second += 1
            universe.write(1, pattern_frame(args.pattern, args.slots, second))

    if args.pattern == "blink":
        threading.Thread(target=blinker, daemon=True).start()
    # SIGTERM/SIGINT end the run cleanly so the port gets its RS-485 mode and line settings back.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _s, _f: sender.stop(timeout=0))
    started = time.monotonic()
    sender.run(max_frames=frames)
    elapsed = time.monotonic() - started
    if sender.error:
        print(f"error: {sender.error}", file=sys.stderr)
        return 1
    print(f"sent {sender.frames} frames in {elapsed:.2f} s ({sender.frames / elapsed:.1f} fps), "
          f"{sender.late_frames} late, {port.drain_fallbacks} tcdrain fallbacks")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wb-mqtt-dmx", description="DMX512 output from a Wiren Board controller")
    parser.add_argument("--debug", action="store_true")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("test-port", help="stream a test pattern on a serial port (no MQTT)")
    p.add_argument("port", help="serial port, e.g. /dev/ttyRS485-2")
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--pattern", choices=PATTERNS, default="blink")
    p.add_argument("--hz", type=int, default=40)
    p.add_argument("--slots", type=int, default=512)
    p.add_argument("--break-us", type=int, default=176)
    p.add_argument("--mab-us", type=int, default=20)
    p.add_argument("--no-rts", action="store_true", help="leave direction control to the kernel/adapter")
    args = parser.parse_args(argv[1:])
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "test-port":
        return cmd_test_port(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
