"""Construccion de la cadena de filtros de video para quemar la capa de texto.

El orden importa y es este:

    [drawbox de fondo negro] → [ass] → [drawtext del CTA]

Los `drawbox` van DELANTE porque son fondo: un corte C sobre negro tapa la
imagen y el titular tiene que dibujarse encima. El CTA va detras porque es la
capa mas alta y no debe quedar nunca por debajo de un rotulo.

La capa entera se aplica al final de la cadena de video (despues de Ken Burns,
de las transiciones y del teaser), en la misma pasada que ya hacia el
subtitulo, asi que no se anade ninguna recodificacion extra.
"""

import os

# Ruta de las fuentes dentro del contenedor. Se pasa a libass con `fontsdir`
# para que no dependa de que fontconfig las haya indexado.
FONTS_DIR_CANDIDATES = ("/app/fonts",)


def find_fonts_dir():
    for path in FONTS_DIR_CANDIDATES:
        if os.path.isdir(path):
            return path
    local = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts")
    return local if os.path.isdir(local) else None


def _escape_filter_arg(value):
    """Escapa un valor para un argumento de filtro de FFmpeg."""
    return str(value).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def black_background_filters(intervals):
    """`drawbox` a pantalla completa para los cues C con `background: "black"`."""
    filters = []
    for start, end in intervals:
        filters.append(
            "drawbox=x=0:y=0:w=iw:h=ih:color=black@1:t=fill:"
            f"enable='between(t,{start:.3f},{end:.3f})'"
        )
    return filters


def ass_filter(ass_path, fonts_dir=None):
    fonts_dir = fonts_dir or find_fonts_dir()
    chain = "ass=" + _escape_filter_arg(ass_path)
    if fonts_dir:
        chain += ":fontsdir=" + _escape_filter_arg(fonts_dir)
    return chain


def build_video_filters(ass_path, black_intervals=(), fonts_dir=None):
    """Devuelve la lista de filtros de la capa de texto, en orden."""
    return black_background_filters(black_intervals) + [ass_filter(ass_path, fonts_dir)]
