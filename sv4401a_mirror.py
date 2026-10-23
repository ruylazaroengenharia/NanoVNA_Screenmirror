#!/usr/bin/env python3
"""
SV4401A Mirror - espelhamento de tela com toque remoto e ferramentas de medição
para o VNA SV4401A (SYSJOINT), firmware 0.7.x.

Usa o mesmo código do NanoVNA Mirror com o driver do SV4401A:
tela 1024x600 (clique e arraste para operar o aparelho), varreduras de até 1001 pontos,
faixa até 4,4 GHz, slots de calibração 0-17.

Requisitos (no PC):
    pip install pyserial pillow numpy matplotlib

Uso:
    python sv4401a_mirror.py

Desenvolvido por Ruy Lazaro - laztel.com.br - ruylazaro.engenharia@gmail.com
"""
import os

import devices
import nanovna_mirror

if __name__ == "__main__":
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("laztel.sv4401a_mirror")
        except Exception:
            pass
    nanovna_mirror.App(profile=devices.SV4401A).mainloop()
