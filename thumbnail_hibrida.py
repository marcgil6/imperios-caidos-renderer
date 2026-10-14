"""Miniatura HIBRIDA de ENIGMAS DEL PASADO (aprobada por Marc el 14/10/2026).

Junta lo que funcionaba de las dos epocas:

* de las antiguas, la escena de cine (figura humana para la escala, antorcha, sol, rayos);
* de las v3, la imagen luminosa, como mucho 3 palabras, la flecha roja y la lupa que revela.

Lo nuevo:

* `grade_cine`: levanta las sombras hasta una luma de ~118 sin aplanar (gamma), las luces
  irradian (bloom) y gana claridad. Las antiguas tenian luma 46-63 y no se leian en el movil.
* La lupa va AL LADO del heroe, nunca encima: se prueban varias posiciones en la mitad derecha
  y gana la que tapa menos detalle. En la primera prueba tapaba al explorador y a las piedras
  de Stonehenge y se perdia justo lo impactante.
* Lupa realista: cristal curvado con brillo especular, aro amarillo biselado, halo, sombra y mango.
* Sin fondo2 la lupa amplia el propio fondo, pero desde el ORIGINAL a doble resolucion
  (ampliando la miniatura de 1280 salia pixelado). Con fondo2 REVELA lo oculto, que es
  cuando de verdad engancha (LiDAR, rayos X, el interior).
* El titular lleva el acabado de impacto de thumbnail_ep (relieve granate, filete rojo).
"""
import math

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

import thumbnail_ep as ep
import thumbnail_v3 as v3

W, H = v3.W, v3.H
AMARILLO = v3.AMARILLO
CARA_BLANCA = ("#FFFFFF", "#F4EFE6", "#C9BFAC")
CARA_AMARILLA = ("#FFFDE0", "#F9EC3A", "#F5D400")
LUMA_OBJETIVO = 118
RADIO = 200
# Centros candidatos de la lupa (fracciones de W, H): siempre en la mitad derecha, el texto va a la izquierda.
CANDIDATOS = ((0.74, 0.62), (0.80, 0.50), (0.64, 0.66), (0.82, 0.68), (0.70, 0.45), (0.56, 0.68))


def cubrir(im, k=1):
    """Como v3._cubrir pero a k veces la resolucion."""
    im = v3._quitar_bordes_lisos(im.convert("RGB"))
    w, h = W * k, H * k
    s = max(w / im.width, h / im.height)
    im = im.resize((math.ceil(im.width * s), math.ceil(im.height * s)), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


def grade_cine(im, objetivo=LUMA_OBJETIVO):
    """Conserva el drama pero levanta las sombras hasta ~objetivo de luma; las luces irradian."""
    a = np.asarray(im, dtype=np.float32) / 255.0
    if v3.luma(im) < objetivo:
        lo, hi = 0.2, 1.0
        for _ in range(18):
            g = (lo + hi) / 2
            m = (np.power(a, g).mean(axis=2) * 255).mean()
            lo, hi = (lo, g) if m < objetivo else (g, hi)
        a = np.power(a, (lo + hi) / 2)
    im = Image.fromarray((a * 255).clip(0, 255).astype(np.uint8))
    lum = np.asarray(im.convert("L"), dtype=np.float32)
    mask = Image.fromarray((np.clip((lum - 190) / 50, 0, 1) * 255).astype(np.uint8))
    radio = 28 * im.width / W
    brillo = Image.composite(im, Image.new("RGB", im.size), mask).filter(ImageFilter.GaussianBlur(radio))
    im = ImageChops.screen(im, ImageEnhance.Brightness(brillo).enhance(0.55))
    im = im.filter(ImageFilter.UnsharpMask(radius=4, percent=55, threshold=2))
    return ImageEnhance.Color(im).enhance(1.12)


def _multiplicar(im, f):
    a = np.asarray(im, dtype=np.float32) * f[..., None]
    return Image.fromarray(a.clip(0, 255).astype(np.uint8))


def vineta(im, fuerza=0.28):
    yy, xx = np.mgrid[0:H, 0:W]
    d = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2) / math.sqrt(2)
    return _multiplicar(im, 1 - fuerza * np.clip((d - 0.45) / 0.55, 0, 1) ** 1.6)


def foco_luz(im, c, r, fuerza=0.18):
    """Atenua un poco lo que queda lejos de la lupa para que el ojo vaya a ella."""
    yy, xx = np.mgrid[0:H, 0:W]
    d = np.hypot(xx - c[0], yy - c[1])
    return _multiplicar(im, 1 - fuerza * np.clip((d - r * 1.2) / (W * 0.6), 0, 1))


def detalle(im):
    """Mapa de detalle (contraste local) a 128x72: alto donde esta el heroe."""
    g = np.asarray(im.convert("L").resize((128, 72)), dtype=np.float32)
    gx = np.abs(np.diff(g, axis=1, prepend=g[:, :1]))
    gy = np.abs(np.diff(g, axis=0, prepend=g[:1, :]))
    s = Image.fromarray((gx + gy).clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(3))
    return np.asarray(s, dtype=np.float32)


def sitio_lupa(im, r=RADIO):
    """Centro (px) de la mitad derecha donde la lupa tapa menos detalle."""
    s = detalle(im)
    yy, xx = np.mgrid[0:72, 0:128]
    mejor, puntos = None, None
    for fx, fy in CANDIDATOS:
        cx, cy = _encajar((fx * W, fy * H), r)
        dentro = np.hypot((xx + 0.5) * W / 128 - cx, (yy + 0.5) * H / 72 - cy) < r
        p = float(s[dentro].mean())
        if puntos is None or p < puntos:
            mejor, puntos = (cx, cy), p
    return mejor


def _encajar(c, r):
    return (int(min(max(c[0], r + 30), W - r - 30)), int(min(max(c[1], r + 30), H - r - 30)))


def lupa(im, fuente, centro, r, zona, zoom, revela=False):
    """Pega una lupa realista en `centro`; amplia `zona` (0-1) de `fuente` (que puede ser 2x)."""
    cx, cy = centro
    k = fuente.width / W
    zx, zy = zona[0] * fuente.width, zona[1] * fuente.height
    lado = 2 * r / (zoom / k)
    rec = fuente.transform((2 * r, 2 * r), Image.EXTENT, (zx - lado / 2, zy - lado / 2, zx + lado / 2, zy + lado / 2),
                           Image.BICUBIC)
    rec = ImageEnhance.Contrast(rec).enhance(1.12)
    rec = ImageEnhance.Brightness(rec).enhance(1.0 if revela else 1.10)
    rec = rec.filter(ImageFilter.UnsharpMask(radius=1.5, percent=45, threshold=3))
    # Cristal: el borde oscurece (curvatura) y hay un brillo especular arriba a la izquierda.
    yy, xx = np.mgrid[0:2 * r, 0:2 * r]
    d = np.hypot(xx - r, yy - r) / r
    rec = _multiplicar(rec, 1 - 0.38 * np.clip((d - 0.72) / 0.28, 0, 1) ** 1.5)
    esp = Image.new("L", (2 * r, 2 * r), 0)
    ImageDraw.Draw(esp).ellipse([r * 0.28, r * 0.16, r * 1.05, r * 0.62], fill=120)
    esp = esp.filter(ImageFilter.GaussianBlur(r * 0.09))
    corte = Image.new("L", (2 * r, 2 * r), 0)                    # media luna: se recorta por abajo
    ImageDraw.Draw(corte).ellipse([r * 0.18, r * 0.34, r * 1.25, r * 0.95], fill=255)
    esp = ImageChops.subtract(esp, corte.filter(ImageFilter.GaussianBlur(r * 0.06)))
    rec = Image.composite(Image.new("RGB", rec.size, (255, 255, 255)), rec, esp)
    m = Image.new("L", (2 * r, 2 * r), 0)
    ImageDraw.Draw(m).ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)

    base = im.convert("RGBA")
    ang = math.radians(48)
    p0 = (cx + math.cos(ang) * (r + 6), cy + math.sin(ang) * (r + 6))
    p1 = (cx + math.cos(ang) * (r + 190), cy + math.sin(ang) * (r + 190))
    sombra = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ds = ImageDraw.Draw(sombra)
    ds.ellipse([cx - r + 2, cy - r + 6, cx + r + 30, cy + r + 34], fill=(0, 0, 0, 150))
    ds.line([(p0[0] + 16, p0[1] + 20), (p1[0] + 16, p1[1] + 20)], fill=(0, 0, 0, 150), width=58)
    base.alpha_composite(sombra.filter(ImageFilter.GaussianBlur(18)))
    halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse([cx - r - 26, cy - r - 26, cx + r + 26, cy + r + 26], outline=AMARILLO + (210,), width=26)
    base.alpha_composite(halo.filter(ImageFilter.GaussianBlur(16)))
    # Mango: negro, cuerpo de madera oscura, filo de luz y casquillo amarillo.
    capa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dc = ImageDraw.Draw(capa)
    dc.line([p0, p1], fill=(10, 10, 10, 255), width=60)
    dc.line([p0, p1], fill=(55, 38, 26, 255), width=44)
    off = (-math.sin(ang) * 9, math.cos(ang) * -9)
    dc.line([(p0[0] + off[0], p0[1] + off[1]), (p1[0] + off[0], p1[1] + off[1])], fill=(140, 100, 60, 255), width=8)
    dc.line([(p0[0] + math.cos(ang) * 4, p0[1] + math.sin(ang) * 4),
             (p0[0] + math.cos(ang) * 34, p0[1] + math.sin(ang) * 34)], fill=AMARILLO + (255,), width=52)
    base.alpha_composite(capa)
    base.paste(rec, (cx - r, cy - r), m)
    d = ImageDraw.Draw(base)
    d.ellipse([cx - r - 16, cy - r - 16, cx + r + 16, cy + r + 16], outline=(10, 10, 10, 255), width=8)
    d.ellipse([cx - r - 9, cy - r - 9, cx + r + 9, cy + r + 9], outline=AMARILLO + (255,), width=18)
    bisel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    db = ImageDraw.Draw(bisel)
    db.arc([cx - r - 7, cy - r - 7, cx + r + 7, cy + r + 7], 160, 290, fill=(255, 250, 210, 230), width=6)
    db.arc([cx - r - 7, cy - r - 7, cx + r + 7, cy + r + 7], 340, 110, fill=(150, 95, 0, 200), width=7)
    base.alpha_composite(bisel.filter(ImageFilter.GaussianBlur(1.2)))
    d.ellipse([cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1], outline=(10, 10, 10, 255), width=4)
    return base.convert("RGB")


def flecha(im, a, b, grosor=24):
    """Flecha roja de las v3 con sombra paralela suave."""
    t = Image.new("RGB", (W, H), (0, 0, 0))
    v3._flecha(t, (a[0] + 8, a[1] + 10), (b[0] + 8, b[1] + 10), grosor=grosor, color=(255, 255, 255))
    s = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    s.putalpha(t.convert("L").point(lambda p: 150 if p > 0 else 0).filter(ImageFilter.GaussianBlur(7)))
    im = im.convert("RGBA")
    im.alpha_composite(s)
    im = im.convert("RGB")
    v3._flecha(im, a, b, grosor=grosor)
    return im


def _oscurecer_texto(im, alto):
    """Si la zona del texto es clara, un degradado oscuro suave solo por detras de las letras."""
    if v3.luma(im.crop((0, 0, W // 2, alto))) <= 140:
        return im
    g = np.linspace(0.5, 0, W // 2)
    alfa = np.tile(g, (H, 1)) * np.clip(1.25 - np.linspace(0, 1, H)[:, None] * 1.2, 0, 1)
    blq = im.crop((0, 0, W // 2, H))
    im = im.copy()
    im.paste(Image.composite(Image.new("RGB", blq.size, (8, 12, 20)), blq,
                             Image.fromarray((alfa * 255).astype(np.uint8))), (0, 0))
    return im


def componer(fondo, salida, linea1="", linea2="", fondo2=None, foco=None, foco2=None, zoom2=1.0,
             lupa_c=None, radio=RADIO, zoom=1.9):
    """Escena + lupa + titular. Devuelve metricas como thumbnail_v3.componer."""
    original = Image.open(fondo)
    base = grade_cine(cubrir(original))
    r = int(radio)
    lc = _encajar((lupa_c[0] * W, lupa_c[1] * H), r) if lupa_c else sitio_lupa(base, r)
    im = vineta(foco_luz(base, lc, r))
    if fondo2:
        fuente = ImageEnhance.Color(cubrir(Image.open(fondo2), 2)).enhance(1.1)
        im = lupa(im, fuente, lc, r, foco2 or (0.5, 0.5), (2 * r / H) * float(zoom2), revela=True)
    else:
        # Sin fondo2 se amplia el punto de mas detalle del fondo, sacado del original a 2x.
        zona = foco or v3.foco_auto(base)
        hd = grade_cine(cubrir(original, 2), objetivo=v3.luma(base))
        im = lupa(im, hd, lc, r, zona, zoom)
    p1, p2 = v3._palabras(linea1, linea2)
    size = v3._tamano([p1, p2], W * 0.44, maximo=184, minimo=120)
    f = ImageFont.truetype(v3.ANTON, size)
    im = _oscurecer_texto(im, int(size * 2.4))
    lienzo = im.convert("RGBA")
    x, y = 44, 26
    anchos = []
    for t, cara in ((p1, CARA_BLANCA), (p2, CARA_AMARILLA)):
        if t:
            ep.pegar_impacto(lienzo, (x, y), t, f, cara, anchor="la")
            anchos.append(f.getlength(t))
            y += int(size * 1.06)
    im = lienzo.convert("RGB")
    con_flecha = False
    if anchos:
        origen = (x + max(anchos) + 26, 26 + (y - 26) * 0.5)
        ang = math.atan2(lc[1] - origen[1], lc[0] - origen[0])
        destino = (lc[0] - math.cos(ang) * (r * 0.55), lc[1] - math.sin(ang) * (r * 0.55))
        if destino[0] - origen[0] > 110:
            im = flecha(im, origen, destino)
            con_flecha = True
    im.save(salida)
    return {"luma": round(v3.luma(im), 1), "cuerpo_px": size, "letra_a_168px": round(size * 0.73 * 168 / W, 1),
            "foco": [round(lc[0] / W, 2), round(lc[1] / H, 2)], "texto": [p1, p2], "estilo": "hibrida",
            "flecha": con_flecha}
