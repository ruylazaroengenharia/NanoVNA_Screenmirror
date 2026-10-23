"""
Gera o ícone do NanoVNA Mirror (docs/img/icon.png e icon.ico).
Desenho: quadrado preto arredondado com a curva de ressonância (vale do S11) em branco.

Uso: python make_icon.py
"""
import math
import os

from PIL import Image, ImageDraw

SS = 8  # supersampling para bordas suaves


def draw_icon(size):
    """Desenha o ícone no tamanho pedido. Traço proporcionalmente mais grosso em tamanhos pequenos."""
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # fundo
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=(0, 0, 0, 255))

    # curva: linha plana, vale estreito e profundo, linha plana (resposta S11 de uma antena)
    m = S * 0.20                      # margem lateral
    top = S * 0.35                    # nível da linha plana
    depth = S * 0.33                  # profundidade do vale
    width = S * 0.085                 # largura do vale
    cx = S * 0.5
    stroke = S * (0.10 if size <= 24 else 0.08 if size <= 48 else 0.065)
    r = stroke / 2

    # o traço é feito "carimbando" círculos ao longo da curva: borda lisa e pontas arredondadas
    def y_of(x):
        u = (x - cx) / width
        return top + depth / (1 + u ** 4)  # lorentziana de 4ª ordem: vale definido, ombros planos

    x, step = m, max(r / 6, 1)
    while x <= S - m:
        y = y_of(x)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, 255))
        # passo menor onde a curva é íngreme, para não deixar falhas
        dy = abs(y_of(x + step) - y)
        x += step / max(1.0, dy / step)

    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "docs", "img")
    os.makedirs(out, exist_ok=True)
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    imgs = [draw_icon(s) for s in sizes]
    imgs[-1].save(os.path.join(out, "icon.png"))
    imgs[-1].save(os.path.join(here, "icon.ico"), format="ICO", sizes=[(s, s) for s in sizes],
                  append_images=imgs[:-1])
    print("Gerados: icon.ico e docs/img/icon.png")
