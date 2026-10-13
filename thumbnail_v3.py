"""Miniatura v3 de ENIGMAS DEL PASADO (estilo del piloto Valeriana, 13/10/2026).

Lo que dijo el CSV de Studio: CTR medio 1,96 % con la plantilla fija (visor, REC, placa,
dos lineas, teal-ambar oscuro). Lo que funciona en el nicho: foto o escena DIURNA, un
sujeto enorme, flecha o lupa, de 1 a 3 palabras, sin plantilla repetida. Aqui:

* estilos: "flecha" (texto + flecha al sujeto), "lupa" (circulo ampliado sobre el sujeto),
  "split" (antes/despues en diagonal con dos imagenes) y "texto" (solo texto enorme);
* nada de marco de visor, punto REC ni placa;
* maximo 3 palabras en total (se recorta si llegan mas) y cuerpo de letra que se lee a
  168 px de ancho (el tamano real en la lista del movil);
* luminancia media >= 120: si el fondo es oscuro se aclara antes de componer.
"""
import math
import os

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

W, H = 1280, 720
AQUI = os.path.dirname(os.path.abspath(__file__))
ANTON = os.path.join(AQUI, "fonts", "Anton-Regular.ttf")
AMARILLO = (255, 222, 40)
BLANCO = (255, 255, 255)
ROJO = (230, 30, 25)
LUMA_MIN = 120


def _quitar_bordes_lisos(im):
    """La IA a veces deja una franja lisa (blanca o de un color) donde se le pidio 'espacio libre'.
    Se recortan las columnas casi uniformes de los bordes antes de encuadrar."""
    g = np.asarray(im.convert("L"), dtype=np.float32)
    std = g.std(axis=0)
    w = len(std)
    izq = 0
    while izq < w * 0.4 and std[izq] < 6:
        izq += 1
    der = w
    while der > w * 0.6 and std[der - 1] < 6:
        der -= 1
    if izq > w * 0.03 or der < w * 0.97:
        im = im.crop((izq, 0, der, im.height))
    return im


def _cubrir(im):
    im = _quitar_bordes_lisos(im.convert("RGB"))
    r = max(W / im.width, H / im.height)
    im = im.resize((math.ceil(im.width * r), math.ceil(im.height * r)), Image.LANCZOS)
    x, y = (im.width - W) // 2, (im.height - H) // 2
    return im.crop((x, y, x + W, y + H))


def luma(im):
    return float(np.asarray(im.convert("L"), dtype=np.float32).mean())


def realzar(im):
    """Color vivo; contraste solo si hace falta, y gamma hasta LUMA_MIN si la imagen es oscura.

    La gamma levanta sombras y medios sin quemar las luces (subir el brillo lineal
    quemaba la piedra blanca de las fotos aereas)."""
    # Realce adaptativo: una imagen ya viva (modelos buenos) casi no se toca; una apagada, si.
    hsv = np.asarray(im.convert("HSV"), dtype=np.float32)
    sat = hsv[..., 1].mean()
    if sat < 90:
        im = ImageEnhance.Color(im).enhance(1.2)
    elif sat < 120:
        im = ImageEnhance.Color(im).enhance(1.06)
    l0 = luma(im)
    if l0 < 150 and np.asarray(im.convert("L"), dtype=np.float32).std() < 55:
        im = ImageEnhance.Contrast(im).enhance(1.12)
    if luma(im) < LUMA_MIN + 5:
        a = np.asarray(im, dtype=np.float32) / 255.0
        lo, hi = 0.25, 1.0
        for _ in range(18):                      # busca la gamma que deja la media en ~125
            g = (lo + hi) / 2
            m = (np.power(a, g).mean(axis=2) * 255).mean()
            lo, hi = (lo, g) if m < LUMA_MIN + 5 else (g, hi)
        im = Image.fromarray((np.power(a, (lo + hi) / 2) * 255).clip(0, 255).astype(np.uint8))
    return im


def foco_auto(im):
    """Punto de mas detalle (contraste local) fuera de la franja del texto (tercio izquierdo)."""
    g = np.asarray(im.convert("L").resize((128, 72)), dtype=np.float32)
    gx = np.abs(np.diff(g, axis=1, prepend=g[:, :1]))
    gy = np.abs(np.diff(g, axis=0, prepend=g[:1, :]))
    s = np.asarray(Image.fromarray((gx + gy).clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(6)),
                   dtype=np.float32)
    yy, xx = np.mgrid[0:72, 0:128]
    s *= np.exp(-(((xx / 128 - 0.66) / 0.28) ** 2 + ((yy / 72 - 0.45) / 0.32) ** 2))   # tercio central-derecho
    y, x = np.unravel_index(int(s.argmax()), s.shape)
    return (x + 0.5) / 128, (y + 0.5) / 72


def _texto(im, xy, t, size, color, anchor="la", stroke=10):
    f = ImageFont.truetype(ANTON, size)
    sombra = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(sombra).text((xy[0] + 8, xy[1] + 10), t, font=f, fill=(0, 0, 0, 170), anchor=anchor,
                                stroke_width=stroke, stroke_fill=(0, 0, 0, 170))
    sombra = sombra.filter(ImageFilter.GaussianBlur(6))
    im.paste(sombra, (0, 0), sombra)
    ImageDraw.Draw(im).text(xy, t, font=f, fill=color, anchor=anchor, stroke_width=stroke, stroke_fill=(10, 10, 10))
    return f.getbbox(t, anchor=anchor)


def _flecha(im, a, b, grosor=26, color=ROJO):
    d = ImageDraw.Draw(im)
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    for g, c in ((grosor + 14, BLANCO), (grosor, color)):
        d.line([a, (b[0] - math.cos(ang) * g * 1.5, b[1] - math.sin(ang) * g * 1.5)], fill=c, width=g)
        L = g * 2.6
        d.polygon([b, (b[0] - L * math.cos(ang - 0.5), b[1] - L * math.sin(ang - 0.5)),
                   (b[0] - L * math.cos(ang + 0.5), b[1] - L * math.sin(ang + 0.5))], fill=c)


def _palabras(l1, l2):
    """Como mucho 3 palabras en total, repartidas en dos lineas (blanca + amarilla)."""
    w1, w2 = (l1 or "").upper().split(), (l2 or "").upper().split()
    if not w2 and len(w1) >= 2:
        w1, w2 = w1[:-1], w1[-1:]
    # Si sobran palabras, primero caen articulos y preposiciones (no cambian el sentido);
    # solo despues se recorta, y siempre la linea blanca: la amarilla es el golpe.
    vacias = {"EL", "LA", "LOS", "LAS", "UN", "UNA", "UNOS", "UNAS", "LO", "DE", "DEL", "EN", "A", "AL", "Y", "QUE", "POR", "CON"}
    while len(w1) + len(w2) > 3:
        quitada = False
        for w in (w1, w2):
            for i, x in enumerate(w):
                if x in vacias and len(w) > 1:
                    del w[i]
                    quitada = True
                    break
            if quitada:
                break
        if not quitada:
            (w1 if len(w1) > 1 or not w2 else w2).pop()
    return " ".join(w1), " ".join(w2)


def _tamano(lineas, ancho_max, maximo=190, minimo=126):
    """Cuerpo maximo que cabe; nunca menos de 126 px (letra de ~12 px a 168 px de ancho)."""
    for size in range(maximo, minimo - 1, -4):
        f = ImageFont.truetype(ANTON, size)
        if all(f.getlength(l) <= ancho_max for l in lineas if l):
            return size
    return minimo


def componer(fondo, salida, linea1="", linea2="", estilo="flecha", fondo2=None, foco=None, foco2=None, zoom2=1.0):
    """Devuelve un dict de metricas (luma, cuerpo de letra, foco, palabras)."""
    base = realzar(_cubrir(Image.open(fondo)))
    fx, fy = foco or foco_auto(base)
    l1, l2 = _palabras(linea1, linea2)
    lado_izq = fx >= 0.42                      # el texto va al lado contrario del sujeto
    x_txt = 40 if lado_izq else W - 40
    anc = "la" if lado_izq else "ra"
    im = base.copy()

    if estilo == "split" and fondo2:
        otra = realzar(_cubrir(Image.open(fondo2)))
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).polygon([(470, 0), (W, 0), (W, H), (330, H)], fill=255)
        im = Image.composite(otra, base, m.filter(ImageFilter.GaussianBlur(2)))
        d = ImageDraw.Draw(im)
        for g, c in ((22, (150, 240, 255)), (7, BLANCO)):
            d.line([(470, 0), (330, H)], fill=c, width=g)
        x_txt, anc, lado_izq = 40, "la", True
        fx, fy = max(fx, 0.62), fy
    elif estilo == "lupa":
        fuente_lupa = realzar(_cubrir(Image.open(fondo2))) if fondo2 else base
        r = 250
        cx, cy = int(min(max(fx * W, r + 20), W - r - 20)), int(min(max(fy * H, r + 20), H - r - 20))
        # Sin fondo2, la lupa amplia el propio sujeto; con fondo2 REVELA otra imagen (lo oculto),
        # centrada en foco2 y con zoom2 (1 = el circulo abarca la altura de la imagen).
        if fondo2:
            f2x, f2y = foco2 or (0.5, 0.5)
            zx, zy, zoom = f2x * W, f2y * H, (2 * r / H) * zoom2
        else:
            zx, zy, zoom = fx * W, fy * H, 1.8
        bw, bh = 2 * r / zoom, 2 * r / zoom
        rec = fuente_lupa.transform((2 * r, 2 * r), Image.EXTENT, (zx - bw / 2, zy - bh / 2, zx + bw / 2, zy + bh / 2), Image.BICUBIC)
        m = Image.new("L", (2 * r, 2 * r), 0)
        ImageDraw.Draw(m).ellipse([0, 0, 2 * r, 2 * r], fill=255)
        im.paste(rec, (cx - r, cy - r), m)
        d = ImageDraw.Draw(im)
        d.ellipse([cx - r - 12, cy - r - 12, cx + r + 12, cy + r + 12], outline=(10, 10, 10), width=10)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=AMARILLO, width=14)
        fx, fy = cx / W, cy / H
        lado_izq = cx > W / 2
        x_txt, anc = (40, "la") if lado_izq else (W - 40, "ra")

    ancho_txt = W * (0.46 if estilo != "texto" else 0.9)
    size = _tamano([l1, l2], ancho_txt)
    # Si la zona del texto es muy clara (cielo blanco, arena), el texto blanco pierde contraste:
    # se oscurece SOLO ese lado con un degradado suave, sin tocar al sujeto.
    alto_txt = 30 + int(size * 1.08) * (bool(l1) + bool(l2))
    x0, x1 = (0, int(ancho_txt) + 60) if lado_izq else (W - int(ancho_txt) - 60, W)
    zona = im.crop((x0, 0, x1, min(H, alto_txt + 20)))
    if luma(zona) > 165:
        g = np.linspace(0.55, 0.0, x1 - x0) if lado_izq else np.linspace(0.0, 0.55, x1 - x0)
        alfa = np.tile(g, (H, 1))
        alfa *= np.clip(1.3 - np.linspace(0, 1, H)[:, None] * 0.9, 0, 1)   # mas fuerte arriba, donde va el texto
        sombra = Image.fromarray((alfa * 255).astype(np.uint8))
        bloque = im.crop((x0, 0, x1, H))
        im.paste(Image.composite(Image.new("RGB", bloque.size, (10, 20, 30)), bloque, sombra), (x0, 0))
    y = 30
    cajas = []
    if l1:
        cajas.append(_texto(im, (x_txt, y), l1, size, BLANCO, anc))
        y += int(size * 1.08)
    if l2:
        cajas.append(_texto(im, (x_txt, y), l2, size, AMARILLO, anc))
        y += int(size * 1.08)
    if estilo in ("flecha", "split", "lupa") and cajas:
        # La flecha sale del borde del bloque de texto hacia el sujeto, sin cruzar las letras.
        f = ImageFont.truetype(ANTON, size)
        ancho = max(f.getlength(l) for l in (l1, l2) if l)
        borde = x_txt + ancho + 30 if lado_izq else x_txt - ancho - 30
        origen = (borde, 30 + (y - 30) * 0.55)
        destino = (fx * W - (70 if lado_izq else -70), fy * H)
        if (destino[0] - origen[0]) * (1 if lado_izq else -1) > 120:
            _flecha(im, origen, destino)
    im.save(salida)
    return {"luma": round(luma(im), 1), "cuerpo_px": size, "letra_a_168px": round(size * 0.73 * 168 / W, 1),
            "foco": [round(fx, 2), round(fy, 2)], "texto": [l1, l2], "estilo": estilo}
