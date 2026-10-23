"""
Tabela de cabos coaxiais de 50 ohm: fator de velocidade e atenuação típica.

Os valores são TÍPICOS DE CATÁLOGO, arredondados, para servir de ponto de partida.
Confira o datasheet do fabricante do cabo que você usa e ajuste na tabela do programa:
as edições ficam salvas em ~/.nanovna_mirror_cabos.json e têm prioridade sobre esta lista.

Atenuação em dB por 100 m, cabo novo, 20 °C. Interpolação log-log entre os pontos.
"""
import json
import math
import os

USER_FILE = os.path.join(os.path.expanduser("~"), ".nanovna_mirror_cabos.json")

# nome: (fator de velocidade, {MHz: dB/100 m}, observação)
PADRAO = {
    "RG-58 / RGC-58 (PE sólido)": (0.66, {10: 4.6, 50: 10.8, 100: 16.1, 200: 24.0, 400: 36.7, 1000: 70.5}, ""),
    "RG-213 / RGC-213 (PE sólido)": (0.66, {10: 2.0, 50: 4.6, 100: 6.9, 200: 10.0, 400: 15.0, 1000: 26.0}, ""),
    "RG-8X / mini-8 (PE espuma)": (0.78, {10: 3.3, 50: 8.2, 100: 12.1, 200: 17.7, 400: 26.2, 1000: 44.3}, ""),
    "RG-174": (0.66, {10: 10.8, 50: 19.0, 100: 27.6, 200: 39.4, 400: 55.8, 1000: 98.0}, ""),
    "RG-316 (PTFE)": (0.695, {10: 8.5, 100: 26.0, 400: 50.0, 1000: 83.0}, ""),
    "LMR-195": (0.83, {30: 6.6, 50: 8.2, 150: 14.4, 220: 17.7, 450: 25.6, 900: 36.4, 1500: 47.6}, ""),
    "LMR-240": (0.84, {30: 4.3, 50: 5.6, 150: 9.8, 220: 12.1, 450: 17.4, 900: 24.9, 1500: 32.5}, ""),
    "LMR-400": (0.85, {30: 2.3, 50: 3.0, 150: 4.9, 220: 6.2, 450: 8.9, 900: 12.8, 1500: 16.7}, ""),
    "LMR-600": (0.87, {30: 1.3, 50: 2.0, 150: 3.3, 220: 3.9, 450: 5.6, 900: 8.2, 1500: 10.8}, ""),
    "Ecoflex 10": (0.86, {145: 4.8, 432: 8.9, 1296: 16.5}, ""),
    "Aircell 7": (0.83, {145: 7.9, 432: 14.1, 1296: 26.1}, ""),
    "Heliax / Cellflex 1/2\" (espuma)": (0.88, {30: 1.2, 50: 1.5, 150: 2.7, 450: 4.8, 900: 7.0}, "valores aproximados"),
    "Heliax / Cellflex 7/8\" (espuma)": (0.89, {30: 0.66, 50: 0.85, 150: 1.5, 450: 2.7, 900: 3.9}, "valores aproximados"),
}


def load():
    """Tabela efetiva: padrão + edições do usuário (que têm prioridade)."""
    tab = {k: (vf, dict(att), obs) for k, (vf, att, obs) in PADRAO.items()}
    try:
        with open(USER_FILE, encoding="utf-8") as fh:
            user = json.load(fh)
        for name, item in user.get("cabos", {}).items():
            if item is None:
                tab.pop(name, None)       # removido pelo usuário
            else:
                tab[name] = (float(item["vf"]), {float(k): float(v) for k, v in item["atenuacao"].items()},
                             item.get("obs", ""))
    except Exception:
        pass
    return tab


def save_user(tab):
    """Salva a tabela inteira como edições do usuário (inclui remoções de cabos padrão)."""
    data = {"cabos": {}}
    for name in PADRAO:
        if name not in tab:
            data["cabos"][name] = None
    for name, (vf, att, obs) in tab.items():
        if name in PADRAO and PADRAO[name] == (vf, att, obs):
            continue
        data["cabos"][name] = {"vf": vf, "atenuacao": {str(k): v for k, v in att.items()}, "obs": obs}
    with open(USER_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def reset_user():
    try:
        os.remove(USER_FILE)
    except OSError:
        pass


def attenuation_db_per_100m(att, f_mhz):
    """Interpolação log-log; extrapola pela inclinação das pontas."""
    pts = sorted(att.items())
    if len(pts) == 1:
        f0, a0 = pts[0]
        return a0 * math.sqrt(f_mhz / f0)
    if f_mhz <= pts[0][0]:
        (f0, a0), (f1, a1) = pts[0], pts[1]
    elif f_mhz >= pts[-1][0]:
        (f0, a0), (f1, a1) = pts[-2], pts[-1]
    else:
        for (f0, a0), (f1, a1) in zip(pts[:-1], pts[1:]):
            if f0 <= f_mhz <= f1:
                break
    k = math.log(a1 / a0) / math.log(f1 / f0)
    return a0 * (f_mhz / f0) ** k


def attenuation_db_per_m(att, f_hz):
    return attenuation_db_per_100m(att, f_hz / 1e6) / 100.0
