"""Testes dos cálculos de RF com respostas exatas conhecidas. Uso: python tests/test_rf_tools.py"""
import sys, math, numpy as np
sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), ".."))
import rf_tools as rt
C = rt.C0_LIGHT
ok = True
def check(name, got, want, tol):
    global ok
    good = abs(got - want) <= tol
    ok &= good
    print(f"{'OK ' if good else 'ERRO'} {name}: {got:.6g} (esperado {want:.6g} ±{tol:g})")

# 1) DTF: cabo VF 0.66, perda 0.1 dB/m, emenda RL 20 dB a 12 m, antena (RL 6 dB) no fim a 35 m
vf, a = 0.66, 0.1
plan = rt.dtf_plan(50, vf)
segs, df = rt.segment_plan(plan["start"], plan["stop"], plan["n_seg"])
f = np.concatenate([np.linspace(s, e, 101) for s, e in segs])
def refl(f, d, g):
    att = 10 ** (-2 * a * d / 20)
    return g * att * np.exp(-1j * 4 * np.pi * f * d / (vf * C))
s11 = refl(f, 12.0, 0.1) + refl(f, 35.0, 0.5)
d, mag = rt.dtf(f, s11, vf, loss_db_per_m=a)
pk = rt.find_peaks(d, mag, 0.03, 1.0)
print("plano:", {k: round(v, 3) if isinstance(v, float) else v for k, v in plan.items()})
check("DTF falha 1 distância", rt.refine_peak(d, mag, pk[0]), 12.0, plan["resolucao_m"] / 4)
check("DTF falha 1 RL (dB)", -20 * math.log10(mag[pk[0]]), 20.0, 0.5)
check("DTF fim distância", rt.refine_peak(d, mag, pk[1]), 35.0, plan["resolucao_m"] / 4)
check("DTF fim RL (dB)", -20 * math.log10(mag[pk[1]]), 6.02, 0.5)

# 2) cabo 20 m em aberto: perda e comprimento
L = 20.0
f2 = np.linspace(1e6, 101e6, 1010)
loss_per_m = 0.15
s11o = 10 ** (-2 * loss_per_m * L / 20) * np.exp(-1j * 4 * np.pi * f2 * L / (vf * C))
check("perda 1 sentido (dB)", rt.cable_loss_open_short(s11o).mean(), loss_per_m * L, 1e-6)
tau, step = rt.group_delay_fit(f2, s11o)
check("comprimento físico (m)", vf * rt.electrical_length(tau), L, 1e-3)
check("fator de velocidade", rt.velocity_factor(L, tau), vf, 1e-5)

# 3) stub lambda/4 aberto para 150 MHz: entrada vira curto em 150 MHz
Ls = rt.stub_length(150e6, vf)
f3 = np.linspace(100e6, 200e6, 303)
zin = -1j * rt.Z0 / np.tan(2 * np.pi * f3 * Ls / (vf * C))  # stub aberto sem perdas
fr = rt.stub_resonance(f3, rt.z_to_gamma(zin), "curto")
check("stub l/4 ressonância (MHz)", fr / 1e6, 150.0, 0.05)
new, cut = rt.stub_cut(Ls * 1.03, 150e6 / 1.03, 150e6)
check("stub corte (m)", cut, Ls * 0.03, 1e-9)

# 4) filtro: ressonador série RLC entre portas (passa-faixa) em 150 MHz, Q carregado ~ 30
f4 = np.linspace(140e6, 160e6, 2001)
w = 2 * np.pi * f4; w0 = 2 * np.pi * 150e6
Lr = 30 * 100 / w0; Cr = 1 / (w0 ** 2 * Lr); Rr = 5.0
zs = Rr + 1j * w * Lr + 1 / (1j * w * Cr)
s21 = 2 * rt.Z0 / (2 * rt.Z0 + zs)
m = rt.filter_metrics(f4, s21, x_db=3.0, passband=(149.8e6, 150.2e6), reject_freqs=[155e6])
bw_exp = (105) / (2 * np.pi * Lr)   # (Rr+2Z0)/(2 pi L)
check("filtro f_pico (MHz)", m["f_pico"] / 1e6, 150.0, 0.01)
check("filtro perda (dB)", m["perda_min_db"], -20 * math.log10(100 / 105), 0.01)
check("filtro banda -3 dB (kHz)", m["banda"] / 1e3, bw_exp / 1e3, bw_exp / 1e3 * 0.01)
mask = rt.duplexer_mask(150e6, 155e6, 0.2e6, 1.0, 20.0)
r = rt.eval_mask(f4, s21, mask)
print("máscara:", [(it["tipo"], p, round(v, 2)) for it, p, v in r])

# 5) componente: indutor 100 nH com R série 0.5 ohm e C parasita 0.5 pF (SRF ~ 712 MHz)
f5 = np.linspace(10e6, 900e6, 891)
w = 2 * np.pi * f5
zl = 1 / (1 / (0.5 + 1j * w * 100e-9) + 1j * w * 0.5e-12)
for mode, s in (("reflexao", rt.z_to_gamma(zl)), ("serie", 2 * rt.Z0 / (2 * rt.Z0 + zl)),
                ("derivacao", 2 * zl / (2 * zl + rt.Z0))):
    z = rt.component_z(mode, s11=s, s21=s)
    v = rt.component_values(f5, z)
    check(f"componente {mode} L@10MHz (nH)", v["L"][0] * 1e9, 100.0, 0.5)
check("componente SRF (MHz)", v["srf"] / 1e6, 1 / (2 * np.pi * np.sqrt(100e-9 * 0.5e-12)) / 1e6, 2)

# 6) cristal 10 MHz: Rm 20 ohm, Lm 10 mH, C0 4 pF
Rm, Lm, C0x = 20.0, 10e-3, 4e-12
Cm = 1 / ((2 * np.pi * 10e6) ** 2 * Lm)
fs = 1 / (2 * np.pi * np.sqrt(Lm * Cm)); fp = fs * np.sqrt(1 + Cm / C0x)
f6 = np.linspace(fs - 2e3, fp + 2e3, 4001)
w = 2 * np.pi * f6
zm = Rm + 1j * w * Lm + 1 / (1j * w * Cm)
zx = 1 / (1 / zm + 1j * w * C0x)
p = rt.crystal_params(f6, 2 * rt.Z0 / (2 * rt.Z0 + zx))
check("cristal fs (Hz) [max. transmissao; ~1 ppm de C0]", p["fs"], fs, 15)
check("cristal Rm (ohm)", p["Rm"], Rm, 0.5)
check("cristal Lm (mH)", p["Lm"] * 1e3, Lm * 1e3, 0.2)
check("cristal C0 (pF)", p["C0"] * 1e12, C0x * 1e12, 0.15)
check("cristal fp (Hz)", p["fp"], fp, 5)

# 7) rede L: várias cargas
for zl in (25 + 15j, 120 - 40j, 10 - 30j, 200 + 0j, 50 + 30j):
    sols = rt.l_network(complex(zl), 150e6)
    worst = max(abs(rt.z_to_gamma(s["zin"])) for s in sols)
    ok &= len(sols) >= 2 and worst < 1e-6
    print(f"{'OK ' if len(sols) >= 2 and worst < 1e-6 else 'ERRO'} rede L Z={zl}: {len(sols)} soluções, |Γ| pior {worst:.1e}")
s = rt.l_network(25 + 15j, 150e6)[0]
els = [(p, w_, k, rt.nearest_e12(v)) for p, w_, k, v in s["elementos"]]
zin = rt.l_network_with_values(25 + 15j, s["topologia"], els, 150e6)
print("  com valores E12:", els, "SWR", round(float(rt.swr(rt.z_to_gamma(zin))), 3))

# 8) comparação
f8 = np.linspace(140e6, 160e6, 201)
def ant(f0): 
    x = 400 * (f8 / f0 - f0 / f8); return rt.z_to_gamma(50 + 1j * x)
c = rt.compare(f8, ant(150e6), ant(147e6))
check("comparação desvio (kHz)", c["desvio_f"] / 1e3, -3000, 100)
print("  situação:", c["situacao"], c["alertas"])
# 9) fator Q
f9 = np.linspace(130e6, 170e6, 8001); f0 = 150e6
a9 = 10 ** (-1 / 20)
q = rt.filter_metrics(f9, a9 / (1 + 1j * 500 * (f9 / f0 - f0 / f9)))
check("Q carregado (ressoador, QL 500)", q["q_carregado"], 500, 1)
check("Q descarregado (IL 1 dB)", q["q_descarregado"], 500 / (1 - a9), 10)
zb = 1 + 1j * 2000 * (f9 / f0 - f0 / f9)
q = rt.filter_metrics(f9, 2 * zb / (2 * zb + 50))
check("Q do notch (ramo série R 1 Ω, Q 2000)", q["q_notch"], 2000 / np.sqrt(674), 0.5)
print("\nTUDO OK" if ok else "\nHÁ ERROS")
