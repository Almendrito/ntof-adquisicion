"""
Rigol MHO984 Diagnostic + Acquisition + Plot GUI  (USB / LAN)
==============================================================

Tabs
----
  Diagnostic  – full capability probe + live benchmark + report export
  Acquisition – PMT pulse capture → RAM buffer → auto-flush to disk
  Plot        – load a saved session folder and inspect pulses interactively

Acquisition saves data in an organised folder structure
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
  <output_dir>/
    session_20260515_143022/
      session_info.json      ← config snapshot (channel, pts, trig…)
      preamble.json          ← voltage / time scaling from scope
      batch_0000.npy         ← np.uint8 shape (N, pts)  raw ADC
      batch_0001.npy
      …

Plot tab
~~~~~~~~
  Browse to a session folder → load all batches → choose between:
    • Single pulse  (index selector, applies voltage scaling)
    • Overlay N     (random sample of N pulses superimposed)
    • PHD           (histogram of per-pulse peak amplitudes in mV)

Requirements
------------
    pip install pyvisa pyvisa-py pyusb numpy matplotlib

Run:
    python rigol_mho984_gui.py
"""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import random
import socket
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk, filedialog, messagebox

try:
    import pyvisa
except ImportError:
    sys.exit("pyvisa is required:  pip install pyvisa pyvisa-py")

try:
    import numpy as np
except ImportError:
    np = None

# matplotlib is optional – only needed for the Plot tab
try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import (
        FigureCanvasTkAgg, NavigationToolbar2Tk
    )
    HAS_MPL = True
except ImportError:
    HAS_MPL = False


RIGOL_VENDOR_IDS = {0x1AB1}
RIGOL_SCPI_PORT  = 5555
VISA_LAN_PORT    = 111


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

def detect_backends() -> dict:
    info = {
        "pyvisa_py": False, "ni_visa": False,
        "pyusb": False,     "libusb": False,
        "recommended": None, "notes": [],
    }
    try:
        rm = pyvisa.ResourceManager("@py")
        info["pyvisa_py"] = True
        rm.close()
    except Exception as e:
        info["notes"].append(f"pyvisa-py not usable: {e}")
    try:
        rm = pyvisa.ResourceManager()
        spec = str(rm).lower()
        if "py" not in spec or "ivi" in spec or "ni" in spec:
            info["ni_visa"] = True
        rm.close()
    except Exception:
        pass
    try:
        import usb.core  # noqa: F401
        info["pyusb"] = True
        try:
            import usb.backend.libusb1
            if usb.backend.libusb1.get_backend() is not None:
                info["libusb"] = True
        except Exception:
            pass
    except ImportError:
        info["notes"].append("pyusb not installed – USB via pyvisa-py disabled")
    if info["ni_visa"]:
        info["recommended"] = "ni"
    elif info["pyvisa_py"]:
        info["recommended"] = "py"
    return info


def open_resource_manager(prefer: str = "auto") -> pyvisa.ResourceManager:
    if prefer == "py":
        return pyvisa.ResourceManager("@py")
    if prefer == "ni":
        return pyvisa.ResourceManager()
    try:
        return pyvisa.ResourceManager()
    except Exception:
        return pyvisa.ResourceManager("@py")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_usb(rm: pyvisa.ResourceManager) -> list[tuple[str, str]]:
    found = []
    try:
        for r in rm.list_resources("USB?*::INSTR"):
            label = r
            try:
                inst = rm.open_resource(r, open_timeout=2000)
                inst.timeout = 2000
                idn = inst.query("*IDN?").strip()
                inst.close()
                prefix = "★ USB  " if "rigol" in idn.lower() else "USB    "
                label = f"{prefix}{r}   [{idn}]"
            except Exception:
                label = "USB    " + r + "   [no IDN response]"
            found.append((r, label))
    except Exception as e:
        print(f"USB discovery error: {e}")
    return found


def discover_usb_via_pyusb() -> list[tuple[str, str]]:
    found = []
    try:
        import usb.core, usb.util
        for dev in usb.core.find(find_all=True):
            if dev.idVendor in RIGOL_VENDOR_IDS:
                try:
                    serial = usb.util.get_string(dev, dev.iSerialNumber)
                except Exception:
                    serial = "?"
                resource = (f"USB0::0x{dev.idVendor:04X}::"
                            f"0x{dev.idProduct:04X}::{serial}::INSTR")
                label = (f"★ USB  {resource}   "
                         f"[Rigol VID=0x{dev.idVendor:04X}, "
                         f"PID=0x{dev.idProduct:04X}]")
                found.append((resource, label))
    except Exception:
        pass
    return found


def _local_subnets() -> list[ipaddress.IPv4Network]:
    nets = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
            if net not in nets:
                nets.append(net)
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
        if net not in nets:
            nets.append(net)
    except Exception:
        pass
    return nets


def _probe_host(ip: str, timeout: float = 0.25) -> bool:
    for port in (VISA_LAN_PORT, RIGOL_SCPI_PORT):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except (OSError, socket.timeout):
            continue
    return False


def _identify_lan(ip: str, timeout: float = 1.5) -> str | None:
    try:
        with socket.create_connection((ip, RIGOL_SCPI_PORT), timeout=timeout) as sock:
            sock.sendall(b"*IDN?\n")
            sock.settimeout(timeout)
            data = b""
            while b"\n" not in data and len(data) < 512:
                chunk = sock.recv(256)
                if not chunk:
                    break
                data += chunk
            return data.decode(errors="ignore").strip() or None
    except Exception:
        return None


def discover_lan(progress_cb=None, stop_event=None) -> list[tuple[str, str]]:
    found = []
    nets   = _local_subnets()
    hosts  = [str(h) for net in nets for h in net.hosts()]
    total  = len(hosts)
    if progress_cb:
        progress_cb(0, total, "Scanning LAN…")
    found_ips: list[str] = []
    lock = threading.Lock()
    counter = {"n": 0}

    def worker(ip):
        if stop_event and stop_event.is_set():
            return
        if _probe_host(ip):
            with lock:
                found_ips.append(ip)
        with lock:
            counter["n"] += 1
            if progress_cb and counter["n"] % 8 == 0:
                progress_cb(counter["n"], total, f"LAN scan… {ip}")

    threads = []
    i = 0
    while i < len(hosts):
        threads = [t for t in threads if t.is_alive()]
        if len(threads) < 64:
            t = threading.Thread(target=worker, args=(hosts[i],), daemon=True)
            t.start()
            threads.append(t)
            i += 1
        else:
            time.sleep(0.005)
        if stop_event and stop_event.is_set():
            break
    for t in threads:
        t.join(timeout=2.0)
    if progress_cb:
        progress_cb(total, total, f"Identifying {len(found_ips)} LAN hosts…")
    for ip in found_ips:
        if stop_event and stop_event.is_set():
            break
        idn = _identify_lan(ip)
        if idn:
            res    = f"TCPIP0::{ip}::INSTR"
            prefix = "★ LAN  " if "rigol" in idn.lower() else "LAN    "
            found.append((res, f"{prefix}{res}   [{idn}]"))
    return found


# ---------------------------------------------------------------------------
# SCPI helper
# ---------------------------------------------------------------------------

@dataclass
class Scope:
    inst: "pyvisa.resources.MessageBasedResource"

    def __post_init__(self):
        self.inst.timeout     = 5000
        self.inst.read_termination  = "\n"
        self.inst.write_termination = "\n"

    def q(self, cmd: str, default: str = "<unsupported>") -> str:
        try:
            return self.inst.query(cmd).strip()
        except Exception as e:
            return f"{default}  (error: {type(e).__name__})"

    def w(self, cmd: str) -> bool:
        try:
            self.inst.write(cmd)
            return True
        except Exception:
            return False


def _ok(v: str) -> bool:
    if v is None:
        return False
    s = v.lower()
    return "unsupported" not in s and "error" not in s and s.strip() != ""


def probe_all(s: Scope):
    sections = []

    rows = [
        ("IDN",              s.q("*IDN?")),
        ("System version",   s.q(":SYSTem:VERSion?")),
        ("Options installed",s.q(":SYSTem:OPTion:INSTall?")),
    ]
    sections.append(("Identity", [(k, v, _ok(v)) for k, v in rows]))

    rows = [
        ("Acquire type",        s.q(":ACQuire:TYPE?")),
        ("Sample rate (Sa/s)",  s.q(":ACQuire:SRATe?")),
        ("Memory depth",        s.q(":ACQuire:MDEPth?")),
        ("CH1 bandwidth limit", s.q(":CHANnel1:BWLimit?")),
    ]
    original = s.q(":ACQuire:TYPE?")
    accepted = []
    for m in ["NORMal","AVERages","PEAK","HRESolution","ULTRa","ULTRAACQ","FASTAcq"]:
        if s.w(f":ACQuire:TYPE {m}"):
            time.sleep(0.04)
            got = s.q(":ACQuire:TYPE?")
            if _ok(got):
                accepted.append(f"{m}->{got}")
    if _ok(original):
        s.w(f":ACQuire:TYPE {original.split()[0]}")
    rows.append(("Modes accepted", ", ".join(accepted) if accepted else "none"))
    sections.append(("Acquisition", [(k, v, _ok(v)) for k, v in rows]))

    rows = []
    seg_any = False
    for cmd in [":RECord:ENABle?",":RECord:FRAMes?",":RECord:CURRent?",
                ":FUNCtion:WRECord:ENABle?",":FUNCtion:WRECord:FRAMes?",
                ":ACQuire:ULTRa:ENABle?",":ACQuire:ULTRa:MAXFrame?",
                ":ACQuire:SEGMented:STATe?",":ACQuire:MMODe?"]:
        v  = s.q(cmd)
        ok = _ok(v)
        seg_any |= ok
        rows.append((cmd, v if ok else "(not supported)", ok))
    rows.append(("Waveform Recording (segmented)",
                 "AVAILABLE – up to 500,000 frames per session" if seg_any
                 else "Not detected (check firmware/programming guide)", seg_any))
    sections.append(("Waveform Recording / Segmented memory", rows))

    rows = []
    hist_any = False
    for cmd in [":HISTogram:ENABle?",":HISTogram:TYPE?",":HISTogram:SOURce?",
                ":HISTogram:HEIGht?",":MEASure:HISTogram:ENABle?"]:
        v  = s.q(cmd)
        ok = _ok(v)
        hist_any |= ok
        rows.append((cmd, v if ok else "(not supported)", ok))
    rows.append(("On-scope histogram",
                 "AVAILABLE – best path for high-rate spectra" if hist_any
                 else "Not detected", hist_any))
    sections.append(("Histogram analysis", rows))

    rows = [
        ("Trigger mode",   s.q(":TRIGger:MODE?")),
        ("Trigger source", s.q(":TRIGger:EDGE:SOURce?")),
        ("Trigger slope",  s.q(":TRIGger:EDGE:SLOPe?")),
        ("Trigger level",  s.q(":TRIGger:EDGE:LEVel?")),
        ("Waveform source",s.q(":WAVeform:SOURce?")),
        ("Waveform format",s.q(":WAVeform:FORMat?")),
        ("Waveform points",s.q(":WAVeform:POINts?")),
    ]
    sections.append(("Trigger / Waveform", [(k, v, _ok(v)) for k, v in rows]))

    srate = 4e9
    for k, v, _ in sections[1][1]:
        if "Sample rate" in k:
            try:
                srate = float(v.split()[0])
            except Exception:
                pass
    window_ns = 200
    samples   = int(srate * window_ns * 1e-9)
    rows = [
        ("Assumed pulse window",            f"{window_ns} ns",            True),
        ("Samples per pulse",               f"{samples}",                 True),
        ("Max frames per session (ds)",     "500,000",                    True),
        ("(A) Fill memory fast-record mode","~5-50 s for 500k frames",    True),
        ("(B) Transfer 500k via USB 2.0",   "~30-90 s typical",           True),
        ("(C) Transfer 500k via LAN 100M",  "~60-300 s typical",          True),
        ("(D) Per-pulse SCPI loop",         "~10-100 pulses/sec (avoid)", False),
    ]
    sections.append(("Throughput estimate (PMT, 200 ns window)", rows))
    return sections


def benchmark(s: Scope, n: int = 50, progress_cb=None):
    if np is None:
        return [("Benchmark", "numpy not installed", False)]
    s.w(":STOP")
    s.w(":WAVeform:SOURce CHANnel1")
    s.w(":WAVeform:FORMat BYTE")
    s.w(":WAVeform:MODE NORMal")
    s.w(":WAVeform:POINts 1000")
    t0 = time.perf_counter()
    ok = 0
    for i in range(n):
        if progress_cb:
            progress_cb(i, n, f"Benchmark {i+1}/{n}")
        s.w(":SINGle")
        for _ in range(200):
            st = s.q(":TRIGger:STATus?")
            if st.upper().startswith(("STOP", "TD")):
                break
            time.sleep(0.001)
        try:
            data = s.inst.query_binary_values(
                ":WAVeform:DATA?", datatype="B", container=np.array)
            if data is not None and len(data) > 0:
                ok += 1
        except Exception:
            pass
    dt   = time.perf_counter() - t0
    rate = ok / dt if dt > 0 else 0.0
    return [
        ("Iterations",     f"{n}",           True),
        ("Successful",     f"{ok}",          ok > 0),
        ("Elapsed",        f"{dt:.2f} s",    True),
        ("Effective rate", f"{rate:.1f} pulses/sec", True),
        ("Verdict",        "Acceptable" if rate >= 500
                           else "Slow – use segmented/histogram",
                           rate >= 500),
    ]


# ---------------------------------------------------------------------------
# Acquisition helpers
# ---------------------------------------------------------------------------

def _parse_preamble(raw: str) -> dict:
    """Parse :WAVeform:PREamble? → dict with voltage/time scaling."""
    try:
        p = [x.strip() for x in raw.split(",")]
        if len(p) >= 10:
            return {
                "points": int(float(p[2])),
                "xinc":   float(p[4]),
                "xorig":  float(p[5]),
                "xref":   float(p[6]),
                "yinc":   float(p[7]),
                "yorig":  float(p[8]),
                "yref":   float(p[9]),
            }
    except Exception:
        pass
    return {}


def _raw_to_volts(arr: "np.ndarray", preamble: dict) -> "np.ndarray":
    """Convert uint8 ADC array → float32 volts using preamble scaling."""
    a = arr.astype(np.float32)
    yinc  = preamble.get("yinc",  1.0)
    yorig = preamble.get("yorig", 0.0)
    yref  = preamble.get("yref",  0.0)
    return (a - yref - yorig) * yinc


def _time_axis(n_pts: int, preamble: dict) -> "np.ndarray":
    """Return time axis in nanoseconds."""
    xinc  = preamble.get("xinc",  1e-9)
    xorig = preamble.get("xorig", 0.0)
    xref  = preamble.get("xref",  0.0)
    t = (np.arange(n_pts) - xref) * xinc + xorig
    return t * 1e9   # → ns


def _flush_batch(buffer: list, session_dir: str,
                 flush_index: int) -> tuple[int, str]:
    """
    Save the current RAM buffer as  session_dir/batch_NNNN.npy
    Returns (n_saved, path) or (0, error_msg).
    """
    if np is None or not buffer:
        return 0, "numpy unavailable or empty buffer"
    try:
        os.makedirs(session_dir, exist_ok=True)
        path = os.path.join(session_dir, f"batch_{flush_index:04d}.npy")
        np.save(path, np.array(buffer, dtype=np.uint8))
        return len(buffer), path
    except Exception as e:
        return 0, f"ERROR: {e}"


def _load_session(session_dir: str) -> tuple[list, dict]:
    """
    Load all batch_*.npy files from a session directory.
    Returns (list_of_2d_arrays, preamble_dict).
    """
    batches   = []
    preamble  = {}
    pre_path  = os.path.join(session_dir, "preamble.json")
    if os.path.isfile(pre_path):
        try:
            with open(pre_path) as f:
                preamble = json.load(f)
        except Exception:
            pass
    files = sorted(
        f for f in os.listdir(session_dir) if f.startswith("batch_") and
        f.endswith(".npy")
    )
    for fname in files:
        try:
            arr = np.load(os.path.join(session_dir, fname))
            if arr.ndim == 2:
                batches.append(arr)
        except Exception:
            pass
    return batches, preamble


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Rigol MHO984  –  Diagnostic / Acquisition / Plot")
        self.geometry("980x820")
        self.minsize(800, 620)

        self.rm:    pyvisa.ResourceManager | None = None
        self.scope: Scope | None                  = None
        self.stop_discover = threading.Event()
        self.msg_q: queue.Queue                   = queue.Queue()
        self._discover_map: dict[str, str]        = {}

        # Acquisition state
        self.acq_running:     bool  = False
        self.acq_stop               = threading.Event()
        self.acq_buffer:      list  = []
        self.acq_preamble:    dict  = {}
        self.acq_session_dir: str   = ""
        self.acq_total_frames: int  = 0
        self.acq_saved_frames: int  = 0
        self.acq_flush_count:  int  = 0
        self.acq_t0:          float = 0.0
        self.acq_maxtime_val: float = 0.0      # 0 = no time limit
        self.acq_timer              = None     # threading.Timer for auto-stop

        # Plot state
        self.plot_batches:  list = []   # list of 2-D np arrays
        self.plot_preamble: dict = {}
        self._plot_total:   int  = 0    # total pulses loaded
        self._plot_nav_busy: bool = False   # guard against slider/button echo
        self.plot_session_dir: str = ""

        self._detect_backends()
        self._build_ui()
        self.after(100, self._drain_queue)
        self.after(300, self._on_scan_usb)

    # -----------------------------------------------------------------------
    # Backend
    # -----------------------------------------------------------------------

    def _detect_backends(self):
        self.backends = detect_backends()
        try:
            pref   = self.backends["recommended"] or "auto"
            self.rm = open_resource_manager(pref)
        except Exception as e:
            messagebox.showerror(
                "VISA backend missing",
                f"Could not open any VISA backend.\n\n{e}\n\n"
                "Install one of:\n"
                "  pip install pyvisa-py pyusb   (pure Python)\n"
                "  -- OR --\n"
                "  NI-VISA from ni.com/visa (recommended for USB on Windows)",
            )
            sys.exit(1)

    # -----------------------------------------------------------------------
    # UI builder
    # -----------------------------------------------------------------------

    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # ── banner ──────────────────────────────────────────────────────────
        banner = ttk.Frame(self, padding=(10, 8, 10, 4))
        banner.pack(fill="x")
        bk    = self.backends
        parts = []
        if bk["ni_visa"]:
            parts.append("NI-VISA (USB ready)")
        if bk["pyvisa_py"]:
            usb_ok = bk["pyusb"] and bk["libusb"]
            parts.append("pyvisa-py " + ("(USB ready)" if usb_ok
                         else "(LAN only – install pyusb+libusb for USB)"))
        if not parts:
            parts.append("No VISA backend!")
        mpl_info = "  |  matplotlib ✓" if HAS_MPL else \
                   "  |  matplotlib ✗ (pip install matplotlib for Plot tab)"
        ttk.Label(banner,
                  text="Backends: " + " | ".join(parts) + mpl_info,
                  foreground="#0a5a0a" if bk["recommended"] else "#a00000",
                  font=("TkDefaultFont", 9, "bold")).pack(side="left")

        # ── connection panel ─────────────────────────────────────────────────
        conn = ttk.LabelFrame(self, text="Connection", padding=8)
        conn.pack(fill="x", padx=10, pady=(2, 5))

        self.nb = ttk.Notebook(conn)
        self.nb.pack(fill="x")

        usb_tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(usb_tab, text="  USB  ")
        ttk.Label(usb_tab,
                  text="Connect scope via USB-B cable then click Scan USB.",
                  foreground="#555").grid(row=0, column=0, columnspan=3,
                                          sticky="w", pady=(0, 6))
        ttk.Label(usb_tab, text="Device:").grid(row=1, column=0, sticky="w")
        self.usb_var   = tk.StringVar()
        self.usb_combo = ttk.Combobox(usb_tab, textvariable=self.usb_var,
                                      width=72)
        self.usb_combo.grid(row=1, column=1, sticky="ew", padx=(6, 6))
        usb_tab.columnconfigure(1, weight=1)
        ttk.Button(usb_tab, text="Scan USB",
                   command=self._on_scan_usb).grid(row=1, column=2)
        self.usb_status = ttk.Label(usb_tab, text="", foreground="#555")
        self.usb_status.grid(row=2, column=0, columnspan=3,
                             sticky="w", pady=(4, 0))

        lan_tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(lan_tab, text="  LAN  ")
        ttk.Label(lan_tab,
                  text="Scan the local network, or type an IP directly.",
                  foreground="#555").grid(row=0, column=0, columnspan=3,
                                          sticky="w", pady=(0, 6))
        ttk.Label(lan_tab, text="Device:").grid(row=1, column=0, sticky="w")
        self.lan_var   = tk.StringVar()
        self.lan_combo = ttk.Combobox(lan_tab, textvariable=self.lan_var,
                                      width=72)
        self.lan_combo.grid(row=1, column=1, sticky="ew", padx=(6, 6))
        lan_tab.columnconfigure(1, weight=1)
        ttk.Button(lan_tab, text="Scan LAN",
                   command=self._on_scan_lan).grid(row=1, column=2)
        ttk.Label(lan_tab, text="Or IP:").grid(row=2, column=0, sticky="w",
                                                pady=(6, 0))
        self.ip_var = tk.StringVar()
        ttk.Entry(lan_tab, textvariable=self.ip_var, width=22).grid(
            row=2, column=1, sticky="w", padx=(6, 6), pady=(6, 0))
        ttk.Button(lan_tab, text="Use IP",
                   command=self._on_use_ip).grid(row=2, column=2,
                                                  sticky="w", pady=(6, 0))

        man_tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(man_tab, text="  Manual  ")
        ttk.Label(man_tab,
                  text="Type any full VISA resource string.",
                  foreground="#555").grid(row=0, column=0, columnspan=2,
                                          sticky="w", pady=(0, 6))
        ttk.Label(man_tab, text="Resource:").grid(row=1, column=0, sticky="w")
        self.manual_var = tk.StringVar()
        ttk.Entry(man_tab, textvariable=self.manual_var, width=80).grid(
            row=1, column=1, sticky="ew", padx=(6, 6))
        man_tab.columnconfigure(1, weight=1)

        bot = ttk.Frame(conn, padding=(0, 8, 0, 0))
        bot.pack(fill="x")
        self.connect_btn    = ttk.Button(bot, text="Connect",
                                         command=self._on_connect)
        self.connect_btn.pack(side="left")
        self.disconnect_btn = ttk.Button(bot, text="Disconnect",
                                         command=self._on_disconnect,
                                         state="disabled")
        self.disconnect_btn.pack(side="left", padx=6)
        self.conn_label = ttk.Label(bot, text="Not connected.",
                                    foreground="#555")
        self.conn_label.pack(side="left", padx=12)

        # ── status / actions ─────────────────────────────────────────────────
        status = ttk.Frame(self, padding=(10, 0))
        status.pack(fill="x")
        self.status_var = tk.StringVar(value="Idle.")
        ttk.Label(status, textvariable=self.status_var,
                  foreground="#555").pack(side="left")
        self.progress = ttk.Progressbar(status, mode="determinate", length=260)
        self.progress.pack(side="right")

        actions = ttk.Frame(self, padding=(10, 5))
        actions.pack(fill="x")
        self.diag_btn = ttk.Button(actions, text="Run diagnostic",
                                   command=self._on_run_diagnostic,
                                   state="disabled")
        self.diag_btn.pack(side="left")
        self.bench_btn = ttk.Button(actions, text="Benchmark per-pulse",
                                    command=self._on_benchmark,
                                    state="disabled")
        self.bench_btn.pack(side="left", padx=6)
        self.save_btn = ttk.Button(actions, text="Save report…",
                                   command=self._on_save, state="disabled")
        self.save_btn.pack(side="left", padx=6)
        ttk.Button(actions, text="Clear",
                   command=self._clear_output).pack(side="left", padx=6)
        ttk.Button(actions, text="Backend info",
                   command=self._show_backend_info).pack(side="right")

        # ── main body notebook ───────────────────────────────────────────────
        body_nb = ttk.Notebook(self)
        body_nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # Diagnostic tab (unchanged tree)
        diag_tab = ttk.Frame(body_nb, padding=(5, 5))
        body_nb.add(diag_tab, text="  Diagnostic  ")

        cols = ("value", "ok")
        self.tree = ttk.Treeview(diag_tab, columns=cols,
                                 show="tree headings")
        self.tree.heading("#0",    text="Item")
        self.tree.heading("value", text="Value")
        self.tree.heading("ok",    text="OK")
        self.tree.column("#0",    width=320, anchor="w")
        self.tree.column("value", width=440, anchor="w")
        self.tree.column("ok",    width=60,  anchor="center")
        self.tree.tag_configure("ok",      foreground="#0a7a0a")
        self.tree.tag_configure("bad",     foreground="#a00000")
        self.tree.tag_configure("section", font=("TkDefaultFont", 10, "bold"))
        self.tree.pack(fill="both", expand=True, side="left")
        vsb = ttk.Scrollbar(diag_tab, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")

        # Acquisition tab
        acq_tab = ttk.Frame(body_nb, padding=(5, 5))
        body_nb.add(acq_tab, text="  Acquisition  ")
        self._build_acquisition_tab(acq_tab)

        # Plot tab
        plot_tab = ttk.Frame(body_nb, padding=(5, 5))
        body_nb.add(plot_tab, text="  Plot  ")
        self._build_plot_tab(plot_tab)

    # =======================================================================
    # ACQUISITION TAB
    # =======================================================================

    def _build_acquisition_tab(self, parent: ttk.Frame):

        # ── config ──────────────────────────────────────────────────────────
        cfg = ttk.LabelFrame(parent, text="Configuration", padding=8)
        cfg.pack(fill="x", pady=(0, 6))

        ttk.Label(cfg, text="Channel:").grid(row=0, column=0, sticky="w")
        self.acq_ch = tk.StringVar(value="CHANnel1")
        ttk.Combobox(cfg, textvariable=self.acq_ch, width=12, state="readonly",
                     values=["CHANnel1","CHANnel2","CHANnel3","CHANnel4"]
                     ).grid(row=0, column=1, sticky="w", padx=(4, 18))

        ttk.Label(cfg, text="Trigger level (V):").grid(
            row=0, column=2, sticky="w")
        self.acq_trig = tk.StringVar(value="0.010")
        ttk.Entry(cfg, textvariable=self.acq_trig, width=9).grid(
            row=0, column=3, padx=(4, 18))

        ttk.Label(cfg, text="Slope:").grid(row=0, column=4, sticky="w")
        self.acq_slope = tk.StringVar(value="POSitive")
        ttk.Combobox(cfg, textvariable=self.acq_slope, width=10,
                     state="readonly",
                     values=["POSitive","NEGative"]
                     ).grid(row=0, column=5, padx=(4, 0))

        ttk.Label(cfg, text="Mode:").grid(row=1, column=0, sticky="w",
                                          pady=(8, 0))
        self.acq_mode = tk.StringVar(value="record")
        ttk.Radiobutton(cfg, text="Waveform Recording  (batch, fast)",
                        variable=self.acq_mode, value="record"
                        ).grid(row=1, column=1, columnspan=3, sticky="w",
                               padx=(4, 0), pady=(8, 0))
        ttk.Radiobutton(cfg, text="Single-trigger loop  (slow, always works)",
                        variable=self.acq_mode, value="single"
                        ).grid(row=1, column=4, columnspan=2, sticky="w",
                               pady=(8, 0))

        ttk.Label(cfg, text="Points/pulse:").grid(row=2, column=0, sticky="w",
                                                   pady=(8, 0))
        self.acq_pts = tk.StringVar(value="800")
        ttk.Combobox(cfg, textvariable=self.acq_pts, width=8,
                     values=["100","200","400","800","1000","2000","5000"]
                     ).grid(row=2, column=1, sticky="w",
                            padx=(4, 18), pady=(8, 0))

        ttk.Label(cfg, text="Frames/batch:").grid(row=2, column=2, sticky="w",
                                                   pady=(8, 0))
        self.acq_batch = tk.StringVar(value="10000")
        ttk.Entry(cfg, textvariable=self.acq_batch, width=9).grid(
            row=2, column=3, padx=(4, 18), pady=(8, 0))

        ttk.Label(cfg, text="Flush at RAM %:").grid(row=2, column=4,
                                                     sticky="w", pady=(8, 0))
        self.acq_flush_pct = tk.StringVar(value="80")
        ttk.Entry(cfg, textvariable=self.acq_flush_pct, width=6).grid(
            row=2, column=5, padx=(4, 0), pady=(8, 0))

        # Max measurement time row
        ttk.Label(cfg, text="Max time (s):").grid(row=3, column=0, sticky="w",
                                                   pady=(8, 0))
        self.acq_maxtime = tk.StringVar(value="0")
        ttk.Entry(cfg, textvariable=self.acq_maxtime, width=9).grid(
            row=3, column=1, sticky="w", padx=(4, 18), pady=(8, 0))
        ttk.Label(cfg, text="(0 = sin límite; auto-Stop al cumplirse)",
                  foreground="#777").grid(row=3, column=2, columnspan=4,
                                          sticky="w", pady=(8, 0))

        # Output directory row
        ttk.Label(cfg, text="Output folder:").grid(row=4, column=0,
                                                    sticky="w", pady=(8, 0))
        self.acq_outdir = tk.StringVar(value=os.path.expanduser("~/rigol_data"))
        ttk.Entry(cfg, textvariable=self.acq_outdir, width=44).grid(
            row=4, column=1, columnspan=4, sticky="ew",
            padx=(4, 4), pady=(8, 0))
        ttk.Button(cfg, text="Browse…",
                   command=self._acq_browse).grid(row=4, column=5,
                                                   pady=(8, 0))

        # ── controls ────────────────────────────────────────────────────────
        ctrl = ttk.Frame(parent)
        ctrl.pack(fill="x", pady=(0, 4))

        self.acq_start_btn = ttk.Button(ctrl, text="▶  Start Acquisition",
                                        command=self._on_acq_start)
        self.acq_start_btn.pack(side="left")
        self.acq_stop_btn = ttk.Button(ctrl, text="■  Stop",
                                       command=self._on_acq_stop,
                                       state="disabled")
        self.acq_stop_btn.pack(side="left", padx=6)
        self.acq_flush_now_btn = ttk.Button(ctrl, text="Flush to disk now",
                                            command=self._on_acq_flush_now,
                                            state="disabled")
        self.acq_flush_now_btn.pack(side="left", padx=6)

        self.acq_state_lbl = ttk.Label(ctrl, text="Idle",
                                       foreground="#888",
                                       font=("TkDefaultFont", 9, "bold"))
        self.acq_state_lbl.pack(side="left", padx=14)

        self.acq_ram_bar = ttk.Progressbar(ctrl, mode="determinate",
                                           length=160, maximum=100)
        self.acq_ram_bar.pack(side="right", padx=(0, 6))
        ttk.Label(ctrl, text="RAM:").pack(side="right")

        # ── live stats ───────────────────────────────────────────────────────
        stats = ttk.LabelFrame(parent, text="Live Statistics", padding=6)
        stats.pack(fill="x", pady=(0, 4))

        self.acq_stat_ram     = tk.StringVar(value="0")
        self.acq_stat_disk    = tk.StringVar(value="0")
        self.acq_stat_flushes = tk.StringVar(value="0")
        self.acq_stat_rate    = tk.StringVar(value="—")
        self.acq_stat_elapsed = tk.StringVar(value="0.0 s")
        self.acq_stat_mb      = tk.StringVar(value="0.0 MB")
        self.acq_stat_folder  = tk.StringVar(value="—")

        def _stat(parent, row, col, label, var):
            ttk.Label(parent, text=label, foreground="#555",
                      font=("TkDefaultFont", 8)).grid(
                row=row, column=col, sticky="w", padx=(6, 2))
            ttk.Label(parent, textvariable=var,
                      font=("TkFixedFont", 10, "bold"),
                      foreground="#003080").grid(
                row=row, column=col + 1, sticky="w", padx=(0, 20))

        _stat(stats, 0, 0, "Frames in RAM:",       self.acq_stat_ram)
        _stat(stats, 0, 2, "Frames saved to disk:", self.acq_stat_disk)
        _stat(stats, 0, 4, "Flush count:",          self.acq_stat_flushes)
        _stat(stats, 1, 0, "Acq. rate:",            self.acq_stat_rate)
        _stat(stats, 1, 2, "RAM buffer:",           self.acq_stat_mb)
        _stat(stats, 1, 4, "Elapsed:",              self.acq_stat_elapsed)
        ttk.Label(stats, text="Session folder:", foreground="#555",
                  font=("TkDefaultFont", 8)).grid(
            row=2, column=0, sticky="w", padx=(6, 2), pady=(4, 0))
        ttk.Label(stats, textvariable=self.acq_stat_folder,
                  foreground="#555", font=("TkFixedFont", 8)).grid(
            row=2, column=1, columnspan=5, sticky="w", pady=(4, 0))

        # ── log ─────────────────────────────────────────────────────────────
        log_frm = ttk.LabelFrame(parent, text="Acquisition log", padding=4)
        log_frm.pack(fill="both", expand=True)

        self.acq_log = tk.Text(log_frm, height=8, state="disabled",
                               font=("TkFixedFont", 9), wrap="word",
                               bg="#1a1a2e", fg="#e0e0e0",
                               insertbackground="white")
        log_sb = ttk.Scrollbar(log_frm, orient="vertical",
                               command=self.acq_log.yview)
        self.acq_log.configure(yscrollcommand=log_sb.set)
        self.acq_log.pack(side="left", fill="both", expand=True)
        log_sb.pack(side="right", fill="y")

        self.acq_log.tag_configure("INFO",  foreground="#7ec8e3")
        self.acq_log.tag_configure("OK",    foreground="#90ee90")
        self.acq_log.tag_configure("WARN",  foreground="#ffd700")
        self.acq_log.tag_configure("ERROR", foreground="#ff6b6b")

    # =======================================================================
    # PLOT TAB
    # =======================================================================

    def _build_plot_tab(self, parent: ttk.Frame):

        if not HAS_MPL:
            ttk.Label(
                parent,
                text="matplotlib not installed.\n\n"
                     "Run:  pip install matplotlib\n\nThen restart the GUI.",
                font=("TkDefaultFont", 11),
                foreground="#a00000",
                justify="center",
            ).pack(expand=True)
            return

        # ── session loader ───────────────────────────────────────────────────
        loader = ttk.LabelFrame(parent, text="Session", padding=6)
        loader.pack(fill="x", pady=(0, 4))

        ttk.Label(loader, text="Session folder:").grid(
            row=0, column=0, sticky="w")
        self.plot_dir_var = tk.StringVar(
            value=os.path.expanduser("~/rigol_data"))
        ttk.Entry(loader, textvariable=self.plot_dir_var, width=52).grid(
            row=0, column=1, sticky="ew", padx=(4, 4))
        loader.columnconfigure(1, weight=1)
        ttk.Button(loader, text="Browse…",
                   command=self._plot_browse).grid(row=0, column=2)
        ttk.Button(loader, text="Load session",
                   command=self._plot_load).grid(row=0, column=3, padx=(6, 0))

        self.plot_info_lbl = ttk.Label(loader, text="No session loaded.",
                                       foreground="#555")
        self.plot_info_lbl.grid(row=1, column=0, columnspan=4,
                                sticky="w", pady=(4, 0))

        # ── controls ────────────────────────────────────────────────────────
        ctrl = ttk.LabelFrame(parent, text="Plot controls", padding=6)
        ctrl.pack(fill="x", pady=(0, 4))

        # Single pulse
        ttk.Label(ctrl, text="Pulse index:").grid(
            row=0, column=0, sticky="w", padx=(0, 4))
        self.plot_pulse_idx = tk.StringVar(value="0")
        ttk.Spinbox(ctrl, textvariable=self.plot_pulse_idx,
                    from_=0, to=9_999_999, width=10).grid(
            row=0, column=1, sticky="w")
        ttk.Button(ctrl, text="Plot single pulse",
                   command=self._plot_single).grid(
            row=0, column=2, padx=(8, 0))

        # Overlay N
        ttk.Label(ctrl, text="Overlay N pulses:").grid(
            row=0, column=3, sticky="w", padx=(18, 4))
        self.plot_overlay_n = tk.StringVar(value="50")
        ttk.Spinbox(ctrl, textvariable=self.plot_overlay_n,
                    from_=1, to=10000, width=8).grid(
            row=0, column=4, sticky="w")
        ttk.Button(ctrl, text="Plot overlay",
                   command=self._plot_overlay).grid(
            row=0, column=5, padx=(8, 0))

        # PHD
        ttk.Label(ctrl, text="PHD bins:").grid(
            row=0, column=6, sticky="w", padx=(18, 4))
        self.plot_bins = tk.StringVar(value="256")
        ttk.Spinbox(ctrl, textvariable=self.plot_bins,
                    from_=16, to=4096, width=7).grid(
            row=0, column=7, sticky="w")
        ttk.Button(ctrl, text="Plot PHD",
                   command=self._plot_phd).grid(
            row=0, column=8, padx=(8, 0))

        # Voltage / raw toggle
        self.plot_use_volts = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctrl, text="Use voltage scaling (requires preamble)",
                        variable=self.plot_use_volts).grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))

        ttk.Button(ctrl, text="Clear plot",
                   command=self._plot_clear).grid(
            row=1, column=8, pady=(6, 0))

        # ── pulse navigation (Prev / slider / Next) ──────────────────────────
        ttk.Label(ctrl, text="Navigate:").grid(row=2, column=0, sticky="w",
                                                pady=(8, 0))
        ttk.Button(ctrl, text="◀ Prev", width=8,
                   command=lambda: self._plot_step(-1)).grid(
            row=2, column=1, sticky="w", pady=(8, 0))
        self.plot_slider = ttk.Scale(ctrl, from_=0, to=0, orient="horizontal",
                                     length=360,
                                     command=self._plot_slider_moved)
        self.plot_slider.grid(row=2, column=2, columnspan=5, sticky="we",
                              padx=(8, 8), pady=(8, 0))
        ttk.Button(ctrl, text="Next ▶", width=8,
                   command=lambda: self._plot_step(1)).grid(
            row=2, column=7, sticky="w", pady=(8, 0))
        self.plot_navlbl = ttk.Label(ctrl, text="– / –", foreground="#555")
        self.plot_navlbl.grid(row=2, column=8, sticky="w", padx=(8, 0),
                              pady=(8, 0))
        # arrow keys step through pulses (when Prev/Next/slider has focus)
        for w in (self.plot_slider,):
            w.bind("<Left>",  lambda e: self._plot_step(-1))
            w.bind("<Right>", lambda e: self._plot_step(1))

        # ── matplotlib canvas ────────────────────────────────────────────────
        canvas_frm = ttk.Frame(parent)
        canvas_frm.pack(fill="both", expand=True)

        self.plot_fig = Figure(figsize=(8, 4), dpi=96, tight_layout=True)
        self.plot_ax  = self.plot_fig.add_subplot(111)
        self.plot_ax.set_title("No data loaded")
        self.plot_ax.set_xlabel("Sample")
        self.plot_ax.set_ylabel("ADC counts")

        self.plot_canvas = FigureCanvasTkAgg(self.plot_fig, master=canvas_frm)
        self.plot_canvas.draw()
        self.plot_canvas.get_tk_widget().pack(fill="both", expand=True,
                                              side="top")

        toolbar_frm = ttk.Frame(canvas_frm)
        toolbar_frm.pack(fill="x", side="bottom")
        NavigationToolbar2Tk(self.plot_canvas, toolbar_frm)

    # -----------------------------------------------------------------------
    # Plot helpers
    # -----------------------------------------------------------------------

    def _plot_browse(self):
        d = filedialog.askdirectory(title="Select session folder",
                                    initialdir=os.path.expanduser("~/rigol_data"))
        if d:
            self.plot_dir_var.set(d)

    def _plot_load(self):
        d = self.plot_dir_var.get().strip()
        if not os.path.isdir(d):
            messagebox.showerror("Plot", f"Directory not found:\n{d}")
            return
        batches, preamble = _load_session(d)
        if not batches:
            messagebox.showwarning("Plot", "No batch_*.npy files found in\n" + d)
            return
        self.plot_batches    = batches
        self.plot_preamble   = preamble
        self.plot_session_dir = d
        total = sum(b.shape[0] for b in batches)
        pts   = batches[0].shape[1]
        self._plot_total = total
        info  = (f"{len(batches)} batch(es)  ·  {total:,} total pulses  "
                 f"·  {pts} pts/pulse")
        if preamble and isinstance(preamble.get("yinc"), (int, float)):
            info += f"  ·  yinc={preamble['yinc']:.3e} V"
        self.plot_info_lbl.config(text=info, foreground="#0a7a0a")
        # set up the navigation controls for this session
        self._plot_nav_busy = True
        self.plot_slider.config(to=max(0, total - 1))
        self.plot_slider.set(0)
        self.plot_pulse_idx.set("0")
        self._plot_nav_busy = False
        self.plot_navlbl.config(text=f"0 / {total - 1}" if total else "– / –")
        self._plot_clear()
        if total:
            self._plot_single()       # show the first pulse right away

    def _all_pulses(self) -> "np.ndarray | None":
        """Concatenate all loaded batches → 2D array (N, pts)."""
        if not self.plot_batches:
            messagebox.showwarning("Plot", "Load a session first.")
            return None
        return np.vstack(self.plot_batches)

    def _maybe_volts(self, raw: "np.ndarray") -> tuple["np.ndarray", str]:
        """
        If voltage scaling is requested and preamble is available, convert.
        Returns (array, ylabel_string).
        """
        if self.plot_use_volts.get() and self.plot_preamble:
            return _raw_to_volts(raw, self.plot_preamble), "Voltage (V)"
        return raw.astype(np.float32), "ADC counts (raw)"

    def _time_or_samples(self, n_pts: int) -> tuple["np.ndarray", str]:
        if self.plot_use_volts.get() and self.plot_preamble:
            return _time_axis(n_pts, self.plot_preamble), "Time (ns)"
        return np.arange(n_pts, dtype=float), "Sample #"

    def _plot_single(self):
        pulses = self._all_pulses()
        if pulses is None:
            return
        try:
            idx = int(self.plot_pulse_idx.get())
        except ValueError:
            messagebox.showerror("Plot", "Enter a valid integer index.")
            return
        if idx < 0 or idx >= len(pulses):
            messagebox.showerror("Plot",
                                 f"Index {idx} out of range "
                                 f"[0, {len(pulses)-1}].")
            return
        y, ylabel = self._maybe_volts(pulses[idx:idx + 1])
        y         = y[0]
        x, xlabel = self._time_or_samples(len(y))

        self.plot_ax.clear()
        self.plot_ax.plot(x, y, color="#1f77b4", linewidth=0.9)
        self.plot_ax.set_title(f"Pulse #{idx}  "
                               f"({os.path.basename(self.plot_session_dir)})")
        self.plot_ax.set_xlabel(xlabel)
        self.plot_ax.set_ylabel(ylabel)
        self.plot_ax.grid(True, alpha=0.3)
        self.plot_canvas.draw()

        # keep the slider + label in sync with whatever index was plotted
        self._plot_nav_busy = True
        try:
            self.plot_slider.set(idx)
        except Exception:
            pass
        self._plot_nav_busy = False
        self.plot_navlbl.config(text=f"{idx} / {max(0, len(pulses) - 1)}")

    def _plot_step(self, delta):
        """Prev/Next: shift the single-pulse index by `delta` and re-plot."""
        if not self.plot_batches:
            return
        try:
            i = int(self.plot_pulse_idx.get())
        except ValueError:
            i = 0
        i = max(0, min(self._plot_total - 1, i + delta))
        self.plot_pulse_idx.set(str(i))
        self._plot_single()
        return "break"   # swallow the key event so the slider doesn't also move

    def _plot_slider_moved(self, value):
        """ttk.Scale callback: jump to the dragged pulse and re-plot."""
        if self._plot_nav_busy or not self.plot_batches:
            return
        try:
            i = int(round(float(value)))
        except (TypeError, ValueError):
            return
        i = max(0, min(self._plot_total - 1, i))
        if str(i) != self.plot_pulse_idx.get():
            self.plot_pulse_idx.set(str(i))
            self._plot_single()

    def _plot_overlay(self):
        pulses = self._all_pulses()
        if pulses is None:
            return
        try:
            n = min(int(self.plot_overlay_n.get()), len(pulses))
        except ValueError:
            n = 50
        indices = random.sample(range(len(pulses)), n)
        sel     = pulses[indices]
        y_all, ylabel = self._maybe_volts(sel)
        x, xlabel     = self._time_or_samples(y_all.shape[1])

        self.plot_ax.clear()
        for row in y_all:
            self.plot_ax.plot(x, row, color="#1f77b4", alpha=0.15,
                              linewidth=0.6)
        self.plot_ax.set_title(
            f"Overlay of {n} random pulses  "
            f"(total {len(pulses):,})")
        self.plot_ax.set_xlabel(xlabel)
        self.plot_ax.set_ylabel(ylabel)
        self.plot_ax.grid(True, alpha=0.3)
        self.plot_canvas.draw()

    def _plot_phd(self):
        """Pulse-height distribution: histogram of per-pulse peak amplitude."""
        pulses = self._all_pulses()
        if pulses is None:
            return
        try:
            bins = int(self.plot_bins.get())
        except ValueError:
            bins = 256

        y_all, ylabel = self._maybe_volts(pulses)
        # peak amplitude = max − baseline (mean of first 10 samples)
        baseline = y_all[:, :10].mean(axis=1)
        peaks    = y_all.max(axis=1) - baseline
        unit     = "V" if "V" in ylabel else "ADC"

        self.plot_ax.clear()
        self.plot_ax.hist(peaks, bins=bins, color="#e05a00",
                          edgecolor="none", log=True)
        self.plot_ax.set_title(
            f"Pulse-Height Distribution  (N={len(pulses):,})")
        self.plot_ax.set_xlabel(f"Peak amplitude ({unit})")
        self.plot_ax.set_ylabel("Counts (log scale)")
        self.plot_ax.grid(True, which="both", alpha=0.3)
        self.plot_canvas.draw()

    def _plot_clear(self):
        if not HAS_MPL:
            return
        self.plot_ax.clear()
        self.plot_ax.set_title("No data loaded")
        self.plot_ax.set_xlabel("Sample")
        self.plot_ax.set_ylabel("ADC counts")
        self.plot_canvas.draw()

    # =======================================================================
    # ACQUISITION logic (background thread)
    # =======================================================================

    def _acq_browse(self):
        d = filedialog.askdirectory(title="Select output folder",
                                    initialdir=os.path.expanduser("~"))
        if d:
            self.acq_outdir.set(d)

    def _on_acq_start(self):
        if not self.scope:
            messagebox.showwarning("Acquisition", "Connect to a scope first.")
            return
        if np is None:
            messagebox.showerror("Acquisition",
                                 "numpy required.\n  pip install numpy")
            return
        if self.acq_running:
            return

        try:
            batch     = int(self.acq_batch.get())
            pts       = int(self.acq_pts.get())
            flush_pct = float(self.acq_flush_pct.get())
            trig_v    = float(self.acq_trig.get())
            max_time  = float(self.acq_maxtime.get() or 0)
        except ValueError as e:
            messagebox.showerror("Acquisition", f"Invalid config: {e}")
            return
        if max_time < 0:
            max_time = 0.0

        base_dir = self.acq_outdir.get().strip()
        if not base_dir:
            messagebox.showerror("Acquisition", "Set an output folder first.")
            return

        # Create session subfolder.  The configured max time (if any) goes in
        # the name now; the real elapsed time is appended when the run ends.
        session_name = "session_" + time.strftime("%Y%m%d_%H%M%S")
        if max_time > 0:
            session_name += f"_max{int(round(max_time))}s"
        session_dir  = os.path.join(base_dir, session_name)
        try:
            os.makedirs(session_dir, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Acquisition",
                                 f"Cannot create session folder:\n{e}")
            return

        self.acq_session_dir = session_dir

        # Save session config
        cfg = {
            "channel":   self.acq_ch.get(),
            "slope":     self.acq_slope.get(),
            "trig_V":    trig_v,
            "pts":       pts,
            "batch":     batch,
            "flush_pct": flush_pct,
            "mode":      self.acq_mode.get(),
            "max_time_s": max_time,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            with open(os.path.join(session_dir, "session_info.json"), "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

        # Reset counters
        self.acq_buffer.clear()
        self.acq_preamble.clear()
        self.acq_total_frames  = 0
        self.acq_saved_frames  = 0
        self.acq_flush_count   = 0
        self.acq_t0            = time.perf_counter()
        self.acq_maxtime_val   = max_time
        self.acq_running       = True
        self.acq_stop.clear()

        # Arm the auto-stop timer (cancelled on manual Stop / completion).
        if self.acq_timer is not None:
            self.acq_timer.cancel()
        self.acq_timer = None
        if max_time > 0:
            self.acq_timer = threading.Timer(max_time, self._on_acq_timeout)
            self.acq_timer.daemon = True
            self.acq_timer.start()

        self.acq_start_btn.config(state="disabled")
        self.acq_stop_btn.config(state="normal")
        self.acq_flush_now_btn.config(state="normal")
        self.acq_state_lbl.config(text="Acquiring…", foreground="#0070c0")
        self.acq_stat_folder.set(session_dir)
        self._update_acq_stats()

        self._post("acq_log",
                   (f"Session started → {session_dir}", "OK"))
        limit_txt = (f"{max_time:g} s max" if max_time > 0 else "no time limit")
        self._post("acq_log",
                   (f"mode={cfg['mode']}, ch={cfg['channel']}, "
                    f"pts={pts}, batch={batch}, "
                    f"trig={trig_v:.4f} V, {limit_txt}", "INFO"))

        t = threading.Thread(
            target=self._acq_worker,
            args=(cfg["mode"], cfg["channel"], trig_v, cfg["slope"],
                  pts, batch, flush_pct),
            daemon=True,
        )
        t.start()

    def _on_acq_stop(self):
        if not self.acq_running:
            return
        if self.acq_timer is not None:
            self.acq_timer.cancel()
        self.acq_stop.set()
        self.acq_state_lbl.config(text="Stopping…", foreground="#c07000")
        self.acq_stop_btn.config(state="disabled")

    def _on_acq_timeout(self):
        """Fired by the auto-stop timer when the max measurement time is up."""
        if self.acq_running:
            self._post("acq_log",
                       (f"Max time reached ({self.acq_maxtime_val:g} s) – "
                        f"stopping.", "WARN"))
            self.acq_stop.set()

    def _on_acq_flush_now(self):
        self._post("acq_flush_now", None)

    def _update_acq_stats(self):
        n_ram   = len(self.acq_buffer)
        n_disk  = self.acq_saved_frames
        try:
            pts = int(self.acq_pts.get())
        except ValueError:
            pts = 800
        mb      = (n_ram * pts) / 1e6
        elapsed = time.perf_counter() - self.acq_t0 if self.acq_t0 else 0.0
        rate    = self.acq_total_frames / elapsed if elapsed > 0 else 0.0

        self.acq_stat_ram.set(f"{n_ram:,}")
        self.acq_stat_disk.set(f"{n_disk:,}")
        self.acq_stat_flushes.set(str(self.acq_flush_count))
        self.acq_stat_rate.set(f"{rate:.1f} fr/s")
        self.acq_stat_mb.set(f"{mb:.1f} MB")
        self.acq_stat_elapsed.set(f"{elapsed:.1f} s")

        try:
            batch = int(self.acq_batch.get())
            pct   = int(100 * n_ram / batch) if batch > 0 else 0
        except ValueError:
            pct = 0
        self.acq_ram_bar["value"] = min(pct, 100)

    # ── worker ──────────────────────────────────────────────────────────────

    def _acq_worker(self, mode, channel, trig_v, slope, pts, batch, flush_pct):
        scope           = self.scope
        flush_threshold = max(1, int(batch * flush_pct / 100))

        # common scope setup
        scope.w(f":WAVeform:SOURce {channel}")
        scope.w(":WAVeform:FORMat BYTE")
        scope.w(":WAVeform:MODE NORMal")
        scope.w(f":WAVeform:POINts {pts}")
        scope.w(":TRIGger:MODE EDGE")
        scope.w(f":TRIGger:EDGE:SOURce {channel}")
        scope.w(f":TRIGger:EDGE:SLOPe {slope}")
        scope.w(f":TRIGger:EDGE:LEVel {trig_v}")

        # grab preamble
        raw_pre = scope.q(":WAVeform:PREamble?")
        self.acq_preamble = _parse_preamble(raw_pre)
        if self.acq_preamble:
            # save alongside batches
            try:
                with open(os.path.join(self.acq_session_dir,
                                       "preamble.json"), "w") as f:
                    json.dump(self.acq_preamble, f, indent=2)
            except Exception:
                pass
            self._post("acq_log",
                       (f"Preamble saved  "
                        f"(yinc={self.acq_preamble.get('yinc','?'):.3e} V, "
                        f"xinc={self.acq_preamble.get('xinc','?'):.3e} s)",
                        "OK"))
        else:
            self._post("acq_log",
                       ("Preamble unavailable – raw ADC counts will be saved.",
                        "WARN"))

        try:
            if mode == "record":
                self._acq_loop_record(scope, channel, pts, batch, flush_threshold)
            else:
                self._acq_loop_single(scope, pts, flush_threshold)
        except Exception as e:
            self._post("acq_log", (f"Acquisition error: {e}", "ERROR"))

        # final flush
        if self.acq_buffer:
            self._post("acq_log",
                       (f"Final flush: {len(self.acq_buffer)} frames.", "INFO"))
            self._do_flush()

        scope.w(":STOP")

        # Stamp the real elapsed (live) time onto the session: append _t{N}s to
        # the folder name and record it in session_info.json (useful for count
        # rate = frames / live_time).
        self._finalise_session()

        self._post("acq_done", None)

    def _finalise_session(self):
        elapsed = time.perf_counter() - self.acq_t0 if self.acq_t0 else 0.0
        elapsed_i = int(round(elapsed))
        # update / extend session_info.json
        info_path = os.path.join(self.acq_session_dir, "session_info.json")
        try:
            with open(info_path) as f:
                info = json.load(f)
        except Exception:
            info = {}
        info.update({
            "elapsed_s":     round(elapsed, 3),
            "total_frames":  self.acq_total_frames,
            "saved_frames":  self.acq_saved_frames,
        })
        try:
            with open(info_path, "w") as f:
                json.dump(info, f, indent=2)
        except Exception:
            pass
        # rename folder to append the real measured time
        try:
            parent = os.path.dirname(self.acq_session_dir)
            name   = os.path.basename(self.acq_session_dir)
            if not name.endswith(f"_t{elapsed_i}s"):
                new_dir = os.path.join(parent, f"{name}_t{elapsed_i}s")
                os.rename(self.acq_session_dir, new_dir)
                self.acq_session_dir = new_dir
                self._post("acq_folder", new_dir)
                self._post("acq_log",
                           (f"Session live time {elapsed:.1f} s → "
                            f"{os.path.basename(new_dir)}", "OK"))
        except Exception as e:
            self._post("acq_log",
                       (f"Could not rename session folder: {e}", "WARN"))

    def _acq_loop_record(self, scope, channel, pts, batch, flush_threshold):
        """
        Fast 'store-in-RAM' acquisition using the MHO900 hardware Waveform
        Recording subsystem (segmented memory).

        Phase 1 (fast, hardware driven): the scope captures every trigger into
        its OWN acquisition RAM as a separate frame, re-arming in microseconds.
        Nothing crosses USB/LAN during this phase.

        Phase 2 (transfer): the recorded frames are read out of scope RAM one
        by one.  Different MHO/DHO firmwares expose a recorded frame to
        :WAVeform:DATA? through slightly different 'select frame' commands, so
        the working command is auto-detected once (see _pick_frame_reader) and
        then reused for every frame.

        SCPI per RIGOL MHO900 Programming Guide, section 3.19 (:RECord).
        """
        scope.inst.timeout = 30_000

        try:
            fmax = int(float(scope.q(":RECord:WRECord:FMAX?")))
        except Exception:
            fmax = 0
        if fmax > 0:
            self._post("acq_log",
                       (f"Scope can hold up to {fmax:,} frames at the current "
                        f"memory depth.", "INFO"))

        while not self.acq_stop.is_set():
            target = min(batch, 500_000)
            if fmax > 0:
                target = min(target, fmax)
            target = max(1, target)

            # ---- Phase 1: fill the scope's RAM -------------------------------
            scope.w(":STOP")
            scope.w(":RECord:WRECord:ENABle ON")
            scope.w(f":RECord:WRECord:FRAMes {target}")
            scope.w(":RECord:WRECord:OPERate RUN")
            self._post("acq_log",
                       (f"Recording into scope RAM: target {target:,} frames...",
                        "INFO"))

            last_n, stall = 0, 0
            while not self.acq_stop.is_set():
                try:
                    n = int(float(scope.q(":RECord:CURRent?")))
                except Exception:
                    n = last_n
                try:
                    op = scope.q(":RECord:WRECord:OPERate?").upper()
                except Exception:
                    op = ""
                if n != last_n:
                    last_n, stall = n, 0
                    self._post("acq_stats_update", {})
                else:
                    stall += 1
                if n >= target or op.startswith("STOP"):
                    break
                if stall > 400:                      # ~20 s with no progress
                    self._post("acq_log",
                               ("No new frames for ~20 s - ending pass.",
                                "WARN"))
                    break
                time.sleep(0.05)

            scope.w(":RECord:WRECord:OPERate STOP")

            try:
                n_rec = int(float(scope.q(":RECord:CURRent?")))
            except Exception:
                n_rec = last_n
            if n_rec < 1:
                self._post("acq_log", ("No frames recorded this pass.", "WARN"))
                time.sleep(0.2)
                continue

            # ---- Phase 2: read frames out of scope RAM -----------------------
            self._post("acq_log",
                       (f"{n_rec:,} frames in scope RAM - downloading...",
                        "INFO"))
            scope.w(":STOP")
            scope.w(f":WAVeform:SOURce {channel}")
            scope.w(":WAVeform:FORMat BYTE")
            scope.w(":WAVeform:MODE NORMal")
            scope.w(f":WAVeform:POINts {pts}")

            reader = self._pick_frame_reader(scope, pts)
            if reader is None:
                self._post("acq_log",
                           ("Recording worked but the frames could not be read "
                            "back over SCPI on this firmware (see scope error "
                            "above). Stopping this run.", "ERROR"))
                self.acq_stop.set()
                break

            downloaded = 0
            for i in range(1, n_rec + 1):
                if self.acq_stop.is_set():
                    break
                arr = reader(i)
                if arr is not None and len(arr) >= pts:
                    self.acq_buffer.append(arr[:pts].copy())
                    downloaded += 1
                    self.acq_total_frames += 1
                if len(self.acq_buffer) >= flush_threshold:
                    self._do_flush()
                if downloaded and downloaded % 500 == 0:
                    self._post("acq_stats_update", {})

            self._post("acq_log",
                       (f"Pass complete: {downloaded:,} frames transferred "
                        f"(session total {self.acq_total_frames:,}).", "OK"))
            self._post("acq_stats_update", {})

        # Leave recording mode cleanly.
        scope.w(":RECord:WRECord:OPERate STOP")
        scope.w(":RECord:WRECord:ENABle OFF")

    def _pick_frame_reader(self, scope, pts):
        """
        Auto-detect how to read one recorded frame on this firmware.

        Tries each known 'select frame' command (with a few settle delays) on
        frame #1; the first whose :WAVeform:DATA? returns a full-length
        waveform wins, and a reader(i) -> ndarray|None closure reusing that
        command is returned.  Returns None (and logs :SYSTem:ERRor?) if none
        of them yield data, so the GUI reports the real cause instead of
        silently saving nothing.
        """
        def read_data():
            try:
                d = scope.inst.query_binary_values(
                    ":WAVeform:DATA?", datatype="B", container=np.array)
                return d if (d is not None and len(d) >= 1) else None
            except Exception:
                return None

        selectors = [
            ("RECord:WREPlay:FCURrent",
             lambda i: scope.w(f":RECord:WREPlay:FCURrent {i}")),
            ("RECord:CURRent",
             lambda i: scope.w(f":RECord:CURRent {i}")),
        ]
        # Put replay into a paused state so frames can be stepped manually.
        scope.w(":RECord:WREPlay:OPERate STOP")

        for name, select in selectors:
            for settle in (0.0, 0.005, 0.02):
                try:
                    select(1)
                    if settle:
                        time.sleep(settle)
                    d = read_data()
                except Exception:
                    d = None
                if d is not None and len(d) >= pts:
                    self._post("acq_log",
                               (f"Frame readout OK via :{name} "
                                f"(settle {settle * 1000:.0f} ms, "
                                f"{len(d)} pts/frame).", "OK"))

                    def reader(i, _sel=select, _settle=settle):
                        _sel(i)
                        if _settle:
                            time.sleep(_settle)
                        return read_data()

                    return reader

        err = scope.q(":SYSTem:ERRor?")
        self._post("acq_log",
                   (f"Frame-readback probe failed - no select command returned "
                    f"data. Scope says: {err}", "ERROR"))
        return None

    def _acq_loop_single(self, scope, pts, flush_threshold):
        scope.inst.timeout = 10_000
        self._post("acq_log",
                   ("Single-trigger mode – ~10-100 Hz.", "WARN"))
        last_upd = time.perf_counter()

        while not self.acq_stop.is_set():
            scope.w(":SINGle")
            triggered = False
            for _ in range(400):
                if self.acq_stop.is_set():
                    break
                if scope.q(":TRIGger:STATus?").upper().startswith(
                        ("STOP", "TD")):
                    triggered = True
                    break
                time.sleep(0.005)
            if not triggered:
                continue
            try:
                data = scope.inst.query_binary_values(
                    ":WAVeform:DATA?", datatype="B", container=np.array)
                if data is not None and len(data) >= pts:
                    self.acq_buffer.append(data[:pts].copy())
                    self.acq_total_frames += 1
            except Exception as e:
                self._post("acq_log", (f"Read error: {e}", "WARN"))
                continue
            if len(self.acq_buffer) >= flush_threshold:
                self._do_flush()
            now = time.perf_counter()
            if now - last_upd >= 1.0:
                self._post("acq_stats_update", {})
                last_upd = now

    def _do_flush(self):
        n, path = _flush_batch(
            self.acq_buffer, self.acq_session_dir, self.acq_flush_count)
        if n > 0:
            self.acq_saved_frames += n
            self.acq_flush_count  += 1
            self.acq_buffer.clear()
            self._post("acq_log",
                       (f"Flushed {n:,} frames → {os.path.basename(path)} "
                        f"(total saved: {self.acq_saved_frames:,})", "OK"))
        else:
            self._post("acq_log", (f"Flush failed: {path}", "ERROR"))
        self._post("acq_stats_update", {})

    # =======================================================================
    # Message queue
    # =======================================================================

    def _post(self, kind, payload):
        self.msg_q.put((kind, payload))

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "progress":
                    cur, total, msg = payload
                    self.progress["maximum"] = max(total, 1)
                    self.progress["value"]   = cur
                    self.status_var.set(msg)
                elif kind == "usb_done":
                    self._usb_done(payload)
                elif kind == "lan_done":
                    self._lan_done(payload)
                elif kind == "diag_done":
                    self._render_sections(payload)
                    self.status_var.set("Diagnostic complete.")
                    self.save_btn.config(state="normal")
                    self._set_busy(False)
                elif kind == "bench_done":
                    self._render_one_section("Live benchmark", payload)
                    self.status_var.set("Benchmark complete.")
                    self._set_busy(False)
                elif kind == "error":
                    messagebox.showerror("Error", payload)
                    self.status_var.set("Error.")
                    self._set_busy(False)
                elif kind == "acq_log":
                    msg, level = payload
                    self.acq_log.config(state="normal")
                    ts = time.strftime("%H:%M:%S")
                    self.acq_log.insert("end", f"[{ts}] {msg}\n", level)
                    self.acq_log.see("end")
                    self.acq_log.config(state="disabled")
                elif kind == "acq_stats_update":
                    self._update_acq_stats()
                elif kind == "acq_folder":
                    self.acq_stat_folder.set(payload)
                elif kind == "acq_flush_now":
                    if self.acq_buffer:
                        threading.Thread(target=self._do_flush,
                                         daemon=True).start()
                elif kind == "acq_done":
                    self.acq_running = False
                    if self.acq_timer is not None:
                        self.acq_timer.cancel()
                        self.acq_timer = None
                    self.acq_start_btn.config(
                        state="normal" if self.scope else "disabled")
                    self.acq_stop_btn.config(state="disabled")
                    self.acq_flush_now_btn.config(state="disabled")
                    self.acq_state_lbl.config(text="Idle", foreground="#888")
                    self._update_acq_stats()
                    self._post("acq_log",
                               (f"Session ended – "
                                f"total={self.acq_total_frames:,}, "
                                f"saved={self.acq_saved_frames:,}, "
                                f"flushes={self.acq_flush_count}.", "OK"))
        except queue.Empty:
            pass
        self.after(80, self._drain_queue)

    # =======================================================================
    # Existing diagnostic methods (unchanged)
    # =======================================================================

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for b in (self.connect_btn, self.diag_btn, self.bench_btn):
            b.config(state=state)
        if not busy:
            self.diag_btn.config(state="normal" if self.scope else "disabled")
            self.bench_btn.config(state="normal" if self.scope else "disabled")
            self.connect_btn.config(state="disabled" if self.scope else "normal")
            self.disconnect_btn.config(
                state="normal" if self.scope else "disabled")

    def _on_scan_usb(self):
        self._set_busy(True)
        self.status_var.set("Scanning USB…")
        self.usb_status.config(text="Scanning…", foreground="#555")

        def task():
            try:
                items  = discover_usb(self.rm)
                extra  = discover_usb_via_pyusb()
                exist  = {r for r, _ in items}
                for r, lbl in extra:
                    if r not in exist:
                        items.append((r, lbl))
                self._post("usb_done", items)
            except Exception as e:
                self._post("error", f"USB scan failed: {e}")

        threading.Thread(target=task, daemon=True).start()

    def _usb_done(self, items):
        if not items:
            self.usb_combo["values"] = []
            self.usb_status.config(
                text="No USB devices found. Check cable / power / drivers.",
                foreground="#a00000")
        else:
            self.usb_combo["values"] = [lbl for _, lbl in items]
            for r, lbl in items:
                self._discover_map[lbl] = r
            self.usb_combo.current(0)
            rc = sum(1 for _, lbl in items if "★" in lbl)
            self.usb_status.config(
                text=f"Found {len(items)} USB device(s)"
                     + (f", {rc} Rigol." if rc else "."),
                foreground="#0a7a0a")
        self.status_var.set("USB scan complete.")
        self._set_busy(False)

    def _on_scan_lan(self):
        self._set_busy(True)
        self.status_var.set("Scanning LAN…")
        self.progress["value"] = 0
        self.stop_discover.clear()

        def task():
            try:
                results = discover_lan(
                    progress_cb=lambda c, t, m: self._post(
                        "progress", (c, t, m)),
                    stop_event=self.stop_discover)
                self._post("lan_done", results)
            except Exception as e:
                self._post("error", f"LAN scan failed: {e}")

        threading.Thread(target=task, daemon=True).start()

    def _lan_done(self, results):
        if not results:
            self.lan_combo["values"] = []
            self.status_var.set("No LAN devices found.")
        else:
            self.lan_combo["values"] = [lbl for _, lbl in results]
            for r, lbl in results:
                self._discover_map[lbl] = r
            self.lan_combo.current(0)
            self.status_var.set(f"Found {len(results)} LAN device(s).")
        self._set_busy(False)

    def _on_use_ip(self):
        ip = self.ip_var.get().strip()
        if ip:
            self.lan_var.set(f"TCPIP0::{ip}::INSTR")

    def _current_resource(self) -> str | None:
        tab  = self.nb.index(self.nb.select())
        text = [self.usb_var, self.lan_var,
                self.manual_var][tab].get().strip()
        if not text:
            return None
        if text in self._discover_map:
            return self._discover_map[text]
        if "::" in text:
            return text
        try:
            ipaddress.ip_address(text)
            return f"TCPIP0::{text}::INSTR"
        except ValueError:
            return text

    def _on_connect(self):
        resource = self._current_resource()
        if not resource:
            messagebox.showwarning("Connect",
                                   "Pick a device or enter an address first.")
            return
        try:
            inst = self.rm.open_resource(resource, open_timeout=3000)
            self.scope = Scope(inst)
            idn       = self.scope.q("*IDN?")
            transport = resource.split("::")[0]
            self.conn_label.config(
                text=f"Connected ({transport}): {idn}",
                foreground="#0a7a0a")
            self.status_var.set(f"Connected via {transport}.")
            self.disconnect_btn.config(state="normal")
            self.diag_btn.config(state="normal")
            self.bench_btn.config(state="normal")
            self.acq_start_btn.config(state="normal")
            self.connect_btn.config(state="disabled")
        except Exception as e:
            hint = ""
            if "USB"   in resource.upper():
                hint = ("\n\nUSB: install NI-VISA or pip install pyusb "
                        "with libusb-1.0.")
            elif "TCPIP" in resource.upper():
                hint = "\n\nLAN: check subnet, I/O settings, firewall."
            messagebox.showerror("Connect",
                                 f"Failed to open {resource}\n\n{e}{hint}")
            self.status_var.set("Connection failed.")

    def _on_disconnect(self):
        if self.acq_running:
            self.acq_stop.set()
        try:
            if self.scope:
                self.scope.inst.close()
        except Exception:
            pass
        self.scope = None
        self.conn_label.config(text="Not connected.", foreground="#555")
        self.connect_btn.config(state="normal")
        self.disconnect_btn.config(state="disabled")
        self.diag_btn.config(state="disabled")
        self.bench_btn.config(state="disabled")
        self.acq_start_btn.config(state="disabled")
        self.status_var.set("Disconnected.")

    def _on_run_diagnostic(self):
        if not self.scope:
            return
        self._set_busy(True)
        self.status_var.set("Running diagnostic…")
        self._clear_output()

        def task():
            try:
                self._post("diag_done", probe_all(self.scope))
            except Exception as e:
                self._post("error", f"Diagnostic failed: {e}")

        threading.Thread(target=task, daemon=True).start()

    def _on_benchmark(self):
        if not self.scope:
            return
        self._set_busy(True)

        def task():
            try:
                rows = benchmark(
                    self.scope, n=50,
                    progress_cb=lambda c, t, m: self._post(
                        "progress", (c, t, m)))
                self._post("bench_done", rows)
            except Exception as e:
                self._post("error", f"Benchmark failed: {e}")

        threading.Thread(target=task, daemon=True).start()

    def _clear_output(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)

    def _render_sections(self, sections):
        self._clear_output()
        for title, rows in sections:
            self._render_one_section(title, rows)

    def _render_one_section(self, title, rows):
        sid = self.tree.insert("", "end", text=title, tags=("section",),
                               open=True)
        for label, value, ok in rows:
            self.tree.insert(sid, "end", text=label,
                             values=(value, "OK" if ok else "X"),
                             tags=("ok" if ok else "bad",))

    def _show_backend_info(self):
        bk    = self.backends
        lines = [
            f"pyvisa-py:   {bk['pyvisa_py']}",
            f"NI-VISA:     {bk['ni_visa']}",
            f"pyusb:       {bk['pyusb']}",
            f"libusb:      {bk['libusb']}",
            f"Recommended: {bk['recommended'] or 'none'}",
            f"matplotlib:  {HAS_MPL}",
            "",
        ]
        if bk["notes"]:
            lines += ["Notes:"] + ["  - " + n for n in bk["notes"]] + [""]
        lines += [
            "To enable USB on Windows without NI-VISA:",
            "  1) pip install pyusb",
            "  2) install libusb-1.0  (https://libusb.info)",
            "     or install Rigol UltraSigma which bundles it.",
            "",
            "Or install NI-VISA Runtime from ni.com/visa (recommended).",
        ]
        messagebox.showinfo("Backend info", "\n".join(lines))

    def _on_save(self):
        if not self.tree.get_children():
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")],
            initialfile="mho984_diagnostic.txt",
        )
        if not path:
            return
        lines = []
        for sec in self.tree.get_children():
            title = self.tree.item(sec, "text")
            lines += ["\n" + "=" * 64, f"  {title}", "=" * 64]
            for row in self.tree.get_children(sec):
                label = self.tree.item(row, "text")
                value, mark = self.tree.item(row, "values")
                lines.append(f"  [{mark}] {label:<32} : {value}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        messagebox.showinfo("Saved", f"Report written to:\n{path}")


if __name__ == "__main__":
    App().mainloop()