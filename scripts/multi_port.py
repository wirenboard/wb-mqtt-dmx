"""Drive several DMX universes from threads of ONE process, the way the service will.

Measures what one test-port cannot: how the frame threads share the Python GIL and the CPU.
Run on the controller from the repository root:

    PYTHONPATH=. python3 scripts/multi_port.py --seconds 40 /dev/ttyRS485-1 /dev/ttyRS485-2 /dev/ttyMOD3 /dev/ttyMOD4
"""
import argparse
import os
import sys
import time

from wb.mqtt_dmx.dmx_port import DmxPort
from wb.mqtt_dmx.sender import FrameSender, Universe


def cpu_seconds() -> float:
    with open(f"/proc/{os.getpid()}/stat", encoding="ascii") as f:
        fields = f.read().split(")")[-1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ports", nargs="+")
    ap.add_argument("--seconds", type=float, default=40.0)
    ap.add_argument("--hz", type=int, default=40)
    ap.add_argument("--slots", type=int, default=512)
    ap.add_argument("--no-rt", action="store_true")
    ap.add_argument("--stagger", action="store_true", help="offset each universe's grid by period/N")
    args = ap.parse_args()

    senders = []
    for i, path in enumerate(args.ports):
        universe = Universe(args.slots)
        universe.write(1, bytes(ch % 256 for ch in range(1, args.slots + 1)))
        sender = FrameSender(universe, DmxPort(path), args.hz, realtime=not args.no_rt)
        senders.append(sender)
        sender.start()
        if args.stagger:
            time.sleep(sender.period / len(args.ports))
    t0, c0 = time.monotonic(), cpu_seconds()
    time.sleep(args.seconds)
    t1, c1 = time.monotonic(), cpu_seconds()
    for s in senders:
        s.stop()
    print(f"{len(senders)} universes, {args.hz} Hz x {args.slots}: process CPU {100 * (c1 - c0) / (t1 - t0):.1f} % "
          f"of one core over {t1 - t0:.0f} s")
    rc = 0
    for s in senders:
        fps = s.frames / (t1 - t0)
        print(f"  {s.port.path}: {s.frames} frames ({fps:.1f} fps), {s.late_frames} late, "
              f"worst start lateness {s.max_late_us:.0f} us, {s.port.drain_fallbacks} tcdrain fallbacks, "
              f"error={s.error!r}")
        if s.error:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
