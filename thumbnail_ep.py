#!/usr/bin/env python3
"""Compositor de miniaturas ENIGMAS DEL PASADO.

Fondo generado por IA (sin texto) + titulares, cartel y marco de visor por codigo.
Tipografia, tamanos y posiciones identicos en todas las miniaturas del canal.
"""
import argparse, os
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter, ImageChops
import numpy as np

RAIZ = os.path.dirname(os.path.abspath(__file__))


def _primera_que_exista(*rutas):
    for r in rutas:
        if r and os.path.exists(r):
            return r
    return None


# En el contenedor las fuentes viven en /app/fonts; en el Mac de Marc, en
# enigmas-del-pasado/assets. Se prueban las dos para que el mismo fichero sirva
# en local y en el servicio.
FUENTE = _primera_que_exista(
    os.environ.get("EP_FUENTE_ANTON"),
    os.path.join(RAIZ, "fonts", "Anton-Regular.ttf"),
    os.path.join(os.path.dirname(RAIZ), "enigmas-del-pasado", "assets", "Anton-Regular.ttf"),
)
# Si falta la fuente NO se levanta aqui: este modulo se importa al arrancar el
# servicio, y una excepcion en el import tumbaria tambien el render de video,
# que no tiene nada que ver con las miniaturas. Falla solo quien la use.
FUENTE_OK = FUENTE is not None

# El wordmark iba en Impact, que es una fuente del sistema de macOS y NO existe
# en el contenedor. Sin este fallback la composicion revienta en el servidor
# justo en el ultimo paso, despues de haber pagado el fondo.
FUENTE_MARCA = _primera_que_exista(
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    os.path.join(RAIZ, "fonts", "Impact.ttf"),
) or FUENTE


def fuentes_ok():
    """Para que /health lo diga antes de que alguien pague un fondo de IA."""
    return FUENTE_OK


W, H = 1280, 720

# --- marco de visor (medido en las referencias de Amazing Earth) ----------
BR_GROSOR, BR_BRAZO, BR_INSET, BR_ALPHA = 4, 70, 65, 217
WORDMARK = "ENIGMAS DEL PASADO"

# --- zona segura del texto: por dentro del visor, sin tocarlo nunca ------
# los brazos ocupan 65..135 px desde cada borde; el texto arranca en 86/100
MARGEN_X   = 86
MARGEN_SUP = 100
MARGEN_INF = 124   # despeja el corchete inferior con holgura

BLANCO, AMARILLO, ROJO = "#FFFFFF", "#F7E30C", "#E01B1B"
# Los titulares subieron un 21 % el 22/09/2026 (dos pasadas de +10 %) (orden de Marc, junto con el
# acabado de cartel de impacto): el relieve y el contorno se comen parte de la
# mancha de la letra, asi que al mismo cuerpo el titular pesaba menos que con el
# relleno plano de antes. El wordmark y el cartel NO cambian.
CAP_T1, CAP_T1_2L, CAP_T2, CAP_CARTEL = 117, 101, 85, 46
ANCHO_CARTEL   = 620   # el cartel se encoge solo hasta caber aqui
CONTORNO_CARTEL = 5
CAP_MARCA, TRACKING_MARCA = 28, 3
CAP_T1_INF = 105    # titular en el reparto inferior: mas bajo, tapa menos
CAP_T2_INF = 58
CAP_DETALLE = 32    # el cartel de arriba, a la altura del wordmark
Y_BANDA_MARCA = BR_INSET + BR_BRAZO // 2   # eje de la banda superior:
                                           # media altura del brazo del corchete
INTERLINEADO   = 0.95
CONTORNO_T1, CONTORNO_T2 = 8, 6
ANCHO_CAJA_T1  = 470


# --- titular "cartel de impacto" (aprobado por Marc 22/09/2026) ----------
# Sustituye al relleno plano con contorno negro. La referencia es
# ~/Downloads/titular-ladrones.png. El acabado (filete rojo, extrusion granate,
# contorno negro, sombra y brillo) es SIEMPRE el mismo: lo unico que cambia
# entre un titular blanco y uno amarillo es el degradado de la cara. Por eso
# el filete y la extrusion viven aqui fuera y no dentro de cada paleta.
IMPACTO_FILETE   = (139, 26, 10)    # #8B1A0A
IMPACTO_EX_CERCA = (107, 15, 5)     # #6B0F05
IMPACTO_EX_LEJOS = (58, 6, 2)       # #3A0602
# El degradado sube de tono respecto a la referencia (que acababa en naranja
# #F29E0C): medido a 168 px, el ancho real al que YouTube sirve la miniatura en
# movil, la media de luminancia caia y el titular perdia pegada frente al
# amarillo plano que se usaba antes. El relieve, el filete y el contorno no
# cambian: el acabado de cartel es el mismo, solo la cara es mas luminosa.
CARA_AMARILLA = ("#FFFDE0", "#F9EC3A", "#F5D400")
CARA_BLANCA   = ("#FFFFFF", "#F4EFE6", "#C9BFAC")

# Medidas a cuerpo de referencia (cap 86 px); pegar_impacto las escala al
# tamano real de cada titular para que el relieve pese igual en las dos lineas.
IMP_CAP_REF  = 86
IMP_CONTORNO = 6      # negro exterior
IMP_FILETE   = 3      # rojo oscuro, por dentro del negro
IMP_PROF     = 15     # capas de extrusion, 1 px cada una
IMP_SKEW     = 5.0    # grados hacia la derecha
# La profundidad TIENE que superar a contorno+filete: el contorno negro de la
# cara se dibuja despues y, si es mas ancho que el desplazamiento, se traga la
# extrusion entera y el titular sale plano con un borde negro gordo.


def _mezcla(c1, c2, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mascara(texto, fuente, tam, pos, stroke=0):
    """Mascara L del texto, dilatada por stroke si se pide."""
    m = Image.new("L", tam, 0)
    ImageDraw.Draw(m).text(pos, texto, font=fuente, fill=255, anchor="la",
                           stroke_width=stroke, stroke_fill=255)
    return m


def _grietas(tam, caja, semilla, escala):
    """Arañazos oscuros. Se siembran con el texto para que la misma frase de
    siempre las mismas grietas y no bailen entre renders."""
    import random
    r = random.Random(semilla)
    capa = Image.new("L", tam, 0)
    d = ImageDraw.Draw(capa)
    x0, y0, x1, y1 = caja
    n = max(8, int((x1 - x0) / 46))
    for _ in range(n):
        x, y = r.uniform(x0, x1), r.uniform(y0, y1)
        largo = r.uniform(0.08, 0.34) * (y1 - y0)
        ang = r.uniform(-0.9, 0.9)
        pts = [(x, y)]
        for _ in range(3):                      # con quiebros: una recta no
            ang += r.uniform(-0.4, 0.4)         # parece una grieta
            x += np.cos(ang) * largo / 3
            y += np.sin(ang) * largo / 3
            pts.append((x, y))
        d.line(pts, fill=r.randint(120, 180),
               width=max(1, round(escala * (2 if r.random() < 0.3 else 1))),
               joint="curve")
    return capa


def capa_impacto(texto, fuente, cara, contorno=IMP_CONTORNO, filete=IMP_FILETE,
                 profundidad=IMP_PROF, skew=IMP_SKEW, grietas=True, brillo=True):
    """Capa RGBA con el titular en acabado de cartel de impacto.

    Devuelve (capa, ancla_x, ancla_y): pegando la capa en
    (X - ancla_x, Y - ancla_y) el texto cae exactamente donde lo habria puesto
    un d.text((X, Y), anchor="la").
    """
    cara_rgb = [_rgb(c) for c in cara]
    med = ImageDraw.Draw(Image.new("L", (1, 1)))
    avance = med.textlength(texto, font=fuente)
    bb = fuente.getbbox(texto)                  # relativo al origen "la"
    escala = max(1.0, (bb[3] - bb[1]) / 90.0)   # las grietas siguen al cuerpo

    borde = contorno + filete
    pad = borde + profundidad + 22              # sitio para la sombra difuminada
    desvio = int(round(np.tan(np.radians(skew)) * (bb[3] + 2 * pad)))
    tam = (int(avance + bb[2] - bb[0] + 2 * pad + desvio) + 4, int(bb[3] + 2 * pad))
    pos = (pad, pad)

    m_relleno = _mascara(texto, fuente, tam, pos, 0)
    m_exterior = _mascara(texto, fuente, tam, pos, borde)
    m_filete = _mascara(texto, fuente, tam, pos, filete)
    anillo_rojo = ImageChops.subtract(m_filete, m_relleno)

    capa = Image.new("RGBA", tam, (0, 0, 0, 0))

    # 1. Sombra paralela. Sale de la capa MAS PROFUNDA del relieve: lanzada
    #    desde la cara se la come la propia extrusion y el titular no se
    #    despega del fondo.
    sombra = Image.new("L", tam, 0)
    sombra.paste(m_exterior, (profundidad, profundidad + 7))
    sombra = sombra.filter(ImageFilter.GaussianBlur(11))
    sombra = sombra.point(lambda v: int(v * 0.70))
    capa.paste(Image.new("RGBA", tam, (0, 0, 0, 255)), (0, 0), sombra)

    # 2. Extrusion: primero el contorno negro de toda la masa (de la capa mas
    #    lejana a la mas cercana) y luego el relleno granate.
    negro = Image.new("RGBA", tam, (0, 0, 0, 255))
    for i in range(profundidad, 0, -1):
        capa.paste(negro, (i, i), m_exterior)
    for i in range(profundidad, 0, -1):
        t = (i - 1) / (profundidad - 1) if profundidad > 1 else 0.0
        col = _mezcla(IMPACTO_EX_CERCA, IMPACTO_EX_LEJOS, t)
        capa.paste(Image.new("RGBA", tam, col + (255,)), (i, i), m_relleno)

    # 3. Cara: contorno negro exterior + degradado + brillo + grietas.
    capa.paste(negro, (0, 0), m_exterior)

    caja = m_relleno.getbbox() or (pad, pad, tam[0] - pad, tam[1] - pad)
    alto = max(1, caja[3] - caja[1])
    rampa = np.zeros((tam[1], 1, 3), dtype=np.float32)
    for y in range(tam[1]):
        t = min(1.0, max(0.0, (y - caja[1]) / alto))
        col = (_mezcla(cara_rgb[0], cara_rgb[1], t / 0.52) if t < 0.52
               else _mezcla(cara_rgb[1], cara_rgb[2], (t - 0.52) / 0.48))
        rampa[y, 0] = col
    degradado = Image.fromarray(
        np.repeat(rampa, tam[0], axis=1).astype(np.uint8), "RGB").convert("RGBA")
    capa.paste(degradado, (0, 0), m_relleno)

    if brillo:
        # Franja de luz en el tercio alto: es lo que hace que parezca metal
        # pulido y no un relleno plano.
        alfa = np.zeros((tam[1], 1), dtype=np.float32)
        tope = caja[1] + alto * 0.42
        for y in range(tam[1]):
            if caja[1] <= y < tope:
                alfa[y, 0] = 255 * 0.55 * (1 - (y - caja[1]) / (tope - caja[1]))
        m_brillo = Image.fromarray(
            np.repeat(alfa, tam[0], axis=1).astype(np.uint8), "L")
        capa.paste(Image.new("RGBA", tam, (255, 255, 255, 255)), (0, 0),
                   ImageChops.multiply(m_brillo, m_relleno))

    if grietas:
        m_g = ImageChops.multiply(
            _grietas(tam, caja, hash(texto) & 0xFFFFFFFF, escala), m_relleno)
        capa.paste(Image.new("RGBA", tam, IMPACTO_EX_LEJOS + (255,)), (0, 0), m_g)

    # 4. Filete rojo, DESPUES del relleno: asi queda entre la cara y el negro.
    capa.paste(Image.new("RGBA", tam, IMPACTO_FILETE + (255,)), (0, 0), anillo_rojo)

    ancla_x, ancla_y = pad, pad
    if skew:
        # Cizalla: la parte alta se va a la derecha. PIL mapea salida -> entrada,
        # de ahi que el desplazamiento entre con signo negativo en c.
        s = float(desvio)
        h = tam[1]
        capa = capa.transform((tam[0], h), Image.AFFINE,
                              (1, s / h, -s, 0, 1, 0),
                              resample=Image.BICUBIC)
        ancla_x = pad + s * (1 - pad / h)

    return capa, ancla_x, ancla_y


def _medidas_para(fuente):
    """Grosores del relieve para el cuerpo de esta fuente.

    Con medidas fijas en pixeles la linea corta (cap 48) recibia el mismo
    contorno de 7 px que el titular grande (cap 86) y salia una plancha negra
    que se comia las letras. Todo se escala contra IMP_CAP_REF.
    """
    cap = fuente.getbbox("H")[3] - fuente.getbbox("H")[1]
    k = max(0.45, cap / IMP_CAP_REF)
    return {
        "contorno": max(2, round(IMP_CONTORNO * k)),
        "filete": max(1, round(IMP_FILETE * k)),
        "profundidad": max(3, round(IMP_PROF * k)),
    }


def pegar_impacto(im, xy, texto, fuente, cara, anchor="la", **kw):
    """Coloca un titular de impacto respetando el anchor de PIL.

    El relieve se escala al cuerpo de la fuente: a 86 px de caja salen las
    medidas de referencia, y una linea pequena recibe un relieve proporcional
    en vez del mismo grosor en pixeles, que la ahogaria.
    """
    for clave, valor in _medidas_para(fuente).items():
        kw.setdefault(clave, valor)
    capa, ax, ay = capa_impacto(texto, fuente, cara, **kw)
    avance = ImageDraw.Draw(im).textlength(texto, font=fuente)
    x, y = xy
    if anchor[0] == "m":
        x -= avance / 2
    elif anchor[0] == "r":
        x -= avance
    im.alpha_composite(capa, (int(round(x - ax)), int(round(y - ay))))
    return im


def grade(im, negro=5, blanco=188, gamma=0.74, lift=0.035, sat=0.86):
    """Levanta sombras y recupera altas luces. Los fondos de IA salen
    subexpuestos y sin blancos reales (p99 ~166 frente a 255 de la referencia)."""
    a = np.asarray(im.convert("RGB")).astype(np.float32)
    a = np.clip((a - negro) * (255.0 / (blanco - negro)), 0, 255) / 255.0
    a = a ** gamma
    a = lift + (1 - lift) * a
    return ImageEnhance.Color(Image.fromarray(np.clip(a * 255, 0, 255).astype(np.uint8))).enhance(sat)


def fuente_cap(cap, ruta=None):
    """Fuente cuyo alto de caja (la 'H') mide exactamente cap px."""
    ruta = ruta or FUENTE
    if not ruta:
        raise RuntimeError(
            "No se encuentra Anton-Regular.ttf. En el contenedor debe estar en "
            "/app/fonts; fuera, define EP_FUENTE_ANTON.")
    f = ImageFont.truetype(ruta, 100)
    c = f.getbbox("H"); c100 = c[3] - c[1]
    return ImageFont.truetype(ruta, max(1, round(100 * cap / c100)))


def texto_tracking(d, xy, texto, fuente, fill, tracking, anchor="lm"):
    """Dibuja con espaciado entre letras. A tamano pequeno el tracking es lo
    que separa un wordmark de un texto apretado."""
    x, y = xy
    for ch in texto:
        d.text((x, y), ch, font=fuente, fill=fill, anchor=anchor)
        x += d.textlength(ch, font=fuente) + tracking
    return x - tracking


def partir(texto, fuente, ancho_max, d):
    palabras, lineas, act = texto.split(), [], ""
    for p in palabras:
        t = f"{act} {p}".strip()
        if d.textlength(t, font=fuente) <= ancho_max or not act:
            act = t
        else:
            lineas.append(act); act = p
    if act: lineas.append(act)
    return lineas


def grade_auto(im, objetivo_oscuro=0.32, umbral=40):
    """Grada hasta que la proporcion de pixeles oscuros baje al objetivo.
    Normaliza el peso visual de fondos generados en sesiones distintas: sin
    esto, cada imagen llega con una exposicion diferente y en la parrilla unas
    se ven vivas y otras un ladrillo marron."""
    mejor = grade(im)
    for gamma, lift, blanco in ((0.74, 0.035, 188), (0.68, 0.06, 180),
                                (0.62, 0.09, 172), (0.56, 0.12, 164),
                                (0.50, 0.15, 156)):
        mejor = grade(im, negro=4, blanco=blanco, gamma=gamma, lift=lift, sat=0.88)
        L = np.asarray(mejor).mean(axis=2)
        if (L < umbral).mean() <= objetivo_oscuro:
            break
    return mejor


def fuente_ancho(texto, cap_max, ancho_max, d, cap_min=40):
    """Fuente mas grande cuyo texto quepa en ancho_max, sin pasar de cap_max."""
    cap = cap_max
    f = fuente_cap(cap)
    while d.textlength(texto, font=f) > ancho_max and cap > cap_min:
        cap -= 2
        f = fuente_cap(cap)
    return f, cap


def realce(im, caja, ganancia=1.90, contraste=1.28, difuminado=42):
    """Cambia luz y contraste solo dentro de una elipse difuminada. Con
    ganancia>1 rescata un elemento del fondo; con ganancia<1 hunde el telon
    que tiene detras. Hacen falta las dos: subir el sujeto solo lo acerca al
    fondo y la silueta no llega a separarse."""
    x0, y0, x1, y1 = caja
    m = Image.new("L", im.size, 0)
    ImageDraw.Draw(m).ellipse([x0, y0, x1, y1], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(difuminado))
    subido = ImageEnhance.Contrast(ImageEnhance.Brightness(im).enhance(ganancia)).enhance(contraste)
    return Image.composite(subido, im, m)


def scrim_banda(im, y_inicio, fuerza=0.72):
    """Banda oscura degradada desde y_inicio hasta el borde inferior. Es lo que
    hace Aztecas: el texto se apoya en una base solida y la imagen queda limpia."""
    g = Image.new("L", (1, H), 0)
    px = g.load()
    for y in range(H):
        if y <= y_inicio:
            px[0, y] = 0
        else:
            t = (y - y_inicio) / max(1, H - y_inicio)
            px[0, y] = int(255 * fuerza * (t ** 0.75))
    m = g.resize(im.size)
    return Image.composite(Image.new("RGB", im.size, (0, 0, 0)), im, m)


def scrim(im, caja, fuerza=0.55, difuminado=90):
    """Oscurece suavemente el area bajo un bloque de texto para que las letras
    despeguen del fondo. Es lo que hace Aztecas con la banda inferior."""
    x0, y0, x1, y1 = caja
    m = Image.new("L", im.size, 0)
    ImageDraw.Draw(m).rectangle([x0, y0, x1, y1], fill=int(255 * fuerza))
    m = m.filter(ImageFilter.GaussianBlur(difuminado))
    return Image.composite(Image.new("RGB", im.size, (0, 0, 0)), im, m)


def visor(im):
    """Corchetes en L en las 4 esquinas + punto REC y wordmark. Sin logo."""
    capa = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    b, L, i, A = BR_GROSOR, BR_BRAZO, BR_INSET, BR_ALPHA
    for x, y, sx, sy in ((i, i, 1, 1), (W - i, i, -1, 1), (i, H - i, 1, -1), (W - i, H - i, -1, -1)):
        d.rectangle([min(x, x + sx * L), min(y, y + sy * b), max(x, x + sx * L), max(y, y + sy * b)],
                    fill=(255, 255, 255, A))
        d.rectangle([min(x, x + sx * b), min(y, y + sy * L), max(x, x + sx * b), max(y, y + sy * L)],
                    fill=(255, 255, 255, A))
    f = fuente_cap(CAP_MARCA, FUENTE_MARCA)
    cy = Y_BANDA_MARCA
    rr = CAP_MARCA // 2
    d.ellipse([i + 13, cy - rr, i + 13 + rr * 2, cy + rr], fill=(224, 27, 27, 255))
    texto_tracking(d, (i + 27 + rr * 2, cy), WORDMARK, f, (255, 255, 255, 245), TRACKING_MARCA)
    return Image.alpha_composite(im.convert("RGBA"), capa).convert("RGB")


def cartel(d, texto, x, y_base, contorno=(0, 0, 0)):
    """Contexto del caso: mismas letras que el resto (Anton), en rojo, sin
    bloque de fondo. Se encoge solo hasta caber en ANCHO_CARTEL."""
    t = texto.upper()
    cap = CAP_CARTEL
    f = fuente_cap(cap)
    while d.textlength(t, font=f) > ANCHO_CARTEL and cap > 26:
        cap -= 2
        f = fuente_cap(cap)
    d.text((x, y_base - cap), t, font=f, fill=ROJO,
           stroke_width=CONTORNO_CARTEL, stroke_fill=contorno, anchor="la")
    return y_base - cap


def encajar(im):
    if im.size == (W, H): return im
    r = im.width / im.height
    if r > W / H:
        nw = int(im.height * W / H); im = im.crop(((im.width - nw) // 2, 0, (im.width - nw) // 2 + nw, im.height))
    elif r < W / H:
        nh = int(im.width * H / W); im = im.crop((0, (im.height - nh) // 2, im.width, (im.height - nh) // 2 + nh))
    return im.resize((W, H), Image.LANCZOS)


def placa(texto, cap=None):
    """Placa roja con relieve: degradado vertical, bisel claro arriba, sombra
    interior abajo y sombra proyectada. Letras blancas. Devuelve RGBA."""
    cap = cap or CAP_DETALLE
    f = fuente_cap(cap)
    med = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    t = texto.upper()
    px, py, r, sombra = 24, 13, 9, 6
    an = int(med.textlength(t, font=f))
    w, h = an + px * 2, cap + py * 2
    cap_img = Image.new("RGBA", (w + sombra * 2, h + sombra * 2), (0, 0, 0, 0))

    # sombra proyectada
    sh = Image.new("L", cap_img.size, 0)
    ImageDraw.Draw(sh).rounded_rectangle([sombra, sombra + 4, sombra + w, sombra + h + 4],
                                         radius=r, fill=150)
    cap_img.paste((0, 0, 0, 255), (0, 0), sh.filter(ImageFilter.GaussianBlur(5)))

    # cuerpo con degradado vertical (arriba mas claro = relieve)
    grad = Image.new("RGB", (1, h))
    gp = grad.load()
    for y in range(h):
        t01 = y / max(1, h - 1)
        gp[0, y] = (int(238 - 78 * t01), int(46 - 30 * t01), int(38 - 26 * t01))
    grad = grad.resize((w, h))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    cap_img.paste(grad, (sombra, sombra), mask)

    d = ImageDraw.Draw(cap_img)
    # bisel: filo claro arriba, filo oscuro abajo
    d.rounded_rectangle([sombra + 2, sombra + 2, sombra + w - 3, sombra + h - 3],
                        radius=r - 2, outline=(255, 150, 140, 110), width=2)
    d.line([sombra + r, sombra + h - 2, sombra + w - r, sombra + h - 2],
           fill=(90, 8, 8, 190), width=2)
    d.text((sombra + w // 2, sombra + py + cap // 2), t, font=f,
           fill=(255, 255, 255, 255), anchor="mm",
           stroke_width=2, stroke_fill=(120, 10, 10, 200))
    return cap_img, sombra, w, h


def _detalle_arriba(im, texto, contorno):
    """Contexto del caso, arriba a la derecha y a la MISMA altura que el
    wordmark: los dos forman la banda superior del marco, no tapan imagen."""
    if not texto:
        return im
    pl, off, wb, hb = placa(texto)
    # posicionada por el CUERPO de la placa, no por el lienzo con sombra, para
    # que el brazo del corchete (y=65..68) quede libre por encima
    x = W - BR_INSET - 13 - wb - off
    y = Y_BANDA_MARCA - hb // 2 - off
    im = im.convert("RGBA")
    im.alpha_composite(pl, (x, y))
    return im.convert("RGB")


def _inferior(im, t1, t2, cartel_txt, acento, color_t1, contorno_cartel, centrado,
              estilo="v3"):
    """Todo el texto en una banda abajo. El lugar arriba, pequeno; la afirmacion
    debajo, grande y en una sola linea siempre que quepa."""
    im = _detalle_arriba(im, cartel_txt, contorno_cartel)
    im = scrim_banda(im, int(H * 0.62))
    d = ImageDraw.Draw(im)
    ancho = W - MARGEN_X * 2

    f1, cap1 = fuente_ancho(t1.upper(), CAP_T1_INF, ancho, d)
    f2 = fuente_cap(CAP_T2_INF)
    cap2 = f2.getbbox("H")[3] - f2.getbbox("H")[1]

    y1 = H - MARGEN_INF - cap1
    y2 = y1 - cap2 - 22
    x = W // 2 if centrado else MARGEN_X
    anc = "ma" if centrado else "la"

    # v3 (por defecto): linea corta en blanco, titular entero en amarillo.
    # v2: al reves, con una sola palabra del titular destacada.
    # Desde el 22/09/2026 ambas lineas van en acabado de cartel de impacto: el
    # relieve, el filete rojo y el contorno son identicos en las dos, y lo unico
    # que las distingue es el degradado de la cara. Es lo que hace que se lean
    # como un mismo rotulo a dos colores y no como dos textos sueltos.
    im = im.convert("RGBA")
    pegar_impacto(im, (x, y2), t2.upper(), f2,
                  CARA_BLANCA if estilo == "v3" else CARA_AMARILLA, anchor=anc)

    if estilo == "v3":
        pegar_impacto(im, (x, y1), t1.upper(), f1, CARA_AMARILLA, anchor=anc)
        return im.convert("RGB")

    d = ImageDraw.Draw(im)
    if acento:
        # el tramo final en amarillo, el resto en blanco. Si acento es una
        # cadena, esa es exactamente la parte que se destaca.
        T = t1.upper()
        if isinstance(acento, str) and acento.upper() in T:
            a = acento.upper()
            i = T.index(a)
            tramos = [(T[:i], color_t1), (a, AMARILLO), (T[i + len(a):], color_t1)]
        else:
            pal = T.rsplit(" ", 1)
            tramos = ([(pal[0] + " ", color_t1), (pal[1], AMARILLO)]
                      if len(pal) == 2 else [(T, AMARILLO)])
        tramos = [(t, c) for t, c in tramos if t]
        ancho_tot = sum(d.textlength(t, font=f1) for t, _ in tramos)
        xx = (W - ancho_tot) / 2 if centrado else MARGEN_X
        for t, c in tramos:
            pegar_impacto(im, (xx, y1), t, f1,
                          CARA_AMARILLA if c == AMARILLO else CARA_BLANCA,
                          anchor="la")
            xx += d.textlength(t, font=f1)
    else:
        pegar_impacto(im, (x, y1), t1.upper(), f1,
                      CARA_AMARILLA if color_t1 == AMARILLO else CARA_BLANCA,
                      anchor=anc)
    return im.convert("RGB")


def componer(fondo, t1, t2, salida, cartel_txt=None, acento=False,
             color_t1=BLANCO, sin_grade=False, contorno_cartel=(0, 0, 0),
             layout="diagonal", caja_realce=None, caja_oscura=None,
             auto_luz=False, estilo_texto="v3"):
    im = encajar(Image.open(fondo).convert("RGB"))
    if not sin_grade:
        im = grade_auto(im) if auto_luz else grade(im)
    if caja_oscura:
        im = realce(im, caja_oscura, 0.62, 1.0, 60)
    if caja_realce:
        im = realce(im, caja_realce)

    if layout in ("inferior", "inferior-centro"):
        im = _inferior(im, t1, t2, cartel_txt, acento, color_t1,
                       contorno_cartel, layout == "inferior-centro", estilo_texto)
        im = visor(im)
        im.save(salida, "PNG")
        return salida

    d0 = ImageDraw.Draw(im)
    f1 = fuente_cap(CAP_T1)
    lineas = partir(t1.upper(), f1, ANCHO_CAJA_T1, d0)
    if len(lineas) > 1:
        f1 = fuente_cap(CAP_T1_2L)
        lineas = partir(t1.upper(), f1, ANCHO_CAJA_T1, d0)
    cap1 = f1.getbbox("H")[3] - f1.getbbox("H")[1]
    paso1 = round(cap1 * (2 - INTERLINEADO) + cap1 * 0.30)
    alto1 = paso1 * (len(lineas) - 1) + cap1
    ancho1 = max(d0.textlength(l, font=f1) for l in lineas)

    # scrims: primero oscurecer, luego escribir encima
    im = scrim(im, (W - MARGEN_X - ancho1 - 40, MARGEN_SUP - 40,
                    W - MARGEN_X + 40, MARGEN_SUP + alto1 + 40))
    f2 = fuente_cap(CAP_T2)
    cap2 = f2.getbbox("H")[3] - f2.getbbox("H")[1]
    ancho2 = d0.textlength(t2.upper(), font=f2)
    y2 = H - MARGEN_INF - cap2
    alto_cluster = cap2 + (CAP_CARTEL + 26 if cartel_txt else 0)
    im = scrim(im, (MARGEN_X - 40, y2 - alto_cluster + cap2 - 40,
                    MARGEN_X + max(ancho2, 300) + 40, H - MARGEN_INF + 40))

    d = ImageDraw.Draw(im)
    if cartel_txt:
        cartel(d, cartel_txt, MARGEN_X, y2 - 22, contorno_cartel)

    # Mismo acabado de cartel de impacto que en el reparto inferior.
    im = im.convert("RGBA")
    # TITULAR 1 - la afirmacion, arriba a la derecha
    y = MARGEN_SUP
    for n, ln in enumerate(lineas):
        col = AMARILLO if (acento and n == len(lineas) - 1 and len(lineas) > 1) else color_t1
        pegar_impacto(im, (W - MARGEN_X, y), ln, f1,
                      CARA_AMARILLA if col == AMARILLO else CARA_BLANCA,
                      anchor="ra")
        y += paso1

    # CARTEL + TITULAR 2 - el contexto y el lugar, abajo a la izquierda
    pegar_impacto(im, (MARGEN_X, y2), t2.upper(), f2, CARA_AMARILLA, anchor="la")
    im = im.convert("RGB")

    im = visor(im)
    im.save(salida, "PNG")
    return salida


def main_cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fondo", required=True)
    ap.add_argument("--t1", required=True)
    ap.add_argument("--t2", required=True)
    ap.add_argument("--cartel")
    ap.add_argument("--acento", nargs="?", const=True, default=False,
                    help="destaca en amarillo el final del titular 1; opcionalmente "
                         "el texto exacto a destacar")
    ap.add_argument("--color-t1", default=BLANCO)
    ap.add_argument("--sin-grade", action="store_true")
    ap.add_argument("--cartel-contorno", default="negro", choices=["negro", "blanco"])
    ap.add_argument("--realce", help="x0,y0,x1,y1 del elemento a rescatar")
    ap.add_argument("--oscurecer", help="x0,y0,x1,y1 del telon que va detras")
    ap.add_argument("--estilo-texto", default="v3", choices=["v2", "v3"],
                    help="v3 (por defecto): linea corta blanca, titular amarillo. "
                         "v2: el esquema de las cinco primeras miniaturas")
    ap.add_argument("--auto-luz", action="store_true",
                    help="grada hasta igualar el peso visual con el resto")
    ap.add_argument("--layout", default="diagonal",
                    choices=["diagonal", "inferior", "inferior-centro"])
    ap.add_argument("--salida", required=True)
    a = ap.parse_args()
    print("OK", componer(a.fondo, a.t1, a.t2, a.salida, a.cartel, a.acento,
                         a.color_t1, a.sin_grade,
                         (255, 255, 255) if a.cartel_contorno == "blanco" else (0, 0, 0),
                         a.layout,
                         tuple(int(v) for v in a.realce.split(",")) if a.realce else None,
                         tuple(int(v) for v in a.oscurecer.split(",")) if a.oscurecer else None,
                         a.auto_luz, a.estilo_texto))


if __name__ == "__main__":
    main_cli()
