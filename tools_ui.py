"""
Janelas das ferramentas de medição do NanoVNA Mirror.

Cada ferramenta é uma janela com controles à esquerda e gráfico à direita.
As medições usam NanoVNA.acquire() (varreduras de 101 pontos emendadas).
"""
import csv
import json
import math
import os
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.patches import Circle

import rf_tools as rt
import cabos

BLUE, GREY, ORANGE, RED, GREEN = "#2a78d6", "#8a8a8a", "#eb6834", "#e34948", "#008300"
HIST_DIR = os.path.join(os.path.expanduser("~"), "NanoVNA_Mirror", "historico")

matplotlib.rcParams.update({
    "font.size": 9, "axes.titlesize": 10, "axes.titleweight": "bold", "axes.grid": True,
    "grid.color": "#e6e6e6", "axes.edgecolor": "#b0b0b0", "axes.spines.top": False,
    "axes.spines.right": False, "legend.frameon": False,
})


def fnum(s):
    return float(str(s).strip().replace(",", ".").replace("−", "-"))


def fmt(v, nd=2):
    return f"{v:.{nd}f}".replace(".", ",")


def fmt_mhz(hz, nd=4):
    return f"{hz / 1e6:.{nd}f}".replace(".", ",") + " MHz"


# ===========================================================================
# Base
# ===========================================================================
class ToolWindow(tk.Toplevel):
    needs_vna = True

    def __init__(self, app, title, geometry="1150x720", nplots=1, panel_width=330):
        super().__init__(app)
        self.app = app
        self.title(f"{title} — {app.profile.APP_NAME}")
        self.geometry(geometry)
        self.minsize(900, 560)
        self.busy = False
        self.data = None

        self.panel = ttk.Frame(self, padding=8, width=panel_width)
        self.panel.pack(side="left", fill="y")
        self.panel.pack_propagate(False)
        right = ttk.Frame(self)
        right.pack(side="left", fill="both", expand=True)

        self.fig = Figure(figsize=(7, 5), dpi=100)
        self.fig.subplots_adjust(left=0.09, right=0.97, top=0.93, bottom=0.09, hspace=0.38)
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        tb = NavigationToolbar2Tk(self.canvas, right, pack_toolbar=False)
        tb.update()
        tb.pack(fill="x")

        bottom = ttk.Frame(self.panel)
        bottom.pack(side="bottom", fill="x")
        self.prog = ttk.Progressbar(bottom, mode="determinate")
        self.prog.pack(fill="x", pady=(4, 2))
        self.status = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.status, foreground="gray", wraplength=panel_width - 20).pack(fill="x")

    # ---------- helpers de interface ----------
    def section(self, text):
        f = ttk.LabelFrame(self.panel, text=text, padding=6)
        f.pack(fill="x", pady=(0, 6))
        f.columnconfigure(1, weight=1)
        return f

    def entry(self, parent, label, default, row, width=12, unit=""):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=1)
        e = ttk.Entry(parent, width=width)
        e.insert(0, str(default))
        e.grid(row=row, column=1, sticky="ew", padx=4, pady=1)
        if unit:
            ttk.Label(parent, text=unit, foreground="gray").grid(row=row, column=2, sticky="w")
        return e

    def combo(self, parent, label, values, row, default=None, width=22):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=1)
        c = ttk.Combobox(parent, values=values, state="readonly", width=width)
        c.grid(row=row, column=1, columnspan=2, sticky="ew", padx=4, pady=1)
        if values:
            c.set(default if default in values else values[0])
        return c

    def result_box(self, height=10):
        t = tk.Text(self.panel, height=height, font=("Consolas", 9), wrap="word", relief="flat",
                    background="#f6f6f4")
        t.pack(fill="both", expand=True, pady=(0, 6))
        return t

    def show(self, box, text):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("end", text)
        box.configure(state="disabled")

    def err(self, msg):
        messagebox.showwarning("NanoVNA Mirror", msg, parent=self)

    # ---------- medição ----------
    def measure(self, start, stop, n_seg, on_done, restore=True, quiet=False):
        if self.busy:
            return
        if not self.app.vna:
            self.err("Conecte o NanoVNA primeiro.")
            return
        if not (50e3 <= start < stop <= self.app.profile.F_MAX * 1.0001):
            self.err("Faixa de frequência inválida.")
            return
        self.busy = True
        was_running = self.app.running
        if was_running:
            self.app.toggle_mirror()
        self.prog.configure(maximum=1, value=0)
        if not quiet:
            self.status.set(f"Medindo {n_seg} × 101 pontos…")
        t0 = time.time()

        def progress(k, n):
            self.after(0, lambda: self.prog.configure(maximum=n, value=k))

        def worker():
            try:
                res = self.app.vna.acquire(start, stop, 101 * n_seg, progress=progress, restore=restore)
                err = None
            except Exception as e:
                res, err = None, e

            def finish():
                self.busy = False
                if was_running and not self.app.running and not getattr(self, "live", False):
                    self.app.toggle_mirror()
                if err:
                    self.status.set(f"Erro: {err}")
                    return
                if not quiet:
                    self.status.set(f"{len(res[0])} pontos em {time.time() - t0:.1f} s")
                on_done(*res)
            try:
                self.after(0, finish)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def export_csv(self, header, rows, name):
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv",
                                            initialfile=time.strftime(f"{name}_%Y%m%d_%H%M%S.csv"),
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(header)
            for r in rows:
                w.writerow([str(v).replace(".", ",") if isinstance(v, float) else v for v in r])
        self.status.set(f"Exportado: {os.path.basename(path)}")


def cable_picker(win, parent, row):
    """Combobox de cabos + campo de VF preenchido automaticamente."""
    tab = cabos.load()
    names = list(tab)
    cb = win.combo(parent, "Cabo", names, row, default="RG-213 / RGC-213 (PE sólido)")
    vf = win.entry(parent, "Fator de velocidade", "", row + 1)

    def fill(*_):
        t = cabos.load()
        if cb.get() in t:
            vf.delete(0, "end")
            vf.insert(0, f"{t[cb.get()][0]:.3f}")
    cb.bind("<<ComboboxSelected>>", fill)
    fill()
    return cb, vf


def cable_att(name, f_hz):
    t = cabos.load()
    if name in t:
        return cabos.attenuation_db_per_m(t[name][1], f_hz)
    return 0.0


# ===========================================================================
# 1. Localizador de falhas (DTF)
# ===========================================================================
class DTFWindow(ToolWindow):
    def __init__(self, app):
        super().__init__(app, "Localizador de falhas no cabo (DTF)")
        s = self.section("Cabo")
        self.cab, self.vf = cable_picker(self, s, 0)
        self.cab.bind("<<ComboboxSelected>>", lambda e: self.after(10, self.auto), add="+")
        self.comp = tk.BooleanVar(value=True)
        ttk.Checkbutton(s, text="Compensar a perda do cabo", variable=self.comp).grid(row=2, column=0, columnspan=3, sticky="w")

        s = self.section("Alcance e faixa")
        self.dmax = self.entry(s, "Distância máxima", "60", 0, unit="m")
        self.f1 = self.entry(s, "Início", "50", 1, unit="MHz")
        self.f2 = self.entry(s, "Fim", "900", 2, unit="MHz")
        self.nseg = self.entry(s, "Segmentos (×101)", "6", 3)
        ttk.Button(s, text="Calcular faixa automática", command=self.auto).grid(row=4, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.info = tk.StringVar()
        ttk.Label(s, textvariable=self.info, foreground="#145aa0", wraplength=300).grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
        for e in (self.f1, self.f2, self.nseg, self.vf):
            e.bind("<KeyRelease>", lambda ev: self.update_info())

        s = self.section("Análise")
        self.thr = self.entry(s, "Alerta abaixo de RL", "20", 0, unit="dB")
        self.vfcal = self.entry(s, "Comprimento real (calibrar VF)", "", 1, unit="m")
        b = ttk.Frame(self.panel)
        b.pack(fill="x", pady=(0, 6))
        ttk.Button(b, text="▶ Medir", command=self.run).pack(side="left", expand=True, fill="x")
        ttk.Button(b, text="Calibrar VF", command=self.cal_vf).pack(side="left", expand=True, fill="x")
        ttk.Button(b, text="CSV", command=self.csv).pack(side="left", expand=True, fill="x")
        self.res = self.result_box()
        self.ax = self.fig.add_subplot(111)
        self.auto()
        self.show(self.res, "Conecte o cabo no CH0 com a antena (ou carga) na ponta e clique em Medir.\n\n"
                            "Calibre o NanoVNA (open/short/load) na ponta do cabo de teste, na mesma faixa da medição.")
        self.draw_empty()

    def auto(self):
        try:
            pr = self.app.profile
            name = self.cab.get()
            p = rt.dtf_plan(fnum(self.dmax.get()), fnum(self.vf.get()), f_hi=min(pr.F_MAX, 3e9), max_seg=pr.MAX_SEG,
                            att_db_per_m=lambda f: cable_att(name, f))
        except ValueError:
            return
        for e, v in ((self.f1, p["start"] / 1e6), (self.f2, round(p["stop"] / 1e6, 1)), (self.nseg, p["n_seg"])):
            e.delete(0, "end")
            e.insert(0, f"{v:g}")
        self.update_info()
        if "perda_ida_volta_db" in p:
            self.status.set(f"Faixa limitada pela perda do cabo: {fmt(p['perda_ida_volta_db'], 0)} dB de ida e volta "
                            f"até {self.dmax.get()} m em {fmt_mhz(p['stop'], 0)}")

    def update_info(self):
        try:
            i = rt.dtf_info(fnum(self.f1.get()) * 1e6, fnum(self.f2.get()) * 1e6, int(fnum(self.nseg.get())), fnum(self.vf.get()))
            self.info.set(f"Resolução {fmt(i['resolucao_m'], 2)} m · alcance {fmt(i['alcance_m'], 0)} m · {i['pontos']} pontos")
        except (ValueError, ZeroDivisionError):
            self.info.set("")

    def draw_empty(self):
        self.ax.clear()
        self.ax.set_title("Reflexão ao longo do cabo")
        self.ax.set_xlabel("Distância (m)")
        self.ax.set_ylabel("Reflexão (dB)")
        self.ax.set_ylim(-60, 0)
        self.canvas.draw_idle()

    def run(self):
        try:
            a, b, n = fnum(self.f1.get()) * 1e6, fnum(self.f2.get()) * 1e6, int(fnum(self.nseg.get()))
            fnum(self.vf.get())
        except ValueError:
            self.err("Valores inválidos.")
            return
        self.measure(a, b, n, self.done)

    def done(self, f, s11, s21):
        self.data = (f, s11)
        self.analyze()

    def analyze(self):
        if self.data is None:
            return
        f, s11 = self.data
        vf = fnum(self.vf.get())
        fc = (f[0] + f[-1]) / 2
        att = cable_att(self.cab.get(), fc) if self.comp.get() else 0.0
        d, mag = rt.dtf(f, s11, vf, loss_db_per_m=att)
        dmax = fnum(self.dmax.get())
        sel = d <= dmax * 1.05
        d, mag = d[sel], mag[sel]
        refl = 20 * np.log10(np.maximum(mag, 1e-6))
        thr = fnum(self.thr.get())
        res_m = rt.dtf_info(f[0], f[-1], len(f) // 101, vf)["resolucao_m"]
        # ignora o primeiro trecho (conector do NanoVNA) para achar falhas
        pk = [i for i in rt.find_peaks(d, refl, -45, max(3 * res_m, 0.3)) if d[i] > 2 * res_m]
        self.peaks = [(rt.refine_peak(d, refl, i), -refl[i]) for i in pk]
        self.dtf = (d, refl)
        self.ax.clear()
        self.ax.plot(d, refl, color=BLUE, lw=1.6)
        self.ax.axhline(-thr, color=RED, lw=1, ls="--")
        self.ax.text(d[-1], -thr + 1, f"alerta: RL < {thr:g} dB", color=RED, ha="right", va="bottom", fontsize=8)
        end = max(self.peaks, key=lambda p: -p[1]) if self.peaks else None
        for dist, rl in self.peaks:
            col = RED if rl < thr else GREY
            self.ax.plot([dist], [-rl], "o", color=col, ms=5)
            self.ax.annotate(f"{fmt(dist, 2)} m\n{fmt(rl, 1)} dB", (dist, -rl), textcoords="offset points",
                             xytext=(0, 8), ha="center", fontsize=8, color="#333")
        self.ax.set_ylim(-60, 2)
        self.ax.set_xlim(0, d[-1])
        self.ax.set_title(f"Reflexão ao longo do cabo · {self.cab.get()} · VF {fmt(vf, 3)}")
        self.ax.set_xlabel("Distância a partir do conector do NanoVNA (m)")
        self.ax.set_ylabel("Reflexão (dB)")
        self.canvas.draw_idle()

        lines = [f"Faixa {fmt_mhz(f[0], 1)} – {fmt_mhz(f[-1], 1)}, {len(f)} pontos",
                 f"Perda compensada: {fmt(att * 100, 1)} dB/100 m" if att else "Perda do cabo não compensada", ""]
        if not self.peaks:
            lines.append("Nenhuma reflexão acima de -45 dB.")
        lines.append(f"{'Distância':>10} {'RL':>7} {'SWR':>6}  ")
        for dist, rl in self.peaks:
            g = min(10 ** (-rl / 20), 0.99)
            sw = (1 + g) / (1 - g)
            tag = "ATENÇÃO" if rl < thr else ""
            if end and dist == end[0]:
                tag = (tag + " provável fim/antena").strip()
            sws = "> 99" if sw >= 99 else fmt(sw, 2)
            lines.append(f"{fmt(dist, 2):>8} m {fmt(rl, 1):>5} dB {sws:>6}  {tag}")
        lines += ["", "A maior reflexão costuma ser a antena (fim da linha).",
                  "Picos antes dela com RL abaixo do limiar indicam conector, emenda ou cabo danificado."]
        self.show(self.res, "\n".join(lines))

    def cal_vf(self):
        if not self.data or not self.peaks:
            self.err("Meça primeiro um cabo de comprimento conhecido com a ponta aberta ou em curto.")
            return
        try:
            real = fnum(self.vfcal.get())
        except ValueError:
            self.err("Informe o comprimento real do cabo em metros.")
            return
        dist = max(self.peaks, key=lambda p: -p[1])[0]
        vf_new = fnum(self.vf.get()) * real / dist
        self.vf.delete(0, "end")
        self.vf.insert(0, f"{vf_new:.3f}")
        self.analyze()
        self.status.set(f"VF ajustado para {vf_new:.3f} (fim do cabo estava em {dist:.2f} m)")

    def csv(self):
        if self.data is None:
            return
        d, refl = self.dtf
        self.export_csv(["distancia_m", "reflexao_db"], zip(np.round(d, 4).tolist(), np.round(refl, 3).tolist()), "dtf")


# ===========================================================================
# 2. Perda, comprimento e stub
# ===========================================================================
class CableWindow(ToolWindow):
    def __init__(self, app):
        super().__init__(app, "Cabo: perda, comprimento e stub")
        nb = ttk.Notebook(self.panel)
        nb.pack(fill="x", pady=(0, 6))
        p1, p2 = ttk.Frame(nb, padding=4), ttk.Frame(nb, padding=4)
        nb.add(p1, text="Perda e comprimento")
        nb.add(p2, text="Stub λ/4 e λ/2")
        nb.bind("<<NotebookTabChanged>>", lambda e: self.mode_changed(nb.index("current")))

        # --- perda/comprimento
        s = ttk.LabelFrame(p1, text="Cabo (ponta aberta ou em curto)", padding=6)
        s.pack(fill="x")
        s.columnconfigure(1, weight=1)
        self.cab, self.vf = cable_picker(self, s, 0)
        self.length = self.entry(s, "Comprimento físico", "", 2, unit="m")
        ttk.Label(s, text="(opcional: com ele, calcula o VF real e dB/100 m)", foreground="gray").grid(row=3, column=0, columnspan=3, sticky="w")
        s = ttk.LabelFrame(p1, text="Faixa", padding=6)
        s.pack(fill="x", pady=(6, 0))
        s.columnconfigure(1, weight=1)
        self.f1 = self.entry(s, "Início", "1", 0, unit="MHz")
        self.f2 = self.entry(s, "Fim", "500", 1, unit="MHz")
        self.nseg = self.entry(s, "Segmentos (×101)", "8", 2)
        ttk.Label(s, text="Mais segmentos = cabos mais longos no cálculo de comprimento.", foreground="gray", wraplength=290).grid(row=3, column=0, columnspan=3, sticky="w")
        ttk.Button(p1, text="▶ Medir cabo", command=self.run_loss).pack(fill="x", pady=(6, 0))

        # --- stub
        s = ttk.LabelFrame(p2, text="Projeto", padding=6)
        s.pack(fill="x")
        s.columnconfigure(1, weight=1)
        self.scab, self.svf = cable_picker(self, s, 0)
        self.sf = self.entry(s, "Frequência alvo", "150", 2, unit="MHz")
        self.stype = self.combo(s, "Tipo", ["λ/4 aberto na ponta", "λ/4 em curto na ponta",
                                            "λ/2 aberto na ponta", "λ/2 em curto na ponta"], 3)
        ttk.Button(s, text="Calcular comprimento", command=self.calc_stub).grid(row=4, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        s = ttk.LabelFrame(p2, text="Ajuste pelo NanoVNA", padding=6)
        s.pack(fill="x", pady=(6, 0))
        s.columnconfigure(1, weight=1)
        self.scur = self.entry(s, "Comprimento atual", "", 0, unit="m")
        ttk.Button(s, text="▶ Medir ressonância", command=self.run_stub).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Label(s, text="Corte o stub um pouco mais longo, meça, e corte o que a ferramenta indicar.", foreground="gray", wraplength=290).grid(row=2, column=0, columnspan=3, sticky="w")

        self.res = self.result_box()
        self.ax = self.fig.add_subplot(111)
        self.show(self.res, "Perda: deixe a ponta do cabo aberta (ou em curto) e meça.\n"
                            "A perda em um sentido é metade da perda de retorno.")

    def mode_changed(self, i):
        self.status.set("")

    def run_loss(self):
        try:
            a, b, n = fnum(self.f1.get()) * 1e6, fnum(self.f2.get()) * 1e6, int(fnum(self.nseg.get()))
        except ValueError:
            self.err("Valores inválidos.")
            return
        self.measure(a, b, n, self.done_loss)

    def done_loss(self, f, s11, s21):
        loss = rt.cable_loss_open_short(s11)
        tau, step = rt.group_delay_fit(f, s11)
        vf = fnum(self.vf.get())
        le = rt.electrical_length(tau)
        try:
            L = fnum(self.length.get())
        except ValueError:
            L = None
        self.ax.clear()
        self.ax.plot(f / 1e6, loss, color=BLUE, lw=1.6, label="medida")
        t = cabos.load()
        if L and self.cab.get() in t:
            nom = [cabos.attenuation_db_per_100m(t[self.cab.get()][1], x / 1e6) * L / 100 for x in f]
            self.ax.plot(f / 1e6, nom, color=GREY, lw=1.3, ls="--", label=f"catálogo ({fmt(L, 1)} m)")
            self.ax.legend(loc="upper left")
        self.ax.set_title("Perda do cabo (um sentido)")
        self.ax.set_xlabel("Frequência (MHz)")
        self.ax.set_ylabel("Perda (dB)")
        self.ax.set_ylim(bottom=0)
        self.canvas.draw_idle()
        lines = [f"Comprimento elétrico: {fmt(le, 2)} m",
                 f"Comprimento físico (VF {fmt(vf, 3)}): {fmt(le * vf, 2)} m"]
        if L:
            lines.append(f"VF medido (com {fmt(L, 2)} m): {fmt(rt.velocity_factor(L, tau), 3)}")
        lines.append("")
        for fx in (150e6, 450e6):
            if f[0] <= fx <= f[-1]:
                lx = float(np.interp(fx, f, loss))
                extra = f" = {fmt(lx / L * 100, 1)} dB/100 m" if L else ""
                lines.append(f"Perda em {fmt_mhz(fx, 0)}: {fmt(lx, 2)} dB{extra}")
        if step > 2.5:
            lines += ["", "AVISO: fase mudou quase 180° entre pontos vizinhos;",
                      "o comprimento pode estar errado. Aumente os segmentos ou reduza a faixa."]
        self.show(self.res, "\n".join(lines))

    def _stub_kind(self):
        t = self.stype.get()
        frac = 0.25 if t.startswith("λ/4") else 0.5
        aberto = "aberto" in t
        # o que a entrada parece na ressonância
        if frac == 0.25:
            seen = "curto" if aberto else "aberto"
        else:
            seen = "aberto" if aberto else "curto"
        return frac, seen

    def calc_stub(self):
        try:
            f, vf = fnum(self.sf.get()) * 1e6, fnum(self.svf.get())
        except ValueError:
            self.err("Valores inválidos.")
            return
        frac, seen = self._stub_kind()
        L = rt.stub_length(f, vf, frac)
        self.show(self.res, f"{self.stype.get()} para {fmt_mhz(f, 3)}\n"
                            f"VF {fmt(vf, 3)}: comprimento {fmt(L * 100, 1)} cm ({fmt(L * 1000, 0)} mm)\n\n"
                            f"Na ressonância a entrada vira {seen}.\n"
                            "Corte ~3% mais longo e ajuste com 'Medir ressonância'\n"
                            "(conectores e a ponta mudam o comprimento efetivo).")

    def run_stub(self):
        try:
            f = fnum(self.sf.get()) * 1e6
        except ValueError:
            self.err("Frequência inválida.")
            return
        self.measure(f * 0.7, f * 1.3, 3, self.done_stub)

    def done_stub(self, f, s11, s21):
        frac, seen = self._stub_kind()
        ft = fnum(self.sf.get()) * 1e6
        fr = rt.stub_resonance(f, s11, seen)
        z = rt.gamma_to_z(s11)
        self.ax.clear()
        self.ax.semilogy(f / 1e6, np.clip(np.abs(z), 0.1, 1e5), color=BLUE, lw=1.6)
        self.ax.axvline(ft / 1e6, color=GREY, ls="--", lw=1)
        self.ax.text(ft / 1e6, self.ax.get_ylim()[1], " alvo", color=GREY, va="top", fontsize=8)
        if fr:
            self.ax.axvline(fr / 1e6, color=ORANGE, lw=1.2)
            self.ax.text(fr / 1e6, self.ax.get_ylim()[0], f" medida {fmt(fr / 1e6, 3)} MHz", color=ORANGE, va="bottom", fontsize=8)
        self.ax.set_title("|Z| na entrada do stub")
        self.ax.set_xlabel("Frequência (MHz)")
        self.ax.set_ylabel("|Z| (Ω)")
        self.canvas.draw_idle()
        if not fr:
            self.show(self.res, "Ressonância não encontrada na faixa ±30%.")
            return
        lines = [f"Ressonância medida: {fmt_mhz(fr, 3)}", f"Alvo: {fmt_mhz(ft, 3)}"]
        try:
            cur = fnum(self.scur.get())
            new, cut = rt.stub_cut(cur, fr, ft)
            if cut >= 0:
                lines += ["", f"Corte {fmt(cut * 1000, 0)} mm", f"(de {fmt(cur * 100, 1)} cm para {fmt(new * 100, 1)} cm)"]
            else:
                lines += ["", f"O stub está CURTO em {fmt(-cut * 1000, 0)} mm.", "Será preciso cortar outro."]
        except ValueError:
            lines += ["", "Informe o comprimento atual para saber quanto cortar."]
        self.show(self.res, "\n".join(lines))


# ===========================================================================
# 3. Filtros e duplexadores
# ===========================================================================
class FilterWindow(ToolWindow):
    def __init__(self, app):
        super().__init__(app, "Analisador de filtros e duplexadores", panel_width=350)
        self.live = False
        s = self.section("Faixa (CH0 → filtro → CH1)")
        self.f1 = self.entry(s, "Início", "145", 0, unit="MHz")
        self.f2 = self.entry(s, "Fim", "175", 1, unit="MHz")
        self.nseg = self.entry(s, "Segmentos (×101)", "2", 2)
        self.xdb = self.entry(s, "Banda a −X dB", "3", 3, unit="dB")

        s = self.section("Duplexador / máscara")
        self.fpass = self.entry(s, "Passa (canal)", "162,5", 0, unit="MHz")
        self.frej = self.entry(s, "Rejeita (canal)", "158,0", 1, unit="MHz")
        self.bw = self.entry(s, "Largura do canal", "25", 2, unit="kHz")
        self.ilmax = self.entry(s, "Perda máxima", "1,5", 3, unit="dB")
        self.rejmin = self.entry(s, "Rejeição mínima", "70", 4, unit="dB")
        self.use_mask = tk.BooleanVar(value=True)
        ttk.Checkbutton(s, text="Avaliar máscara (aprovado/reprovado)", variable=self.use_mask).grid(row=5, column=0, columnspan=3, sticky="w")

        b = ttk.Frame(self.panel)
        b.pack(fill="x", pady=(0, 6))
        ttk.Button(b, text="▶ Medir", command=self.run).pack(side="left", expand=True, fill="x")
        self.live_btn = ttk.Button(b, text="⟳ Ao vivo (sintonia)", command=self.toggle_live)
        self.live_btn.pack(side="left", expand=True, fill="x")
        ttk.Button(b, text="CSV", command=self.csv).pack(side="left", expand=True, fill="x")
        b2 = ttk.Frame(self.panel)
        b2.pack(fill="x", pady=(0, 6))
        ttk.Button(b2, text="Zoom: canal de passagem", command=lambda: self.zoom(self.fpass)).pack(side="left", expand=True, fill="x")
        ttk.Button(b2, text="Zoom: canal rejeitado", command=lambda: self.zoom(self.frej)).pack(side="left", expand=True, fill="x")
        self.res = self.result_box()
        self.ax = self.fig.add_subplot(111)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.show(self.res, "Ligue a entrada do filtro no CH0 e a saída no CH1.\n"
                            "Duplexador: meça um ramo por vez (TX→ANT, depois ANT→RX).\n\n"
                            f"O {app.profile.NAME} mede cerca de 70 dB de rejeição (menos nas frequências altas);\n"
                            "notches mais profundos aparecem limitados pelo piso de ruído.")

    def params(self):
        return (fnum(self.f1.get()) * 1e6, fnum(self.f2.get()) * 1e6, int(fnum(self.nseg.get())))

    def run(self):
        try:
            a, b, n = self.params()
        except ValueError:
            self.err("Valores inválidos.")
            return
        self.measure(a, b, n, self.done)

    def toggle_live(self):
        if self.live:
            self.live = False
            self.live_btn.configure(text="⟳ Ao vivo (sintonia)")
            return
        if not self.app.vna:
            self.err("Conecte o NanoVNA primeiro.")
            return
        try:
            self.params()
        except ValueError:
            self.err("Valores inválidos.")
            return
        self.live = True
        self.live_btn.configure(text="■ Parar ao vivo")
        self.orig = self.app.vna.command("sweep").split()
        self.loop()

    def loop(self):
        if not self.live:
            try:
                self.app.vna.restore_sweep(self.orig)
            except Exception:
                pass
            if not self.app.running:
                self.app.toggle_mirror()
            return
        a, b, n = self.params()

        def done(f, s11, s21):
            self.done(f, s11, s21)
            self.after(100, self.loop)
        self.measure(a, b, min(n, 2), done, restore=False, quiet=True)
        self.status.set("Ao vivo: ajuste o duplexador e acompanhe")

    def close(self):
        self.live = False
        if getattr(self, "orig", None) and self.app.vna:
            try:
                self.app.vna.restore_sweep(self.orig)
            except Exception:
                pass
        self.destroy()

    def zoom(self, entry):
        try:
            fc = fnum(entry.get())
        except ValueError:
            return
        half = max(0.5, fnum(self.bw.get()) / 1e3 * 20)
        setv_ = lambda e, v: (e.delete(0, "end"), e.insert(0, f"{v:g}".replace(".", ",")))
        setv_(self.f1, round(fc - half, 4))
        setv_(self.f2, round(fc + half, 4))
        setv_(self.nseg, 2)
        if not self.live:
            self.run()

    def mask(self):
        return rt.duplexer_mask(fnum(self.fpass.get()) * 1e6, fnum(self.frej.get()) * 1e6,
                                fnum(self.bw.get()) * 1e3, fnum(self.ilmax.get()), fnum(self.rejmin.get()))

    def done(self, f, s11, s21):
        self.data = (f, s11, s21)
        xdb = fnum(self.xdb.get())
        try:
            mask = self.mask() if self.use_mask.get() else []
        except ValueError:
            mask = []
        pb = (mask[0]["f1"], mask[0]["f2"]) if mask else None
        rej = [(m_["f1"] + m_["f2"]) / 2 for m_ in mask[1:]]
        nw = (rej[0] * 0.99, rej[0] * 1.01) if rej else None
        m = rt.filter_metrics(f, s21, s11, x_db=xdb, passband=pb, reject_freqs=rej, notch_window=nw)
        y = rt.db(s21)
        pass_in = not mask or (f[0] <= mask[0]["f1"] and mask[0]["f2"] <= f[-1])
        self.ax.clear()
        self.ax.plot(f / 1e6, rt.db(s11), color=GREY, lw=1.1, label="S11 (retorno)")
        self.ax.plot(f / 1e6, y, color=BLUE, lw=1.8, label="S21 (transmissão)")
        ev = rt.eval_mask(f, s21, mask) if mask else []
        span = (f[-1] - f[0]) / 1e6
        for it, ok, worst in ev:
            if ok is None:
                continue
            col = GREEN if ok else RED
            yl = -it["limite_db"]
            fc, half = (it["f1"] + it["f2"]) / 2e6, max((it["f2"] - it["f1"]) / 2e6, span * 0.004)
            self.ax.axvspan(fc - half, fc + half, color=col, alpha=0.15, lw=0)
            self.ax.hlines(yl, fc - 3 * half, fc + 3 * half, color=col, lw=2)
            lab = "passa" if it["tipo"] == "max_perda" else "rejeita"
            self.ax.text(fc, 1.5, lab, color=col, ha="center", va="bottom", fontsize=8)
        if m["f_lo"] and m["f_hi"]:
            lvl = -m["perda_min_db"] - xdb
            self.ax.hlines(lvl, m["f_lo"] / 1e6, m["f_hi"] / 1e6, color=ORANGE, lw=1.2)
            self.ax.text(m["f_hi"] / 1e6, lvl, f"  −{fmt(xdb, 0)} dB: {fmt(m['banda'] / 1e3, 1)} kHz", color=ORANGE, va="center", fontsize=8)
        self.ax.plot([m["f_notch"] / 1e6], [m["notch_db"]], "v", color=RED, ms=6)
        if m["q_carregado"] and pass_in:
            self.ax.annotate(f"QL {fmt(m['q_carregado'], 0)}", (m["f_pico"] / 1e6, -m["perda_min_db"]),
                             xytext=(0, 8), textcoords="offset points", ha="center", color=BLUE, fontsize=8)
        if m["q_notch"]:
            self.ax.annotate(f"Q notch {fmt(m['q_notch'], 0)}", (m["f_notch"] / 1e6, m["notch_db"]),
                             xytext=(0, -14), textcoords="offset points", ha="center", color=RED, fontsize=8)
        self.ax.set_ylim(max(-100, min(y.min(), -40) - 5), 3)
        self.ax.set_xlim(f[0] / 1e6, f[-1] / 1e6)
        self.ax.set_title("Resposta do filtro")
        self.ax.set_xlabel("Frequência (MHz)")
        self.ax.set_ylabel("dB")
        self.ax.legend(loc="upper right")
        self.canvas.draw_idle()

        L = [f"Pico: {fmt_mhz(m['f_pico'])}  perda {fmt(m['perda_min_db'], 2)} dB"] if pass_in else ["(canal de passagem fora desta faixa)"]
        if m["banda"] and pass_in:
            L.append(f"Banda −{fmt(xdb, 0)} dB: {fmt(m['banda'] / 1e3, 1)} kHz")
            L.append(f"  de {fmt_mhz(m['f_lo'])} a {fmt_mhz(m['f_hi'])}")
            L.append(f"  centro {fmt_mhz(m['f_centro'])}")
        if m["q_carregado"] and pass_in:
            L.append(f"Fator Q (−3 dB): QL = {fmt(m['q_carregado'], 0)}   (banda {fmt(m['bw3'] / 1e3, 1)} kHz)")
            if m["q_descarregado"]:
                L.append(f"  Qu = {fmt(m['q_descarregado'], 0)}  (se for um único ressoador/cavidade)")
        L.append(f"Notch{' (perto do canal rejeitado)' if nw else ''}: {fmt_mhz(m['f_notch'])}  {fmt(m['notch_db'], 1)} dB")
        if m["q_notch"]:
            L.append(f"  Q do notch = {fmt(m['q_notch'], 0)}   (largura {fmt(m['bw3_notch'] / 1e3, 1)} kHz a 3 dB"
                     f" abaixo do nível em volta, {fmt(m['notch_ref_db'], 1)} dB)")
        if "ripple_db" in m:
            L.append(f"No canal de passagem: perda máx {fmt(m['perda_max_banda_db'], 2)} dB, ripple {fmt(m['ripple_db'], 2)} dB")
            if "rl_min_banda_db" in m:
                L.append(f"  perda de retorno mín {fmt(m['rl_min_banda_db'], 1)} dB")
        for fr, v in m["rejeicoes"]:
            L.append(f"Rejeição em {fmt_mhz(fr)}: {fmt(-v, 1)} dB")
        if ev:
            L.append("")
            for it, ok, worst in ev:
                nome = "Perda no canal de passagem" if it["tipo"] == "max_perda" else "Rejeição no canal rejeitado"
                if ok is None:
                    L.append(f"(não medido) {nome}: fora da faixa")
                    continue
                L.append(f"{'APROVADO ' if ok else 'REPROVADO'} {nome}: {fmt(abs(worst), 1)} dB (limite {fmt(it['limite_db'], 1)})")
        try:
            chan = fnum(self.bw.get()) * 1e3
            if mask and m["passo"] > chan:
                L += ["", f"AVISO: passo de {fmt(m['passo'] / 1e3, 1)} kHz é maior que o canal ({fmt(chan / 1e3, 1)} kHz).",
                      "Um notch estreito pode cair entre pontos: use os botões de zoom."]
        except ValueError:
            pass
        if -m["notch_db"] > 65:
            L += ["", f"Rejeição perto de 70 dB: pode ser o piso de ruído do {self.app.profile.NAME}."]
        self.show(self.res, "\n".join(L))

    def csv(self):
        if self.data is None:
            return
        f, s11, s21 = self.data
        self.export_csv(["freq_hz", "s11_db", "s21_db"], zip(f.tolist(), np.round(rt.db(s11), 3).tolist(), np.round(rt.db(s21), 3).tolist()), "filtro")


# ===========================================================================
# 4. Componentes
# ===========================================================================
class ComponentWindow(ToolWindow):
    MODES = {"Reflexão no CH0 (S11)": "reflexao", "Série entre CH0 e CH1 (S21)": "serie",
             "Derivação para o terra entre CH0 e CH1 (S21)": "derivacao"}

    def __init__(self, app):
        super().__init__(app, "Medidor de componentes em RF", geometry="1150x780")
        s = self.section("Montagem")
        self.mode = self.combo(s, "Modo", list(self.MODES), 0, width=30)
        ttk.Label(s, text="Reflexão: 5 Ω a 500 Ω. Série: valores altos (C pequenos, L grandes).\n"
                          "Derivação: valores baixos (C grandes, L pequenos).", foreground="gray", wraplength=300, justify="left").grid(row=1, column=0, columnspan=3, sticky="w")
        s = self.section("Faixa")
        self.f1 = self.entry(s, "Início", "1", 0, unit="MHz")
        self.f2 = self.entry(s, "Fim", "300", 1, unit="MHz")
        self.nseg = self.entry(s, "Segmentos (×101)", "2", 2)
        self.fread = self.entry(s, "Ler valores em", "145", 3, unit="MHz")
        b = ttk.Frame(self.panel)
        b.pack(fill="x", pady=(0, 6))
        ttk.Button(b, text="▶ Medir", command=self.run).pack(side="left", expand=True, fill="x")
        ttk.Button(b, text="Reler na frequência", command=self.readout).pack(side="left", expand=True, fill="x")
        self.res = self.result_box()
        self.axz = self.fig.add_subplot(311)
        self.axv = self.fig.add_subplot(312, sharex=self.axz)
        self.axq = self.fig.add_subplot(313, sharex=self.axz)
        self.show(self.res, "Calibre o NanoVNA no ponto onde o componente será soldado.\nTerminais curtos: cada mm de fio soma ~1 nH.")

    def run(self):
        try:
            a, b, n = fnum(self.f1.get()) * 1e6, fnum(self.f2.get()) * 1e6, int(fnum(self.nseg.get()))
        except ValueError:
            self.err("Valores inválidos.")
            return
        self.measure(a, b, n, self.done)

    def done(self, f, s11, s21):
        mode = self.MODES[self.mode.get()]
        z = rt.component_z(mode, s11=s11, s21=s21)
        self.data = (f, z, rt.component_values(f, z))
        self.plot()
        self.readout()

    def plot(self):
        f, z, v = self.data
        fm = f / 1e6
        ind = np.nanmedian(v["X"][: max(3, len(f) // 10)]) > 0
        for ax in (self.axz, self.axv, self.axq):
            ax.clear()
        self.axz.plot(fm, v["R"], color=GREY, lw=1.3, label="R")
        self.axz.plot(fm, v["X"], color=BLUE, lw=1.6, label="X")
        self.axz.set_ylabel("Ω")
        self.axz.set_title("Impedância")
        self.axz.legend(loc="upper left", ncol=2)
        self.axz.set_yscale("symlog", linthresh=10)
        if ind:
            self.axv.plot(fm, v["L"] * 1e9, color=BLUE, lw=1.6)
            self.axv.set_ylabel("L (nH)")
            self.axv.set_title("Indutância equivalente")
            vals = v["L"] * 1e9
        else:
            self.axv.plot(fm, v["C"] * 1e12, color=BLUE, lw=1.6)
            self.axv.set_ylabel("C (pF)")
            self.axv.set_title("Capacitância equivalente")
            vals = v["C"] * 1e12
        good = vals[np.isfinite(vals) & (vals > 0)]
        if len(good):
            self.axv.set_ylim(0, np.percentile(good, 90) * 1.6)
        self.axq.plot(fm, v["Q"], color=BLUE, lw=1.6)
        self.axq.set_ylabel("Q")
        self.axq.set_title("Fator de qualidade")
        self.axq.set_xlabel("Frequência (MHz)")
        qg = v["Q"][np.isfinite(v["Q"])]
        if len(qg):
            self.axq.set_ylim(0, min(np.percentile(qg, 95) * 1.3, 2000))
        if v["srf"]:
            for ax in (self.axz, self.axv, self.axq):
                ax.axvline(v["srf"] / 1e6, color=ORANGE, lw=1, ls="--")
            right = v["srf"] > (f[0] + f[-1]) / 2
            self.axz.text(v["srf"] / 1e6, self.axz.get_ylim()[1], f"ressonância {fmt(v['srf'] / 1e6, 1)} MHz ",
                          color=ORANGE, va="top", ha="right" if right else "left", fontsize=8)
        self.canvas.draw_idle()

    def readout(self):
        if not self.data:
            return
        f, z, v = self.data
        try:
            fr = fnum(self.fread.get()) * 1e6
        except ValueError:
            return
        if not f[0] <= fr <= f[-1]:
            self.status.set("Frequência de leitura fora da faixa medida")
            return
        r, x = float(np.interp(fr, f, z.real)), float(np.interp(fr, f, z.imag))
        q = abs(x) / max(r, 1e-9)
        kind, val = rt.reactance_to_lc(x, fr)
        L = [f"Em {fmt_mhz(fr, 3)}:",
             f"  Z = {fmt(r, 2)} {'+' if x >= 0 else '−'} j{fmt(abs(x), 2)} Ω",
             f"  {'Indutância' if kind == 'L' else 'Capacitância'}: {rt.fmt_si(val, 'H' if kind == 'L' else 'F')}",
             f"  Q = {fmt(q, 1)}",
             f"  ESR = {fmt(r, 3)} Ω"]
        if v["srf"]:
            L.append(f"\nAutorressonância: {fmt_mhz(v['srf'], 2)}")
            L.append("Acima dela o componente inverte de comportamento.")
        self.show(self.res, "\n".join(L))


# ===========================================================================
# 5. Cristal
# ===========================================================================
class CrystalWindow(ToolWindow):
    def __init__(self, app):
        super().__init__(app, "Analisador de cristal")
        s = self.section("Cristal em série entre CH0 e CH1")
        self.fn = self.entry(s, "Frequência nominal", "10", 0, unit="MHz")
        self.span = self.entry(s, "Busca ±", "0,3", 1, unit="%")
        ttk.Button(self.panel, text="▶ Medir (3 passadas automáticas)", command=self.run).pack(fill="x", pady=(0, 6))
        self.res = self.result_box(14)
        self.ax = self.fig.add_subplot(111)
        self.show(self.res, "Suporte: o cristal em série entre o pino central do CH0 e o do CH1,\n"
                            "malhas unidas e terminais curtos.\n\n"
                            "fs é a frequência de máxima transmissão; C0 desloca-a ~1 ppm da\n"
                            "ressonância 'motional'. A exatidão absoluta depende do oscilador\n"
                            "do NanoVNA (alguns ppm).")

    def run(self):
        try:
            f0 = fnum(self.fn.get()) * 1e6
            sp = fnum(self.span.get()) / 100
        except ValueError:
            self.err("Valores inválidos.")
            return
        self.measure(f0 * (1 - sp), f0 * (1 + sp), 3, self.pass1)

    def pass1(self, f, s11, s21):
        m = np.abs(s21)
        i = int(np.argmax(m))
        j = i + int(np.argmin(m[i:]))
        fs, fp = f[i], f[j]
        if fp <= fs:
            fp = fs + (f[-1] - f[0]) / 20
        d = fp - fs
        self.coarse = (f, s21)
        self.measure(fs - 0.4 * d, fp + 0.4 * d, 3, self.pass2)

    def pass2(self, f, s11, s21):
        self.mid = (f, s21)
        p = rt.crystal_params(f, s21)
        bw = p.get("bw3", (f[-1] - f[0]) / 50)
        self.p2 = p
        self.measure(p["fs"] - 4 * bw, p["fs"] + 4 * bw, 2, self.pass3)

    def pass3(self, f, s11, s21):
        p = rt.crystal_params(f, s21)
        if "fp" in self.p2 and "Cm" in p:
            p["fp"] = self.p2["fp"]
            p["C0"] = p["Cm"] * p["fs"] / (2 * (p["fp"] - p["fs"]))
        fm, sm = self.mid
        self.ax.clear()
        self.ax.plot(fm / 1e6, rt.db(sm), color=BLUE, lw=1.6)
        for key, col, lab in (("fs", GREEN, "fs"), ("fp", RED, "fp")):
            if key in p:
                self.ax.axvline(p[key] / 1e6, color=col, lw=1, ls="--")
                self.ax.text(p[key] / 1e6, self.ax.get_ylim()[1], f" {lab} {p[key] / 1e6:.6f}".replace(".", ","),
                             color=col, va="top", fontsize=8)
        self.ax.set_title("Transmissão através do cristal")
        self.ax.set_xlabel("Frequência (MHz)")
        self.ax.set_ylabel("S21 (dB)")
        self.canvas.draw_idle()
        L = [f"fs  = {p['fs'] / 1e6:.6f} MHz".replace(".", ",")]
        if "fp" in p:
            L.append(f"fp  = {p['fp'] / 1e6:.6f} MHz".replace(".", ","))
            L.append(f"fp − fs = {fmt((p['fp'] - p['fs']) / 1e3, 3)} kHz")
        L.append(f"Rm  = {fmt(p['Rm'], 2)} Ω")
        if "Lm" in p:
            L += [f"Lm  = {rt.fmt_si(p['Lm'], 'H')}", f"Cm  = {rt.fmt_si(p['Cm'], 'F')}",
                  f"Q   = {p['Q']:,.0f}".replace(",", "."), f"BW −3 dB = {fmt(p['bw3'], 1)} Hz"]
        if "C0" in p:
            L.append(f"C0  = {rt.fmt_si(p['C0'], 'F')}")
        L += ["", "Modelo: Rm, Lm, Cm em série, com C0 em paralelo."]
        self.show(self.res, "\n".join(L))


# ===========================================================================
# 6. Casamento de impedância (rede L) + carta de Smith
# ===========================================================================
def smith_grid(ax):
    ax.set_aspect("equal")
    ax.axis("off")
    clip = Circle((0, 0), 1, transform=ax.transData, fill=False)
    ax.add_patch(Circle((0, 0), 1, fill=False, color="#888", lw=1.2))
    ax.plot([-1, 1], [0, 0], color="#bbb", lw=0.8)
    for r in (0.2, 0.5, 1, 2, 5):
        ax.add_patch(Circle((r / (1 + r), 0), 1 / (1 + r), fill=False, color="#d0d0d0", lw=0.8))
        ax.text(r / (1 + r) - 1 / (1 + r) + 0.01, 0.01, f"{r:g}", fontsize=7, color="#999", va="bottom")
    for x in (0.2, 0.5, 1, 2, 5):
        for sgn in (1, -1):
            c = Circle((1, sgn / x), 1 / x, fill=False, color="#d0d0d0", lw=0.8)
            ax.add_patch(c)
            c.set_clip_path(clip)
    ax.set_xlim(-1.08, 1.08)
    ax.set_ylim(-1.08, 1.08)


class MatchWindow(ToolWindow):
    needs_vna = False

    def __init__(self, app):
        super().__init__(app, "Casamento de impedância (rede L)", geometry="1200x760", panel_width=380)
        s = self.section("Impedância da carga")
        self.src = self.combo(s, "Origem", ["Medir com o NanoVNA (CH0)", "Digitar"], 0)
        self.f = self.entry(s, "Frequência", "150", 1, unit="MHz")
        self.r = self.entry(s, "R", "25", 2, unit="Ω")
        self.x = self.entry(s, "X", "15", 3, unit="Ω")
        self.span = self.entry(s, "Varredura ±", "10", 4, unit="%")
        ttk.Button(self.panel, text="▶ Calcular", command=self.run).pack(fill="x", pady=(0, 6))
        cols = ("rede", "fonte", "carga", "e12")
        self.tree = ttk.Treeview(self.panel, columns=cols, show="headings", height=4, selectmode="browse")
        for c, t, w in zip(cols, ("Rede", "Junto à fonte", "Junto à carga", "SWR E12"), (38, 125, 125, 60)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="x", pady=(0, 6))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.draw())
        self.res = self.result_box(8)
        self.ax = self.fig.add_subplot(111)
        self.fig.subplots_adjust(left=0.02, right=0.98, top=0.94, bottom=0.03)
        smith_grid(self.ax)
        self.canvas.draw_idle()
        self.sols, self.trace, self.zl = [], None, None
        self.show(self.res, "Meça a antena no CH0 (calibrado na ponta do cabo) ou digite R e X.\n"
                            "A rede L é montada entre o cabo (fonte, 50 Ω) e a antena (carga).")

    def run(self):
        try:
            f = fnum(self.f.get()) * 1e6
        except ValueError:
            self.err("Frequência inválida.")
            return
        if self.src.get().startswith("Medir"):
            sp = fnum(self.span.get()) / 100
            self.measure(f * (1 - sp), f * (1 + sp), 1, lambda fr, s11, s21: self.solve(f, fr, s11))
        else:
            try:
                zl = complex(fnum(self.r.get()), fnum(self.x.get()))
            except ValueError:
                self.err("R e X inválidos.")
                return
            self.trace = None
            self.solve(f, None, None, zl)

    def solve(self, f, fr=None, s11=None, zl=None):
        if s11 is not None:
            i = int(np.argmin(np.abs(fr - f)))
            zl = complex(rt.gamma_to_z(s11[i]))
            self.trace = s11
            for e, v in ((self.r, zl.real), (self.x, zl.imag)):
                e.delete(0, "end")
                e.insert(0, f"{v:.2f}".replace(".", ","))
        self.zl, self.freq = zl, f
        self.sols = rt.l_network(zl, f)
        self.tree.delete(*self.tree.get_children())
        for k, s in enumerate(self.sols):
            els = s["elementos"]
            e12 = [(p, w, kd, rt.nearest_e12(v)) for p, w, kd, v in els]
            zin = rt.l_network_with_values(zl, s["topologia"], e12, f)
            s["e12"], s["swr_e12"] = e12, float(rt.swr(rt.z_to_gamma(zin)))
            desc = {}
            for p, w, kd, v in els:
                unit = "H" if kd == "L" else "F"
                desc[p] = f"{'série' if w == 'série' else 'deriv.'} {kd} {rt.fmt_si(v, unit)}"
            self.tree.insert("", "end", iid=str(k), values=(chr(65 + k), desc["fonte"], desc["carga"], fmt(s["swr_e12"], 2)))
        g = rt.z_to_gamma(zl)
        L = [f"Carga em {fmt_mhz(f, 3)}: Z = {fmt(zl.real, 1)} {'+' if zl.imag >= 0 else '−'} j{fmt(abs(zl.imag), 1)} Ω",
             f"SWR sem casamento: {fmt(float(rt.swr(g)), 2)}", ""]
        if not self.sols:
            L.append("Sem solução de rede L para esta carga.")
        else:
            L.append(f"{len(self.sols)} soluções. 'deriv.' = do sinal para o terra.")
            L.append("SWR E12 = SWR usando o valor comercial E12 mais próximo de cada componente.")
            L.append("Com capacitor em derivação, a rede também funciona como passa-baixa e atenua harmônicos.")
        self.show(self.res, "\n".join(L))
        if self.sols:
            self.tree.selection_set("0")
        self.draw()

    def draw(self):
        self.ax.clear()
        smith_grid(self.ax)
        if self.trace is not None:
            self.ax.plot(self.trace.real, self.trace.imag, color=GREY, lw=1.2)
        if self.zl is not None:
            g = rt.z_to_gamma(self.zl)
            self.ax.plot([g.real], [g.imag], "o", color=RED, ms=7)
            self.ax.annotate("carga", (g.real, g.imag), textcoords="offset points", xytext=(6, 6), fontsize=8)
        sel = self.tree.selection()
        if sel and self.sols:
            s = self.sols[int(sel[0])]
            t = np.linspace(0, 1, 60)
            zl = self.zl
            if s["topologia"] == "shunt_carga":
                z1 = 1 / (1 / zl + 1j * t * s["b"])
                z2 = 1 / (1 / zl + 1j * s["b"]) + 1j * t * s["x"]
            else:
                z1 = zl + 1j * t * s["x"]
                z2 = 1 / (1 / (zl + 1j * s["x"]) + 1j * t * s["b"])
            for zz, col in ((z1, ORANGE), (z2, BLUE)):
                gg = rt.z_to_gamma(zz)
                self.ax.plot(gg.real, gg.imag, color=col, lw=2.2)
            self.ax.plot([0], [0], "o", color=GREEN, ms=7)
            self.ax.set_title(f"Rede {chr(65 + int(sel[0]))}: laranja = elemento junto à carga, azul = junto à fonte")
        self.canvas.draw_idle()


# ===========================================================================
# 7. Histórico e comparação
# ===========================================================================
class HistoryWindow(ToolWindow):
    needs_vna = False

    def __init__(self, app):
        super().__init__(app, "Histórico e comparação de antenas", geometry="1200x760", panel_width=390)
        os.makedirs(HIST_DIR, exist_ok=True)
        s = self.section("Nova referência")
        self.f1 = self.entry(s, "Início", "140", 0, unit="MHz")
        self.f2 = self.entry(s, "Fim", "160", 1, unit="MHz")
        self.nseg = self.entry(s, "Segmentos (×101)", "2", 2)
        ttk.Button(s, text="▶ Medir e salvar como referência", command=self.save_ref).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        cols = ("data", "nome", "faixa")
        self.tree = ttk.Treeview(self.panel, columns=cols, show="headings", height=8, selectmode="browse")
        for c, t, w in zip(cols, ("Data", "Antena / site", "Faixa"), (95, 170, 95)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="x", pady=(0, 4))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_ref())
        b = ttk.Frame(self.panel)
        b.pack(fill="x", pady=(0, 6))
        ttk.Button(b, text="▶ Comparar com agora", command=self.compare_now).pack(side="left", expand=True, fill="x")
        ttk.Button(b, text="Excluir", command=self.delete).pack(side="left", fill="x")
        ttk.Button(b, text="Pasta", command=lambda: self.open_folder()).pack(side="left", fill="x")
        self.res = self.result_box(9)
        self.ax = self.fig.add_subplot(211)
        self.axd = self.fig.add_subplot(212, sharex=self.ax)
        self.refresh()
        self.show(self.res, "Salve uma referência na instalação da antena.\n"
                            "Nas visitas seguintes, selecione-a e clique em 'Comparar com agora':\n"
                            "a medição é refeita na mesma faixa e as diferenças aparecem aqui.\n\n"
                            f"Arquivos em: {HIST_DIR}")

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.refs = {}
        for fn in sorted(os.listdir(HIST_DIR), reverse=True):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(HIST_DIR, fn), encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception:
                continue
            self.refs[fn] = d
            self.tree.insert("", "end", iid=fn, values=(d["data"][:16], d["nome"],
                             f"{d['freqs'][0] / 1e6:g}–{d['freqs'][-1] / 1e6:g} MHz"))

    def open_folder(self):
        import subprocess, sys
        try:
            if sys.platform.startswith("win"):
                os.startfile(HIST_DIR)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", HIST_DIR])
            else:
                subprocess.Popen(["xdg-open", HIST_DIR])
        except Exception:
            self.status.set(HIST_DIR)

    def save_ref(self):
        try:
            a, b, n = fnum(self.f1.get()) * 1e6, fnum(self.f2.get()) * 1e6, int(fnum(self.nseg.get()))
        except ValueError:
            self.err("Valores inválidos.")
            return
        nome = simpledialog.askstring("Nova referência", "Antena / site (ex.: Torre 1 – dipolo VHF):", parent=self)
        if not nome:
            return
        obs = simpledialog.askstring("Nova referência", "Observações (opcional):", parent=self) or ""

        def done(f, s11, s21):
            d = {"nome": nome, "obs": obs, "data": time.strftime("%Y-%m-%d %H:%M:%S"), "n_seg": n,
                 "freqs": f.tolist(), "s11_re": s11.real.tolist(), "s11_im": s11.imag.tolist()}
            safe = "".join(c if c.isalnum() else "_" for c in nome)[:40]
            fn = time.strftime("%Y%m%d_%H%M%S_") + safe + ".json"
            with open(os.path.join(HIST_DIR, fn), "w", encoding="utf-8") as fh:
                json.dump(d, fh, ensure_ascii=False)
            self.refresh()
            self.tree.selection_set(fn)
            self.status.set(f"Referência salva: {fn}")
        self.measure(a, b, n, done)

    def _ref(self):
        sel = self.tree.selection()
        if not sel:
            return None, None
        d = self.refs[sel[0]]
        return sel[0], (np.array(d["freqs"]), np.array(d["s11_re"]) + 1j * np.array(d["s11_im"]), d)

    def show_ref(self):
        fn, r = self._ref()
        if not r:
            return
        f, s11, d = r
        self.plot(f, s11, None)
        s = rt.s11_summary(f, s11)
        self.show(self.res, f"{d['nome']}\n{d['data']}\n{d.get('obs', '')}\n\n"
                            f"SWR mínimo {fmt(s['swr_min'], 2)} em {fmt_mhz(s['f_min'], 3)}\n"
                            + (f"Banda SWR ≤ 2: {'≥ ' if s['banda_parcial'] else ''}{fmt(s['banda_swr'] / 1e3, 0)} kHz" if s["banda_swr"] else "Não atinge SWR 2:1"))

    def compare_now(self):
        fn, r = self._ref()
        if not r:
            self.err("Selecione uma referência na lista.")
            return
        f, s11_ref, d = r
        n = int(d.get("n_seg", max(1, len(f) // 101)))

        def bstr(x):
            if not x["banda_swr"]:
                return "—"
            return ("≥ " if x["banda_parcial"] else "") + fmt(x["banda_swr"] / 1e3, 0)

        def done(fn_, s11, s21):
            if len(fn_) != len(f):
                s11 = np.interp(f, fn_, s11.real) + 1j * np.interp(f, fn_, s11.imag)
            c = rt.compare(f, s11_ref, s11)
            self.plot(f, s11_ref, s11)
            a, b = c["ref"], c["agora"]
            L = [f"{d['nome']} — referência de {d['data'][:16]}", "",
                 f"SITUAÇÃO: {c['situacao']}", ""]
            L += [f"{'':18}{'referência':>12}{'agora':>12}",
                  f"{'SWR mínimo':18}{fmt(a['swr_min'], 2):>12}{fmt(b['swr_min'], 2):>12}",
                  f"{'em (MHz)':18}{fmt(a['f_min'] / 1e6, 3):>12}{fmt(b['f_min'] / 1e6, 3):>12}",
                  f"{'banda SWR≤2 (kHz)':18}{bstr(a):>12}{bstr(b):>12}",
                  "", f"Maior mudança de perda de retorno: {fmt(c['max_delta_rl'], 1)} dB em {fmt_mhz(c['f_max_delta_rl'], 3)}"]
            if c["alertas"]:
                L += [""] + [f"• {a_}" for a_ in c["alertas"]]
            self.show(self.res, "\n".join(L))
        self.measure(f[0], f[-1], n, done)

    def plot(self, f, ref, now):
        self.ax.clear()
        self.axd.clear()
        fm = f / 1e6
        self.ax.plot(fm, np.minimum(rt.swr(ref), 10), color=GREY, lw=1.6, ls="--", label="referência")
        if now is not None:
            self.ax.plot(fm, np.minimum(rt.swr(now), 10), color=BLUE, lw=1.8, label="agora")
            self.axd.plot(fm, rt.return_loss(now) - rt.return_loss(ref), color=BLUE, lw=1.4)
            self.axd.axhline(0, color=GREY, lw=0.8)
        self.ax.axhline(2, color=RED, lw=0.8, ls=":")
        self.ax.set_ylim(1, min(10, max(3, float(np.nanmax(np.minimum(rt.swr(ref), 10))) + 0.5)))
        self.ax.set_ylabel("SWR")
        self.ax.set_title("SWR: referência × agora")
        self.ax.legend(loc="upper right")
        self.axd.set_ylabel("Δ perda de retorno (dB)")
        self.axd.set_xlabel("Frequência (MHz)")
        self.axd.set_title("Mudança (positivo = melhorou)")
        self.canvas.draw_idle()

    def delete(self):
        sel = self.tree.selection()
        if sel and messagebox.askyesno("Excluir", "Excluir a referência selecionada?", parent=self):
            os.remove(os.path.join(HIST_DIR, sel[0]))
            self.refresh()


# ===========================================================================
# Tabela de cabos
# ===========================================================================
class CableTableWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.title("Tabela de cabos — NanoVNA Mirror")
        self.geometry("820x480")
        cols = ("cabo", "vf", "a150", "a450", "obs")
        self.tree = ttk.Treeview(self, columns=cols, show="headings")
        for c, t, w in zip(cols, ("Cabo", "VF", "dB/100 m 150 MHz", "dB/100 m 450 MHz", "Obs."), (280, 60, 130, 130, 160)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)
        self.tree.bind("<Double-1>", lambda e: self.edit())
        b = ttk.Frame(self, padding=(8, 0, 8, 8))
        b.pack(fill="x")
        for t, cmd in (("Novo", lambda: self.edit(new=True)), ("Editar", self.edit), ("Excluir", self.delete),
                       ("Restaurar padrão", self.reset)):
            ttk.Button(b, text=t, command=cmd).pack(side="left")
        ttk.Label(b, text="Valores típicos de catálogo; confira o datasheet do seu cabo.", foreground="gray").pack(side="right")
        self.refresh()

    def refresh(self):
        self.tab = cabos.load()
        self.tree.delete(*self.tree.get_children())
        for name, (vf, att, obs) in self.tab.items():
            self.tree.insert("", "end", iid=name, values=(name, f"{vf:.3f}",
                             fmt(cabos.attenuation_db_per_100m(att, 150), 1),
                             fmt(cabos.attenuation_db_per_100m(att, 450), 1), obs))

    def edit(self, new=False):
        sel = self.tree.selection()
        if not new and not sel:
            return
        name = "" if new else sel[0]
        vf, att, obs = (0.66, {100: 10.0, 450: 20.0}, "") if new else self.tab[name]
        dlg = tk.Toplevel(self)
        dlg.title("Cabo")
        dlg.transient(self)
        f = ttk.Frame(dlg, padding=10)
        f.pack(fill="both")
        ents = {}
        for r, (k, lab, val) in enumerate((("nome", "Nome", name), ("vf", "Fator de velocidade", f"{vf:.3f}"),
                                           ("att", "Atenuação (MHz=dB/100 m; ...)", "; ".join(f"{k:g}={v:g}" for k, v in sorted(att.items()))),
                                           ("obs", "Observação", obs))):
            ttk.Label(f, text=lab).grid(row=r, column=0, sticky="w", pady=2)
            e = ttk.Entry(f, width=48)
            e.insert(0, val)
            e.grid(row=r, column=1, pady=2, padx=4)
            ents[k] = e

        def ok():
            try:
                nm = ents["nome"].get().strip()
                v = fnum(ents["vf"].get())
                a = {}
                for part in ents["att"].get().split(";"):
                    if "=" in part:
                        k, x = part.split("=")
                        a[fnum(k)] = fnum(x)
                if not nm or not (0.4 < v <= 1.0) or not a:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("Cabo", "Confira nome, VF (0,4–1,0) e atenuação (ex.: 100=7; 450=15).", parent=dlg)
                return
            if not new and nm != name:
                self.tab.pop(name, None)
            self.tab[nm] = (v, a, ents["obs"].get())
            cabos.save_user(self.tab)
            dlg.destroy()
            self.refresh()
        ttk.Button(f, text="Salvar", command=ok).grid(row=5, column=1, sticky="e", pady=(8, 0))
        dlg.grab_set()

    def delete(self):
        sel = self.tree.selection()
        if sel and messagebox.askyesno("Excluir", f"Excluir '{sel[0]}' da tabela?", parent=self):
            self.tab.pop(sel[0], None)
            cabos.save_user(self.tab)
            self.refresh()

    def reset(self):
        if messagebox.askyesno("Restaurar", "Descartar suas edições e voltar à tabela padrão?", parent=self):
            cabos.reset_user()
            self.refresh()


TOOLS = [
    ("Localizador de falhas no cabo (DTF)", DTFWindow, "Distância até conectores ruins, emendas e danos"),
    ("Cabo: perda, comprimento e stub", CableWindow, "Perda, VF, comprimento elétrico e corte de stubs"),
    ("Filtros e duplexadores", FilterWindow, "Banda, notch, rejeição, máscara e sintonia ao vivo"),
    ("Medidor de componentes", ComponentWindow, "L, C, R e Q na frequência de trabalho"),
    ("Analisador de cristal", CrystalWindow, "fs, fp, Rm, Lm, Cm, C0 e Q"),
    ("Casamento de impedância", MatchWindow, "Rede L com carta de Smith"),
    ("Histórico e comparação", HistoryWindow, "Referência da instalação × medição de hoje"),
    ("Tabela de cabos", CableTableWindow, "VF e atenuação dos cabos (editável)"),
]
