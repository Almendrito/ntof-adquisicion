"""
Rigol MHO984 Diagnostic Script
==============================

Probes a Rigol MHO984 oscilloscope over LAN (or USB) to determine its
acquisition capabilities for high-throughput pulse capture (e.g. PMT +
plastic scintillator, Compton-edge measurement).

What it checks
--------------
1. Connection + identity (*IDN?)
2. Available acquisition modes (NORMAL, AVERAGE, PEAK, HIGH-RES,
   ULTRA-ACQUIRE / fast capture)
3. Segmented memory ("UltraAcquire") support and max segment count
4. Memory depth options
5. Real-time sample rate and bandwidth
6. Histogram math function (for on-scope pulse-height analysis)
7. Estimated throughput in pulses/sec for three workflows:
     a) segmented memory + batch transfer
     b) on-scope histogram (no waveform transfer)
     c) one-shot trigger-and-transfer loop

Requirements
------------
    pip install pyvisa pyvisa-py numpy

A VISA backend is needed. pyvisa-py is pure Python and works fine for
LAN (TCPIP) and USB-TMC on Linux/macOS/Windows without NI-VISA.

Usage
-----
    python rigol_mho984_diagnostic.py --ip 192.168.1.50
    python rigol_mho984_diagnostic.py --resource USB0::0x1AB1::...::INSTR
    python rigol_mho984_diagnostic.py --list           # list visible devices
    python rigol_mho984_diagnostic.py --ip 192.168.1.50 --benchmark
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import pyvisa
except ImportError:
    sys.exit("pyvisa is required. Install with:  pip install pyvisa pyvisa-py")

try:
    import numpy as np
except ImportError:
    np = None  # only needed for the optional --benchmark waveform transfer


# ---------------------------------------------------------------------------
# SCPI helper
# ---------------------------------------------------------------------------

@dataclass
class Scope:
    """Thin wrapper around a pyvisa instrument with safe query/write."""
    inst: "pyvisa.resources.MessageBasedResource"
    timeout_ms: int = 5000

    def __post_init__(self):
        self.inst.timeout = self.timeout_ms
        # Rigol prefers LF terminator; pyvisa-py + TCPIP usually auto-detects
        self.inst.read_termination = "\n"
        self.inst.write_termination = "\n"

    def q(self, cmd: str, default: str = "<unsupported>") -> str:
        """Query and return stripped string, or default on failure."""
        try:
            return self.inst.query(cmd).strip()
        except Exception as e:
            return f"{default}  (error: {type(e).__name__})"

    def w(self, cmd: str) -> bool:
        """Write and return True on success."""
        try:
            self.inst.write(cmd)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Diagnostic report container
# ---------------------------------------------------------------------------

@dataclass
class Report:
    sections: list[tuple[str, list[tuple[str, str]]]] = field(default_factory=list)

    def section(self, title: str, rows: list[tuple[str, str]]):
        self.sections.append((title, rows))

    def print(self):
        width = 64
        for title, rows in self.sections:
            print()
            print("=" * width)
            print(f"  {title}")
            print("=" * width)
            keylen = max((len(k) for k, _ in rows), default=0)
            for k, v in rows:
                print(f"  {k.ljust(keylen)} : {v}")


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def probe_identity(s: Scope) -> list[tuple[str, str]]:
    return [
        ("IDN", s.q("*IDN?")),
        ("System version",   s.q(":SYSTem:VERSion?")),
        ("Options installed", s.q(":SYSTem:OPTion:INSTall?")),
    ]


def probe_acquisition(s: Scope) -> list[tuple[str, str]]:
    rows = []
    rows.append(("Acquire type",     s.q(":ACQuire:TYPE?")))
    rows.append(("Sample rate (Sa/s)", s.q(":ACQuire:SRATe?")))
    rows.append(("Memory depth",     s.q(":ACQuire:MDEPth?")))
    rows.append(("Bandwidth limit",  s.q(":CHANnel1:BWLimit?")))

    # Try each acquisition mode the MHO900 family advertises
    modes_to_try = ["NORMal", "AVERages", "PEAK", "HRESolution",
                    "ULTRa", "ULTRAACQ", "FASTAcq"]
    supported = []
    original = s.q(":ACQuire:TYPE?")
    for m in modes_to_try:
        if s.w(f":ACQuire:TYPE {m}"):
            time.sleep(0.05)
            got = s.q(":ACQuire:TYPE?")
            # If the scope accepted the command, `got` will not be the original
            # mode (unless the requested mode happens to equal original).
            if got and "unsupported" not in got and "error" not in got.lower():
                supported.append(f"{m} → {got}")
    # restore
    if original and "unsupported" not in original:
        s.w(f":ACQuire:TYPE {original.split()[0]}")
    rows.append(("Acquire modes accepted",
                 ", ".join(supported) if supported else "none probed"))
    return rows


def probe_segmented(s: Scope) -> list[tuple[str, str]]:
    """
    Check segmented-memory / UltraAcquire capability.

    Rigol's newer scopes (DHO/MHO families) expose segmented capture under
    :ACQuire:ULTRa: ...  The exact subkeys vary by firmware, so we try
    several common ones and report what works.
    """
    rows = []
    candidates = [
        ":ACQuire:ULTRa:ENABle?",
        ":ACQuire:ULTRa:MAXFrame?",
        ":ACQuire:ULTRa:FRAMe?",
        ":ACQuire:ULTRa:TIMeout?",
        ":ACQuire:ULTRa:MODE?",
        # Older/alternative spellings
        ":ACQuire:SEGMented:STATe?",
        ":ACQuire:SEGMented:COUNt?",
        ":ACQuire:MMODe?",          # mode select on some models
    ]
    any_supported = False
    for c in candidates:
        v = s.q(c)
        ok = "unsupported" not in v and "error" not in v.lower() and v != ""
        rows.append((c, v if ok else "(not supported)"))
        if ok:
            any_supported = True

    rows.append(("Segmented capture available",
                 "YES — use UltraAcquire / segmented mode for batch capture"
                 if any_supported else
                 "Not detected on this firmware — try latest Rigol firmware"))
    return rows


def probe_histogram(s: Scope) -> list[tuple[str, str]]:
    """On-scope histogram is the fastest path for pulse-height spectra."""
    rows = []
    candidates = [
        ":HISTogram:ENABle?",
        ":HISTogram:TYPE?",
        ":HISTogram:SOURce?",
        ":HISTogram:HEIGht?",
        ":MEASure:HISTogram:ENABle?",
    ]
    any_supported = False
    for c in candidates:
        v = s.q(c)
        ok = "unsupported" not in v and "error" not in v.lower() and v != ""
        rows.append((c, v if ok else "(not supported)"))
        if ok:
            any_supported = True
    rows.append(("Histogram math available",
                 "YES — best path for >>10 kpulse/s pulse-height spectra"
                 if any_supported else "Not detected"))
    return rows


def probe_trigger_and_waveform(s: Scope) -> list[tuple[str, str]]:
    return [
        ("Trigger mode",   s.q(":TRIGger:MODE?")),
        ("Trigger source", s.q(":TRIGger:EDGE:SOURce?")),
        ("Trigger slope",  s.q(":TRIGger:EDGE:SLOPe?")),
        ("Trigger level",  s.q(":TRIGger:EDGE:LEVel?")),
        ("Waveform source",  s.q(":WAVeform:SOURce?")),
        ("Waveform format",  s.q(":WAVeform:FORMat?")),
        ("Waveform points",  s.q(":WAVeform:POINts?")),
    ]


# ---------------------------------------------------------------------------
# Throughput estimator
# ---------------------------------------------------------------------------

def estimate_throughput(rows_acq: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Rough back-of-envelope throughput numbers for the three workflows."""
    # Try to pull sample rate
    srate = 4e9
    for k, v in rows_acq:
        if "Sample rate" in k:
            try:
                srate = float(v.split()[0])
            except Exception:
                pass

    # Assume 200 ns window per PMT pulse → samples per pulse
    window_ns = 200
    samples_per_pulse = int(srate * window_ns * 1e-9)
    mem_mpts = 100  # MHO984 standard depth
    segments_in_memory = (mem_mpts * 1_000_000) // max(samples_per_pulse, 1)

    return [
        ("Assumed pulse window",   f"{window_ns} ns"),
        ("Samples per pulse",      f"{samples_per_pulse}"),
        ("Segments fitting in 100 Mpts",
         f"~{segments_in_memory:,}"),
        ("(A) Segmented + batch LAN transfer",
         "~1,000 – 10,000 pulses/sec sustained to PC"),
        ("(B) On-scope histogram (no waveform xfer)",
         "up to ~1,000,000 wfms/s (vendor max capture rate)"),
        ("(C) Per-pulse SCPI trigger+transfer loop",
         "~10 – 100 pulses/sec (avoid for spectroscopy)"),
    ]


# ---------------------------------------------------------------------------
# Optional: actually time a waveform transfer to measure (C) on this LAN
# ---------------------------------------------------------------------------

def benchmark_transfer(s: Scope, n: int = 50) -> list[tuple[str, str]]:
    """Trigger -> read waveform -> repeat. Times the per-pulse loop."""
    if np is None:
        return [("Benchmark", "numpy not installed; skipped")]

    s.w(":STOP")
    s.w(":WAVeform:SOURce CHANnel1")
    s.w(":WAVeform:FORMat BYTE")
    s.w(":WAVeform:MODE NORMal")
    s.w(":WAVeform:POINts 1000")  # short record

    t0 = time.perf_counter()
    ok = 0
    for _ in range(n):
        s.w(":SINGle")
        # poll for trigger complete
        for _ in range(200):
            status = s.q(":TRIGger:STATus?")
            if status.upper().startswith(("STOP", "TD")):
                break
            time.sleep(0.001)
        try:
            data = s.inst.query_binary_values(
                ":WAVeform:DATA?", datatype="B", container=np.array
            )
            if data is not None and len(data) > 0:
                ok += 1
        except Exception:
            pass
    dt = time.perf_counter() - t0

    rate = ok / dt if dt > 0 else 0.0
    return [
        ("Per-pulse loop iterations", f"{n}"),
        ("Successful transfers",     f"{ok}"),
        ("Elapsed",                  f"{dt:.2f} s"),
        ("Effective rate",           f"{rate:.1f} pulses/sec"),
        ("Verdict",
         "Use segmented / histogram instead for high rates"
         if rate < 500 else "Per-pulse loop is acceptable"),
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def list_resources():
    rm = pyvisa.ResourceManager("@py")
    res = rm.list_resources()
    if not res:
        print("No VISA resources found. For LAN, pass --ip directly.")
        return
    print("Visible VISA resources:")
    for r in res:
        print(f"  {r}")


def main():
    p = argparse.ArgumentParser(description="Rigol MHO984 diagnostic")
    p.add_argument("--ip", help="Scope IP address (LAN)")
    p.add_argument("--resource", help="Full VISA resource string")
    p.add_argument("--list", action="store_true", help="List VISA resources and exit")
    p.add_argument("--benchmark", action="store_true",
                   help="Time a per-pulse trigger+transfer loop (slow path)")
    args = p.parse_args()

    if args.list:
        list_resources()
        return

    if not args.ip and not args.resource:
        p.error("provide --ip <addr> or --resource <visa string>  (or --list)")

    resource = args.resource or f"TCPIP0::{args.ip}::INSTR"

    rm = pyvisa.ResourceManager("@py")
    print(f"Opening {resource} ...")
    try:
        inst = rm.open_resource(resource)
    except Exception as e:
        sys.exit(f"Failed to open resource: {e}")

    s = Scope(inst)
    report = Report()

    report.section("Identity",          probe_identity(s))
    rows_acq = probe_acquisition(s)
    report.section("Acquisition",       rows_acq)
    report.section("Segmented memory",  probe_segmented(s))
    report.section("Histogram math",    probe_histogram(s))
    report.section("Trigger / waveform", probe_trigger_and_waveform(s))
    report.section("Throughput estimate (PMT pulses, 200 ns window)",
                   estimate_throughput(rows_acq))

    if args.benchmark:
        report.section("LIVE benchmark: per-pulse trigger+transfer",
                       benchmark_transfer(s))

    report.print()
    print("\nDone.")
    inst.close()


if __name__ == "__main__":
    main()
