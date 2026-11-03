"""Measure a DMX frame on the Rigol DHO804: BREAK, MAB, frame period, byte errors.

CH1 on DMX line A (against GND), 10:1 probe. The scope is armed on a negative pulse longer than
50 us (the BREAK), one 50 ms window at 1 Mpts is fetched and decoded as 250 kbit/s 8N2.

Usage: dmx_measure.py [--window 0.05] [--level 2.0]
"""
import argparse
import time

from scope import Scope

BIT_US = 4.0
MIN_BREAK_US = 88.0


def arm(sc: Scope, window_s: float, level: float) -> None:
    for c in (":CHANnel1:DISPlay ON", ":CHANnel1:PROBe 10", ":CHANnel1:SCALe 1", ":CHANnel1:OFFSet -2",
              ":CHANnel1:COUPling DC", ":BUS1:DISPlay OFF",
              ":TRIGger:MODE PULSe", ":TRIGger:PULSe:SOURce CHANnel1", ":TRIGger:PULSe:POLarity NEGative",
              ":TRIGger:PULSe:WHEN GREater", ":TRIGger:PULSe:LWIDth 5e-5", f":TRIGger:PULSe:LEVel {level}",
              ":TRIGger:SWEep SINGle", ":ACQuire:MDEPth 1M",
              f":TIMebase:MAIN:SCALe {window_s / 10}", f":TIMebase:MAIN:OFFSet {window_s * 0.4}", ":SINGle"):
        sc.cmd(c)
    for _ in range(50):
        time.sleep(0.2)
        if sc.query(":TRIGger:STATus?") == "STOP":
            return
    raise SystemExit("no trigger: is CH1 on line A and is a sender running?")


def fetch(sc: Scope):
    sc.cmd(":WAVeform:SOURce CHANnel1")
    sc.cmd(":WAVeform:MODE RAW")
    sc.cmd(":WAVeform:FORMat BYTE")
    pre = sc.query(":WAVeform:PREamble?").split(",")
    points, xinc, xorig = int(pre[2]), float(pre[4]), float(pre[5])
    yinc, yorig, yref = float(pre[7]), float(pre[8]), float(pre[9])
    # The preamble under-reports the point count (250000 at 1 Mpts), so read up to the memory
    # depth and stop at the first short chunk.
    try:
        points = max(points, int(float(sc.query(":ACQuire:MDEPth?"))))
    except ValueError:
        pass
    raw = b""
    start = 1
    while start <= points:
        stop = min(points, start + 250000 - 1)
        sc.cmd(f":WAVeform:STARt {start}")
        sc.cmd(f":WAVeform:STOP {stop}")
        chunk = sc.query(":WAVeform:DATA?", binary=True)
        raw += chunk
        if len(chunk) < stop - start + 1:
            break
        start = stop + 1
    volts = [(b - yorig - yref) * yinc for b in raw]
    return volts, xinc * 1e6, xorig * 1e6


def decode(volts, dt_us, t0_us):
    """Events: ('BREAK', t, len_us), ('MAB', t, len_us), ('BYTE', t, value, stop_ok)."""
    vmax, vmin = max(volts), min(volts)
    thr = (vmax + vmin) / 2
    bits = [1 if v > thr else 0 for v in volts]
    n = len(bits)
    out = []
    i = 0
    while i < n:
        if bits[i]:
            i += 1
            continue
        j = i
        while j < n and not bits[j]:
            j += 1
        low_us = (j - i) * dt_us
        if low_us >= MIN_BREAK_US:
            k = j
            while k < n and bits[k]:
                k += 1
            out.append(("BREAK", t0_us + i * dt_us, low_us))
            out.append(("MAB", t0_us + j * dt_us, (k - j) * dt_us))
            i = k
            continue
        base = i + int(round(BIT_US * 0.5 / dt_us))
        step = BIT_US / dt_us
        byte = 0
        for b in range(8):
            idx = int(round(base + (1 + b) * step))
            if idx < n and bits[idx]:
                byte |= 1 << b
        stop_idx = int(round(base + 9 * step))
        out.append(("BYTE", t0_us + i * dt_us, byte, stop_idx < n and bits[stop_idx] == 1))
        i = int(round(i + 11 * step)) - 2
        while i < n and not bits[i]:
            i += 1
    return out, vmin, vmax


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=float, default=0.05, help="capture window, s")
    ap.add_argument("--level", type=float, default=2.0, help="trigger level, V")
    args = ap.parse_args()
    sc = Scope()
    arm(sc, args.window, args.level)
    volts, dt_us, t0_us = fetch(sc)
    sc.cmd(":TRIGger:SWEep NORMal")
    sc.cmd(":RUN")
    events, vmin, vmax = decode(volts, dt_us, t0_us)
    print(f"samples={len(volts)} dt={dt_us * 1000:.0f} ns  line A: {vmin:.2f}..{vmax:.2f} V")
    breaks = [e for e in events if e[0] == "BREAK"]
    mabs = [e for e in events if e[0] == "MAB"]
    for b, m in zip(breaks, mabs):
        print(f"  t={b[1]:9.1f} us  BREAK {b[2]:6.1f} us   MAB {m[2]:6.1f} us")
    for a, b in zip(breaks, breaks[1:]):
        print(f"  frame period {b[1] - a[1]:.1f} us")
    frames = []
    cur = None
    for e in events:
        if e[0] == "BREAK":
            cur = []
            frames.append(cur)
        elif e[0] == "BYTE" and cur is not None:
            cur.append(e)
    for k, f in enumerate(frames):
        bad = sum(1 for e in f if not e[3])
        head = " ".join(f"{e[2]:02X}" for e in f[:12])
        print(f"  frame {k}: {len(f)} bytes (start code + {len(f) - 1} slots), {bad} bad stop bits: {head} ...")


if __name__ == "__main__":
    main()
