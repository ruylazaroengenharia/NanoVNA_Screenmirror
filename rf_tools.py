"""
Cálculos de RF das ferramentas do NanoVNA Mirror (sem interface, testáveis).

Convenções: frequências em Hz, S-parâmetros complexos (numpy), Z0 = 50 ohm.
"""
import math

import numpy as np

C0_LIGHT = 299_792_458.0
Z0 = 50.0


# ---------------------------------------------------------------------------
# Conversões básicas
# ---------------------------------------------------------------------------
def gamma_to_z(g, z0=Z0):
    g = np.asarray(g, dtype=complex)
    with np.errstate(divide="ignore", invalid="ignore"):
        return z0 * (1 + g) / (1 - g)


def z_to_gamma(z, z0=Z0):
    z = np.asarray(z, dtype=complex)
    return (z - z0) / (z + z0)


def db(x):
    return 20 * np.log10(np.maximum(np.abs(x), 1e-12))


def return_loss(g):
    return -db(g)


def swr(g):
    m = np.minimum(np.abs(g), 0.999999)
    return (1 + m) / (1 - m)


def reactance_to_lc(x, f):
    """Reatância série -> ('L', henry) ou ('C', farad)."""
    w = 2 * math.pi * f
    if x >= 0:
        return "L", x / w
    return "C", -1 / (w * x)


def fmt_si(v, unit):
    if v == 0 or not math.isfinite(v):
        return f"{v} {unit}"
    for k, p in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"), (1e-15, "f")):
        if abs(v) >= k:
            return f"{v / k:.4g} {p}{unit}".replace(".", ",")
    return f"{v:.3g} {unit}".replace(".", ",")


def dbm_to_w(dbm):
    return 10 ** ((dbm - 30) / 10)


def dbm_to_vrms(dbm, z0=Z0):
    """Tensão RMS sobre z0 (50 Ω) para uma potência em dBm."""
    return math.sqrt(dbm_to_w(dbm) * z0)


def dbm_to_dbuv(dbm, z0=Z0):
    return 20 * math.log10(dbm_to_vrms(dbm, z0) / 1e-6)


# ---------------------------------------------------------------------------
# Planejamento de varredura segmentada (firmware: 101 pontos por varredura)
# ---------------------------------------------------------------------------
SEG = 101


def segment_plan(start, stop, n_seg):
    """Divide [start, stop] em n_seg varreduras contíguas de 101 pontos com passo uniforme."""
    n = SEG * n_seg
    df = (stop - start) / (n - 1)
    segs = []
    for k in range(n_seg):
        a = start + k * SEG * df
        segs.append((int(round(a)), int(round(a + (SEG - 1) * df))))
    return segs, df


# ---------------------------------------------------------------------------
# DTF / localizador de falhas (modo passa-banda, como os analisadores de site)
# ---------------------------------------------------------------------------
def dtf_plan(d_max, vf, f_lo=50e6, f_hi=900e6, max_seg=10, margin=1.25, att_db_per_m=None, max_rt_loss=20.0):
    """
    Escolhe a faixa e o número de segmentos para cobrir d_max metros.
    att_db_per_m(f): atenuação do cabo; limita a frequência máxima para que a perda de
    ida e volta até d_max não passe de max_rt_loss (o fim do cabo continua visível).
    Retorna dict com start, stop, n_seg, resolução, alcance e perda de ida e volta.
    """
    if att_db_per_m is not None:
        rt_loss = lambda f: 2 * att_db_per_m(f) * d_max
        if rt_loss(f_hi) > max_rt_loss:
            lo, hi = f_lo * 2, f_hi
            if rt_loss(lo) > max_rt_loss:
                f_hi = lo
            else:
                for _ in range(40):
                    mid = (lo + hi) / 2
                    lo, hi = (mid, hi) if rt_loss(mid) <= max_rt_loss else (lo, mid)
                f_hi = lo
    span_max = SEG * max_seg * vf * C0_LIGHT / (2 * d_max * margin)
    span = min(f_hi - f_lo, span_max)
    start, stop = f_lo, f_lo + span
    n_needed = 2 * d_max * span * margin / (vf * C0_LIGHT) + 1
    n_seg = max(1, min(max_seg, int(math.ceil(n_needed / SEG))))
    out = dtf_info(start, stop, n_seg, vf) | {"start": start, "stop": stop, "n_seg": n_seg}
    if att_db_per_m is not None:
        out["perda_ida_volta_db"] = 2 * att_db_per_m(stop) * d_max
    return out


def dtf_info(start, stop, n_seg, vf):
    n = SEG * n_seg
    df = (stop - start) / (n - 1)
    return {
        "resolucao_m": vf * C0_LIGHT / (2 * (stop - start)),
        "alcance_m": vf * C0_LIGHT / (2 * df),
        "pontos": n,
    }


def dtf(freqs, s11, vf, loss_db_per_m=0.0, window="kaiser", beta=6.0, n_fft=16384):
    """
    Transformada para distância (passa-banda).
    freqs uniformes, s11 complexo. loss_db_per_m = atenuação do cabo (um sentido) no centro da faixa.
    Retorna (dist_m, gamma_mag) com a perda do cabo compensada.
    """
    freqs = np.asarray(freqs, float)
    s11 = np.asarray(s11, complex)
    n = len(freqs)
    df = (freqs[-1] - freqs[0]) / (n - 1)
    if window == "kaiser":
        w = np.kaiser(n, beta)
    elif window == "hann":
        w = np.hanning(n)
    else:
        w = np.ones(n)
    n_fft = max(n_fft, 4 * n)
    x = np.fft.ifft(s11 * w, n_fft) * n_fft / np.sum(w)
    t = np.arange(n_fft) / (n_fft * df)            # ida e volta
    d = vf * C0_LIGHT * t / 2
    mag = np.abs(x)
    if loss_db_per_m:
        mag = mag * 10 ** (2 * loss_db_per_m * d / 20)
    keep = d <= vf * C0_LIGHT / (2 * df)
    return d[keep], mag[keep]


def find_peaks(x, y, thresh, min_sep, n_max=8):
    """Máximos locais de y acima de thresh, separados por min_sep (em unidades de x)."""
    y = np.asarray(y)
    idx = np.where((y[1:-1] > y[:-2]) & (y[1:-1] >= y[2:]) & (y[1:-1] > thresh))[0] + 1
    idx = sorted(idx, key=lambda i: -y[i])
    out = []
    for i in idx:
        if all(abs(x[i] - x[j]) >= min_sep for j in out):
            out.append(i)
        if len(out) >= n_max:
            break
    return sorted(out, key=lambda i: x[i])


def refine_peak(x, y, i):
    """Interpolação parabólica do pico em i."""
    if 0 < i < len(y) - 1:
        a, b, c = y[i - 1], y[i], y[i + 1]
        den = a - 2 * b + c
        if den != 0:
            p = 0.5 * (a - c) / den
            return x[i] + p * (x[i + 1] - x[i - 1]) / 2
    return x[i]


# ---------------------------------------------------------------------------
# Cabo: perda, comprimento elétrico, fator de velocidade, stubs
# ---------------------------------------------------------------------------
def cable_loss_open_short(s11):
    """Perda em um sentido (dB) com a ponta do cabo aberta ou em curto: metade da perda de retorno."""
    return return_loss(s11) / 2


def group_delay_fit(freqs, s11):
    """
    Atraso de ida e volta (s) pelo ajuste linear da fase desembrulhada de S11.
    Retorna (tau, passo_max_rad) — passo_max perto de pi indica fase ambígua (poucos pontos).
    """
    ph = np.unwrap(np.angle(s11))
    steps = np.abs(np.diff(ph))
    slope, _ = np.polyfit(freqs, ph, 1)
    return -slope / (2 * math.pi), float(np.max(steps)) if len(steps) else 0.0


def electrical_length(tau_round_trip):
    return C0_LIGHT * tau_round_trip / 2


def velocity_factor(physical_len, tau_round_trip):
    return 2 * physical_len / (C0_LIGHT * tau_round_trip)


def stub_length(f, vf, fraction=0.25):
    return fraction * vf * C0_LIGHT / f


def stub_resonance(freqs, s11, kind):
    """
    Frequência de ressonância de um stub medido pela entrada.
    kind = 'curto' -> procura onde a entrada vira curto (|Z| mínimo, X cruza zero subindo)
           'aberto' -> onde vira aberto (|Z| máximo)
    """
    z = gamma_to_z(s11)
    x = z.imag
    cands = []
    for i in range(len(x) - 1):
        if np.sign(x[i]) != np.sign(x[i + 1]) and np.isfinite(x[i]) and np.isfinite(x[i + 1]):
            f0 = freqs[i] - x[i] * (freqs[i + 1] - freqs[i]) / (x[i + 1] - x[i])
            zm = abs(z[i]) + abs(z[i + 1])
            cands.append((f0, zm))
    if not cands:
        return None
    if kind == "curto":
        return min(cands, key=lambda c: c[1])[0]
    return max(cands, key=lambda c: c[1])[0]


def stub_cut(current_len, f_measured, f_target):
    """Comprimento novo e quanto cortar (positivo = cortar)."""
    new = current_len * f_measured / f_target
    return new, current_len - new


# ---------------------------------------------------------------------------
# Filtros e duplexadores (S21)
# ---------------------------------------------------------------------------
def _cross(freqs, y, level, i0, direction):
    """Interpola a frequência em que y cruza 'level' andando de i0 na direção dada."""
    i = i0
    while 0 <= i + direction < len(y):
        j = i + direction
        if (y[i] - level) * (y[j] - level) <= 0 and y[i] != y[j]:
            return freqs[i] + (level - y[i]) * (freqs[j] - freqs[i]) / (y[j] - y[i])
        i = j
    return None


def filter_metrics(freqs, s21, s11=None, x_db=3.0, passband=None, reject_freqs=(), notch_window=None):
    """Métricas de filtro passa-faixa/notch a partir de S21 (e S11 opcional)."""
    freqs = np.asarray(freqs, float)
    m = db(s21)
    ip = int(np.argmax(m))
    peak = float(m[ip])
    lo = _cross(freqs, m, peak - x_db, ip, -1)
    hi = _cross(freqs, m, peak - x_db, ip, +1)
    out = {"f_pico": freqs[ip], "perda_min_db": -peak, "x_db": x_db,
           "f_lo": lo, "f_hi": hi,
           "banda": (hi - lo) if (lo is not None and hi is not None) else None,
           "f_centro": (lo + hi) / 2 if (lo is not None and hi is not None) else None}
    if notch_window:
        selw = (freqs >= notch_window[0]) & (freqs <= notch_window[1])
        idxw = np.where(selw)[0]
        inn = int(idxw[np.argmin(m[selw])]) if len(idxw) else int(np.argmin(m))
    else:
        inn = int(np.argmin(m))
    out["f_notch"] = freqs[inn]
    out["notch_db"] = float(m[inn])
    out["notch_rel_db"] = float(m[inn] - peak)
    if passband:
        sel = (freqs >= passband[0]) & (freqs <= passband[1])
        if sel.any():
            out["ripple_db"] = float(m[sel].max() - m[sel].min())
            out["perda_max_banda_db"] = float(-m[sel].min())
            if s11 is not None:
                out["rl_min_banda_db"] = float(return_loss(np.asarray(s11)[sel]).min())
    out.update(q_factors(freqs, m, ip, inn))
    out["rejeicoes"] = [(f, float(np.interp(f, freqs, m))) for f in reject_freqs]
    out["passo"] = float(freqs[1] - freqs[0]) if len(freqs) > 1 else 0.0
    return out


HALF_POWER_DB = 10 * math.log10(2)     # 3,0103 dB


def q_factors(freqs, m, ip, inn=None):
    """
    Fator de qualidade a partir de |S21| em dB (m), com o pico em ip e o notch em inn.

    q_carregado     QL = f0 / BW(−3 dB), f0 = média geométrica das bordas.
                    Num filtro de vários polos é o inverso da banda relativa.
    q_descarregado  Qu = QL / (1 − 10^(−IL/20)): vale para UM ressoador
                    (cavidade, ressoador helicoidal) acoplado simetricamente.
    q_notch         fn / largura do notch a −3 dB do nível em volta dele (ombros).
    """
    freqs = np.asarray(freqs, float)
    peak = float(m[ip])
    out = {"q_carregado": None, "q_descarregado": None, "q_notch": None, "bw3": None, "bw3_notch": None}
    lo = _cross(freqs, m, peak - HALF_POWER_DB, ip, -1)
    hi = _cross(freqs, m, peak - HALF_POWER_DB, ip, +1)
    if lo is not None and hi is not None and hi > lo > 0:
        f0 = math.sqrt(lo * hi)
        ql = f0 / (hi - lo)
        out.update(q_carregado=ql, bw3=hi - lo, f0_q=f0)
        il = -peak
        if 0.01 < il < 30:
            out["q_descarregado"] = ql / (1 - 10 ** (-il / 20))
    if inn is not None:
        ref = min(_shoulder(m, inn, -1), _shoulder(m, inn, +1))
        if ref - m[inn] > 6:
            nlo = _cross(freqs, m, ref - HALF_POWER_DB, inn, -1)
            nhi = _cross(freqs, m, ref - HALF_POWER_DB, inn, +1)
            if nlo is not None and nhi is not None and nhi > nlo:
                out.update(q_notch=freqs[inn] / (nhi - nlo), bw3_notch=nhi - nlo, notch_ref_db=ref)
    return out


def _shoulder(m, i0, direction, hyst=1.0):
    """Nível do 'ombro' ao lado do notch: sobe a partir de i0 até a curva voltar a cair."""
    best = m[i0]
    i = i0
    while 0 <= i + direction < len(m):
        i += direction
        if m[i] > best:
            best = m[i]
        elif m[i] < best - hyst:
            break
    return float(best)


def eval_mask(freqs, s21, mask):
    """
    mask: lista de dict {f1, f2, tipo, limite_db}
      tipo 'max_perda' -> exige S21 >= -limite em [f1,f2]
      tipo 'min_rejeicao' -> exige S21 <= -limite em [f1,f2]
    Retorna lista de (item, passou, pior_valor_db).
    """
    freqs = np.asarray(freqs, float)
    m = db(s21)
    res = []
    for it in mask:
        if it["f2"] < freqs[0] or it["f1"] > freqs[-1]:
            res.append((it, None, None))        # fora da faixa medida
            continue
        sel = (freqs >= it["f1"]) & (freqs <= it["f2"])
        if not sel.any():
            # janela estreita: avalia por interpolação no centro
            v = np.array([np.interp((it["f1"] + it["f2"]) / 2, freqs, m)])
        else:
            v = m[sel]
        if it["tipo"] == "max_perda":
            worst = float(v.min())
            res.append((it, worst >= -it["limite_db"], worst))
        else:
            worst = float(v.max())
            res.append((it, worst <= -it["limite_db"], worst))
    return res


def duplexer_mask(f_pass, f_reject, bw, il_max, rej_min):
    return [
        {"f1": f_pass - bw / 2, "f2": f_pass + bw / 2, "tipo": "max_perda", "limite_db": il_max},
        {"f1": f_reject - bw / 2, "f2": f_reject + bw / 2, "tipo": "min_rejeicao", "limite_db": rej_min},
    ]


# ---------------------------------------------------------------------------
# Componentes (L, C, R, Q)
# ---------------------------------------------------------------------------
def component_z(mode, s11=None, s21=None, z0=Z0):
    """
    mode 'reflexao' : componente no conector CH0 (S11)
    mode 'serie'    : componente em série entre CH0 e CH1 (S21)
    mode 'derivacao': componente do sinal para o terra entre CH0 e CH1 (S21)
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        if mode == "reflexao":
            return gamma_to_z(s11, z0)
        s21 = np.asarray(s21, complex)
        if mode == "serie":
            return 2 * z0 * (1 - s21) / s21
        if mode == "derivacao":
            return z0 * s21 / (2 * (1 - s21))
    raise ValueError(mode)


def component_values(freqs, z):
    """L ou C equivalente, Q e frequência de autorressonância."""
    freqs = np.asarray(freqs, float)
    w = 2 * np.pi * freqs
    x, r = z.imag, z.real
    with np.errstate(divide="ignore", invalid="ignore"):
        L = x / w
        C = -1 / (w * x)
        Q = np.abs(x) / np.where(r > 1e-9, r, 1e-9)
    srf = None
    for i in range(len(x) - 1):
        if np.sign(x[i]) != np.sign(x[i + 1]) and np.isfinite(x[i]) and np.isfinite(x[i + 1]):
            srf = freqs[i] - x[i] * (freqs[i + 1] - freqs[i]) / (x[i + 1] - x[i])
            break
    return {"L": L, "C": C, "Q": Q, "R": r, "X": x, "srf": srf}


# ---------------------------------------------------------------------------
# Cristal (suporte série 50 ohm, S21)
# ---------------------------------------------------------------------------
def crystal_params(freqs, s21, r0=Z0):
    """
    Modelo de Butterworth-Van Dyke a partir de S21 em suporte série (fonte e carga de r0).
    Retorna fs, fp, Rm, Lm, Cm, C0, Q.
    fs é a frequência de máxima transmissão; por causa de C0 ela fica ~1 ppm abaixo da
    ressonância "motional" 1/(2*pi*sqrt(Lm*Cm)), como nos medidores de cristal comerciais.
    """
    freqs = np.asarray(freqs, float)
    mag = np.abs(np.asarray(s21))
    m = db(s21)
    i = int(np.argmax(mag))
    fs = refine_peak(freqs, m, i)
    s_max = mag[i]
    rm = 2 * r0 * (1 / s_max - 1)
    lo = _cross(freqs, m, m[i] - 3.0103, i, -1)
    hi = _cross(freqs, m, m[i] - 3.0103, i, +1)
    res = {"fs": fs, "Rm": rm, "s21_max_db": float(m[i])}
    if lo is None or hi is None:
        return res
    bw = hi - lo
    lm = (rm + 2 * r0) / (2 * math.pi * bw)
    cm = 1 / ((2 * math.pi * fs) ** 2 * lm)
    res.update({"bw3": bw, "Lm": lm, "Cm": cm, "Q": 2 * math.pi * fs * lm / max(rm, 1e-9),
                "Q_carregado": fs / bw})
    above = freqs > fs
    if above.any():
        j = int(np.argmin(np.where(above, mag, np.inf)))
        if j < len(freqs) - 1 and freqs[j] > fs:
            fp = refine_peak(freqs, -m, j)
            res["fp"] = fp
            res["C0"] = cm * fs / (2 * (fp - fs))
    return res


# ---------------------------------------------------------------------------
# Casamento de impedância com rede L
# ---------------------------------------------------------------------------
def l_network(zl, f, z0=Z0):
    """
    Todas as soluções de rede L (sem perdas) que levam zl a z0.
    Cada solução: dict(topologia, x_serie, b_shunt, elementos=[(posição, tipo, valor)]).
      'shunt_carga'  : elemento em derivação junto à carga, depois série até a fonte
      'serie_carga'  : elemento em série junto à carga, depois derivação na fonte
    Valida cada solução calculando a impedância de entrada.
    """
    rl, xl = zl.real, zl.imag
    w = 2 * math.pi * f
    sols = []
    # Pozar 5.1: (a) derivação junto à carga, série junto à fonte — exige rl > 0
    if rl > 0:
        den = rl * rl + xl * xl
        inside = rl / z0 * (rl * rl + xl * xl - z0 * rl)
        if inside >= 0 and abs(den) > 0:
            for sgn in (+1, -1):
                b = (xl + sgn * math.sqrt(inside)) / den
                if abs(b) < 1e-15:
                    x = -xl if rl == z0 else None
                    if x is None:
                        continue
                else:
                    x = 1 / b + xl * z0 / rl - z0 / (b * rl)
                sols.append({"topologia": "shunt_carga", "b": b, "x": x})
    # (b) série junto à carga, derivação junto à fonte — exige rl < z0
    if 0 < rl < z0:
        for sgn in (+1, -1):
            x = sgn * math.sqrt(rl * (z0 - rl)) - xl
            b = sgn * math.sqrt((z0 - rl) / rl) / z0
            sols.append({"topologia": "serie_carga", "b": b, "x": x})
    out = []
    for s in sols:
        zin = l_network_zin(zl, s["topologia"], s["x"], s["b"])
        if abs(z_to_gamma(zin, z0)) > 1e-6:
            continue
        s["zin"] = zin
        s["elementos"] = _elements(s, w)
        out.append(s)
    # remove duplicadas
    uniq = []
    for s in out:
        if not any(abs(s["x"] - u["x"]) < 1e-6 and abs(s["b"] - u["b"]) < 1e-9 and s["topologia"] == u["topologia"] for u in uniq):
            uniq.append(s)
    return uniq


def l_network_zin(zl, topo, x, b):
    if topo == "shunt_carga":
        yl = 1 / zl if zl != 0 else complex("inf")
        z1 = 1 / (yl + 1j * b)
        return z1 + 1j * x
    z1 = zl + 1j * x
    return 1 / (1 / z1 + 1j * b)


def _elements(s, w):
    el = []
    x, b = s["x"], s["b"]
    serie = ("série", "L", x / w) if x > 0 else ("série", "C", -1 / (w * x)) if x < 0 else ("série", "—", 0)
    shunt = ("derivação", "C", b / w) if b > 0 else ("derivação", "L", -1 / (w * b)) if b < 0 else ("derivação", "—", 0)
    if s["topologia"] == "shunt_carga":
        el = [("fonte",) + serie, ("carga",) + shunt]
    else:
        el = [("fonte",) + shunt, ("carga",) + serie]
    return el


E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]


def nearest_e12(v):
    if v <= 0:
        return v
    dec = 10 ** math.floor(math.log10(v))
    cands = [k * dec for k in E12] + [10 * dec]
    return min(cands, key=lambda c: abs(math.log(c / v)))


def l_network_with_values(zl, topo, elements, f):
    """Impedância de entrada usando valores reais (ex.: E12) dos elementos."""
    w = 2 * math.pi * f
    x = b = 0.0
    for pos, where, kind, val in elements:
        if where == "série":
            x = w * val if kind == "L" else -1 / (w * val)
        else:
            b = w * val if kind == "C" else -1 / (w * val)
    return l_network_zin(zl, topo, x, b)


# ---------------------------------------------------------------------------
# Comparação com referência
# ---------------------------------------------------------------------------
def s11_summary(freqs, s11, swr_lim=2.0):
    freqs = np.asarray(freqs, float)
    v = swr(s11)
    i = int(np.argmin(v))
    out = {"f_min": freqs[i], "swr_min": float(v[i]), "rl_max": float(return_loss(s11)[i])}
    lo = _cross(freqs, v, swr_lim, i, -1)
    hi = _cross(freqs, v, swr_lim, i, +1)
    out["banda_parcial"] = False
    if v[i] < swr_lim:
        if lo is None or hi is None:
            out["banda_parcial"] = True      # uma borda ficou fora da faixa medida
        lo = freqs[0] if lo is None else lo
        hi = freqs[-1] if hi is None else hi
        out["banda_swr"] = hi - lo
    else:
        out["banda_swr"] = None
    return out


def compare(freqs, s11_ref, s11_now, tol_shift=0.01, tol_swr=0.3):
    a = s11_summary(freqs, s11_ref)
    b = s11_summary(freqs, s11_now)
    d_rl = return_loss(s11_now) - return_loss(s11_ref)
    shift = b["f_min"] - a["f_min"]
    res = {"ref": a, "agora": b, "desvio_f": shift, "desvio_f_rel": shift / a["f_min"],
           "delta_swr_min": b["swr_min"] - a["swr_min"],
           "max_delta_rl": float(np.max(np.abs(d_rl))),
           "f_max_delta_rl": float(np.asarray(freqs)[int(np.argmax(np.abs(d_rl)))])}
    alerts = []
    if abs(res["desvio_f_rel"]) > tol_shift:
        alerts.append(f"ressonância deslocou {shift / 1e3:+.0f} kHz ({100 * res['desvio_f_rel']:+.1f}%)")
    if res["delta_swr_min"] > tol_swr:
        alerts.append(f"SWR mínimo subiu {res['delta_swr_min']:+.2f}")
    if a["banda_swr"] and b["banda_swr"] is None:
        alerts.append("a antena não atinge mais SWR 2:1 na faixa")
    res["alertas"] = alerts
    res["situacao"] = "ATENÇÃO" if alerts else "OK"
    return res
