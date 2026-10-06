"""
Visor de Pulsos v2 - análisis de archivos .h5 producidos por manual.py

Pestañas:
    - Pulso individual: visualiza 1 pulso con baseline, pk-pk e integral.
    - PHS (histograma): histograma de integrales (o pk-pk, a elección).
    - Overlay: N pulsos aleatorios superpuestos + forma promedio.
    - Estadísticas: resumen numérico, compara con 2do archivo opcional.

Unidades físicas:
    - ADC de 8 bits (0..255) con 0 V asumido en 128.
    - Escala vertical (V/div) configurable en la barra superior.
    - Timebase (ns/div) configurable para el eje temporal y la integral.
    - Amplitud en mV.
    - Integral en mV·ns (proporcional a la carga del PMT, y por tanto a la
      energía depositada en el scintillador). Es la métrica física correcta
      para un PHS de espectro de energía.
"""

import os
import random
import tkinter as tk
from tkinter import filedialog, ttk, messagebox

import h5py
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import (
    FigureCanvasTkAgg, NavigationToolbar2Tk,
)


# ========================== CONFIGURACIÓN ==========================
DEFAULT_V_DIV             = 0.2    # V/div vertical
DEFAULT_N_DIVS_VERT       = 8.0    # divisiones verticales totales
DEFAULT_TIMEBASE_NS_DIV   = 10.0   # ns/div horizontal (Rigol: 12 divs)
DEFAULT_N_DIVS_HORIZ      = 12.0   # divisiones horizontales totales
# ===================================================================


def mv_per_count(v_div, n_divs=DEFAULT_N_DIVS_VERT):
    return v_div * n_divs * 1000.0 / 256.0


def counts_to_mv(counts, v_div=DEFAULT_V_DIV, n_divs=DEFAULT_N_DIVS_VERT):
    """ADC 0..255 → mV, 0 V en 128."""
    return (np.asarray(counts, dtype=np.float32) - 128.0) * mv_per_count(v_div, n_divs)


def dt_ns_for(n_points, timebase_ns_div, n_divs=DEFAULT_N_DIVS_HORIZ):
    """dt por muestra [ns] dado timebase y cantidad de puntos por frame."""
    total_ns = timebase_ns_div * n_divs
    return total_ns / max(n_points, 1)


def integral_mv_ns(pulso_mv, dt_ns):
    """Integral del pulso restando baseline (mediana), con signo.

    - Usa mediana como baseline (robusta al pico).
    - Devuelve área firmada: negativa para pulsos PMT típicos (ánodo
      acoplado DC produce pulsos bajo 0 V). La magnitud es la carga
      depositada en mV·ns.
    """
    baseline = np.median(pulso_mv)
    return float(np.sum(pulso_mv - baseline)) * dt_ns


class VisorPulsosV2:
    def __init__(self, root):
        self.root = root
        self.root.title("Visor de Pulsos v2 - Tesis Mateo")
        self.root.geometry("1180x800")
        self._style_tk()

        # --- Estado ---
        self.archivo_h5  = None
        self.ruta_actual = None
        self.grupo       = None
        self.nombres     = []
        self.indice      = 0

        # Caches globales (se recalculan al abrir o cambiar escala)
        self._amp_cache_mv    = None     # amplitud pk-pk [mV]
        self._int_cache_mvns  = None     # integral firmada [mV·ns]
        self._absint_cache    = None     # integral absoluta [mV·ns]
        self._baseline_cache  = None     # baseline [mV]
        self._npts            = None     # puntos por pulso (del primero)

        # Archivo B opcional para comparar PHS
        self.amp_cache_mv_B    = None
        self.int_cache_mvns_B  = None
        self.absint_cache_B    = None
        self.nombre_archivo_B  = None

        # Escalas editables
        self.v_div        = tk.DoubleVar(value=DEFAULT_V_DIV)
        self.tb_ns_div    = tk.DoubleVar(value=DEFAULT_TIMEBASE_NS_DIV)
        self.metric_var   = tk.StringVar(value="integral")  # integral | abs_integral | amplitude

        # --- Layout ---
        self._build_topbar()
        self._build_tabs()
        self._bind_keys()
        self._refresh_controls()

    def _style_tk(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TNotebook.Tab", padding=(14, 6), font=("Segoe UI", 10))
        style.configure("Section.TLabel", font=("Segoe UI", 9, "bold"), foreground="#444")

    # ------------------------------------------------------------------
    # Topbar
    # ------------------------------------------------------------------
    def _build_topbar(self):
        bar = tk.Frame(self.root, bg="#f5f5f7")
        bar.pack(side=tk.TOP, fill=tk.X)

        row = tk.Frame(bar, bg="#f5f5f7")
        row.pack(side=tk.TOP, fill=tk.X, padx=10, pady=8)

        tk.Button(row, text="Abrir .h5", command=self.abrir_archivo,
                  bg="#2d6cdf", fg="white", relief=tk.FLAT, padx=12, pady=4,
                  activebackground="#1e50aa", activeforeground="white"
                  ).pack(side=tk.LEFT)
        tk.Button(row, text="+ Archivo B (comparar)",
                  command=self.abrir_archivo_B,
                  bg="#7b4ad1", fg="white", relief=tk.FLAT, padx=10, pady=4,
                  activebackground="#5d35a8", activeforeground="white"
                  ).pack(side=tk.LEFT, padx=(6, 0))

        # Calibración
        sep = tk.Frame(row, bg="#d6d6d6", width=1)
        sep.pack(side=tk.LEFT, padx=12, fill=tk.Y)

        tk.Label(row, text="V/div:", bg="#f5f5f7").pack(side=tk.LEFT)
        tk.Entry(row, textvariable=self.v_div, width=6, justify="center"
                 ).pack(side=tk.LEFT, padx=(2, 8))

        tk.Label(row, text="Timebase [ns/div]:", bg="#f5f5f7").pack(side=tk.LEFT)
        tk.Entry(row, textvariable=self.tb_ns_div, width=6, justify="center"
                 ).pack(side=tk.LEFT, padx=(2, 8))

        tk.Button(row, text="↻ Recalcular", command=self._recompute_cache,
                  bg="#4a4a4a", fg="white", relief=tk.FLAT, padx=10, pady=3,
                  activebackground="#2b2b2b"
                  ).pack(side=tk.LEFT, padx=(4, 0))

        # Estado
        self.lbl_estado = tk.Label(row, text="Ningún archivo cargado",
                                   anchor="e", fg="#666", bg="#f5f5f7")
        self.lbl_estado.pack(side=tk.RIGHT)

        sep2 = tk.Frame(bar, bg="#d6d6d6", height=1)
        sep2.pack(side=tk.TOP, fill=tk.X)

    # ------------------------------------------------------------------
    # Pestañas
    # ------------------------------------------------------------------
    def _build_tabs(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 10))

        self.tab_pulso   = tk.Frame(nb, bg="white")
        self.tab_phs     = tk.Frame(nb, bg="white")
        self.tab_diff    = tk.Frame(nb, bg="white")
        self.tab_overlay = tk.Frame(nb, bg="white")
        self.tab_stats   = tk.Frame(nb, bg="white")
        nb.add(self.tab_pulso,   text="Pulso individual")
        nb.add(self.tab_phs,     text="Histograma (PHS)")
        nb.add(self.tab_diff,    text="Diferencia A − B")
        nb.add(self.tab_overlay, text="Overlay")
        nb.add(self.tab_stats,   text="Estadísticas")

        self._build_tab_pulso()
        self._build_tab_phs()
        self._build_tab_diff()
        self._build_tab_overlay()
        self._build_tab_stats()

    # ---------- Pulso individual ----------
    def _build_tab_pulso(self):
        ctrl = tk.Frame(self.tab_pulso, bg="white")
        ctrl.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)

        self.btn_first = tk.Button(ctrl, text="|<", width=4, command=lambda: self._goto(0))
        self.btn_prev  = tk.Button(ctrl, text="<", width=4,
                                   command=lambda: self._goto(self.indice - 1))
        self.btn_next  = tk.Button(ctrl, text=">", width=4,
                                   command=lambda: self._goto(self.indice + 1))
        self.btn_last  = tk.Button(ctrl, text=">|", width=4,
                                   command=lambda: self._goto(len(self.nombres) - 1))
        self.btn_big   = tk.Button(ctrl, text="↗ Siguiente grande",
                                   command=self._next_big)
        for b in (self.btn_first, self.btn_prev, self.btn_next, self.btn_last, self.btn_big):
            b.pack(side=tk.LEFT, padx=2)

        tk.Label(ctrl, text="  Ir a #:", bg="white").pack(side=tk.LEFT, padx=(12, 0))
        self.entry_goto = tk.Entry(ctrl, width=8, justify="center")
        self.entry_goto.pack(side=tk.LEFT)
        self.entry_goto.bind("<Return>", self._goto_from_entry)

        tk.Button(ctrl, text="Guardar PNG",
                  command=lambda: self._save_current_png("pulso")
                  ).pack(side=tk.RIGHT)

        self.fig_p, self.ax_p = plt.subplots(figsize=(9, 5))
        self.fig_p.patch.set_facecolor("white")
        self.canvas_p = FigureCanvasTkAgg(self.fig_p, master=self.tab_pulso)
        NavigationToolbar2Tk(self.canvas_p, self.tab_pulso)
        self.canvas_p.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

    # ---------- PHS ----------
    def _build_tab_phs(self):
        ctrl = tk.Frame(self.tab_phs, bg="white")
        ctrl.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)

        # Selector de métrica (también se aplica a la pestaña "Diferencia")
        tk.Label(ctrl, text="Métrica:", bg="white").pack(side=tk.LEFT)
        for value, label in (("integral",     "Integral"),
                             ("abs_integral", "|Integral|"),
                             ("amplitude",    "Amp. pk-pk")):
            tk.Radiobutton(ctrl, text=label, value=value, variable=self.metric_var,
                           bg="white",
                           command=lambda: (self._update_phs(), self._update_diff())
                           ).pack(side=tk.LEFT, padx=(2, 2))

        tk.Label(ctrl, text="  Bins:", bg="white").pack(side=tk.LEFT, padx=(10, 0))
        self.sp_bins = tk.Spinbox(ctrl, from_=10, to=500, width=5, increment=10,
                                  command=self._update_phs)
        self.sp_bins.delete(0, tk.END); self.sp_bins.insert(0, "80")
        self.sp_bins.pack(side=tk.LEFT, padx=(2, 10))

        tk.Label(ctrl, text="Rango:", bg="white").pack(side=tk.LEFT)
        self.entry_xmin = tk.Entry(ctrl, width=8, justify="center")
        self.entry_xmin.pack(side=tk.LEFT, padx=(2, 2))
        self.entry_xmin.bind("<Return>", lambda e: self._update_phs())
        tk.Label(ctrl, text="—", bg="white").pack(side=tk.LEFT)
        self.entry_xmax = tk.Entry(ctrl, width=8, justify="center")
        self.entry_xmax.pack(side=tk.LEFT, padx=(2, 6))
        self.entry_xmax.bind("<Return>", lambda e: self._update_phs())

        self.var_logy = tk.BooleanVar(value=False)
        tk.Checkbutton(ctrl, text="log-Y", variable=self.var_logy,
                       bg="white", command=self._update_phs
                       ).pack(side=tk.LEFT, padx=6)

        self.var_normalize = tk.BooleanVar(value=False)
        tk.Checkbutton(ctrl, text="Normalizar", variable=self.var_normalize,
                       bg="white", command=self._update_phs
                       ).pack(side=tk.LEFT, padx=6)

        tk.Button(ctrl, text="Actualizar", command=self._update_phs
                  ).pack(side=tk.LEFT, padx=4)
        tk.Button(ctrl, text="Guardar PNG",
                  command=lambda: self._save_current_png("phs")
                  ).pack(side=tk.RIGHT)

        self.fig_h, self.ax_h = plt.subplots(figsize=(9, 5))
        self.fig_h.patch.set_facecolor("white")
        self.canvas_h = FigureCanvasTkAgg(self.fig_h, master=self.tab_phs)
        NavigationToolbar2Tk(self.canvas_h, self.tab_phs)
        self.canvas_h.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

    # ---------- Diferencia A − B ----------
    def _build_tab_diff(self):
        ctrl = tk.Frame(self.tab_diff, bg="white")
        ctrl.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)

        tk.Label(ctrl, text="Bins:", bg="white").pack(side=tk.LEFT)
        self.sp_bins_d = tk.Spinbox(ctrl, from_=10, to=500, width=5, increment=10,
                                    command=self._update_diff)
        self.sp_bins_d.delete(0, tk.END); self.sp_bins_d.insert(0, "80")
        self.sp_bins_d.pack(side=tk.LEFT, padx=(2, 10))

        tk.Label(ctrl, text="Rango:", bg="white").pack(side=tk.LEFT)
        self.entry_xmin_d = tk.Entry(ctrl, width=8, justify="center")
        self.entry_xmin_d.pack(side=tk.LEFT, padx=(2, 2))
        self.entry_xmin_d.bind("<Return>", lambda e: self._update_diff())
        tk.Label(ctrl, text="—", bg="white").pack(side=tk.LEFT)
        self.entry_xmax_d = tk.Entry(ctrl, width=8, justify="center")
        self.entry_xmax_d.pack(side=tk.LEFT, padx=(2, 6))
        self.entry_xmax_d.bind("<Return>", lambda e: self._update_diff())

        tk.Label(ctrl, text="Escala B:", bg="white").pack(side=tk.LEFT, padx=(8, 0))
        self.entry_scale_b = tk.Entry(ctrl, width=7, justify="center")
        self.entry_scale_b.insert(0, "1.0")
        self.entry_scale_b.pack(side=tk.LEFT, padx=(2, 2))
        self.entry_scale_b.bind("<Return>", lambda e: self._update_diff())
        tk.Label(ctrl, text="(B×factor antes de restar)",
                 bg="white", fg="#777", font=("Segoe UI", 8)
                 ).pack(side=tk.LEFT, padx=(0, 8))

        self.var_errbars = tk.BooleanVar(value=True)
        tk.Checkbutton(ctrl, text="Barras error (Poisson)",
                       variable=self.var_errbars, bg="white",
                       command=self._update_diff
                       ).pack(side=tk.LEFT, padx=6)

        self.var_show_both = tk.BooleanVar(value=True)
        tk.Checkbutton(ctrl, text="Superponer A y B",
                       variable=self.var_show_both, bg="white",
                       command=self._update_diff
                       ).pack(side=tk.LEFT, padx=6)

        tk.Button(ctrl, text="Actualizar", command=self._update_diff
                  ).pack(side=tk.LEFT, padx=4)
        tk.Button(ctrl, text="Guardar PNG",
                  command=lambda: self._save_current_png("diff")
                  ).pack(side=tk.RIGHT)

        tk.Button(ctrl, text="Exportar CSV",
                  command=self._export_diff_csv
                  ).pack(side=tk.RIGHT, padx=(0, 6))

        self.fig_d, self.ax_d = plt.subplots(2, 1, figsize=(9, 6),
                                             gridspec_kw={"height_ratios": [2, 1]},
                                             sharex=True)
        self.fig_d.patch.set_facecolor("white")
        self.canvas_d = FigureCanvasTkAgg(self.fig_d, master=self.tab_diff)
        NavigationToolbar2Tk(self.canvas_d, self.tab_diff)
        self.canvas_d.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

    # ---------- Overlay ----------
    def _build_tab_overlay(self):
        ctrl = tk.Frame(self.tab_overlay, bg="white")
        ctrl.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)

        tk.Label(ctrl, text="Cantidad de pulsos:", bg="white").pack(side=tk.LEFT)
        self.sp_nover = tk.Spinbox(ctrl, from_=5, to=500, width=5, increment=5,
                                   command=self._update_overlay)
        self.sp_nover.delete(0, tk.END); self.sp_nover.insert(0, "50")
        self.sp_nover.pack(side=tk.LEFT, padx=(2, 10))

        self.var_show_mean = tk.BooleanVar(value=True)
        tk.Checkbutton(ctrl, text="Mostrar forma promedio",
                       variable=self.var_show_mean, bg="white",
                       command=self._update_overlay
                       ).pack(side=tk.LEFT, padx=6)

        tk.Button(ctrl, text="Re-sortear", command=self._update_overlay
                  ).pack(side=tk.LEFT, padx=4)
        tk.Button(ctrl, text="Guardar PNG",
                  command=lambda: self._save_current_png("overlay")
                  ).pack(side=tk.RIGHT)

        self.fig_o, self.ax_o = plt.subplots(figsize=(9, 5))
        self.fig_o.patch.set_facecolor("white")
        self.canvas_o = FigureCanvasTkAgg(self.fig_o, master=self.tab_overlay)
        NavigationToolbar2Tk(self.canvas_o, self.tab_overlay)
        self.canvas_o.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

    # ---------- Stats ----------
    def _build_tab_stats(self):
        self.txt_stats = tk.Text(self.tab_stats, font=("Consolas", 11),
                                 padx=12, pady=12, bg="#fafafa",
                                 relief=tk.FLAT, wrap=tk.NONE)
        self.txt_stats.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.txt_stats.insert("1.0", "Abre un archivo .h5 para ver estadísticas.\n")
        self.txt_stats.config(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Teclado
    # ------------------------------------------------------------------
    def _bind_keys(self):
        self.root.bind("<Left>",  lambda e: self._goto(self.indice - 1))
        self.root.bind("<Right>", lambda e: self._goto(self.indice + 1))
        self.root.bind("<Home>",  lambda e: self._goto(0))
        self.root.bind("<End>",   lambda e: self._goto(len(self.nombres) - 1))
        self.root.bind("<Prior>", lambda e: self._goto(self.indice - 50))   # PgUp
        self.root.bind("<Next>",  lambda e: self._goto(self.indice + 50))   # PgDn

    # ------------------------------------------------------------------
    # Carga y cache
    # ------------------------------------------------------------------
    def abrir_archivo(self):
        ruta = filedialog.askopenfilename(filetypes=[("Archivos HDF5", "*.h5")])
        if not ruta:
            return
        try:
            if self.archivo_h5:
                self.archivo_h5.close()
            self.archivo_h5 = h5py.File(ruta, "r")
            self.ruta_actual = ruta

            if "pulsos_crudos" in self.archivo_h5:
                self.grupo = self.archivo_h5["pulsos_crudos"]
            elif "pulsos" in self.archivo_h5:
                self.grupo = self.archivo_h5["pulsos"]
            else:
                raise ValueError("El archivo no tiene grupo 'pulsos_crudos' ni 'pulsos'.")

            self.nombres = sorted(list(self.grupo.keys()),
                                  key=lambda x: int(x.split("_")[1]))
            self.indice = 0
            self._recompute_cache()
        except Exception as e:
            messagebox.showerror("Error al abrir", str(e))

    def abrir_archivo_B(self):
        ruta = filedialog.askopenfilename(filetypes=[("Archivos HDF5", "*.h5")],
                                          title="Segundo archivo (comparación)")
        if not ruta:
            return
        try:
            with h5py.File(ruta, "r") as f:
                if "pulsos_crudos" in f:
                    g = f["pulsos_crudos"]
                elif "pulsos" in f:
                    g = f["pulsos"]
                else:
                    raise ValueError("Archivo B sin grupo conocido.")
                nombres = sorted(list(g.keys()), key=lambda x: int(x.split("_")[1]))
                if not nombres:
                    raise ValueError("Archivo B vacío.")
                n_pts = len(g[nombres[0]][:])
                dt = dt_ns_for(n_pts, self.tb_ns_div.get())
                mpc = mv_per_count(self.v_div.get())

                amps = np.zeros(len(nombres), dtype=np.float32)
                ints = np.zeros(len(nombres), dtype=np.float32)
                for i, n in enumerate(nombres):
                    p = g[n][:]
                    mv = (p.astype(np.float32) - 128.0) * mpc
                    amps[i] = float(np.ptp(mv))
                    ints[i] = integral_mv_ns(mv, dt)
                self.amp_cache_mv_B   = amps
                self.int_cache_mvns_B = ints
                self.absint_cache_B   = np.abs(ints)
                self.nombre_archivo_B = os.path.basename(ruta)
            self._update_phs()
            self._update_stats()
            messagebox.showinfo("Archivo B cargado",
                                f"{self.nombre_archivo_B}: {len(nombres)} pulsos")
        except Exception as e:
            messagebox.showerror("Error al abrir archivo B", str(e))

    def _recompute_cache(self):
        if not self.nombres:
            return
        mpc = mv_per_count(self.v_div.get())
        n_pts = len(self.grupo[self.nombres[0]][:])
        self._npts = n_pts
        dt = dt_ns_for(n_pts, self.tb_ns_div.get())

        n = len(self.nombres)
        amps  = np.zeros(n, dtype=np.float32)
        ints  = np.zeros(n, dtype=np.float32)
        bases = np.zeros(n, dtype=np.float32)
        for i, name in enumerate(self.nombres):
            p_raw = self.grupo[name][:]
            mv = (p_raw.astype(np.float32) - 128.0) * mpc
            amps[i]  = float(np.ptp(mv))
            ints[i]  = integral_mv_ns(mv, dt)
            bases[i] = float(np.median(mv))

        self._amp_cache_mv   = amps
        self._int_cache_mvns = ints
        self._absint_cache   = np.abs(ints)
        self._baseline_cache = bases

        # También recalcular B si está cargado (porque cambiaron escalas)
        if self.amp_cache_mv_B is not None and self.nombre_archivo_B:
            # No re-leemos B, solo avisamos; para re-cachear B el usuario
            # debería abrirlo otra vez.
            pass

        self._refresh_all_views()
        self._refresh_controls()

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------
    def _refresh_controls(self):
        state = tk.NORMAL if self.nombres else tk.DISABLED
        for b in (self.btn_first, self.btn_prev, self.btn_next,
                  self.btn_last, self.btn_big):
            b.config(state=state)
        if self.nombres:
            dt = dt_ns_for(self._npts, self.tb_ns_div.get())
            self.lbl_estado.config(
                text=(f"{os.path.basename(self.ruta_actual)} · "
                      f"{len(self.nombres)} pulsos · {self._npts} pts/pulso · "
                      f"dt = {dt:.3f} ns"))
        else:
            self.lbl_estado.config(text="Ningún archivo cargado")

    def _refresh_all_views(self):
        self._draw_pulso()
        self._update_phs()
        self._update_diff()
        self._update_overlay()
        self._update_stats()

    # ------------------------------------------------------------------
    # Métrica activa
    # ------------------------------------------------------------------
    def _metric_info(self):
        m = self.metric_var.get()
        if m == "integral":
            return ("integral",     self._int_cache_mvns, self.int_cache_mvns_B,
                    "Integral [mV·ns]", "Integral de pulsos")
        if m == "abs_integral":
            return ("abs_integral", self._absint_cache,   self.absint_cache_B,
                    "|Integral| [mV·ns]", "Integral absoluta")
        # amplitude
        return ("amplitude", self._amp_cache_mv, self.amp_cache_mv_B,
                "Amplitud pk-pk [mV]", "Pulse Height Spectrum (pk-pk)")

    # ------------------------------------------------------------------
    # Pulso individual
    # ------------------------------------------------------------------
    def _goto(self, i):
        if not self.nombres:
            return
        self.indice = max(0, min(i, len(self.nombres) - 1))
        self._draw_pulso()

    def _goto_from_entry(self, _evt):
        try:
            i = int(self.entry_goto.get()) - 1
        except ValueError:
            return
        self._goto(i)

    def _next_big(self):
        """Salta al siguiente pulso > p95 según la métrica activa."""
        _, arr, _, _, _ = self._metric_info()
        if arr is None:
            return
        abs_arr = np.abs(arr)
        thresh = float(np.percentile(abs_arr, 95))
        for i in range(self.indice + 1, len(self.nombres)):
            if abs_arr[i] >= thresh:
                self._goto(i); return
        for i in range(len(self.nombres)):
            if abs_arr[i] >= thresh:
                self._goto(i); return

    def _draw_pulso(self):
        if not self.nombres:
            return
        name = self.nombres[self.indice]
        raw = self.grupo[name][:]
        mv = counts_to_mv(raw, v_div=self.v_div.get())
        n_pts = len(mv)
        dt = dt_ns_for(n_pts, self.tb_ns_div.get())
        t_ns = np.arange(n_pts) * dt

        base = float(np.median(mv))
        amp_pp = float(np.ptp(mv))
        integ = integral_mv_ns(mv, dt)

        self.ax_p.clear()
        self.ax_p.fill_between(t_ns, mv, base,
                               where=(mv < base), color="#2d6cdf", alpha=0.15)
        self.ax_p.fill_between(t_ns, mv, base,
                               where=(mv >= base), color="#d94141", alpha=0.15)
        self.ax_p.plot(t_ns, mv, color="#1e50aa", drawstyle="steps-mid",
                       linewidth=1.1)
        self.ax_p.axhline(base, color="#666", linestyle="--", linewidth=0.9,
                          label=f"baseline = {base:.1f} mV")

        title = (f"{name}   (#{self.indice + 1} de {len(self.nombres)})"
                 f"   ·   pk-pk = {amp_pp:.1f} mV   ·   ∫ = {integ:+.1f} mV·ns")
        self.ax_p.set_title(title, fontsize=11)
        self.ax_p.set_xlabel("Tiempo [ns]")
        self.ax_p.set_ylabel("Amplitud [mV]")
        self.ax_p.grid(True, alpha=0.3)
        self.ax_p.legend(loc="upper right", fontsize=9)
        self.fig_p.tight_layout()
        self.canvas_p.draw()

    # ------------------------------------------------------------------
    # PHS
    # ------------------------------------------------------------------
    def _update_phs(self):
        _, arr_A, arr_B, xlabel, title = self._metric_info()
        if arr_A is None:
            return

        try:
            nbins = int(self.sp_bins.get())
        except Exception:
            nbins = 80

        # Rango: el user puede vaciar los campos para "auto"
        xmin_txt = self.entry_xmin.get().strip()
        xmax_txt = self.entry_xmax.get().strip()
        if xmin_txt:
            try: xmin = float(xmin_txt)
            except ValueError: xmin = float(arr_A.min())
        else:
            xmin = float(arr_A.min()) if arr_B is None else float(min(arr_A.min(), arr_B.min()))
        if xmax_txt:
            try: xmax = float(xmax_txt)
            except ValueError: xmax = float(arr_A.max())
        else:
            xmax = float(arr_A.max()) if arr_B is None else float(max(arr_A.max(), arr_B.max()))

        if xmax <= xmin:
            xmax = xmin + 1.0

        bins = np.linspace(xmin, xmax, nbins + 1)

        self.ax_h.clear()

        w_A = np.ones_like(arr_A) / len(arr_A) if self.var_normalize.get() else None
        self.ax_h.hist(arr_A, bins=bins, weights=w_A, alpha=0.6,
                       color="#2d6cdf", edgecolor="#1e50aa", linewidth=0.4,
                       label=os.path.basename(self.ruta_actual or "A"))

        if arr_B is not None:
            w_B = np.ones_like(arr_B) / len(arr_B) if self.var_normalize.get() else None
            self.ax_h.hist(arr_B, bins=bins, weights=w_B, alpha=0.55,
                           color="#d94141", edgecolor="#8a2828", linewidth=0.4,
                           label=self.nombre_archivo_B)

        if self.var_logy.get():
            self.ax_h.set_yscale("log")
        self.ax_h.set_xlabel(xlabel)
        self.ax_h.set_ylabel("Fracción" if self.var_normalize.get() else "Cuentas")
        self.ax_h.set_title(title)
        self.ax_h.grid(True, alpha=0.3)
        self.ax_h.legend(fontsize=9)
        self.fig_h.tight_layout()
        self.canvas_h.draw()

    # ------------------------------------------------------------------
    # Diferencia A − B
    # ------------------------------------------------------------------
    def _update_diff(self):
        """Histograma de la resta A − (escala * B) usando binning común.

        Si no hay archivo B cargado muestra un mensaje invitando a cargarlo.
        Las barras de error son Poisson: sigma_i = sqrt(N_A_i + f^2 * N_B_i)
        """
        ax_top, ax_bot = self.ax_d
        ax_top.clear(); ax_bot.clear()

        _, arr_A, arr_B, xlabel, _ = self._metric_info()

        if arr_A is None:
            ax_top.text(0.5, 0.5, "Carga un archivo en 'Abrir .h5'",
                        ha="center", va="center", transform=ax_top.transAxes,
                        color="#999", fontsize=12)
            ax_top.set_xticks([]); ax_top.set_yticks([])
            ax_bot.set_xticks([]); ax_bot.set_yticks([])
            self.canvas_d.draw()
            return
        if arr_B is None:
            ax_top.text(0.5, 0.5,
                        "Carga un 2º archivo con '+ Archivo B (comparar)'\n"
                        "para ver A − B",
                        ha="center", va="center", transform=ax_top.transAxes,
                        color="#999", fontsize=12)
            ax_top.set_xticks([]); ax_top.set_yticks([])
            ax_bot.set_xticks([]); ax_bot.set_yticks([])
            self.canvas_d.draw()
            return

        # Parámetros
        try:
            nbins = int(self.sp_bins_d.get())
        except Exception:
            nbins = 80
        try:
            scale_B = float(self.entry_scale_b.get())
        except ValueError:
            scale_B = 1.0

        xmin_txt = self.entry_xmin_d.get().strip()
        xmax_txt = self.entry_xmax_d.get().strip()
        if xmin_txt:
            try: xmin = float(xmin_txt)
            except ValueError: xmin = float(min(arr_A.min(), arr_B.min()))
        else:
            xmin = float(min(arr_A.min(), arr_B.min()))
        if xmax_txt:
            try: xmax = float(xmax_txt)
            except ValueError: xmax = float(max(arr_A.max(), arr_B.max()))
        else:
            xmax = float(max(arr_A.max(), arr_B.max()))
        if xmax <= xmin:
            xmax = xmin + 1.0

        bins = np.linspace(xmin, xmax, nbins + 1)
        centers = 0.5 * (bins[:-1] + bins[1:])
        width = bins[1] - bins[0]

        N_A, _ = np.histogram(arr_A, bins=bins)
        N_B, _ = np.histogram(arr_B, bins=bins)

        # Conteos esperados y resta
        N_B_scaled = scale_B * N_B
        diff = N_A.astype(np.float64) - N_B_scaled
        # Poisson: sigma^2 = N_A + scale^2 * N_B
        sigma = np.sqrt(N_A.astype(np.float64) + (scale_B ** 2) * N_B.astype(np.float64))

        # --- Panel superior: A, B superpuestos + resta ---
        if self.var_show_both.get():
            ax_top.step(centers, N_A, where="mid", color="#2d6cdf",
                        linewidth=1.2, alpha=0.8,
                        label=f"A  ({len(arr_A)} pulsos)")
            ax_top.step(centers, N_B_scaled, where="mid", color="#d94141",
                        linewidth=1.2, alpha=0.8,
                        label=(f"B × {scale_B:g}  ({len(arr_B)} pulsos)"
                               if scale_B != 1 else f"B  ({len(arr_B)} pulsos)"))
            ax_top.fill_between(centers, 0, N_A, step="mid",
                                color="#2d6cdf", alpha=0.10)
            ax_top.fill_between(centers, 0, N_B_scaled, step="mid",
                                color="#d94141", alpha=0.10)

        ax_top.set_ylabel("Cuentas")
        ax_top.set_title("Espectros A y B (fondo) y diferencia")
        ax_top.grid(True, alpha=0.3)
        ax_top.legend(fontsize=9, loc="upper right")

        # --- Panel inferior: A − B con barras de error ---
        # Coloreamos positivo (exceso) vs negativo (sobre-resta) distinto
        colors = np.where(diff >= 0, "#1e8f4e", "#9b3030")
        ax_bot.bar(centers, diff, width=width * 0.95, color=colors, alpha=0.8,
                   edgecolor="black", linewidth=0.3,
                   label="A − B (señal neta)")
        if self.var_errbars.get():
            ax_bot.errorbar(centers, diff, yerr=sigma, fmt="none",
                            ecolor="black", elinewidth=0.7, capsize=1.5, alpha=0.6)
        ax_bot.axhline(0, color="#444", linewidth=0.8)
        ax_bot.set_xlabel(xlabel)
        ax_bot.set_ylabel("A − B")
        ax_bot.grid(True, alpha=0.3)

        # Resumen arriba a la izquierda del panel inferior
        total_excess = diff.sum()
        total_err = np.sqrt((sigma ** 2).sum())
        sn = total_excess / total_err if total_err > 0 else float("nan")
        ax_bot.text(0.01, 0.96,
                    f"Exceso total: {total_excess:+.0f} ± {total_err:.0f}   "
                    f"(S/N ≈ {sn:.1f})",
                    transform=ax_bot.transAxes, ha="left", va="top",
                    fontsize=9,
                    bbox=dict(boxstyle="round,pad=0.3",
                              facecolor="white", edgecolor="#ccc", alpha=0.9))

        self.fig_d.tight_layout()
        self.canvas_d.draw()

        # Guardar último binning para posible export
        self._last_diff = dict(centers=centers, bins=bins, width=width,
                               N_A=N_A, N_B=N_B, scale_B=scale_B,
                               diff=diff, sigma=sigma, xlabel=xlabel)

    def _export_diff_csv(self):
        """Exporta a CSV el espectro diferencia (bin, A, B, A-B, sigma)."""
        if not hasattr(self, "_last_diff") or self._last_diff is None:
            messagebox.showwarning("Sin datos", "Calcula primero la diferencia.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        d = self._last_diff
        header = (f"# bin_center [{d['xlabel']}], N_A, N_B, "
                  f"scale_B={d['scale_B']:g}, A-scale*B, sigma_poisson\n")
        with open(path, "w") as f:
            f.write(header)
            for c, na, nb, di, si in zip(d["centers"], d["N_A"], d["N_B"],
                                         d["diff"], d["sigma"]):
                f.write(f"{c:.6g},{int(na)},{int(nb)},{di:.6g},{si:.6g}\n")
        messagebox.showinfo("Exportado", f"CSV guardado en\n{path}")

    # ------------------------------------------------------------------
    # Overlay
    # ------------------------------------------------------------------
    def _update_overlay(self):
        if not self.nombres:
            return
        try:
            n = int(self.sp_nover.get())
        except Exception:
            n = 50
        n = min(n, len(self.nombres))
        idx = random.sample(range(len(self.nombres)), n)

        dt = dt_ns_for(self._npts, self.tb_ns_div.get())
        t_ns = np.arange(self._npts) * dt

        self.ax_o.clear()
        shape_sum = None
        for i in idx:
            p = counts_to_mv(self.grupo[self.nombres[i]][:], v_div=self.v_div.get())
            if len(p) != self._npts:
                continue
            self.ax_o.plot(t_ns, p, color="#2d6cdf", alpha=0.12, linewidth=0.9)
            if shape_sum is None:
                shape_sum = p.astype(np.float64)
            else:
                shape_sum += p

        if self.var_show_mean.get() and shape_sum is not None:
            mean_shape = shape_sum / n
            self.ax_o.plot(t_ns, mean_shape, color="#d94141", linewidth=1.8,
                           label=f"promedio (N={n})")
            self.ax_o.legend(fontsize=9)

        self.ax_o.set_xlabel("Tiempo [ns]")
        self.ax_o.set_ylabel("Amplitud [mV]")
        self.ax_o.set_title(f"Overlay de {n} pulsos aleatorios")
        self.ax_o.grid(True, alpha=0.3)
        self.fig_o.tight_layout()
        self.canvas_o.draw()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    def _update_stats(self):
        self.txt_stats.config(state=tk.NORMAL)
        self.txt_stats.delete("1.0", tk.END)
        if self._amp_cache_mv is None:
            self.txt_stats.insert("1.0", "Sin datos.\n")
            self.txt_stats.config(state=tk.DISABLED)
            return

        amps  = self._amp_cache_mv
        ints  = self._int_cache_mvns
        absin = self._absint_cache
        bases = self._baseline_cache
        dt    = dt_ns_for(self._npts, self.tb_ns_div.get())

        lines = []
        lines.append(f"Archivo        : {self.ruta_actual}")
        lines.append(f"Pulsos         : {len(self.nombres)}")
        lines.append(f"Pts/pulso      : {self._npts}   (dt = {dt:.3f} ns)")
        lines.append(f"V/div          : {self.v_div.get():.3f}     "
                     f"mV/cuenta = {mv_per_count(self.v_div.get()):.3f}")
        lines.append(f"Timebase       : {self.tb_ns_div.get():.2f} ns/div")
        lines.append("")
        lines.append("Amplitud pk-pk [mV]")
        lines.append(f"  media        : {amps.mean():10.2f}")
        lines.append(f"  mediana      : {np.median(amps):10.2f}")
        lines.append(f"  std          : {amps.std():10.2f}")
        lines.append(f"  mín / máx    : {amps.min():10.2f} / {amps.max():.2f}")
        lines.append(f"  p95 / p99    : {np.percentile(amps, 95):10.2f} / "
                     f"{np.percentile(amps, 99):.2f}")
        lines.append("")
        lines.append("Integral (con signo) [mV·ns]")
        lines.append(f"  media        : {ints.mean():+10.2f}")
        lines.append(f"  mediana      : {np.median(ints):+10.2f}")
        lines.append(f"  std          : {ints.std():10.2f}")
        lines.append(f"  mín / máx    : {ints.min():+10.2f} / {ints.max():+.2f}")
        lines.append("")
        lines.append("|Integral| [mV·ns]")
        lines.append(f"  media        : {absin.mean():10.2f}")
        lines.append(f"  mediana      : {np.median(absin):10.2f}")
        lines.append(f"  p95 / p99    : {np.percentile(absin, 95):10.2f} / "
                     f"{np.percentile(absin, 99):.2f}")
        lines.append("")
        lines.append(f"Baseline [mV]  media = {bases.mean():+.2f}   "
                     f"std = {bases.std():.2f}")

        if self.absint_cache_B is not None:
            B_abs = self.absint_cache_B
            B_amp = self.amp_cache_mv_B
            lines.append("")
            lines.append(f"--- Archivo B: {self.nombre_archivo_B} ---")
            lines.append(f"  Pulsos       : {len(B_abs)}")
            lines.append(f"  |Int| media  : {B_abs.mean():10.2f} mV·ns")
            lines.append(f"  |Int| mediana: {np.median(B_abs):10.2f} mV·ns")
            lines.append(f"  Amp media    : {B_amp.mean():10.2f} mV")
            lines.append("")
            lines.append("Diferencia (A - B)")
            lines.append(f"  #pulsos      : {len(amps) - len(B_amp):+d}")
            lines.append(f"  |Int| media  : {absin.mean() - B_abs.mean():+.2f} mV·ns")
            lines.append(f"  Amp media    : {amps.mean() - B_amp.mean():+.2f} mV")

        self.txt_stats.insert("1.0", "\n".join(lines))
        self.txt_stats.config(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Guardar PNG
    # ------------------------------------------------------------------
    def _save_current_png(self, which):
        fig_map = {
            "pulso":   self.fig_p,
            "phs":     self.fig_h,
            "diff":    self.fig_d,
            "overlay": self.fig_o,
        }
        fig = fig_map.get(which)
        if fig is None:
            return
        path = filedialog.asksaveasfilename(defaultextension=".png",
                                            filetypes=[("PNG", "*.png")])
        if path:
            fig.savefig(path, dpi=140, bbox_inches="tight",
                        facecolor="white")
            messagebox.showinfo("Guardado", f"Figura guardada en\n{path}")


if __name__ == "__main__":
    root = tk.Tk()
    app = VisorPulsosV2(root)
    root.mainloop()
