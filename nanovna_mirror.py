#!/usr/bin/env python3
"""
NanoVNA Mirror v4 - espelhamento de tela e controle remoto para NanoVNA-H
(firmware edy555 / hugen, shell ChibiOS)

- Espelha a tela via comando `capture` (framebuffer 320x240 RGB565 big-endian)
- Controle por abas: Estimulo, Traces, Marcadores, Calibracao, Sistema
- Leitura de marcadores calculada a partir de `data` (dB, SWR, impedancia)
- Exportacao Touchstone (.s1p / .s2p)
- Relatorio em PNG/PDF com cabecalho, tela ampliada, marcadores e resumo
- Gerador CW na porta CH0 e medidor S21 estilo power meter (span zero)
- Ferramentas: localizador de falhas (DTF), perda/comprimento de cabo e stubs, filtros e
  duplexadores, componentes L/C/Q, cristal, casamento de impedância e histórico

Requisitos (no PC):
    pip install pyserial pillow numpy matplotlib
(tkinter ja vem com o Python no Windows e no macOS; no Linux: sudo apt install python3-tk)

Uso:
    python nanovna_mirror.py

Desenvolvido por Ruy Lazaro - laztel.com.br - ruylazaro.engenharia@gmail.com
"""

import cmath
import webbrowser
import json
import math
import os
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
import serial
import serial.tools.list_ports
from PIL import Image, ImageDraw, ImageFont, ImageTk

AUTHOR = "Ruy Lazaro"
AUTHOR_SITE = "laztel.com.br"
AUTHOR_EMAIL = "ruylazaro.engenharia@gmail.com"
CREDIT = f"Desenvolvido por {AUTHOR} · {AUTHOR_SITE} · {AUTHOR_EMAIL}"

SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".nanovna_mirror.json")
SNAPSHOT_SCALE = 3  # ampliacao do "Salvar PNG" e do relatorio
STEPS = {"1 kHz": 1_000, "10 kHz": 10_000, "100 kHz": 100_000,
         "1 MHz": 1_000_000, "10 MHz": 10_000_000}

WIDTH, HEIGHT = 320, 240
FRAME_BYTES = WIDTH * HEIGHT * 2
PROMPT = b"ch> "
NANOVNA_VID_PID = (0x0483, 0x5740)  # STM32 Virtual COM Port
Z0 = 50.0

TRACE_FORMATS = ["logmag", "phase", "delay", "smith", "polar",
                 "linear", "swr", "real", "imag", "r", "x"]
CHANNELS = ["S11 (CH0)", "S21 (CH1)"]
PRESETS = [
    ("HF 1–30 MHz", 1, 30),
    ("160 m", 1.7, 2.1),
    ("80 m", 3.4, 4.1),
    ("40 m", 6.9, 7.4),
    ("20 m", 13.9, 14.5),
    ("10 m", 27.5, 30),
    ("6 m", 49, 55),
    ("2 m", 140, 152),
    ("70 cm", 420, 452),
    ("Faixa total", 0.05, 900),
]
PRESETS_UHF = [            # aparelhos que vão além de 900 MHz (SV4401A)
    ("UHF 400–520", 400, 520),
    ("23 cm", 1240, 1300),
    ("2,4 GHz", 2400, 2500),
    ("Faixa total", 0.05, 4400),
]


# ----------------------------------------------------------------------------
# Comunicacao serial
# ----------------------------------------------------------------------------
import devices
import rf_tools as rt

NanoVNA = devices.NanoVNAH   # compatibilidade


# ----------------------------------------------------------------------------
# Calculos RF
# ----------------------------------------------------------------------------
def fmt_freq(hz):
    if hz >= 1e9:
        return f"{hz / 1e9:.6f} GHz"
    if hz >= 1e6:
        return f"{hz / 1e6:.6f} MHz"
    return f"{hz / 1e3:.3f} kHz"


def s11_summary(g, f):
    mag = abs(g)
    db = 20 * math.log10(mag) if mag > 0 else -999
    swr = (1 + mag) / (1 - mag) if mag < 1 else float("inf")
    z = Z0 * (1 + g) / (1 - g) if g != 1 else complex(float("inf"), 0)
    w = 2 * math.pi * f
    if z.imag >= 0:
        lc = f"L={z.imag / w * 1e9:.2f} nH" if w else ""
    else:
        lc = f"C={-1 / (w * z.imag) * 1e12:.2f} pF" if w and z.imag else ""
    return (f"S11 {db:6.2f} dB  SWR {swr:5.2f}\n"
            f"    Z = {z.real:.2f} {'+' if z.imag >= 0 else '-'} j{abs(z.imag):.2f} Ω  {lc}")


def s21_summary(s):
    mag = abs(s)
    db = 20 * math.log10(mag) if mag > 0 else -999
    ph = math.degrees(cmath.phase(s))
    return f"S21 {db:6.2f} dB  fase {ph:7.2f}°"


def s11_values(g, f):
    """Devolve (dB, SWR, Z complexo, texto L/C equivalente)."""
    mag = abs(g)
    db = 20 * math.log10(mag) if mag > 0 else -999.0
    swr = (1 + mag) / (1 - mag) if mag < 1 else float("inf")
    z = Z0 * (1 + g) / (1 - g) if g != 1 else complex(float("inf"), 0)
    w = 2 * math.pi * f
    lc = ""
    if w and math.isfinite(z.imag):
        if z.imag > 0:
            lc = f"{z.imag / w * 1e9:.2f} nH"
        elif z.imag < 0:
            lc = f"{-1 / (w * z.imag) * 1e12:.2f} pF"
    return db, swr, z, lc


def swr_band(freqs, s11, limit=2.0):
    """Ponto de SWR mínimo e a faixa contínua ao redor dele com SWR <= limit."""
    if not freqs:
        return None
    swrs = [s11_values(g, f)[1] for g, f in zip(s11, freqs)]
    i_min = min(range(len(swrs)), key=lambda i: swrs[i])
    res = {"i_min": i_min, "f_min": freqs[i_min], "swr_min": swrs[i_min], "band": None}
    if swrs[i_min] <= limit:
        lo = hi = i_min
        while lo > 0 and swrs[lo - 1] <= limit:
            lo -= 1
        while hi < len(swrs) - 1 and swrs[hi + 1] <= limit:
            hi += 1
        res["band"] = (freqs[lo], freqs[hi], lo == 0, hi == len(swrs) - 1)
    return res


def fmt_mhz(hz):
    return f"{hz / 1e6:.4f} MHz"


# ----------------------------------------------------------------------------
# Relatorio
# ----------------------------------------------------------------------------
_FONT_CANDIDATES = {
    False: ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf",
            "Helvetica.ttc", "/System/Library/Fonts/Helvetica.ttc"],
    True: ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf",
           "Helvetica.ttc", "/System/Library/Fonts/Helvetica.ttc"],
}
_MONO_CANDIDATES = ["consola.ttf", "DejaVuSansMono.ttf", "LiberationMono-Regular.ttf",
                    "Menlo.ttc", "/System/Library/Fonts/Menlo.ttc"]


def load_font(size, bold=False, mono=False):
    for name in (_MONO_CANDIDATES if mono else _FONT_CANDIDATES[bold]):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow antigo
        return ImageFont.load_default()


def wrap_text(draw, text, font, width):
    lines = []
    for para in text.splitlines() or [""]:
        words, cur = para.split(), ""
        for w in words:
            test = (cur + " " + w).strip()
            if draw.textlength(test, font=font) <= width:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def compose_report(screen, meta, freqs, s11, s21, markers, include_s21=True):
    """
    Monta a imagem do relatório.
    screen  : PIL.Image 320x240 da tela do NanoVNA
    meta    : dict com title, dut, location, operator, notes, firmware, timestamp
    markers : lista de (numero, indice)
    """
    W, M = 1600, 40
    ink, muted, line, accent = (25, 28, 33), (105, 112, 122), (215, 219, 225), (20, 90, 160)
    f_title, f_sub = load_font(38, True), load_font(20)
    f_label, f_val = load_font(17), load_font(21, True)
    f_h, f_tab, f_tabb = load_font(22, True), load_font(19, mono=True), load_font(19, True)
    f_small = load_font(15)

    sw_ = 960
    k = sw_ / screen.width
    method = Image.NEAREST if abs(k - round(k)) < 1e-6 else Image.LANCZOS
    scr = screen.resize((sw_, int(round(screen.height * k))), method)
    side_x = M + scr.width + 36
    side_w = W - side_x - M

    img = Image.new("RGB", (W, 3000), "white")
    d = ImageDraw.Draw(img)

    # --- cabecalho ---
    d.rectangle([0, 0, W, 8], fill=accent)
    y = 30
    d.text((M, y), meta.get("title") or f"Medição com {meta.get('instrumento', 'NanoVNA')}", font=f_title, fill=ink)
    ts = meta["timestamp"]
    d.text((W - M, y + 6), time.strftime("%d/%m/%Y  %H:%M:%S", ts), font=f_sub, fill=muted, anchor="ra")
    y += 62

    sweep = f"{fmt_mhz(freqs[0])} – {fmt_mhz(freqs[-1])}  ({len(freqs)} pontos)" if freqs else "—"
    fields = [("Dispositivo sob teste", meta.get("dut")), ("Local", meta.get("location")),
              ("Operador", meta.get("operator")), ("Varredura", sweep),
              ("Instrumento", f"{meta.get('instrumento', 'NanoVNA-H')}  ·  firmware {meta.get('firmware', '?')}")]
    fields = [(k, v) for k, v in fields if v]
    col_w = (W - 2 * M) // 3
    for i, (k, v) in enumerate(fields):
        cx, cy = M + (i % 3) * col_w, y + (i // 3) * 58
        d.text((cx, cy), k.upper(), font=f_label, fill=muted)
        fv = f_val
        for size in (19, 17, 15):
            if d.textlength(str(v), font=fv) <= col_w - 16:
                break
            fv = load_font(size, True)
        d.text((cx, cy + 22), str(v), font=fv, fill=ink)
    y += ((len(fields) + 2) // 3) * 58 + 10
    d.line([M, y, W - M, y], fill=line, width=2)
    y += 26

    # --- tela ---
    top = y
    img.paste(scr, (M, y))
    d.rectangle([M - 1, y - 1, M + scr.width, y + scr.height], outline=line)
    d.text((M, y + scr.height + 8), "Tela do instrumento (captura direta)", font=f_small, fill=muted)

    # --- coluna lateral: resumo ---
    sy = top
    summary = swr_band(freqs, s11)
    d.text((side_x, sy), "Resumo S11", font=f_h, fill=accent)
    sy += 36
    if summary:
        db, swr, z, lc = s11_values(s11[summary["i_min"]], summary["f_min"])
        rows = [("SWR mínimo", f"{swr:.2f} em {fmt_mhz(summary['f_min'])}"),
                ("Return loss", f"{-db:.2f} dB"),
                ("Impedância", f"{z.real:.1f} {'+' if z.imag >= 0 else '−'} j{abs(z.imag):.1f} Ω")]
        if summary["band"]:
            a, b, open_lo, open_hi = summary["band"]
            bw = f"{fmt_mhz(a)}{' (ou menos)' if open_lo else ''} – {fmt_mhz(b)}{' (ou mais)' if open_hi else ''}"
            rows.append(("Banda SWR ≤ 2", bw))
            bw_hz = b - a
            rows.append(("Largura", f"{bw_hz / 1e6:.3f} MHz" if bw_hz >= 1e6 else f"{bw_hz / 1e3:.1f} kHz"))
        else:
            rows.append(("Banda SWR ≤ 2", "não atinge SWR 2 na faixa"))
        for k, v in rows:
            d.text((side_x, sy), k, font=f_label, fill=muted)
            for ln in wrap_text(d, v, f_val, side_w - 170):
                d.text((side_x + 170, sy - 2), ln, font=f_val, fill=ink)
                sy += 28
            sy += 4
    sy += 14

    # --- marcadores ---
    d.line([side_x, sy, W - M, sy], fill=line, width=2)
    sy += 18
    d.text((side_x, sy), "Marcadores", font=f_h, fill=accent)
    sy += 38
    if not markers:
        d.text((side_x, sy), "Nenhum marcador ativo.", font=f_label, fill=muted)
        sy += 30
    for m, idx in markers:
        if idx >= len(freqs):
            continue
        f = freqs[idx]
        db, swr, z, lc = s11_values(s11[idx], f)
        d.rounded_rectangle([side_x, sy, W - M, sy + (128 if include_s21 else 100)],
                            radius=8, outline=line, width=2)
        d.text((side_x + 14, sy + 10), f"M{m}", font=f_tabb, fill=accent)
        d.text((side_x + 70, sy + 10), fmt_mhz(f), font=f_tabb, fill=ink)
        d.text((W - M - 14, sy + 12), f"ponto {idx}", font=f_small, fill=muted, anchor="ra")
        tx = side_x + 14
        d.text((tx, sy + 42), f"S11 {db:7.2f} dB   SWR {swr:5.2f}", font=f_tab, fill=ink)
        zt = f"Z {z.real:7.2f} {'+' if z.imag >= 0 else '-'} j{abs(z.imag):.2f} Ω"
        d.text((tx, sy + 70), zt + (f"   ({lc})" if lc else ""), font=f_tab, fill=ink)
        if include_s21 and idx < len(s21):
            s = s21[idx]
            sdb = 20 * math.log10(abs(s)) if abs(s) > 0 else -999
            d.text((tx, sy + 98), f"S21 {sdb:7.2f} dB   fase {math.degrees(cmath.phase(s)):7.2f}°",
                   font=f_tab, fill=ink)
        sy += (128 if include_s21 else 100) + 12

    y = max(top + scr.height + 40, sy + 10)

    # --- observacoes ---
    notes = (meta.get("notes") or "").strip()
    if notes:
        d.line([M, y, W - M, y], fill=line, width=2)
        y += 18
        d.text((M, y), "Observações", font=f_h, fill=accent)
        y += 36
        for ln in wrap_text(d, notes, f_sub, W - 2 * M):
            d.text((M, y), ln, font=f_sub, fill=ink)
            y += 28
        y += 10

    # --- rodape ---
    y += 10
    d.line([M, y, W - M, y], fill=line, width=1)
    y += 10
    d.text((M, y), f"Gerado por {meta.get('app', 'NanoVNA Mirror')}  ·  {CREDIT}", font=f_small, fill=muted)
    d.text((W - M, y), "Valores calculados a partir dos dados brutos (comando data), Z0 = 50 Ω",
           font=f_small, fill=muted, anchor="ra")
    y += 34
    return img.crop((0, 0, W, y))


# ----------------------------------------------------------------------------
# Interface grafica
# ----------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self, profile=None):
        super().__init__()
        self.profile = profile or devices.NanoVNAH
        self.sw, self.sh = self.profile.SCREEN
        self.title(self.profile.APP_NAME)
        self._set_icon()
        self.geometry("1280x800" if self.sw <= 480 else "1520x900")
        self.gen_status = tk.StringVar(value="Desligado (aparelho em varredura)")
        self.touch_queue = []
        self._drag_last = 0.0
        self._img_geom = None
        self.minsize(900, 600)
        self.vna = None
        self.running = False
        self.last_image = None
        self._tkimg = None
        self.zoom = tk.StringVar(value="Ajustar")
        self.fps_text = tk.StringVar(value="Desconectado")
        self._ports = []
        self.cw_saved_sweep = None  # faixa original enquanto gerador/medidor estão ativos
        self.meter = None
        self._build_ui()
        self.refresh_ports()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<F5>", lambda e: self.read_state())
        self.bind("<F12>", lambda e: self.save_png())

    def _set_icon(self):
        """Ícone da janela e da barra de tarefas (vale também para as janelas filhas)."""
        try:
            from make_icon import draw_icon
            self._icons = [ImageTk.PhotoImage(draw_icon(s)) for s in (64, 32, 16)]
            self.iconphoto(True, *self._icons)
        except Exception:
            pass  # sem o make_icon.py o programa funciona normalmente, só sem ícone

    # =======================================================================
    # Montagem da janela
    # =======================================================================
    def _build_ui(self):
        style = ttk.Style(self)
        style.configure("Small.TButton", padding=(4, 1))

        # ---- barra superior ----
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="Porta:").pack(side="left")
        self.port_cb = ttk.Combobox(top, width=30, state="readonly")
        self.port_cb.pack(side="left", padx=4)
        ttk.Button(top, text="↻", width=3, command=self.refresh_ports).pack(side="left")
        self.conn_btn = ttk.Button(top, text="Conectar", command=self.toggle_connect)
        self.conn_btn.pack(side="left", padx=6)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(top, text="Zoom:").pack(side="left")
        zcb = ttk.Combobox(top, width=8, state="readonly", textvariable=self.zoom,
                           values=["Ajustar", "2x", "3x", "4x"] if self.sw <= 480 else ["Ajustar", "1x"])
        zcb.pack(side="left", padx=4)
        zcb.bind("<<ComboboxSelected>>", lambda e: self._redraw())
        self.mirror_btn = ttk.Button(top, text="⏸ Espelhamento", command=self.toggle_mirror)
        self.mirror_btn.pack(side="left", padx=6)
        ttk.Button(top, text="📷 PNG (F12)", command=self.save_png).pack(side="left")
        ttk.Button(top, text="📄 Relatório", command=self.open_report).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="📶 Medidor S21", command=self.open_meter).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="⟳ Ler (F5)", command=self.read_state).pack(side="left", padx=6)
        ttk.Label(top, textvariable=self.fps_text, foreground="gray").pack(side="right")

        # ---- rodape com credito ----
        foot = tk.Frame(self, background="#e9ecef")
        foot.pack(side="bottom", fill="x")
        fnt = ("Segoe UI", 8)
        tk.Label(foot, text=f"Desenvolvido por {AUTHOR}", font=fnt, background="#e9ecef",
                 foreground="#6c757d").pack(side="left", padx=(8, 0), pady=2)
        for txt, url in ((AUTHOR_SITE, f"https://{AUTHOR_SITE}"),
                         (AUTHOR_EMAIL, f"mailto:{AUTHOR_EMAIL}")):
            tk.Label(foot, text="·", font=fnt, background="#e9ecef",
                     foreground="#6c757d").pack(side="left", padx=4)
            lk = tk.Label(foot, text=txt, font=fnt, background="#e9ecef",
                          foreground="#145aa0", cursor="hand2")
            lk.pack(side="left")
            lk.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))

        # ---- corpo: tela (esquerda) + painel (direita) ----
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        side = ttk.Frame(body, width=450)
        side.pack(side="right", fill="y", padx=(6, 0))
        side.pack_propagate(False)

        k = 2 if self.sw <= 480 else 1
        self.canvas = tk.Canvas(body, background="#111", highlightthickness=0,
                                width=self.sw * k, height=self.sh * k)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        if self.profile.HAS_TOUCH:
            self.canvas.configure(cursor="hand2")
            self.canvas.bind("<ButtonPress-1>", lambda e: self._on_touch(e, "down"))
            self.canvas.bind("<B1-Motion>", lambda e: self._on_touch(e, "move"))
            self.canvas.bind("<ButtonRelease-1>", lambda e: self._on_touch(e, "up"))

        nb = ttk.Notebook(side)
        nb.pack(fill="both", expand=True)
        nb.add(self._tab_stimulus(nb), text="Estímulo")
        if self.profile.HAS_TRACES:
            nb.add(self._tab_traces(nb), text="Traces")
        nb.add(self._tab_markers(nb), text="Marcadores")
        nb.add(self._tab_cal(nb), text="Calibração")
        if self.profile.HAS_GENERATOR:
            nb.add(self._tab_generator(nb), text="Gerador")
        nb.add(self._tab_system(nb), text="Sistema")
        self._build_tools_menu()

        # ---- console ----
        cs = ttk.LabelFrame(side, text="Console", padding=4)
        cs.pack(fill="both", expand=False, pady=(6, 0))
        self.cmd_e = ttk.Entry(cs)
        self.cmd_e.pack(fill="x")
        self.cmd_e.bind("<Return>", lambda e: self.send_console())
        self.out = tk.Text(cs, height=9, font=("Consolas", 9), wrap="word")
        self.out.pack(fill="both", expand=True, pady=(4, 0))

        self._show_blank()

    # ---------- aba Estimulo ----------
    def _tab_stimulus(self, nb):
        f = ttk.Frame(nb, padding=8)

        box = ttk.LabelFrame(f, text="Início / Fim", padding=6)
        box.pack(fill="x")
        self.start_e = self._entry(box, "Início (MHz)", 0, "1")
        self.stop_e = self._entry(box, "Fim (MHz)", 1, "30")
        ttk.Button(box, text="Aplicar", command=self.apply_start_stop).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        box = ttk.LabelFrame(f, text="Centro / Span", padding=6)
        box.pack(fill="x", pady=(6, 0))
        self.center_e = self._entry(box, "Centro (MHz)", 0, "14.2")
        self.span_e = self._entry(box, "Span (MHz)", 1, "1")
        ttk.Button(box, text="Aplicar", command=self.apply_center_span).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        if len(self.profile.POINT_OPTIONS) > 1:
            box = ttk.LabelFrame(f, text="Pontos por varredura", padding=6)
            box.pack(fill="x", pady=(6, 0))
            self.points_cb = ttk.Combobox(box, state="readonly", width=8,
                                          values=[str(x) for x in self.profile.POINT_OPTIONS])
            self.points_cb.set(str(self.profile.POINT_OPTIONS[-1] if self.profile.POINT_OPTIONS[-1] <= 501 else 501))
            self.points_cb.pack(side="left")
            ttk.Label(box, text="  (mais pontos = varredura mais lenta)", foreground="gray").pack(side="left")
        else:
            self.points_cb = None

        box = ttk.LabelFrame(f, text="Faixas rápidas", padding=6)
        box.pack(fill="x", pady=(6, 0))
        presets = PRESETS if self.profile.F_MAX <= 1e9 else PRESETS[:-1] + PRESETS_UHF
        for i, (name, a, b) in enumerate(presets):
            ttk.Button(box, text=name, style="Small.TButton",
                       command=lambda a=a, b=b: self.set_sweep(a, b)).grid(
                row=i // 2, column=i % 2, sticky="ew", padx=1, pady=1)
        box.columnconfigure(0, weight=1)
        box.columnconfigure(1, weight=1)

        box = ttk.LabelFrame(f, text="Varredura", padding=6)
        box.pack(fill="x", pady=(6, 0))
        ttk.Button(box, text="⏸ Pause", command=lambda: self.send("pause")).pack(side="left", expand=True, fill="x")
        ttk.Button(box, text="▶ Resume", command=lambda: self.send("resume")).pack(side="left", expand=True, fill="x")
        return f

    # ---------- aba Traces ----------
    def _tab_traces(self, nb):
        f = ttk.Frame(nb, padding=8)
        hdr = ["", "Ativo", "Formato", "Canal", "Escala/div", "Ref pos", ""]
        for c, h in enumerate(hdr):
            ttk.Label(f, text=h, font=("TkDefaultFont", 8, "bold")).grid(row=0, column=c, padx=2)
        self.trace_rows = []
        defaults = [("logmag", 0), ("logmag", 1), ("smith", 0), ("phase", 1)]
        for t in range(4):
            on = tk.BooleanVar(value=True)
            fmt = tk.StringVar(value=defaults[t][0])
            ch = tk.StringVar(value=CHANNELS[defaults[t][1]])
            ttk.Label(f, text=f"T{t}").grid(row=t + 1, column=0)
            ttk.Checkbutton(f, variable=on).grid(row=t + 1, column=1)
            ttk.Combobox(f, values=TRACE_FORMATS, textvariable=fmt, width=7,
                         state="readonly").grid(row=t + 1, column=2, padx=1)
            ttk.Combobox(f, values=CHANNELS, textvariable=ch, width=9,
                         state="readonly").grid(row=t + 1, column=3, padx=1)
            sc = ttk.Entry(f, width=6)
            sc.grid(row=t + 1, column=4, padx=1)
            rp = ttk.Entry(f, width=5)
            rp.grid(row=t + 1, column=5, padx=1)
            ttk.Button(f, text="OK", width=3,
                       command=lambda t=t: self.apply_trace(t)).grid(row=t + 1, column=6, padx=1, pady=1)
            self.trace_rows.append((on, fmt, ch, sc, rp))

        ttk.Label(f, foreground="gray", wraplength=330, justify="left",
                  text="Escala e Ref pos são opcionais (deixe em branco para manter). "
                       "Ref pos é a posição da linha de referência em divisões (0–8)."
                  ).grid(row=6, column=0, columnspan=7, sticky="w", pady=(8, 4))
        bar = ttk.Frame(f)
        bar.grid(row=7, column=0, columnspan=7, sticky="ew")
        ttk.Button(bar, text="Aplicar todos", command=self.apply_all_traces).pack(side="left", expand=True, fill="x")
        ttk.Button(bar, text="Desligar todos", command=lambda: self.send("trace all off")).pack(side="left", expand=True, fill="x")
        ttk.Button(bar, text="Listar", command=lambda: self.send("trace")).pack(side="left", expand=True, fill="x")
        return f

    # ---------- aba Marcadores ----------
    def _tab_markers(self, nb):
        f = ttk.Frame(nb, padding=8)
        ttk.Label(f, text="Frequência (MHz)", font=("TkDefaultFont", 8, "bold")).grid(row=0, column=1)
        self.marker_rows = []
        for m in range(1, 5):
            ttk.Label(f, text=f"M{m}").grid(row=m, column=0, padx=2)
            e = ttk.Entry(f, width=12)
            e.grid(row=m, column=1, padx=2, pady=1)
            ttk.Button(f, text="Pôr", width=4, command=lambda m=m: self.set_marker(m)).grid(row=m, column=2)
            ttk.Button(f, text="On", width=4, command=lambda m=m: self.send(f"marker {m} on")).grid(row=m, column=3)
            ttk.Button(f, text="Off", width=4, command=lambda m=m: self.send(f"marker {m} off")).grid(row=m, column=4)
            self.marker_rows.append(e)

        bar = ttk.Frame(f)
        bar.grid(row=5, column=0, columnspan=5, sticky="ew", pady=(6, 0))
        ttk.Button(bar, text="Ler valores", command=self.read_markers).pack(side="left", expand=True, fill="x")
        ttk.Button(bar, text="Mín. SWR → M1", command=self.marker_min_swr).pack(side="left", expand=True, fill="x")
        ttk.Button(bar, text="Todos off", command=lambda: self.send("marker off")).pack(side="left", expand=True, fill="x")

        self.marker_out = tk.Text(f, height=12, width=44, font=("Consolas", 9))
        self.marker_out.grid(row=6, column=0, columnspan=5, sticky="nsew", pady=(6, 0))
        f.rowconfigure(6, weight=1)
        return f

    # ---------- aba Calibracao ----------
    def _tab_cal(self, nb):
        f = ttk.Frame(nb, padding=8)
        ttk.Label(f, wraplength=340, justify="left", foreground="gray",
                  text="Sequência: Reset → conecte cada padrão na porta e clique no botão "
                       "correspondente → Done → Salvar em um slot.").pack(fill="x")

        box = ttk.LabelFrame(f, text="Passos", padding=6)
        box.pack(fill="x", pady=(6, 0))
        steps = [("1. Reset", "cal reset")] + [(f"{i + 2}. {n}", c) for i, (n, c) in enumerate(self.profile.CAL_STEPS)]
        for i, (txt, cmd) in enumerate(steps):
            ttk.Button(box, text=txt, command=lambda c=cmd: self.send(c)).grid(
                row=i // 2, column=i % 2, sticky="ew", padx=1, pady=1)
        box.columnconfigure(0, weight=1)
        box.columnconfigure(1, weight=1)

        box = ttk.LabelFrame(f, text="Correção", padding=6)
        box.pack(fill="x", pady=(6, 0))
        ttk.Button(box, text="Cal ON", command=lambda: self.send("cal on")).pack(side="left", expand=True, fill="x")
        ttk.Button(box, text="Cal OFF", command=lambda: self.send("cal off")).pack(side="left", expand=True, fill="x")
        ttk.Button(box, text="Status", command=lambda: self.send("cal")).pack(side="left", expand=True, fill="x")

        box = ttk.LabelFrame(f, text=f"Slots de calibração (0–{self.profile.CAL_SLOTS - 1})", padding=6)
        box.pack(fill="x", pady=(6, 0))
        self.slot_cb = ttk.Combobox(box, state="readonly", width=5,
                                    values=[str(i) for i in range(self.profile.CAL_SLOTS)])
        self.slot_cb.set("0")
        self.slot_cb.pack(side="left")
        ttk.Button(box, text="Salvar", command=lambda: self.slot("save", int(self.slot_cb.get()))).pack(side="left", expand=True, fill="x")
        ttk.Button(box, text="Carregar", command=lambda: self.slot("recall", int(self.slot_cb.get()))).pack(side="left", expand=True, fill="x")
        ttk.Button(box, text="Listar", command=lambda: self.send("save")).pack(side="left", expand=True, fill="x")
        return f

    # ---------- aba Gerador ----------
    def _tab_generator(self, nb):
        f = ttk.Frame(nb, padding=8)

        box = ttk.LabelFrame(f, text="Portadora fixa na porta CH0 (S11)", padding=6)
        box.pack(fill="x")
        self.gen_freq_e = self._entry(box, "Frequência (MHz)", 0, "10")
        self.gen_freq_e.bind("<Return>", lambda e: self.gen_on())
        ttk.Label(box, text="Passo").grid(row=1, column=0, sticky="w")
        self.gen_step = ttk.Combobox(box, width=10, state="readonly", values=list(STEPS))
        self.gen_step.set("100 kHz")
        self.gen_step.grid(row=1, column=1, sticky="ew", padx=4, pady=1)
        sb = ttk.Frame(box)
        sb.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        for txt, k in (("◀◀ ×10", -10), ("◀", -1), ("▶", 1), ("×10 ▶▶", 10)):
            ttk.Button(sb, text=txt, style="Small.TButton",
                       command=lambda k=k: self.gen_step_by(k)).pack(side="left", expand=True, fill="x")

        bar = ttk.Frame(box)
        bar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(bar, text="● Ligar / aplicar", command=self.gen_on).pack(side="left", expand=True, fill="x")
        ttk.Button(bar, text="■ Voltar à varredura", command=self.gen_off).pack(side="left", expand=True, fill="x")
        self.gen_status = tk.StringVar(value="Desligado (aparelho em varredura)")
        ttk.Label(box, textvariable=self.gen_status, foreground="#145aa0").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        box = ttk.LabelFrame(f, text="Drive de saída (Si5351)", padding=6)
        box.pack(fill="x", pady=(6, 0))
        for i, lbl in enumerate(["2 mA", "4 mA", "6 mA", "8 mA"]):
            ttk.Button(box, text=lbl, command=lambda i=i: self.send(f"power {i}")).pack(
                side="left", expand=True, fill="x")

        ttk.Button(f, text="📶 Abrir Medidor S21 (gera em CH0, lê em CH1)",
                   command=self.open_meter).pack(fill="x", pady=(8, 0))

        ttk.Label(f, foreground="gray", wraplength=400, justify="left",
                  text="Atenção: a saída é onda quadrada, com harmônicos fortes, e nível "
                       "aproximadamente fixo (ordem de −10 dBm). Acima de ~300 MHz o aparelho "
                       "usa o 3º harmônico: a fundamental (f/3) sai mais forte que a "
                       "frequência pedida. Use filtro se precisar de sinal limpo."
                  ).pack(fill="x", pady=(8, 0))
        return f

    def _gen_freq_hz(self):
        return int(round(self._mhz(self.gen_freq_e) * 1e6))

    def gen_step_by(self, k):
        try:
            f = self._gen_freq_hz() + k * STEPS[self.gen_step.get()]
        except ValueError:
            return
        f = max(f, 10_000)
        self.gen_freq_e.delete(0, "end")
        self.gen_freq_e.insert(0, f"{f / 1e6:.6f}".rstrip("0").rstrip("."))
        if self.cw_saved_sweep is not None:  # já ligado: aplica na hora
            self.gen_on()

    def _save_sweep_if_needed(self):
        """Guarda a faixa de varredura original (uma vez) para restaurar depois."""
        if self.cw_saved_sweep is None:
            p = self.vna.command("sweep").split()
            if len(p) >= 2 and p[0].isdigit() and p[1].isdigit() and p[0] != p[1]:
                self.cw_saved_sweep = (int(p[0]), int(p[1]))
            else:
                self.cw_saved_sweep = (1_000_000, 30_000_000)

    def gen_on(self):
        try:
            f = self._gen_freq_hz()
        except ValueError:
            messagebox.showwarning("NanoVNA", "Frequência inválida.")
            return

        def job():
            self._save_sweep_if_needed()
            self.vna.command("pause")
            self.vna.command(f"freq {f}")
            return f
        self.run_bg(job, lambda f: (self.gen_status.set(f"● Emitindo {fmt_freq(f)} (varredura pausada)"),
                                    self.log(f"Gerador: {fmt_freq(f)}")))

    def restore_sweep(self, on_done=None):
        if self.cw_saved_sweep is None:
            if on_done:
                on_done()
            return

        def job():
            a, b = self.cw_saved_sweep
            self.vna.command(f"sweep {a} {b}")
            self.vna.command("resume")
            self.cw_saved_sweep = None
            return a, b

        def done(r):
            self.gen_status.set("Desligado (aparelho em varredura)")
            self._fill_sweep_fields(*r)
            self.log(f"Varredura restaurada: {fmt_freq(r[0])} – {fmt_freq(r[1])}")
            if on_done:
                on_done()
        self.run_bg(job, done)

    def gen_off(self):
        self.restore_sweep()

    def open_meter(self):
        if not self._need_vna():
            return
        if self.meter and self.meter.winfo_exists():
            self.meter.lift()
            return
        self.meter = PowerMeterWindow(self)

    # ---------- aba Ferramentas ----------
    TOOL_LIST = [
        ("Localizador de falhas no cabo (DTF)", "Distância até conectores ruins, emendas e danos"),
        ("Cabo: perda, comprimento e stub", "Perda, VF, comprimento elétrico e corte de stubs"),
        ("Filtros e duplexadores", "Banda, notch, rejeição, máscara e sintonia ao vivo"),
        ("Medidor de componentes", "L, C, R e Q na frequência de trabalho"),
        ("Analisador de cristal", "fs, fp, Rm, Lm, Cm, C0 e Q"),
        ("Casamento de impedância", "Rede L com carta de Smith"),
        ("Histórico e comparação", "Referência da instalação × medição de hoje"),
        ("Tabela de cabos", "VF e atenuação dos cabos (editável)"),
    ]

    def _build_tools_menu(self):
        self.tool_windows = {}
        bar = tk.Menu(self)
        m = tk.Menu(bar, tearoff=False)
        for i, (name, desc) in enumerate(self.TOOL_LIST):
            if i == 7:
                m.add_separator()
            m.add_command(label=name, command=lambda i=i: self.open_tool(i))
        bar.add_cascade(label="Ferramentas", menu=m)
        h = tk.Menu(bar, tearoff=False)
        h.add_command(label="Sobre", command=lambda: messagebox.showinfo(
            "NanoVNA Mirror", f"NanoVNA Mirror\n\n{CREDIT}\n\nAs ferramentas fazem varreduras próprias em blocos de "
                              "101 pontos e devolvem a faixa original ao terminar. Calibre o NanoVNA na faixa e no "
                              "ponto de medição."))
        bar.add_cascade(label="Ajuda", menu=h)
        self.config(menu=bar)

    def open_tool(self, i):
        w = self.tool_windows.get(i)
        if w is not None and w.winfo_exists():
            w.lift()
            return
        try:
            import tools_ui
        except Exception as e:
            messagebox.showerror("Ferramentas", f"Não foi possível carregar as ferramentas:\n{e}\n\n"
                                                "Instale: pip install matplotlib")
            return
        self.tool_windows[i] = tools_ui.TOOLS[i][1](self)

    # ---------- aba Sistema ----------
    def _tab_system(self, nb):
        if not self.profile.HAS_NANOVNA_SYSTEM:
            return self._tab_system_sv(nb)
        f = ttk.Frame(nb, padding=8)

        box = ttk.LabelFrame(f, text="Electrical delay", padding=6)
        box.pack(fill="x")
        self.edelay_e = ttk.Entry(box, width=10)
        self.edelay_e.insert(0, "0")
        self.edelay_e.pack(side="left")
        ttk.Label(box, text="ps").pack(side="left", padx=4)
        ttk.Button(box, text="Aplicar",
                   command=lambda: self.send(f"edelay {self.edelay_e.get().replace(',', '.')}")).pack(side="left")

        box = ttk.LabelFrame(f, text="Domínio do tempo (TDR)", padding=6)
        box.pack(fill="x", pady=(6, 0))
        rows = [[("ON", "transform on"), ("OFF", "transform off")],
                [("Impulse", "transform impulse"), ("Step", "transform step"), ("Bandpass", "transform bandpass")],
                [("Janela mín.", "transform minimum"), ("normal", "transform normal"), ("máx.", "transform maximum")]]
        for r in rows:
            rf = ttk.Frame(box)
            rf.pack(fill="x")
            for txt, cmd in r:
                ttk.Button(rf, text=txt, style="Small.TButton",
                           command=lambda c=cmd: self.send(c)).pack(side="left", expand=True, fill="x")

        box = ttk.LabelFrame(f, text="Exportar medição", padding=6)
        box.pack(fill="x", pady=(6, 0))
        ttk.Button(box, text="Touchstone .s1p (S11)", command=lambda: self.export_touchstone(1)).pack(fill="x")
        ttk.Button(box, text="Touchstone .s2p (S11 + S21)", command=lambda: self.export_touchstone(2)).pack(fill="x", pady=(2, 0))

        box = ttk.LabelFrame(f, text="Aparelho", padding=6)
        box.pack(fill="x", pady=(6, 0))
        for txt, cmd in (("Bateria", "vbat"), ("Versão", "version"), ("Info", "info")):
            ttk.Button(box, text=txt, command=lambda c=cmd: self.send(c)).pack(side="left", expand=True, fill="x")
        ttk.Button(box, text="Salvar config", command=lambda: self.send("saveconfig")).pack(side="left", expand=True, fill="x")
        return f

    def _tab_system_sv(self, nb):
        f = ttk.Frame(nb, padding=8)
        box = ttk.LabelFrame(f, text="Toque remoto na tela", padding=6)
        box.pack(fill="x")
        ttk.Label(box, wraplength=380, justify="left", foreground="gray",
                  text="Clique e arraste na imagem da tela para operar o aparelho. "
                       "A sintaxe do comando 'touch' é perguntada ao firmware ao conectar; "
                       "se o toque não funcionar, teste outro formato.").pack(fill="x")
        rf = ttk.Frame(box)
        rf.pack(fill="x", pady=(4, 0))
        ttk.Label(rf, text="Formato:").pack(side="left")
        self.touch_fmt_cb = ttk.Combobox(rf, state="readonly", width=26, values=list(self.profile.TOUCH_FORMATS))
        self.touch_fmt_cb.set(self.profile.TOUCH_FORMATS[0])
        self.touch_fmt_cb.pack(side="left", padx=4)
        self.touch_fmt_cb.bind("<<ComboboxSelected>>", lambda e: self._set_touch_format())
        ttk.Button(rf, text="Sintaxe", command=lambda: self.send("touch")).pack(side="left")

        box = ttk.LabelFrame(f, text="Exportar medição", padding=6)
        box.pack(fill="x", pady=(6, 0))
        ttk.Button(box, text="Touchstone .s1p (S11)", command=lambda: self.export_touchstone(1)).pack(fill="x")
        ttk.Button(box, text="Touchstone .s2p (S11 + S21)", command=lambda: self.export_touchstone(2)).pack(fill="x", pady=(2, 0))

        box = ttk.LabelFrame(f, text="Aparelho", padding=6)
        box.pack(fill="x", pady=(6, 0))
        for txt, cmd in (("Versão", "version"), ("Info", "info"), ("Nº de série", "sn"),
                         ("Resolução", "resolution"), ("Bateria", "vbat")):
            ttk.Button(box, text=txt, style="Small.TButton", command=lambda c=cmd: self.send(c)).pack(side="left", expand=True, fill="x")
        return f

    def _set_touch_format(self):
        if self.vna and hasattr(self.vna, "touch_format"):
            self.vna.touch_format = self.touch_fmt_cb.get()
            self.log(f"Formato do toque: {self.vna.touch_format}")

    def _entry(self, parent, label, row, default):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        e = ttk.Entry(parent, width=12)
        e.insert(0, default)
        e.grid(row=row, column=1, padx=4, pady=1, sticky="ew")
        parent.columnconfigure(1, weight=1)
        return e

    # =======================================================================
    # Conexao
    # =======================================================================
    def refresh_ports(self):
        ports = list(serial.tools.list_ports.comports())
        self.port_cb["values"] = [f"{p.device} - {p.description}" for p in ports]
        self._ports = [p.device for p in ports]
        idx = next((i for i, p in enumerate(ports) if (p.vid, p.pid) == NANOVNA_VID_PID), 0)
        if ports:
            self.port_cb.current(idx)

    def toggle_connect(self):
        if self.vna:
            self.disconnect()
            return
        if not self._ports:
            messagebox.showwarning("NanoVNA", "Nenhuma porta serial encontrada.")
            return
        port = self._ports[self.port_cb.current()]
        try:
            self.vna = self.profile(port)
            ver = self.vna.command("version")
            info = self.vna.info_text()
        except Exception as e:
            self.vna = None
            messagebox.showerror(self.profile.APP_NAME, f"Falha ao conectar em {port}:\n{e}")
            return
        self.log(f"Conectado em {port} — firmware {ver}")
        if self.profile.INFO_MATCH and not any(m in info for m in self.profile.INFO_MATCH):
            self.log(f"AVISO: o aparelho não se identificou como {self.profile.NAME}. "
                     f"Este programa é para o {self.profile.NAME}; verifique a porta.")
        if self.profile.HAS_TOUCH:
            usage = self.vna.touch_usage()
            self.log(f"Toque — resposta do firmware a 'touch': {usage or '(vazia)'}")
            u = usage.lower()
            if "x y" in u or "<x> <y>" in u:
                self.touch_fmt_cb.set(self.profile.TOUCH_FORMATS[1])
            self._set_touch_format()
        self.conn_btn.config(text="Desconectar")
        self.running = True
        self.mirror_btn.config(text="⏸ Espelhamento")
        threading.Thread(target=self._capture_loop, daemon=True).start()
        self.read_state()

    def _shutdown_vna(self):
        """Fecha o medidor, devolve o aparelho à varredura normal e fecha a porta."""
        if self.meter and self.meter.winfo_exists():
            self.meter.stop(restore=False)
        self.running = False
        vna, self.vna = self.vna, None
        time.sleep(0.2)
        if vna:
            if self.cw_saved_sweep:
                try:
                    a, b = self.cw_saved_sweep
                    vna.command(f"sweep {a} {b}")
                    vna.command("resume")
                except Exception:
                    pass
            vna.close()
        self.cw_saved_sweep = None

    def disconnect(self):
        self._shutdown_vna()
        self.gen_status.set("Desligado (aparelho em varredura)")
        self.touch_queue.clear()
        self.conn_btn.config(text="Conectar")
        self.fps_text.set("Desconectado")
        self.log("Desconectado")

    # =======================================================================
    # Espelhamento
    # =======================================================================
    def _capture_loop(self):
        t0, frames = time.time(), 0
        fails = 0
        while self.vna:
            if not self.running:
                time.sleep(0.1)
                t0, frames = time.time(), 0
                continue
            try:
                self._flush_touch()
                img = self.vna.capture()
                fails = 0
            except Exception as e:
                if not self.vna:
                    return
                fails += 1
                if fails > 3:
                    self.after(0, self._on_error, e)
                    return
                self.after(0, self.log, f"Falha na porta ({e}); reconectando ({fails}/3)...")
                time.sleep(1.0 * fails)
                try:
                    if self.vna:
                        self.vna.reopen()
                        self.after(0, self.log, "Reconectado.")
                except Exception as e2:
                    self.after(0, self.log, f"  ainda sem resposta: {e2}")
                continue
            self.last_image = img
            self.after(0, self._redraw)
            frames += 1
            dt = time.time() - t0
            if dt >= 1.0:
                self.after(0, self.fps_text.set, f"{frames / dt:.1f} fps")
                t0, frames = time.time(), 0

    def _on_error(self, e):
        self.log(f"Erro: {e}")
        self.disconnect()

    def toggle_mirror(self):
        self.running = not self.running
        self.mirror_btn.config(text="⏸ Espelhamento" if self.running else "▶ Espelhamento")
        if not self.running and self.vna:
            self.fps_text.set("Espelhamento pausado")

    def _show_blank(self):
        self.last_image = Image.new("RGB", (self.sw, self.sh), "black")
        self._redraw()

    # ---------- toque remoto ----------
    def _on_touch(self, e, kind):
        if not self.vna or not self._img_geom:
            return
        x0, y0, s = self._img_geom
        x, y = (e.x - x0) / s, (e.y - y0) / s
        if not (0 <= x < self.sw and 0 <= y < self.sh):
            return
        if kind == "move":
            now = time.time()
            if now - self._drag_last < 0.12:
                return
            self._drag_last = now
            kind = "down"
        self.touch_queue.append((int(round(x)), int(round(y)), kind))
        self.canvas.delete("touchmark")
        r = 7
        self.canvas.create_oval(e.x - r, e.y - r, e.x + r, e.y + r, outline="#ffcc00", width=2, tags="touchmark")
        if not self.running:          # sem espelhamento: envia direto
            self.run_bg(lambda: self._flush_touch())

    def _flush_touch(self):
        while self.touch_queue:
            x, y, kind = self.touch_queue.pop(0)
            try:
                self.vna.touch(x, y, kind)
            except Exception as ex:
                self.after(0, self.log, f"Toque: {ex}")
                self.touch_queue.clear()

    def _redraw(self):
        if self.last_image is None:
            return
        cw, ch = max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)
        z = self.zoom.get()
        if z == "Ajustar":
            s = min(cw / self.sw, ch / self.sh)
        else:
            s = float(z.rstrip("x"))
        w, h = max(int(self.sw * s), 1), max(int(self.sh * s), 1)
        self._img_geom = (cw // 2 - w / 2, ch // 2 - h / 2, w / self.sw)
        # escala inteira: pixels nítidos; fracionaria: suavizado
        method = Image.NEAREST if abs(s - round(s)) < 1e-6 else (Image.LANCZOS if s < 1 else Image.BILINEAR)
        self._tkimg = ImageTk.PhotoImage(self.last_image.resize((w, h), method))
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2, image=self._tkimg, anchor="center")

    def save_png(self):
        if self.last_image is None:
            return
        name = time.strftime(f"{self.profile.NAME.lower().replace('-', '')}_%Y%m%d_%H%M%S.png")
        path = filedialog.asksaveasfilename(defaultextension=".png", initialfile=name,
                                            filetypes=[("PNG", "*.png")])
        if path:
            k = self.profile.SNAPSHOT_SCALE
            big = self.last_image.resize((self.sw * k, self.sh * k), Image.NEAREST)
            big.save(path)
            self.log(f"Imagem salva ({big.width}×{big.height}): {path}")

    # =======================================================================
    # Relatorio
    # =======================================================================
    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save_settings(self, data):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def open_report(self):
        if not self._need_vna():
            return
        ReportDialog(self, self._load_settings())

    def build_report(self, meta, include_s21, path):
        """Coleta os dados do aparelho e gera o relatório (PNG ou PDF)."""
        meta = dict(meta)
        self._save_settings({k: meta[k] for k in ("title", "dut", "location", "operator")})

        def job():
            screen = self.vna.capture()
            firmware = self.vna.command("version")
            mk = self.vna.command("marker")
            freqs, s11, s21 = self._fetch_all()
            markers = []
            for line in mk.splitlines():
                p = line.split()
                if len(p) >= 2 and p[0].isdigit() and p[1].isdigit():
                    markers.append((int(p[0]), int(p[1])))
            meta["firmware"] = firmware.splitlines()[0] if firmware else "?"
            meta["instrumento"] = self.profile.NAME
            meta["app"] = self.profile.APP_NAME
            meta["timestamp"] = time.localtime()
            img = compose_report(screen, meta, freqs, s11, s21, markers, include_s21)
            if path.lower().endswith(".pdf"):
                img.save(path, "PDF", resolution=200.0)
            else:
                img.save(path)
            return img.size

        self.run_bg(job, lambda size: self.log(f"Relatório gerado ({size[0]}×{size[1]}): {path}"))

    # =======================================================================
    # Comandos genericos
    # =======================================================================
    def _need_vna(self):
        if not self.vna:
            messagebox.showinfo("NanoVNA", "Conecte o NanoVNA primeiro.")
            return False
        return True

    def run_bg(self, func, on_done=None):
        """Executa func() numa thread; on_done(resultado) roda na thread da interface."""
        if not self._need_vna():
            return

        def worker():
            try:
                res = func()
            except Exception as e:
                self.after(0, self.log, f"Erro: {e}")
                return
            if on_done:
                self.after(0, on_done, res)

        threading.Thread(target=worker, daemon=True).start()

    def send(self, *cmds):
        """Envia um ou mais comandos em sequência e mostra as respostas no console."""
        def job():
            out = []
            for c in cmds:
                r = self.vna.command(c)
                out.append(f"> {c}" + (f"\n{r}" if r else ""))
            return "\n".join(out)
        self.run_bg(job, self.log)

    def send_console(self):
        cmd = self.cmd_e.get().strip()
        if not cmd:
            return
        if cmd.split()[0] == "capture":
            self.log("O espelhamento já usa 'capture'; não é preciso enviar pelo console.")
            return
        self.cmd_e.delete(0, "end")
        self.send(cmd)

    def log(self, text):
        self.out.insert("end", text + "\n")
        self.out.see("end")

    @staticmethod
    def _mhz(entry):
        return float(entry.get().strip().replace(",", "."))

    # =======================================================================
    # Estimulo
    # =======================================================================
    def set_sweep(self, start_mhz, stop_mhz):
        if start_mhz >= stop_mhz:
            messagebox.showwarning("NanoVNA", "O início deve ser menor que o fim.")
            return
        a, b = int(round(start_mhz * 1e6)), int(round(stop_mhz * 1e6))
        self._fill_sweep_fields(a, b)
        pts = f" {self.points_cb.get()}" if getattr(self, "points_cb", None) else ""
        self.send(f"sweep {a} {b}{pts}")

    def apply_start_stop(self):
        try:
            self.set_sweep(self._mhz(self.start_e), self._mhz(self.stop_e))
        except ValueError:
            messagebox.showwarning("NanoVNA", "Frequências inválidas.")

    def apply_center_span(self):
        try:
            c, s = self._mhz(self.center_e), self._mhz(self.span_e)
        except ValueError:
            messagebox.showwarning("NanoVNA", "Frequências inválidas.")
            return
        if s <= 0:
            messagebox.showwarning("NanoVNA", "O span deve ser maior que zero.")
            return
        self.set_sweep(max(c - s / 2, 0.01), c + s / 2)

    def _fill_sweep_fields(self, a, b):
        for e, v in ((self.start_e, a / 1e6), (self.stop_e, b / 1e6),
                     (self.center_e, (a + b) / 2e6), (self.span_e, (b - a) / 1e6)):
            e.delete(0, "end")
            e.insert(0, f"{v:.6g}")

    def read_state(self):
        """Lê faixa de varredura, traces e marcadores do aparelho."""
        keys = ("sweep", "trace", "marker") if self.profile.HAS_TRACES else ("sweep", "marker")

        def job():
            return {k: self.vna.command(k) for k in keys}

        def done(r):
            parts = r["sweep"].split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                self._fill_sweep_fields(int(parts[0]), int(parts[1]))
            if len(parts) >= 3 and getattr(self, "points_cb", None) and parts[2] in self.points_cb["values"]:
                self.points_cb.set(parts[2])
            txt = f"Varredura: {r['sweep']}\n"
            if "trace" in r:
                self._parse_traces(r["trace"])
                txt += f"Traces:\n{r['trace']}\n"
            self.log(txt + f"Marcadores:\n{r['marker'] or '(nenhum)'}")
        self.run_bg(job, done)

    # =======================================================================
    # Traces
    # =======================================================================
    def _parse_traces(self, text):
        """Formato típico: '0 LOGMAG CH0 10.000000 7.000000'"""
        for line in text.splitlines():
            p = line.split()
            if len(p) < 3 or not p[0].isdigit():
                continue
            t = int(p[0])
            if t >= len(self.trace_rows):
                continue
            on, fmt, ch, sc, rp = self.trace_rows[t]
            if p[1].lower() in TRACE_FORMATS:
                fmt.set(p[1].lower())
            on.set(True)
            if p[2].upper() in ("CH0", "CH1"):
                ch.set(CHANNELS[int(p[2][-1])])
            if len(p) >= 5:
                for e, v in ((sc, p[3]), (rp, p[4])):
                    e.delete(0, "end")
                    try:
                        e.insert(0, f"{float(v):g}")
                    except ValueError:
                        pass

    def _trace_cmds(self, t):
        on, fmt, ch, sc, rp = self.trace_rows[t]
        if not on.get():
            return [f"trace {t} off"]
        cmds = [f"trace {t} {fmt.get()} {CHANNELS.index(ch.get())}"]
        s = sc.get().strip().replace(",", ".")
        r = rp.get().strip().replace(",", ".")
        if s:
            cmds.append(f"trace {t} scale {s}")
        if r:
            cmds.append(f"trace {t} refpos {r}")
        return cmds

    def apply_trace(self, t):
        self.send(*self._trace_cmds(t))

    def apply_all_traces(self):
        cmds = []
        for t in range(4):
            cmds += self._trace_cmds(t)
        self.send(*cmds)

    # =======================================================================
    # Marcadores
    # =======================================================================
    def set_marker(self, m):
        try:
            f_target = self._mhz(self.marker_rows[m - 1]) * 1e6
        except ValueError:
            messagebox.showwarning("NanoVNA", "Frequência do marcador inválida.")
            return

        def job():
            freqs = self.vna.frequencies()
            if not freqs:
                raise IOError("Não foi possível ler a lista de frequências")
            idx = min(range(len(freqs)), key=lambda i: abs(freqs[i] - f_target))
            self.vna.command(f"marker {m} on")
            self.vna.command(f"marker {m} {idx}")
            return idx, freqs[idx]

        self.run_bg(job, lambda r: self.log(f"M{m} → ponto {r[0]} ({fmt_freq(r[1])})"))

    def _fetch_all(self):
        freqs = self.vna.frequencies()
        s11 = self.vna.data(0)
        s21 = self.vna.data(1)
        n = min(len(freqs), len(s11), len(s21))
        return freqs[:n], s11[:n], s21[:n]

    def read_markers(self):
        def job():
            mk = self.vna.command("marker")
            freqs, s11, s21 = self._fetch_all()
            return mk, freqs, s11, s21

        def done(r):
            mk, freqs, s11, s21 = r
            lines = []
            for line in mk.splitlines():
                p = line.split()
                if len(p) >= 2 and p[0].isdigit() and p[1].isdigit():
                    m, idx = int(p[0]), int(p[1])
                    if idx < len(freqs):
                        f = freqs[idx]
                        lines.append(f"M{m}  {fmt_freq(f)}  (ponto {idx})")
                        lines.append("    " + s11_summary(s11[idx], f))
                        lines.append("    " + s21_summary(s21[idx]))
                        lines.append("")
                        if m <= 4:
                            e = self.marker_rows[m - 1]
                            e.delete(0, "end")
                            e.insert(0, f"{f / 1e6:.6g}")
            self.marker_out.delete("1.0", "end")
            self.marker_out.insert("end", "\n".join(lines) if lines else "Nenhum marcador ativo.")
        self.run_bg(job, done)

    def marker_min_swr(self):
        def job():
            freqs, s11, _ = self._fetch_all()
            if not freqs:
                raise IOError("Sem dados de medição")
            idx = min(range(len(s11)), key=lambda i: abs(s11[i]))
            self.vna.command("marker 1 on")
            self.vna.command(f"marker 1 {idx}")
            return idx, freqs[idx], s11[idx]

        def done(r):
            idx, f, g = r
            self.log(f"Mínimo SWR em {fmt_freq(f)} (ponto {idx})\n" + s11_summary(g, f))
            self.read_markers()
        self.run_bg(job, done)

    # =======================================================================
    # Calibracao
    # =======================================================================
    def slot(self, verb, i):
        if verb == "save" and not messagebox.askyesno(
                "NanoVNA", f"Salvar a calibração atual no slot {i}?\nO conteúdo anterior será substituído."):
            return
        self.send(f"{verb} {i}")
        if verb == "recall":
            self.after(800, self.read_state)

    # =======================================================================
    # Exportacao
    # =======================================================================
    def export_touchstone(self, ports):
        ext = f".s{ports}p"
        name = time.strftime(f"nanovna_%Y%m%d_%H%M%S{ext}")
        path = filedialog.asksaveasfilename(defaultextension=ext, initialfile=name,
                                            filetypes=[(f"Touchstone {ext}", f"*{ext}")])
        if not path:
            return

        def done(r):
            freqs, s11, s21 = r
            with open(path, "w", encoding="ascii") as fh:
                fh.write(f"! {self.profile.NAME} - exportado por {self.profile.APP_NAME} em {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                fh.write("# Hz S RI R 50\n")
                for f, a, b in zip(freqs, s11, s21):
                    if ports == 1:
                        fh.write(f"{f} {a.real:.9g} {a.imag:.9g}\n")
                    else:
                        # S12 e S22 não são medidos pelo NanoVNA (gravados como zero)
                        fh.write(f"{f} {a.real:.9g} {a.imag:.9g} {b.real:.9g} {b.imag:.9g} 0 0 0 0\n")
            self.log(f"Exportado {len(freqs)} pontos: {path}")
        self.run_bg(self._fetch_all, done)

    # =======================================================================
    def on_close(self):
        self._shutdown_vna()
        self.destroy()


def _mix(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


class PowerMeterWindow(tk.Toplevel):
    """
    Medidor estilo power meter: o NanoVNA gera na porta CH0 e lê na CH1.
    Usa varredura de span zero (início = fim) e calcula S21 médio dos 101 pontos.
    """
    BG, PANEL, INK, MUTED, ACCENT, GRID = "#0e1116", "#161b22", "#e6edf3", "#8b949e", "#3fb950", "#2d333b"
    PEAK = "#f0883e"

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title(f"Medidor S21 — {app.profile.NAME}")
        self.geometry("1000x740")
        self.minsize(860, 640)
        self.configure(background=self.BG)
        self.protocol("WM_DELETE_WINDOW", lambda: self.stop(restore=True))

        self.active = True
        self.hold = False
        self.freq_hz = None
        self.pending_freq = None
        self.skip = 0
        self.zero_db = 0.0
        self.history = []
        self.window = []
        self.stats = None
        self.rate = 0.0
        self.last = None
        self.csv = None
        self.needle = None          # valor mostrado pelo ponteiro (com inércia)
        self._t_tick = time.time()

        self.unit = tk.StringVar(value="dB")
        self.avg_n = tk.StringVar(value="4")
        self.scale_v = tk.StringVar(value="−100 … +10")
        self._build()

        # pausa o espelhamento para acelerar as leituras
        self.mirror_was_running = app.running
        if app.running:
            app.toggle_mirror()

        try:
            f0 = app._gen_freq_hz()
        except Exception:          # aparelho sem a aba Gerador
            f0 = 10_000_000
        self.freq_e.insert(0, f"{f0 / 1e6:.6f}".rstrip("0").rstrip("."))
        self.set_freq()
        threading.Thread(target=self._loop, daemon=True).start()
        self._tick()

    # ------------------------------------------------------------------
    def _build(self):
        ctl = ttk.Frame(self, padding=8)
        ctl.pack(fill="x")

        r1 = ttk.Frame(ctl)
        r1.pack(fill="x")
        ttk.Label(r1, text="Frequência (MHz)").pack(side="left")
        self.freq_e = ttk.Entry(r1, width=14)
        self.freq_e.pack(side="left", padx=4)
        self.freq_e.bind("<Return>", lambda e: self.set_freq())
        ttk.Button(r1, text="Aplicar", command=self.set_freq).pack(side="left")
        ttk.Label(r1, text="Passo").pack(side="left", padx=(12, 2))
        self.step = ttk.Combobox(r1, width=8, state="readonly", values=list(STEPS))
        self.step.set("100 kHz")
        self.step.pack(side="left")
        for txt, k in (("◀◀", -10), ("◀", -1), ("▶", 1), ("▶▶", 10)):
            ttk.Button(r1, text=txt, width=4, command=lambda k=k: self.step_by(k)).pack(side="left", padx=1)

        r2 = ttk.Frame(ctl)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="Unidade").pack(side="left")
        ttk.Radiobutton(r2, text="dB (S21)", value="dB", variable=self.unit).pack(side="left", padx=2)
        ttk.Radiobutton(r2, text="dBm", value="dBm", variable=self.unit).pack(side="left", padx=2)
        ttk.Radiobutton(r2, text="W", value="W", variable=self.unit).pack(side="left", padx=2)
        ttk.Radiobutton(r2, text="V (50 Ω)", value="V", variable=self.unit).pack(side="left", padx=2)
        ttk.Label(r2, text="Nível da fonte (dBm)").pack(side="left", padx=(8, 2))
        self.src_e = ttk.Entry(r2, width=7)
        self.src_e.insert(0, "-10")
        self.src_e.pack(side="left")
        ttk.Label(r2, text="Média").pack(side="left", padx=(12, 2))
        ttk.Combobox(r2, width=4, state="readonly", textvariable=self.avg_n,
                     values=["1", "4", "8", "16"]).pack(side="left")
        ttk.Label(r2, text="Escala").pack(side="left", padx=(12, 2))
        ttk.Combobox(r2, width=11, state="readonly", textvariable=self.scale_v,
                     values=["−100 … +10", "−60 … 0", "−30 … 0", "−10 … +10", "Auto"]).pack(side="left")

        self.cv = tk.Canvas(self, background=self.BG, highlightthickness=0)
        self.cv.pack(fill="both", expand=True, padx=8)

        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="Zerar (com thru)", command=self.set_zero).pack(side="left")
        ttk.Button(bar, text="Remover zero", command=self.clear_zero).pack(side="left", padx=4)
        ttk.Button(bar, text="Limpar estatística", command=self.reset_stats).pack(side="left")
        self.hold_btn = ttk.Button(bar, text="⏸ Hold", command=self.toggle_hold)
        self.hold_btn.pack(side="left", padx=4)
        self.csv_btn = ttk.Button(bar, text="● Gravar CSV", command=self.toggle_csv)
        self.csv_btn.pack(side="left")
        ttk.Button(bar, text="Fechar", command=lambda: self.stop(restore=True)).pack(side="right")

        self.bind("<Left>", lambda e: self._key_step(-1))
        self.bind("<Right>", lambda e: self._key_step(1))
        self.bind("<Shift-Left>", lambda e: self._key_step(-10))
        self.bind("<Shift-Right>", lambda e: self._key_step(10))
        self.cv.bind("<Button-1>", lambda e: self.cv.focus_set())

    # ------------------------------------------------------------------
    def set_freq(self):
        try:
            f = int(round(float(self.freq_e.get().replace(",", ".")) * 1e6))
        except ValueError:
            messagebox.showwarning("Medidor", "Frequência inválida.", parent=self)
            return
        self.pending_freq = max(f, 10_000)

    def _key_step(self, k):
        # setas mudam a frequência, exceto quando o cursor está num campo de texto
        if not isinstance(self.focus_get(), (ttk.Entry, tk.Entry, ttk.Combobox)):
            self.step_by(k)

    def step_by(self, k):
        base = self.pending_freq or self.freq_hz or 10_000_000
        f = max(base + k * STEPS[self.step.get()], 10_000)
        self.freq_e.delete(0, "end")
        self.freq_e.insert(0, f"{f / 1e6:.6f}".rstrip("0").rstrip("."))
        self.pending_freq = f

    def set_zero(self):
        if self.last is None:
            return
        self.zero_db = self.last["raw_db"]
        self.reset_stats()

    def clear_zero(self):
        self.zero_db = 0.0
        self.reset_stats()

    def reset_stats(self):
        self.history.clear()
        self.window.clear()
        self.stats = None

    def toggle_hold(self):
        self.hold = not self.hold
        self.hold_btn.config(text="▶ Continuar" if self.hold else "⏸ Hold")

    def toggle_csv(self):
        if self.csv:
            self.csv.close()
            self.csv = None
            self.csv_btn.config(text="● Gravar CSV")
            return
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv",
            initialfile=time.strftime("medidor_s21_%Y%m%d_%H%M%S.csv"), filetypes=[("CSV", "*.csv")])
        if not path:
            return
        self.csv = open(path, "w", encoding="utf-8", newline="")
        self.csv.write("data_hora;frequencia_hz;s21_bruto_db;zero_db;leitura_db_ou_dbm;unidade;potencia_w;tensao_vrms_50ohm\n")
        self.csv_btn.config(text="■ Parar CSV")
        self.app.log(f"Gravando medidor em: {path}")

    def _source_dbm(self):
        try:
            return float(self.src_e.get().replace(",", ".").replace("−", "-"))
        except ValueError:
            return 0.0

    # ------------------------------------------------------------------
    def _loop(self):
        t0, n = time.time(), 0
        while self.active:
            vna = self.app.vna
            if vna is None:
                return
            try:
                if self.pending_freq is not None:
                    f, self.pending_freq = self.pending_freq, None
                    self.app._save_sweep_if_needed()
                    vna.meter_set(f)
                    self.freq_hz = f
                    self.skip = 2  # descarta leituras da varredura anterior
                    self.reset_stats()
                    self.after(0, self.app.gen_status.set,
                               f"● Medidor ativo em {fmt_freq(f)} (span zero)")
                if self.hold or self.freq_hz is None:
                    time.sleep(0.1)
                    continue
                s21 = vna.meter_read()
            except Exception as e:
                if self.active:
                    self.after(0, self.app.log, f"Medidor: {e}")
                time.sleep(0.5)
                continue
            if not s21:
                continue
            if self.skip:
                self.skip -= 1
                time.sleep(0.15)
                continue
            mags = [abs(s) for s in s21]
            lin = sum(mags) / len(mags)
            raw_db = 20 * math.log10(lin) if lin > 0 else -150.0
            pts_db = [20 * math.log10(m) for m in mags if m > 0]
            sigma = float(np.std(pts_db)) if len(pts_db) > 1 else 0.0
            self._push(raw_db, sigma)
            n += 1
            dt = time.time() - t0
            if dt >= 1.0:
                self.rate, t0, n = n / dt, time.time(), 0
            time.sleep(0.05)

    def _push(self, raw_db, sigma):
        try:
            N = int(self.avg_n.get())
        except ValueError:
            N = 1
        self.window.append(raw_db)
        del self.window[:-N]
        # média em potência (linear), não em dB
        avg_db = 10 * math.log10(sum(10 ** (v / 10) for v in self.window) / len(self.window))
        rel = avg_db - self.zero_db
        self.history.append(rel)
        del self.history[:-300]
        s = self.stats
        self.stats = {"min": rel, "max": rel, "sum": rel, "n": 1} if s is None else {
            "min": min(s["min"], rel), "max": max(s["max"], rel), "sum": s["sum"] + rel, "n": s["n"] + 1}
        self.last = {"raw_db": raw_db, "rel": rel, "sigma": sigma, "freq": self.freq_hz}
        if self.csv:
            absolute = self.unit.get() != "dB"
            off = self._source_dbm() if absolute else 0.0
            extra = f"{rt.dbm_to_w(rel + off):.6e};{rt.dbm_to_vrms(rel + off):.6e}" if absolute else ";"
            self.csv.write((f"{time.strftime('%Y-%m-%d %H:%M:%S')};{self.freq_hz};{raw_db:.3f};"
                            f"{self.zero_db:.3f};{rel + off:.3f};{'dB' if not absolute else 'dBm'};{extra}\n").replace(".", ","))

    # ------------------------------------------------------------------
    def _scale(self, off):
        v = self.scale_v.get()
        if v == "Auto" and self.history:
            lo, hi = min(self.history) + off, max(self.history) + off
            pad = max((hi - lo) * 0.2, 2)
            return math.floor((lo - pad) / 5) * 5, math.ceil((hi + pad) / 5) * 5
        table = {"−100 … +10": (-100, 10), "−60 … 0": (-60, 0), "−30 … 0": (-30, 0),
                 "−10 … +10": (-10, 10)}
        lo, hi = table.get(v, (-100, 10))
        return lo + off, hi + off

    def _absolute(self):
        return self.unit.get() != "dB"

    def _offset(self):
        return self._source_dbm() if self._absolute() else 0.0

    def _fmt(self, v):
        """Formata um valor em dB/dBm na unidade escolhida."""
        u = self.unit.get()
        if u == "W":
            return rt.fmt_si(rt.dbm_to_w(v), "W")
        if u == "V":
            return rt.fmt_si(rt.dbm_to_vrms(v), "V")
        return f"{v:+.2f}".replace(".", ",")

    def _tick(self):
        if not self.active:
            return
        now = time.time()
        dt, self._t_tick = now - self._t_tick, now
        if self.last is not None:
            target = self.last["rel"] + self._offset()
            if self.needle is None or abs(target - self.needle) > 200:
                self.needle = target
            else:
                # balística de VU: ~300 ms para chegar a 99 %
                self.needle += (target - self.needle) * (1 - math.exp(-dt / 0.065))
        try:
            self._draw()
        except tk.TclError:
            return
        self.after(40, self._tick)

    # ------------------------------------------------------------------
    VU_FACE, VU_FACE2, VU_INK, VU_RED, VU_BEZEL = "#f6eed8", "#e6d8b2", "#2b2620", "#c0392b", "#05070a"

    def _draw(self):
        c = self.cv
        c.delete("all")
        W, H = max(c.winfo_width(), 300), max(c.winfo_height(), 300)
        off = self._offset()
        c.create_rectangle(0, 0, W, H, fill=self.BG, outline="")

        # cabeçalho
        fq = fmt_freq(self.freq_hz) if self.freq_hz else "—"
        c.create_text(16, 12, anchor="nw", fill=self.MUTED, font=("Segoe UI", 11),
                      text=f"S21  ·  gera CH0 → lê CH1  ·  {fq}")
        tags = []
        if self.zero_db:
            tags.append(f"ZERO {self.zero_db:+.2f} dB")
        if self.hold:
            tags.append("HOLD")
        if self.csv:
            tags.append("● CSV")
        if self._absolute():
            tags.append(f"fonte {off:+.1f} dBm".replace(".", ","))
        c.create_text(W - 16, 12, anchor="ne", fill=self.PEAK, font=("Segoe UI", 11, "bold"),
                      text="   ".join(tags))

        top, bottom = 40, int(H * 0.60)
        vu_w = int(W * 0.55)
        self._draw_vu(16, top, 16 + vu_w, bottom, off)
        self._draw_digital(16 + vu_w + 20, top, W - 16, bottom, off)
        sy = bottom + 18
        self._draw_stats(16, sy, W - 16, off)
        self._draw_history(16, sy + 62, W - 16, H - 12, off)

    def _draw_vu(self, x0, y0, x1, y1, off):
        c = self.cv
        # moldura e mostrador com "luz" quente (faixas em degradê)
        c.create_rectangle(x0, y0, x1, y1, fill=self.VU_BEZEL, outline="#30363d", width=2)
        fx0, fy0, fx1, fy1 = x0 + 10, y0 + 10, x1 - 10, y1 - 10
        bands = 24
        for k in range(bands):
            t = k / (bands - 1)
            col = _mix(self.VU_FACE, self.VU_FACE2, t ** 1.6)
            ya = fy0 + (fy1 - fy0) * k / bands
            yb = fy0 + (fy1 - fy0) * (k + 1) / bands + 1
            c.create_rectangle(fx0, ya, fx1, yb, fill=col, outline="")
        wf, hf = fx1 - fx0, fy1 - fy0
        cx = (fx0 + fx1) / 2
        cy = fy1 + hf * 0.22                     # pivô abaixo do mostrador
        span = math.radians(48)
        R = min(cy - fy0 - hf * 0.20, (wf / 2 - 34) / math.sin(span))
        lo, hi = self._scale(off)

        def ang(v):
            t = min(max((v - lo) / (hi - lo), -0.03), 1.03)
            return -span + 2 * span * t          # 0 = vertical

        def pt(a, r):
            return cx + r * math.sin(a), cy - r * math.cos(a)

        def arc(a0, a1, r, **kw):
            n = max(int(abs(a1 - a0) / 0.02), 2)
            pts = []
            for i in range(n + 1):
                pts += pt(a0 + (a1 - a0) * i / n, r)
            c.create_line(*pts, **kw)

        red_from = hi - (hi - lo) * 0.18
        arc(ang(lo), ang(red_from), R, fill=self.VU_INK, width=2)
        arc(ang(red_from), ang(hi), R, fill=self.VU_RED, width=2)
        arc(ang(red_from), ang(hi), R + 5, fill=self.VU_RED, width=8)

        rng = hi - lo
        step = 10 if rng > 40 else (5 if rng > 15 else 2)
        minor = step / 5 if step != 5 else 1
        v = math.ceil(lo / minor) * minor
        f_lab = ("Segoe UI", max(9, int(hf / 22)), "bold")
        while v <= hi + 1e-9:
            a = ang(v)
            major = abs(v / step - round(v / step)) < 1e-6
            col = self.VU_RED if v >= red_from else self.VU_INK
            r_in = R - (12 if major else 6)
            c.create_line(*pt(a, r_in), *pt(a, R), fill=col, width=2 if major else 1)
            if major:
                c.create_text(*pt(a, R + 18), text=f"{v:g}".replace("-", "−"), fill=col, font=f_lab)
            v += minor

        # escala interna em potência (só com unidade absoluta)
        if self._absolute():
            f_in = ("Segoe UI", max(8, int(hf / 30)))
            d = math.ceil(lo / 10) * 10
            while d <= hi:
                a = ang(d)
                c.create_line(*pt(a, R - 22), *pt(a, R - 16), fill=self.VU_INK)
                c.create_text(*pt(a, R - 34), text=rt.fmt_si(rt.dbm_to_w(d), "W").replace(" ", ""),
                              fill="#6b5f4b", font=f_in)
                d += 10

        unit = "dBm" if self._absolute() else "dB"
        c.create_text(cx, fy0 + hf * 0.60, text=unit, fill=self.VU_INK,
                      font=("Georgia", max(14, int(hf / 11)), "italic"))
        c.create_text(cx, fy0 + hf * 0.60 + max(18, hf / 9), text=f"{self.app.profile.NAME}  ·  S21",
                      fill="#7a6d55", font=("Segoe UI", max(8, int(hf / 32))))

        # marca de pico
        if self.stats:
            a = ang(self.stats["max"] + off)
            p1, p2, p3 = pt(a, R + 2), pt(a - 0.025, R + 13), pt(a + 0.025, R + 13)
            c.create_polygon(*p1, *p2, *p3, fill=self.PEAK, outline="")

        # ponteiro (com sombra), visível só dentro do mostrador
        if self.needle is not None:
            a = ang(self.needle)
            (ax_, ay_), (bx_, by_) = pt(a, cy - fy1 + hf * 0.06), pt(a, R + 10)
            c.create_line(ax_ + 3, ay_ + 3, bx_ + 3, by_ + 3, fill="#cbbd96", width=3)
            c.create_line(ax_, ay_, bx_, by_, fill="#141414", width=3)
            c.create_line(*pt(a, R - 30), bx_, by_, fill=self.VU_RED, width=2)

        # tampa do pivô
        rc = cy - fy1 + hf * 0.15
        phi = math.degrees(math.acos((cy - fy1) / rc))
        c.create_arc(cx - rc, cy - rc, cx + rc, cy + rc, start=90 - phi, extent=2 * phi,
                     style="chord", fill="#1c2128", outline="#3b434d", width=2)
        c.create_rectangle(fx0, fy1, fx1, y1 - 1, fill=self.VU_BEZEL, outline="")
        # reflexo do vidro
        c.create_polygon(fx0, fy0, fx0 + wf * 0.42, fy0, fx0 + wf * 0.18, fy0 + hf * 0.5, fx0, fy0 + hf * 0.5,
                         fill="#ffffff", stipple="gray12", outline="")

    def _draw_digital(self, x0, y0, x1, y1, off):
        c = self.cv
        c.create_rectangle(x0, y0, x1, y1, fill=self.PANEL, outline="#30363d")
        w, h = x1 - x0, y1 - y0
        pad = 18
        u = self.unit.get()
        if self.last is None:
            c.create_text((x0 + x1) / 2, y0 + h * 0.3, fill=self.MUTED, font=("Segoe UI", 18),
                          text="aguardando leitura…")
            return
        v = self.last["rel"] + off
        big = max(26, int(h / 6.5))
        if u in ("W", "V"):
            num, _, unit = self._fmt(v).partition(" ")
        else:
            num, unit = self._fmt(v), ("dBm" if u == "dBm" else "dB")
        c.create_text(x1 - pad, y0 + pad + big * 0.55, anchor="e", fill=self.ACCENT,
                      font=("Consolas", big, "bold"), text=num)
        c.create_text(x1 - pad, y0 + pad + big * 1.35, anchor="e", fill=self.MUTED,
                      font=("Segoe UI", max(12, int(big / 2.6))), text=unit)

        rows = []
        if self._absolute():
            p_w = rt.dbm_to_w(v)
            vr = rt.dbm_to_vrms(v)
            rows = [("Potência", rt.fmt_si(p_w, "W")),
                    ("Tensão RMS (50 Ω)", rt.fmt_si(vr, "V")),
                    ("Tensão pico a pico", rt.fmt_si(vr * 2 * math.sqrt(2), "V")),
                    ("Nível", f"{v:+.2f} dBm   {rt.dbm_to_dbuv(v):.1f} dBµV".replace(".", ","))]
        else:
            ratio_v = 10 ** (v / 20)
            rows = [("Razão de tensão", f"{ratio_v:.4g} ×".replace(".", ",")),
                    ("Razão de potência", f"{ratio_v ** 2 * 100:.4g} %".replace(".", ",")),
                    ("Perda" if v < 0 else "Ganho", f"{abs(v):.2f} dB".replace(".", ","))]
        ry = y0 + pad + big * 1.35 + max(28, h * 0.10)
        step = min(34, (y1 - ry - 24) / max(len(rows), 1))
        for k, val in rows:
            c.create_text(x0 + pad, ry, anchor="w", fill=self.MUTED, font=("Segoe UI", 11), text=k)
            c.create_text(x1 - pad, ry, anchor="e", fill=self.INK, font=("Consolas", 14, "bold"), text=val)
            ry += step
        note = ("Valores absolutos dependem do nível da fonte informado."
                if self._absolute() else "Escolha dBm, W ou V para ver µW/mW e µV/mV.")
        c.create_text(x0 + pad, y1 - 12, anchor="sw", fill=self.MUTED, font=("Segoe UI", 9), text=note)

    def _draw_stats(self, x0, y, x1, off):
        c = self.cv
        if not self.stats:
            return
        st = self.stats
        items = [("Mín", self._fmt(st["min"] + off)), ("Máx (pico)", self._fmt(st["max"] + off)),
                 ("Média", self._fmt(st["sum"] / st["n"] + off)),
                 ("Ruído σ", f"{self.last['sigma']:.2f} dB".replace(".", ",")),
                 ("Leituras/s", f"{self.rate:.1f}".replace(".", ","))]
        gap = 8
        cw = (x1 - x0 - gap * (len(items) - 1)) / len(items)
        for i, (k, v) in enumerate(items):
            bx = x0 + i * (cw + gap)
            c.create_rectangle(bx, y, bx + cw, y + 50, fill=self.PANEL, outline="#30363d")
            c.create_text(bx + cw / 2, y + 14, fill=self.MUTED, font=("Segoe UI", 9), text=k)
            c.create_text(bx + cw / 2, y + 34, fill=self.PEAK if i == 1 else self.INK,
                          font=("Consolas", 13, "bold"), text=v)

    def _draw_history(self, gx0, gy0, gx1, gy1, off):
        c = self.cv
        if gy1 - gy0 < 40:
            return
        c.create_rectangle(gx0, gy0, gx1, gy1, fill=self.PANEL, outline="#30363d")
        for k in range(1, 4):
            y = gy0 + (gy1 - gy0) * k / 4
            c.create_line(gx0, y, gx1, y, fill=self.GRID, dash=(2, 4))
        hist = [v + off for v in self.history]
        if len(hist) > 1:
            hlo, hhi = min(hist), max(hist)
            if hhi - hlo < 1:
                mid = (hhi + hlo) / 2
                hlo, hhi = mid - 0.5, mid + 0.5
            n = len(hist)
            pts = []
            for i, v in enumerate(hist):
                x = gx0 + (gx1 - gx0) * (i / 299 if n >= 300 else i / max(n - 1, 1))
                pts += [x, gy1 - 6 - (gy1 - gy0 - 12) * (v - hlo) / (hhi - hlo)]
            c.create_line(*pts, fill=self.ACCENT, width=2)
            c.create_text(gx0 + 6, gy0 + 4, anchor="nw", fill=self.MUTED, font=("Segoe UI", 9),
                          text=self._fmt(hhi))
            c.create_text(gx0 + 6, gy1 - 4, anchor="sw", fill=self.MUTED, font=("Segoe UI", 9),
                          text=self._fmt(hlo))
        c.create_text(gx1 - 6, gy0 + 4, anchor="ne", fill=self.MUTED, font=("Segoe UI", 9),
                      text="histórico (últimas 300 leituras)")

    # ------------------------------------------------------------------
    def stop(self, restore=True):
        self.active = False
        if self.csv:
            self.csv.close()
            self.csv = None
        app = self.app
        if self.mirror_was_running and not app.running:
            app.toggle_mirror()
        if restore and app.vna:
            app.restore_sweep()
        app.meter = None
        self.destroy()


class ReportDialog(tk.Toplevel):
    """Janela para preencher os dados do relatório."""

    def __init__(self, app, saved):
        super().__init__(app)
        self.app = app
        self.title("Gerar relatório")
        self.resizable(False, False)
        self.transient(app)
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)

        self.entries = {}
        for r, (key, label) in enumerate([("title", "Título"), ("dut", "Dispositivo sob teste"),
                                          ("location", "Local"), ("operator", "Operador")]):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=2)
            e = ttk.Entry(f, width=48)
            e.insert(0, saved.get(key, "Medição com NanoVNA" if key == "title" else ""))
            e.grid(row=r, column=1, sticky="ew", pady=2, padx=(8, 0))
            self.entries[key] = e

        ttk.Label(f, text="Observações").grid(row=4, column=0, sticky="nw", pady=2)
        self.notes = tk.Text(f, width=48, height=5, wrap="word")
        self.notes.grid(row=4, column=1, sticky="ew", pady=2, padx=(8, 0))

        uses_s21 = any(on.get() and ch.get() == CHANNELS[1]
                       for on, _fmt, ch, _sc, _rp in getattr(app, "trace_rows", []))
        self.inc_s21 = tk.BooleanVar(value=uses_s21)
        ttk.Checkbutton(f, text="Incluir S21 nos marcadores", variable=self.inc_s21).grid(
            row=5, column=1, sticky="w", pady=(6, 0), padx=(8, 0))

        ttk.Label(f, foreground="gray", wraplength=420, justify="left",
                  text="Dica: posicione os marcadores antes (aba Marcadores). "
                       "Título, DUT, local e operador ficam salvos para a próxima vez.").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))

        bar = ttk.Frame(f)
        bar.grid(row=7, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(bar, text="Cancelar", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Salvar PDF", command=lambda: self.go(".pdf")).pack(side="right", padx=6)
        ttk.Button(bar, text="Salvar PNG", command=lambda: self.go(".png")).pack(side="right")

        self.entries["dut"].focus_set()
        self.grab_set()

    def go(self, ext):
        meta = {k: e.get().strip() for k, e in self.entries.items()}
        meta["notes"] = self.notes.get("1.0", "end").strip()
        base = "".join(c if c.isalnum() or c in "-_" else "_" for c in (meta["dut"] or "nanovna"))
        name = f"relatorio_{base}_{time.strftime('%Y%m%d_%H%M%S')}{ext}"
        kind = "PDF" if ext == ".pdf" else "PNG"
        path = filedialog.asksaveasfilename(parent=self, defaultextension=ext, initialfile=name,
                                            filetypes=[(kind, f"*{ext}")])
        if not path:
            return
        inc = self.inc_s21.get()
        self.destroy()
        self.app.build_report(meta, inc, path)


if __name__ == "__main__":
    if os.name == "nt":
        # faz a barra de tarefas do Windows usar o ícone do programa, e não o do Python
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("laztel.nanovna_mirror")
        except Exception:
            pass
    App().mainloop()
