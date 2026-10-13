"""Motor de fotogramas del render v2 (estilo del piloto Valeriana, 13/10/2026).

Cada fotograma es funcion pura del tiempo `t` y de la linea de tiempo, asi que se
puede renderizar en paralelo por tramos. El encuadre se hace con
`Image.transform(EXTENT)` en coma flotante: movimiento sub-pixel, sin el temblor
del `zoompan` de ffmpeg (que redondea la entrada a pixeles enteros).
"""
import functools
import math
import os

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

W, H, FPS = 1920, 1080, 30
AQUI = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(os.path.dirname(AQUI), "fonts")


def _primera(*rutas):
    for r in rutas:
        if os.path.exists(r):
            return r
    return rutas[-1]


FUENTES = {
    "anton": os.path.join(FONTS, "Anton-Regular.ttf"),
    "serif": os.path.join(FONTS, "CormorantGaramond-MediumItalic.ttf"),
    # Subtitulo del canal: Liberation Sans Bold en el contenedor, Arial Bold en local.
    "sub": _primera("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                    "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
}
AMARILLO = (249, 216, 74)
ROJO = (214, 40, 32)
BLANCO = (255, 255, 255)


@functools.lru_cache(maxsize=64)
def fuente(nombre, size):
    return ImageFont.truetype(FUENTES[nombre], size)


def ease(p):
    return 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, p)))


def lerp(a, b, p):
    return a + (b - a) * p


def clamp01(x):
    return max(0.0, min(1.0, x))


# ── imagenes ───────────────────────────────────────────────

RUTAS = {}   # clave -> ruta local; lo rellena el orquestador antes de renderizar


@functools.lru_cache(maxsize=32)
def imagen(clave):
    im = Image.open(RUTAS[clave]).convert("RGB")
    if im.width > 3200:
        im = im.resize((3200, round(im.height * 3200 / im.width)), Image.LANCZOS)
    return im


def encuadre(src, cx, cy, z):
    """z = ancho visible / ancho de la imagen. Caja 16:9 siempre dentro de la imagen."""
    w, h = src.size
    zmax = min(1.0, (h * W / H) / w)
    z = min(z, zmax)
    bw = w * z
    bh = bw * H / W
    x0 = min(max(cx * w - bw / 2, 0), w - bw)
    y0 = min(max(cy * h - bh / 2, 0), h - bh)
    return (x0, y0, x0 + bw, y0 + bh)


def gradar(fr, brillo=1.0):
    fr = ImageEnhance.Contrast(fr).enhance(1.08)
    fr = ImageEnhance.Color(fr).enhance(1.10)
    if brillo != 1.0:
        fr = ImageEnhance.Brightness(fr).enhance(brillo)
    return fr


@functools.lru_cache(maxsize=12)
def preparada(clave, zmin, brillo=1.0):
    """Imagen escalada al tamano justo que pide el encuadre mas cerrado del plano (1,15x la
    salida) y ya gradada. Se hace UNA vez por plano: encuadrar desde 3.200 px con BICUBIC y
    gradar cada fotograma costaba ~70 ms; asi el fotograma queda en ~15-20 ms con BILINEAR."""
    src = imagen(clave)
    escala = min(1.0, (1.15 * W) / (max(zmin, 0.05) * src.width))
    if escala < 0.98:
        src = src.resize((max(W, round(src.width * escala)), max(1, round(src.height * escala))), Image.LANCZOS)
    return gradar(src, brillo)


def render_img(sh, p):
    m = sh["mov"]
    zmin = round(min(m["z"]), 3)
    src = preparada(sh["src"], zmin, sh.get("brillo", 1.0))
    e = ease(p)
    caja = encuadre(src, lerp(m["cx"][0], m["cx"][1], e), lerp(m["cy"][0], m["cy"][1], e),
                    lerp(m["z"][0], m["z"][1], e))
    fr = src.transform((W, H), Image.EXTENT, caja, Image.BILINEAR)
    sh["_caja"], sh["_size"] = caja, src.size      # para colocar marcas sobre la imagen
    return fr


def render_scan(sh, p):
    """Barrido de escaner: la imagen A se revela como B tras una linea luminosa (estilo piloto)."""
    a = render_img(dict(sh, src=sh["a"]), p)
    b = render_img(dict(sh, src=sh["b"]), p)
    xl = int(lerp(-80, W + 80, ease(clamp01((p - 0.1) / 0.75))))
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).rectangle([0, 0, max(0, xl), H], fill=255)
    fr = Image.composite(b, a, m)
    d = ImageDraw.Draw(fr)
    for k, col in ((26, (120, 230, 255)), (8, (230, 250, 255))):
        d.line([(xl, 0), (xl, H)], fill=col, width=k)
    return fr


@functools.lru_cache(maxsize=16)
def fondo_desenfocado(clave, oscuro=0.45):
    src = imagen(clave)
    fr = src.transform((W, H), Image.EXTENT, encuadre(src, 0.5, 0.5, 1.0), Image.BICUBIC)
    return ImageEnhance.Brightness(fr.filter(ImageFilter.GaussianBlur(18))).enhance(oscuro)


def _con_sombra(im, capa, dx=8, dy=10, r=12):
    sombra = capa.filter(ImageFilter.GaussianBlur(r))
    negro = Image.new("L", (W, H), 0)
    sombra = Image.merge("RGBA", (negro, negro, negro, sombra.getchannel("A")))
    im.paste(sombra, (dx, dy), sombra)
    im.paste(capa, (0, 0), capa)


def render_titulo(sh, p):
    im = fondo_desenfocado(sh["fondo"], 0.5).copy()
    z = 1 + 0.06 * p
    im = im.resize((int(W * z), int(H * z)), Image.BICUBIC)
    ox, oy = int(W * (z - 1) / 2), int(H * (z - 1) / 2)
    im = im.crop((ox, oy, ox + W, oy + H))
    a = clamp01(p * 3)
    esp = int(lerp(60, 18, ease(clamp01(p * 2))))
    txt = sh["texto"]
    size = 230 if len(txt) <= 10 else max(110, int(230 * 10 / len(txt)))
    f = fuente("anton", size)
    d = ImageDraw.Draw(im)
    anchos = [d.textlength(c, font=f) for c in txt]
    total = sum(anchos) + esp * (len(txt) - 1)
    x = W / 2 - total / 2
    capa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dc = ImageDraw.Draw(capa)
    for c, w_ in zip(txt, anchos):
        dc.text((x, H / 2 - 40), c, font=f, fill=AMARILLO + (int(255 * a),), anchor="lm",
                stroke_width=6, stroke_fill=(30, 20, 10, int(255 * a)))
        x += w_ + esp
    if sh.get("sub"):
        dc.text((W / 2, H / 2 + 120), sh["sub"], font=fuente("anton", 52),
                fill=(255, 255, 255, int(255 * clamp01(p * 3 - 0.6))), anchor="mm")
    _con_sombra(im, capa, r=14)
    return im


def _partir(texto, ancho_max, f, d):
    lineas, cur = [], []
    for w in texto.split():
        if cur and d.textlength(" ".join(cur + [w]), font=f) > ancho_max:
            lineas.append(" ".join(cur))
            cur = []
        cur.append(w)
    if cur:
        lineas.append(" ".join(cur))
    return lineas


def render_cita(sh, p):
    im = fondo_desenfocado(sh["fondo"], 0.38).copy()
    z = 1 + 0.04 * p
    im = im.resize((int(W * z), int(H * z)), Image.BICUBIC).crop((0, 0, W, H))
    a = int(255 * clamp01(p * 4))
    capa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dc = ImageDraw.Draw(capa)
    f = fuente("serif", 80)
    lineas = _partir("«" + sh["texto"].strip("«»\"") + "»", 1500, f, dc)[:5]
    y = H / 2 - (len(lineas) * 100) / 2 - 20
    for l in lineas:
        dc.text((W / 2, y), l, font=f, fill=(255, 255, 255, a), anchor="mm")
        y += 100
    dc.text((W / 2, y + 40), sh.get("autor", ""), font=fuente("anton", 44), fill=AMARILLO + (a,), anchor="mm")
    im.paste(capa, (0, 0), capa)
    return im


# ── superposiciones ────────────────────────────────────────

def ov_placa(im, ov, lt):
    a = clamp01(lt / 0.35) * clamp01((ov["dur"] - lt) / 0.4)
    dx = int(-420 * (1 - ease(clamp01(lt / 0.45))))
    capa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    d.rectangle([60 + dx, 70, 72 + dx, 160], fill=ROJO + (int(255 * a),))
    d.text((92 + dx, 92), ov["l1"], font=fuente("anton", 56), fill=(255, 255, 255, int(255 * a)),
           anchor="lm", stroke_width=3, stroke_fill=(0, 0, 0, int(200 * a)))
    if ov.get("l2"):
        d.text((94 + dx, 142), ov["l2"], font=fuente("sub", 30), fill=(255, 255, 255, int(230 * a)),
               anchor="lm", stroke_width=2, stroke_fill=(0, 0, 0, int(200 * a)))
    im.paste(capa, (0, 0), capa)


def ov_cifra(im, ov, lt):
    a = clamp01(lt / 0.2) * clamp01((ov["dur"] - lt) / 0.3)
    im.paste(Image.blend(im, Image.new("RGB", (W, H)), 0.42 * a), (0, 0))
    s = lerp(0.82, 1.0, ease(clamp01(lt / 0.28)))
    capa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    g = ov["grande"]
    if ov.get("contar"):
        # Contador: la parte numerica sube desde 0 en 0,8 s (6.764 -> 0 ... 6.764)
        import re as _re
        m_ = _re.search(r"[\d.]+", g)
        if m_:
            objetivo = int(m_.group().replace(".", "") or 0)
            n_ = int(objetivo * ease(clamp01(lt / 0.8)))
            txt_n = f"{n_:,}".replace(",", ".") if "." in m_.group() else str(n_)
            g = g[:m_.start()] + txt_n + g[m_.end():]
    size = int((210 if len(g) <= 8 else max(110, 210 * 8 / len(g))) * s)
    d.text((W / 2, H / 2 - 50), g, font=fuente("anton", size), fill=AMARILLO + (int(255 * a),),
           anchor="mm", stroke_width=7, stroke_fill=(25, 15, 5, int(255 * a)))
    if ov.get("peq"):
        d.text((W / 2, H / 2 + 105), ov["peq"], font=fuente("anton", 58), fill=(255, 255, 255, int(255 * a)),
               anchor="mm", stroke_width=3, stroke_fill=(0, 0, 0, int(255 * a)))
    _con_sombra(im, capa, 6, 8, 10)


def ov_nombre(im, ov, lt):
    a = clamp01(lt / 0.3) * clamp01((ov["dur"] - lt) / 0.4)
    dy = int(30 * (1 - ease(clamp01(lt / 0.4))))
    capa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    f1 = fuente("anton", 60)
    w1 = d.textlength(ov["n"], font=f1)
    d.rectangle([60, 640 + dy, 60 + w1 + 50, 720 + dy], fill=(15, 15, 15, int(215 * a)))
    d.rectangle([60, 640 + dy, 70, 720 + dy], fill=AMARILLO + (int(255 * a),))
    d.text((90, 680 + dy), ov["n"], font=f1, fill=(255, 255, 255, int(255 * a)), anchor="lm")
    if ov.get("r"):
        f2 = fuente("sub", 32)
        w2 = d.textlength(ov["r"], font=f2)
        d.rectangle([60, 722 + dy, 60 + w2 + 50, 772 + dy], fill=AMARILLO + (int(235 * a),))
        d.text((90, 747 + dy), ov["r"], font=f2, fill=(20, 20, 20, int(255 * a)), anchor="lm")
    im.paste(capa, (0, 0), capa)


def ov_capitulo(im, ov, lt):
    a = clamp01(lt / 0.25) * clamp01((ov["dur"] - lt) / 0.35)
    capa = Image.new("RGBA", (W, H), (0, 0, 0, int(110 * a)))
    d = ImageDraw.Draw(capa)
    d.text((W / 2, H / 2 - 30), ov["num"], font=fuente("anton", 120), fill=AMARILLO + (int(255 * a),), anchor="mm")
    d.text((W / 2, H / 2 + 80), ov["txt"], font=fuente("anton", 86), fill=(255, 255, 255, int(255 * a)),
           anchor="mm", stroke_width=3, stroke_fill=(0, 0, 0, int(255 * a)))
    im.paste(capa, (0, 0), capa)


def ov_marca(im, ov, lt, sh=None):
    if not sh or "_caja" not in sh:
        return
    x0, y0, x1, y1 = sh["_caja"]
    w, h = sh["_size"]
    x = (ov["x"] * w - x0) / (x1 - x0) * W
    y = (ov["y"] * h - y0) / (y1 - y0) * H
    a = clamp01(lt / 0.25) * clamp01((ov["dur"] - lt) / 0.3)
    if a <= 0:
        return
    capa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    r = ov.get("r", 70) * (1 + 0.08 * math.sin(lt * 6))
    for k, al in ((10, 90), (6, 255)):
        d.ellipse([x - r, y - r, x + r, y + r], outline=AMARILLO + (int(al * a),), width=k)
    lado = 1 if x < W * 0.6 else -1
    tx, ty = x + lado * (r + 30), y - r * 0.6
    d.line([(x + lado * r * 0.7, y - r * 0.7), (tx, ty)], fill=AMARILLO + (int(255 * a),), width=5)
    f = fuente("anton", 64)
    tw = d.textlength(ov["txt"], font=f)
    bx = tx if lado > 0 else tx - tw - 36
    d.rectangle([bx, ty - 44, bx + tw + 36, ty + 40], fill=(15, 15, 15, int(220 * a)))
    d.text((bx + 18, ty), ov["txt"], font=f, fill=AMARILLO + (int(255 * a),), anchor="lm")
    im.paste(capa, (0, 0), capa)


def ov_credito(im, txt):
    capa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    d.text((W - 28, H - 24), txt[:150], font=fuente("sub", 20), fill=(255, 255, 255, 190), anchor="rs",
           stroke_width=2, stroke_fill=(0, 0, 0, 160))
    im.paste(capa, (0, 0), capa)


OVERLAYS = {"placa": ov_placa, "cifra": ov_cifra, "nombre": ov_nombre, "capitulo": ov_capitulo}


# ── subtitulos (estilo del canal) ──────────────────────────

@functools.lru_cache(maxsize=256)
def capa_sub(texto):
    f = fuente("sub", 64)
    capa = Image.new("RGBA", (W, 260), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    lineas = texto.split("\n")
    y = 250 - (len(lineas) - 1) * 78
    for l in lineas:
        d.text((W / 2 + 3, y + 3), l, font=f, fill=(0, 0, 0, 150), anchor="ms", stroke_width=5,
               stroke_fill=(0, 0, 0, 150))
        d.text((W / 2, y), l, font=f, fill=BLANCO, anchor="ms", stroke_width=4, stroke_fill=(0, 0, 0))
        y += 78
    return capa


def pegar_sub(im, cue, t):
    a = clamp01((t - cue["ini"]) / 0.2) * clamp01((cue["fin"] - t) / 0.2)
    if a <= 0:
        return
    c = capa_sub(cue["txt"])
    if a < 1:
        c = c.copy()
        c.putalpha(c.getchannel("A").point(lambda v: int(v * a)))
    im.paste(c, (0, H - 162 - 250), c)


# ── fotograma completo ─────────────────────────────────────

RENDER = {"img": render_img, "titulo": render_titulo, "cita": render_cita, "scan": render_scan}


def _indice(tl, t):
    planos = tl["planos"]
    lo, hi = 0, len(planos) - 1
    while lo < hi:   # busqueda binaria: ~400 planos por video
        mid = (lo + hi + 1) // 2
        if planos[mid]["t0"] <= t:
            lo = mid
        else:
            hi = mid - 1
    return lo


def dibujar_plano(sh, t):
    p = clamp01((t - sh["t0"]) / max(1e-3, sh["t1"] - sh["t0"]))
    return RENDER[sh["tipo"]](sh, p)


def fotograma(tl, t):
    i = _indice(tl, t)
    sh = tl["planos"][i]
    fr = dibujar_plano(sh, t)
    lt = t - sh["t0"]
    tr = sh.get("trans", "corte")
    tdur = sh.get("tdur", 0.35)
    if i > 0 and tr in ("fundido", "barrido") and lt < tdur:
        q = lt / tdur
        prev = dibujar_plano(tl["planos"][i - 1], t)
        if tr == "fundido":
            fr = Image.blend(prev, fr, ease(q))
        else:
            off = int(W * ease(q))
            bl = lambda x: x.resize((W // 14, H), Image.BILINEAR).resize((W, H), Image.BILINEAR)
            lienzo = Image.new("RGB", (W, H))
            lienzo.paste(bl(prev), (-off, 0))
            lienzo.paste(bl(fr), (W - off, 0))
            fr = lienzo
    if sh.get("negro_in") and lt < sh["negro_in"]:
        fr = Image.blend(Image.new("RGB", (W, H)), fr, lt / sh["negro_in"])
    if sh.get("negro_out") and sh["t1"] - t < sh["negro_out"]:
        fr = Image.blend(Image.new("RGB", (W, H)), fr, max(0.0, (sh["t1"] - t) / sh["negro_out"]))
    if sh.get("flash_en") is not None and 0 <= t - sh["flash_en"] < 0.14:
        fr = Image.blend(fr, Image.new("RGB", (W, H), (255, 250, 235)), 0.75 * (1 - (t - sh["flash_en"]) / 0.14))
    if sh.get("flash") and lt < 0.14:
        fr = Image.blend(fr, Image.new("RGB", (W, H), (255, 250, 235)), 0.75 * (1 - lt / 0.14))
    for ov in sh.get("ovs", []):
        olt = t - (sh["t0"] + ov.get("d", 0))
        if 0 <= olt < ov["dur"]:
            if ov["k"] == "marca":
                ov_marca(fr, ov, olt, sh)
            else:
                OVERLAYS[ov["k"]](fr, ov, olt)
    if sh.get("credito"):
        ov_credito(fr, sh["credito"])
    for cue in tl["subs_por_plano"].get(str(i), []):
        cue = tl["subs"][cue]
        if cue["ini"] - 0.05 <= t <= cue["fin"] + 0.05:
            pegar_sub(fr, cue, t)
    return fr
