"""Medida de texto con las metricas reales de la fuente.

Hace falta porque la regla que de verdad gobierna un golpe C o una linea de
subtitulo B no es "cuantas palabras" sino "cabe o no cabe a 177 px de Anton
dentro del ancho util del fotograma". Contar palabras es una aproximacion que
falla en los dos sentidos: "PERO HAY TRES COSAS QUE NO ENCAJAN" son siete
palabras y cabe de sobra; una sola palabra larga en Anton puede no caber.

Solo se leen `head`, `hmtx` y `cmap`, asi que la medida es la suma de anchos de
avance. No incluye kerning (libass lo aplica via harfbuzz), que en Anton y
Cormorant mueve la aguja menos del 1%; de ahi el margen de seguridad de abajo.
"""

import os
from functools import lru_cache

SAFETY = 1.02      # margen por el kerning que no medimos

_FONT_FILES = {
    "Anton": "Anton-Regular.ttf",
    "Cormorant Garamond Medium": "CormorantGaramond-Medium.ttf",
    "Cormorant Garamond Medium Italic": "CormorantGaramond-MediumItalic.ttf",
    "Cormorant Garamond SemiBold": "CormorantGaramond-SemiBold.ttf",
}


def fonts_dir():
    from .burn import find_fonts_dir
    return find_fonts_dir()


@lru_cache(maxsize=8)
def _load(family):
    """(unitsPerEm, {codepoint: advance}, ancho por defecto, alto de linea)."""
    filename = _FONT_FILES.get(family)
    directory = fonts_dir()
    if not filename or not directory:
        return None
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return None
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return None
    font = TTFont(path, lazy=True)
    upem = font["head"].unitsPerEm
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    advances = {cp: hmtx[name][0] for cp, name in cmap.items() if name in hmtx.metrics}
    fallback = hmtx["space"][0] if "space" in hmtx.metrics else upem // 2
    os2 = font["OS/2"]
    hhea = font["hhea"]
    vm = {"win_asc": os2.usWinAscent, "win_desc": os2.usWinDescent,
          "hhea_asc": hhea.ascent, "hhea_desc": hhea.descent}
    font.close()
    return upem, advances, fallback, vm


def text_width(text, family, size, spacing=0.0):
    """Ancho en px de `text` a `size` px con `spacing` px entre caracteres.

    Devuelve None si la fuente no se puede medir (fontTools ausente o fichero
    no encontrado): quien llama trata eso como "no se puede comprobar", nunca
    como "cabe".
    """
    loaded = _load(family)
    if loaded is None or not text:
        return 0.0 if loaded is not None else None
    upem, advances, fallback, _vm = loaded
    total = sum(advances.get(ord(ch), fallback) for ch in text)
    return (total * size / upem + spacing * max(len(text) - 1, 0)) * SAFETY


def fits(text, family, size, spacing, max_width):
    width = text_width(text, family, size, spacing)
    return True if width is None else width <= max_width


# ── Fontsize de ASS vs font-size del CSS ───────────────────


def ass_fontsize(family, css_px):
    """px de CSS → `Fontsize` del ASS que da ese tamano en pantalla.

    NO son lo mismo, y la diferencia es enorme. El `font-size` del CSS es el
    cuadratin (em). El `Fontsize` de ASS es el ALTO DE LINEA de la fuente
    (usWinAscent + usWinDescent de la tabla OS/2), que es lo que libass iguala
    al valor pedido. En fuentes de metricas generosas la diferencia es brutal:

        Anton      usWin 2876+674 sobre upem 2048  →  em = 0,577 x Fontsize
        Cormorant  usWin 1097+283 sobre upem 1000  →  em = 0,725 x Fontsize

    O sea que poner `Fontsize: 211` para el rotulo E, que es lo que dice el
    mock en px de CSS, lo dibuja a 122 px: un 42% mas pequeno. Se detecto
    comparando el render con el mock fotograma a fotograma (FASE 5).

    Se calcula desde la propia fuente y no con una constante para que cambiar
    una cara no rompa la escala en silencio.
    """
    loaded = _load(family)
    if loaded is None:
        return round(css_px)
    upem, _, _, vm = loaded
    return round(css_px * (vm["win_asc"] + vm["win_desc"]) / upem)


def vmetrics(family):
    """{upem, hhea_asc, hhea_desc, win_asc, win_desc} o None."""
    loaded = _load(family)
    return None if loaded is None else dict(loaded[3], upem=loaded[0])


def css_baseline(line_box_center, family, css_px, css_line_height=1.0):
    """Linea base que pondria el navegador dentro de una caja de linea.

    El navegador monta la caja con `line-height`, centra dentro el area de
    contenido de la fuente (hheaAscent - hheaDescent) repartiendo el sobrante
    a los dos lados (half-leading), y apoya el texto en su ascendente.
    """
    vm = vmetrics(family)
    if vm is None:
        return line_box_center
    upem, ha, hd = vm["upem"], vm["hhea_asc"], vm["hhea_desc"]
    pitch = css_line_height * css_px
    half_leading = (pitch - (ha - hd) * css_px / upem) / 2
    return line_box_center - pitch / 2 + half_leading + ha * css_px / upem


def ass_y_for_baseline(baseline, family, css_px):
    """`y` de un `\\an5\\pos` para que la linea base caiga en `baseline`.

    libass escala la fuente para que `usWinAscent + usWinDescent` sea el
    Fontsize, asi que la caja de linea es eso y la base cae a winAscent del
    borde de arriba. Respecto al centro, a (winA - winD) / 2.
    """
    vm = vmetrics(family)
    if vm is None:
        return round(baseline)
    shift = (vm["win_asc"] - vm["win_desc"]) * css_px / (2 * vm["upem"])
    return round(baseline - shift)
