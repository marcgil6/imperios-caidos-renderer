"""Generador ASS de la capa de texto de EP.

La tabla de estilos es una transcripcion literal del CSS de
`docs/referencia-subtitulos/{subtitulos,gancho}-ep.html`, que es la verdad
visual del encargo. Cada numero de aqui se ha contrastado contra el mock
medido en Chromium a 1920x1080 (FASE 5).

TRES TRAMPAS QUE HAY QUE CONOCER ANTES DE TOCAR NADA
====================================================

1. UNIDADES. Los mocks miden en `cqw`, que es % del ANCHO del contenedor, no
   del alto: 1 cqw = 19,2 px sobre 1920. `font-size: 4.1cqw` del subtitulo B
   son **79 px**, no 44. La tabla del encargo convirtio los tamanos de fuente
   contra 1080 y salieron todos ~1,78x pequenos (el resto de sus medidas si
   estaban bien, porque esas si las dedujo del ancho). Marc confirmo el
   2026-09-08 que manda el mock.

2. `Fontsize` DE ASS != `font-size` DEL CSS. El del CSS es el cuadratin; el de
   ASS es el alto de linea de la fuente (usWinAscent + usWinDescent), que es lo
   que libass iguala al valor pedido. Con Anton la diferencia es del 42%.
   Lo traduce `metrics.ass_fontsize`, calculado desde la propia fuente.

3. `\\blur` DIFUMINA EL GLIFO, no solo la sombra, cuando `\\bord` es 0. Por eso
   cada texto sale como varios eventos apilados: copias negras difuminadas
   debajo (el `text-shadow` del CSS) y encima el glifo nitido. De paso permite
   las DOS sombras que llevan B y C, que en un evento ASS no caben.

Ademas, cada LINEA es su propio evento con `\\an5\\pos`: el interlineado de
libass es el alto de linea de la fuente (1,73x en Anton) y el del mock es el
`line-height` del CSS (1,0 en C, 1,28 en B). Dejando que libass reparta las
lineas, un golpe C de dos lineas sale con 130 px de mas entre ellas.
"""

from dataclasses import dataclass, field
from typing import List

from .alignment import STRONG_PUNCT, WEAK_PUNCT, Word, anchor_candidates, find_anchor
from .metrics import ass_y_for_baseline, css_baseline, text_width
from .schema import MIN_DUR, TextLayerError, normalize

# ── Lienzo ─────────────────────────────────────────────────

PLAY_W, PLAY_H = 1920, 1080
CQW = PLAY_W / 100.0          # 19,2 px


def cqw(value):
    """Unidad del mock → px sobre 1920x1080."""
    return value * CQW


# ── Paleta (ASS es &HAABBGGRR: alpha, azul, verde, rojo) ───

AMBER = "&H003FACE3&"          # #E3AC3F
IVORY = "&H00E3EFF5&"          # #F5EFE3
WHITE = "&H00FFFFFF&"
BLACK_STYLE = "&H00000000"


def _colour(hex_rgb, opacity=1.0):
    r, g, b = int(hex_rgb[0:2], 16), int(hex_rgb[2:4], 16), int(hex_rgb[4:6], 16)
    return f"&H{round((1.0 - opacity) * 255):02X}{b:02X}{g:02X}{r:02X}"


# ── Fuentes ────────────────────────────────────────────────
#
# Cada cara tiene nombre de familia propio (ver fonts/README.md). libass elige
# por familia + flags Bold/Italic con un sistema de puntuacion; con familias
# distintas la eleccion es exacta y no puede caer en fallback silencioso.

FONT_SERIF = "Cormorant Garamond Medium"
FONT_SERIF_ITALIC = "Cormorant Garamond Medium Italic"
FONT_IMPACT = "Anton"


@dataclass(frozen=True)
class Style:
    """Un estilo del mock.

    `size` es SIEMPRE el `font-size` del CSS en px, y `line_height` su
    `line-height`. Toda la geometria de este modulo razona en px de CSS, que
    es en lo que esta escrito el mock; la traduccion a ASS ocurre solo al
    emitir.
    """

    name: str
    font: str
    size: int
    colour: str
    spacing: float
    line_height: float
    # `text-shadow` del CSS. drop_* es la caida (offset + radio) y halo_* la
    # segunda sombra ancha sin desplazar que llevan B y C. sigma = radio / 2.
    drop_offset: float = 0.0
    drop_blur: float = 0.0
    drop_opacity: float = 0.0
    halo_blur: float = 0.0
    halo_opacity: float = 0.0
    max_width: float = PLAY_W
    fade: str = r"\fad(200,200)"

    @property
    def ass_size(self):
        from .metrics import ass_fontsize
        return ass_fontsize(self.font, self.size)

    @property
    def pitch(self):
        """Separacion entre lineas, en px: el `line-height` del CSS."""
        return self.line_height * self.size

    def to_ass(self):
        # Alignment 5 y margenes 0 porque cada linea se coloca con \an5\pos.
        # Outline y Shadow a 0: ningun texto lleva borde, y la sombra se dibuja
        # como eventos aparte.
        return (
            f"Style: {self.name},{self.font},{self.ass_size},{self.colour},&H000000FF,"
            f"{BLACK_STYLE},{BLACK_STYLE},0,0,0,0,100,100,{self.spacing:g},0,1,"
            f"0,0,5,0,0,0,1"
        )


# ── La tabla ───────────────────────────────────────────────
#
# mock              css                       px    donde
# B                 4.1cqw  /1.28  bottom 9%  79    .s-doc p
# B_LOOP            4.6cqw  /1.28             88    .s-doc.q p
# C                 9.2cqw  /1     bottom 14% 177   .s-clave p
# C mid             11cqw   /1     centrado   211   .s-clave.mid p
# E big             11cqw   /1                211   .s-dato .k
# E small           3.6cqw  /1.2              69    .s-dato .l
# E_Q big           13cqw   /1                250   .s-dato.q .k
# E_Q small         4.2cqw  /1.2              81    .s-dato.q .l
# contador          2.6cqw  /normal top 4%    50    .count
#
# `text-shadow` del mock:
#   .s-doc p     0 .15cqw .5cqw  rgba(0,0,0,.95) , 0 0 2.2cqw rgba(0,0,0,.8)
#   .s-clave p   0 .5cqw  1cqw   rgba(0,0,0,.95) , 0 0 3cqw   rgba(0,0,0,.7)
#   .s-dato .k   0 .6cqw  1.4cqw rgba(0,0,0,.95)
#   .s-dato .l   0 .2cqw  .6cqw  rgba(0,0,0,.95)
#   .rule/.count sin sombra
# En px: .15cqw=2,9 · .2cqw=3,8 · .5cqw=9,6 · .6cqw=11,5 · 1cqw=19,2 ·
# 1.4cqw=26,9 · 2.2cqw=42,2 · 3cqw=57,6.  sigma = radio / 2.

B_MARGIN = 230        # max-width 76cqw = 1459 px → (1920-1459)/2
C_MARGIN = 115        # padding 6cqw del contenedor

# El titular centrado sobre negro usa casi todo el ancho y no el padding de
# 6cqw. Motivo: a 211 px "PERO HAY TRES COSAS" mide ~1.780 px y dentro de 1.690
# el navegador lo parte en tres lineas, cortando el acento ambar "TRES COSAS"
# por la mitad (comprobado capturando el mock a tamano real). El `<br>` del
# propio mock dice que son dos lineas, asi que manda la intencion; el tamano
# de letra no se toca.
C_MID_MARGIN = 40

S_B = Style("B", FONT_SERIF, round(cqw(4.1)), _colour("F5EFE3"),
            round(cqw(4.1) * 0.02, 1), line_height=1.28,
            drop_offset=3, drop_blur=4.8, drop_opacity=0.95,
            halo_blur=21, halo_opacity=0.80,
            max_width=PLAY_W - 2 * B_MARGIN)

S_B_LOOP = Style("BLOOP", FONT_SERIF_ITALIC, round(cqw(4.6)), _colour("E3AC3F"),
                 round(cqw(4.6) * 0.02, 1), line_height=1.28,
                 drop_offset=3, drop_blur=4.8, drop_opacity=0.95,
                 halo_blur=21, halo_opacity=0.80,
                 max_width=PLAY_W - 2 * B_MARGIN)

S_C = Style("C", FONT_IMPACT, round(cqw(9.2)), _colour("FFFFFF"),
            round(cqw(9.2) * 0.02, 1), line_height=1.0,
            drop_offset=10, drop_blur=9.6, drop_opacity=0.95,
            halo_blur=29, halo_opacity=0.70,
            max_width=PLAY_W - 2 * C_MARGIN, fade=r"\fad(120,200)")

S_C_MID = Style("CMID", FONT_IMPACT, round(cqw(11)), _colour("FFFFFF"),
                round(cqw(11) * 0.02, 1), line_height=1.0,
                drop_offset=10, drop_blur=9.6, drop_opacity=0.95,
                halo_blur=29, halo_opacity=0.70,
                max_width=PLAY_W - 2 * C_MID_MARGIN, fade=r"\fad(120,200)")

S_E_BIG = Style("EBIG", FONT_IMPACT, round(cqw(11)), _colour("FFFFFF"),
                round(cqw(11) * 0.02, 1), line_height=1.0,
                drop_offset=12, drop_blur=13.4, drop_opacity=0.95,
                max_width=PLAY_W - 2 * C_MARGIN, fade=r"\fad(250,350)")

S_E_SMALL = Style("ESMALL", FONT_SERIF_ITALIC, round(cqw(3.6)), _colour("F5EFE3", 0.92),
                  round(cqw(3.6) * 0.04, 1), line_height=1.2,
                  drop_offset=4, drop_blur=5.8, drop_opacity=0.95,
                  max_width=cqw(80), fade=r"\fad(250,350)")

S_EQ_BIG = Style("EQBIG", FONT_IMPACT, round(cqw(13)), _colour("FFFFFF"),
                 round(cqw(13) * 0.02, 1), line_height=1.0,
                 drop_offset=12, drop_blur=13.4, drop_opacity=0.95,
                 max_width=PLAY_W - 2 * C_MARGIN, fade=r"\fad(250,350)")

S_EQ_SMALL = Style("EQSMALL", FONT_SERIF_ITALIC, round(cqw(4.2)), _colour("F5EFE3"),
                   round(cqw(4.2) * 0.04, 1), line_height=1.2,
                   drop_offset=4, drop_blur=5.8, drop_opacity=0.95,
                   max_width=cqw(80), fade=r"\fad(250,350)")

# La linea ambar es un dibujo vectorial: el Fontsize da igual, pero hace falta
# un estilo del que heredar color y fundido.
S_RULE = Style("ERULE", FONT_SERIF, 40, _colour("E3AC3F"), 0, line_height=1.0,
               fade=r"\fad(250,350)")

# `line-height: normal` de Cormorant = (hheaAsc - hheaDesc)/upem = 1,211.
S_COUNTER = Style("EQCOUNT", FONT_SERIF, round(cqw(2.6)), _colour("E3AC3F", 0.85),
                  round(cqw(2.6) * 0.12, 1), line_height=1.211,
                  fade=r"\fad(250,350)")

ALL_STYLES = [S_B, S_B_LOOP, S_C, S_C_MID, S_E_BIG, S_E_SMALL,
              S_EQ_BIG, S_EQ_SMALL, S_RULE, S_COUNTER]

# ── Anclajes verticales (px de CSS) ────────────────────────

B_BOTTOM = PLAY_H * 0.91            # .s-doc  bottom: 9%   → 982,8
C_BOTTOM = PLAY_H * 0.86            # .s-clave bottom: 14% → 928,8
MID_CENTER = PLAY_H / 2             # .s-clave.mid centrado
COUNTER_TOP = PLAY_H * 0.04         # .count top: 4%       → 43,2
COUNTER_X = PLAY_W - PLAY_W * 0.03  # .count right: 3%     → 1862
CENTER_X = PLAY_W / 2

# Pila de los rotulos E: flex column centrado con `gap: 1.4cqw`.
E_GAP = cqw(1.4)                              # 26,9
RULE_W, RULE_H = cqw(14), cqw(0.25)           # 268,8 x 4,8
COUNTER_DASH_W, COUNTER_DASH_H = cqw(3), cqw(0.2)   # 57,6 x 3,8


def e_stack(big_style, small_style, small_lines=1):
    """Centros de caja de las tres capas de un rotulo E, como el flex del mock.

    `small_lines` importa: cuando la pregunta ocupa dos lineas (la incognita 3
    del storyboard) el flex recentra la pila y la cifra sube. Contrastado con
    el navegador: con dos lineas da big=414, rule=568, small=694, que es
    exactamente lo que mide el mock.
    """
    h_big = big_style.pitch
    h_small = small_style.pitch * small_lines
    total = h_big + E_GAP + RULE_H + E_GAP + h_small
    top = (PLAY_H - total) / 2.0
    return {"big": top + h_big / 2.0,
            "rule": top + h_big + E_GAP + RULE_H / 2.0,
            "small": top + h_big + E_GAP + RULE_H + E_GAP + h_small / 2.0}


# ── Colocacion vertical ────────────────────────────────────


def line_box_centers(n, pitch, anchor_y, anchor):
    """Centros de las n cajas de linea de un bloque de texto."""
    if anchor == "bottom":
        top = anchor_y - n * pitch
    elif anchor == "center":
        top = anchor_y - n * pitch / 2.0
    elif anchor == "top":
        top = anchor_y
    else:
        raise ValueError(f"anclaje desconocido: {anchor!r}")
    return [top + (i + 0.5) * pitch for i in range(n)]


def ass_ys(style, n_lines, anchor_y, anchor):
    """`y` de cada linea para un `\\an5\\pos`, respetando la linea base del CSS."""
    return [ass_y_for_baseline(
                css_baseline(c, style.font, style.size, style.line_height),
                style.font, style.size)
            for c in line_box_centers(n_lines, style.pitch, anchor_y, anchor)]


# ── Utilidades ─────────────────────────────────────────────


def tc(t):
    """Segundos → codigo de tiempo ASS H:MM:SS.cc"""
    t = max(t, 0.0)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    cs = int(round((t % 1) * 100))
    if cs == 100:            # el redondeo puede desbordar el segundo
        cs, s = 0, s + 1
        if s == 60:
            s, m = 0, m + 1
            if m == 60:
                m, h = 0, h + 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def rect(w, h):
    """Dibujo vectorial ASS de un rectangulo w x h (para \\p1)."""
    return f"m 0 0 l {round(w)} 0 l {round(w)} {round(h)} l 0 {round(h)}"


def _escape(text):
    """ASS trata `{` como apertura de bloque de override."""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _accentuate(text, accent, base_colour):
    """Envuelve `accent` dentro de `text` en ambar y vuelve al color base.

    Se restaura el color explicitamente y no con `{\\r}` porque `{\\r}` resetea
    TODOS los override del evento, y el texto de detras del acento perderia el
    posicionamiento.
    """
    if not accent:
        return _escape(text)
    target = normalize(accent)
    words = text.split()
    n = len(target.split())
    for i in range(len(words) - n + 1):
        if normalize(" ".join(words[i:i + n])) == target:
            before, middle = " ".join(words[:i]), " ".join(words[i:i + n])
            after = " ".join(words[i + n:])
            out = (_escape(before) + " ") if before else ""
            out += "{\\c" + AMBER + "}" + _escape(middle) + "{\\c" + base_colour + "}"
            if after:
                out += " " + _escape(after)
            return out
    return None    # el acento no cae en esta linea; ya lo intentara la siguiente


# ── Emision de eventos ─────────────────────────────────────
#
# Capas: primero todo lo de B, encima todo lo de los cues. B y los cues nunca
# coinciden en el tiempo, pero el orden deja el fichero legible al depurar.
L_B, L_CUE = 0, 10


def _dialogue(layer, start, end, style_name, text, offset=0.0, name=""):
    # El campo Name (Actor) se usa para etiquetar el papel del evento:
    # halo / drop / text / rule / count. No lo pinta libass, pero deja el .ass
    # legible al depurar y permite comprobarlo en los tests.
    return (f"Dialogue: {layer},{tc(start + offset)},{tc(end + offset)},"
            f"{style_name},{name},0,0,0,,{text}")


def _alpha(opacity):
    return f"&H{round((1.0 - opacity) * 255):02X}&"


def stacked(base_layer, start, end, style, lines_plain, lines_rich,
            anchor_y, anchor, offset=0.0, x=CENTER_X):
    """Un bloque de texto → sus eventos de halo, caida y glifo.

    `lines_plain` es el texto sin colores en linea (la sombra es toda negra);
    `lines_rich` el que se ve, con el acento ambar donde toque.
    """
    ys = ass_ys(style, len(lines_plain), anchor_y, anchor)
    x = round(x)
    events = []
    for capa, (y, plain, rich) in enumerate(zip(ys, lines_plain, lines_rich)):
        if style.halo_blur:
            events.append(_dialogue(
                base_layer, start, end, style.name,
                "{" + style.fade + f"\\an5\\pos({x},{y})\\bord0\\shad0"
                f"\\blur{style.halo_blur:g}\\1c&H000000&\\1a{_alpha(style.halo_opacity)}" + "}"
                + plain, offset, name="halo"))
        if style.drop_blur or style.drop_offset:
            events.append(_dialogue(
                base_layer + 1, start, end, style.name,
                "{" + style.fade + f"\\an5\\pos({x},{round(y + style.drop_offset)})"
                f"\\bord0\\shad0\\blur{style.drop_blur:g}"
                f"\\1c&H000000&\\1a{_alpha(style.drop_opacity)}" + "}"
                + plain, offset, name="drop"))
        events.append(_dialogue(
            base_layer + 2, start, end, style.name,
            "{" + style.fade + f"\\an5\\pos({x},{y})\\bord0\\shad0" + "}" + rich,
            offset, name="text"))
    return events


# ── Reparto de lineas ──────────────────────────────────────


def _ends_sentence(token):
    return token.rstrip('"”»)').endswith(STRONG_PUNCT)


def _ends_clause(token):
    return token.rstrip('"”»)').endswith(WEAK_PUNCT)


def wrap_lines(text, style, max_lines=2, max_chars_per_line=None):
    """Reparte el texto en como mucho `max_lines` lineas.

    Dos limites a la vez: el de caracteres del esquema (`b_style`) y el ancho
    real del estilo medido con las metricas de la fuente. El primero es la
    regla editorial; el segundo es el que impide que libass tenga que re-partir
    nada por su cuenta.

    Prioridad de corte: puntuacion fuerte → puntuacion debil → reparto
    equilibrado. Si el texto entero cabe en una linea va en una sola: nunca se
    deja una palabra huerfana abajo por gusto.
    """
    def fits(s):
        if max_chars_per_line is not None and len(s) > max_chars_per_line:
            return False
        w = text_width(s, style.font, style.size, style.spacing)
        return w is None or w <= style.max_width

    words = text.split()
    if not words:
        return [""]
    if fits(text) or max_lines == 1:
        return [text] if fits(text) else None

    best, best_score = None, None
    for cut in range(1, len(words)):
        first, second = " ".join(words[:cut]), " ".join(words[cut:])
        if not fits(first) or not fits(second):
            continue
        prev = words[cut - 1]
        punct = 0 if _ends_sentence(prev) else (1 if _ends_clause(prev) else 2)
        score = (punct, abs(len(first) - len(second)))
        if best_score is None or score < best_score:
            best, best_score = [first, second], score
    return best


# ── Agrupado del subtitulo B ───────────────────────────────


@dataclass
class BEvent:
    start: float
    end: float
    words: List[Word]
    loop: bool = False

    @property
    def text(self):
        return " ".join(w.word for w in self.words)


def group_b_events(words, loop_flags, style=None, max_lines=2, max_chars_per_line=42):
    """Palabras alineadas → eventos de subtitulo B.

    Corta primero en punto (una frase = un subtitulo), y solo si la frase no
    cabe la parte en trozos, prefiriendo los cortes de coma. Un cambio de
    B_LOOP fuerza corte siempre: una frase en cursiva ambar nunca comparte
    evento con narracion normal.

    El criterio de "cabe" es el propio `wrap_lines`, no un presupuesto de
    caracteres. Contar caracteres parecia bastar y no basta: un trozo de 84
    caracteres solo se parte en 42+42 si hay un espacio justo en el medio, y
    casi nunca lo hay. Midiendo con `wrap_lines` se comprueba que existe un
    corte real antes de aceptar la palabra.
    """
    style = style or S_B
    events, chunk = [], []

    def cabe(candidato):
        texto = " ".join(x.word for x in candidato)
        return wrap_lines(texto, style, max_lines, max_chars_per_line) is not None

    def flush():
        if chunk:
            events.append(BEvent(chunk[0].start, chunk[-1].end, list(chunk),
                                 loop=loop_flags[id(chunk[0])]))
            chunk.clear()

    for w in words:
        same_loop = not chunk or loop_flags[id(chunk[0])] == loop_flags[id(w)]
        if chunk and (not same_loop or not cabe(chunk + [w])):
            flush()
        chunk.append(w)
        if _ends_sentence(w.word):
            flush()
        elif _ends_clause(w.word) and not cabe(chunk + [chunk[-1]]):
            flush()     # corte de respiro: acaba de pasar una coma y queda poco sitio
    flush()

    # Eventos demasiado cortos. Pasa con los rabos de frase: "…correspondencia
    # diplomatica continua." deja "continua." suelto, que dura 0,5 s. Estirarlo
    # no vale porque el evento siguiente ya empieza; y descartarlo, que es lo
    # que hacia antes, borra palabras de la narracion.
    # Se resuelve moviendole palabras al evento ANTERIOR: el rabo crece hasta
    # llegar al minimo y no se pierde nada.
    for i in range(1, len(events)):
        ev, prev = events[i], events[i - 1]
        while (ev.end - ev.start < MIN_DUR["B"] and len(prev.words) > 1
               and prev.loop == ev.loop):
            candidato = [prev.words[-1]] + ev.words
            texto = " ".join(x.word for x in candidato)
            if wrap_lines(texto, style, max_lines, max_chars_per_line) is None:
                break
            prev.words.pop()
            prev.end = prev.words[-1].end
            ev.words = candidato
            ev.start = candidato[0].start

    # El ultimo no tiene siguiente contra el que chocar: se puede estirar.
    if events and events[-1].end - events[-1].start < MIN_DUR["B"]:
        events[-1].end = events[-1].start + MIN_DUR["B"]
    return events


def subtract_intervals(events, blocked):
    """Recorta los eventos B contra los intervalos ocupados por C/E.

    Recorta, no elimina: si el cue cae en medio de una frase, el subtitulo se
    parte en dos trozos. Los restos por debajo del minimo de 1 s se descartan,
    porque un parpadeo de medio segundo es peor que nada.
    """
    if not blocked:
        return list(events), 0

    blocked = sorted(blocked)
    out, trimmed = [], 0
    for ev in events:
        pieces = [(ev.start, ev.end)]
        cortado = False
        for b_start, b_end in blocked:
            nxt = []
            for p_start, p_end in pieces:
                if b_end <= p_start or b_start >= p_end:
                    nxt.append((p_start, p_end))
                    continue
                trimmed += 1
                cortado = True
                if p_start < b_start:
                    nxt.append((p_start, b_start))
                if b_end < p_end:
                    nxt.append((b_end, p_end))
            pieces = nxt
        for p_start, p_end in pieces:
            # El minimo solo descarta restos DEL RECORTE: un parpadeo de medio
            # segundo bajo un rotulo es peor que nada. Un evento que nadie ha
            # cortado se respeta aunque sea corto, porque tirarlo borraria
            # palabras que no se ven en ningun otro sitio.
            if not cortado or p_end - p_start >= MIN_DUR["B"]:
                out.append(BEvent(p_start, p_end, ev.words, ev.loop))
    return out, trimmed


# ── Cues C y E ─────────────────────────────────────────────


def _check_width(cue, text, style, what):
    width = text_width(text, style.font, style.size, style.spacing)
    if width is not None and width > style.max_width:
        raise TextLayerError(
            f"cue {cue.id!r}: {what} {text!r} mide {width:.0f} px a {style.size} px de "
            f"{style.font} y solo caben {style.max_width:.0f}. Acorta el texto."
        )


def _c_events(cue, offset):
    style = S_C_MID if cue.placement == "mid" else S_C
    rich, plain, placed = [], [], False
    for line in cue.lines:
        upper = line.upper()
        _check_width(cue, upper, style, "la linea")
        plain.append(_escape(upper))
        rendered = None
        if cue.accent and not placed:
            rendered = _accentuate(upper, cue.accent.upper(), WHITE)
            placed = rendered is not None
        rich.append(rendered if rendered is not None else _escape(upper))

    if cue.accent and not placed:
        raise TextLayerError(
            f"cue {cue.id!r}: accent={cue.accent!r} no cae entero dentro de ninguna "
            f"linea ({' / '.join(cue.lines)}). Parte el acento o reajusta las lineas."
        )

    anchor_y, anchor = ((MID_CENTER, "center") if style is S_C_MID
                        else (C_BOTTOM, "bottom"))
    return stacked(L_CUE, cue.start, cue.end, style, plain, rich,
                   anchor_y, anchor, offset)


def _e_events(cue, offset):
    is_q = cue.type == "E_Q"
    big_style = S_EQ_BIG if is_q else S_E_BIG
    small_style = S_EQ_SMALL if is_q else S_E_SMALL

    # La linea pequena se reparte antes de posicionar nada: si ocupa dos
    # lineas, el flex del mock recentra la pila entera y la cifra grande sube.
    small_lines = wrap_lines(cue.small, small_style, max_lines=2)
    if small_lines is None:
        raise TextLayerError(
            f"cue {cue.id!r}: small={cue.small!r} no cabe en dos lineas de "
            f"{small_style.max_width:.0f} px a {small_style.size} px. Acorta la frase."
        )
    pos = e_stack(big_style, small_style, len(small_lines))

    big_plain, big_accent = cue.big.upper(), cue.big_accent.upper()
    joined = (f"{big_accent} {big_plain}" if cue.accent_first
              else f"{big_plain} {big_accent}").strip()
    _check_width(cue, joined, big_style, "la cifra")

    accent_part = "{\\c" + AMBER + "}" + _escape(big_accent) + "{\\c" + WHITE + "}"
    plain_part = _escape(big_plain)
    if big_plain and big_accent:
        big_rich = (f"{accent_part} {plain_part}" if cue.accent_first
                    else f"{plain_part} {accent_part}")
    else:
        big_rich = accent_part if big_accent else plain_part

    events = stacked(L_CUE, cue.start, cue.end, big_style,
                     [_escape(joined)], [big_rich], pos["big"], "center", offset)

    events.append(_dialogue(L_CUE + 3, cue.start, cue.end, S_RULE.name,
                            "{" + S_RULE.fade
                            + f"\\an5\\pos({round(CENTER_X)},{round(pos['rule'])})\\p1" + "}"
                            + rect(RULE_W, RULE_H) + "{\\p0}", offset, name="rule"))

    escaped_small = [_escape(l) for l in small_lines]
    events += stacked(L_CUE + 4, cue.start, cue.end, small_style,
                      escaped_small, escaped_small, pos["small"], "center", offset)

    if is_q:
        # El guion ambar del contador es el ::before del mock: va EN LINEA con
        # el texto, no como evento aparte, para que quede pegado a el pase lo
        # que pase con el ancho de "1 / 3". \an6 ancla a la derecha, como el
        # `right: 3%` del CSS.
        y = ass_ys(S_COUNTER, 1, COUNTER_TOP, "top")[0]
        dash = "{\\p1}" + rect(COUNTER_DASH_W, COUNTER_DASH_H) + "{\\p0}\\h"
        events.append(_dialogue(
            L_CUE + 7, cue.start, cue.end, S_COUNTER.name,
            "{" + S_COUNTER.fade + f"\\an6\\pos({round(COUNTER_X)},{y})\\bord0\\shad0" + "}"
            + dash + _escape(cue.counter), offset, name="count"))
    return events


# ── Entrada publica ────────────────────────────────────────


def header():
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {PLAY_W}\n"
        f"PlayResY: {PLAY_H}\n"
        # WrapStyle 2 = libass NO reparte lineas por su cuenta. El reparto lo
        # decide `wrap_lines` con las metricas reales y cada linea sale como su
        # propio evento posicionado.
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(s.to_ass() for s in ALL_STYLES) + "\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _resolve(cue, words, what):
    hit = find_anchor(cue.anchor_text, words)
    if hit is None:
        best = anchor_candidates(cue.anchor_text, words)
        shown = "; ".join(f"{txt!r} ({r:.0%})" for r, txt in best) or "(ninguno)"
        raise TextLayerError(
            f"cue {cue.id!r}: anchor_text={cue.anchor_text!r} ({what}) no aparece en la "
            f"narracion. Lo mas parecido que hay: {shown}"
        )
    return hit


def resolve_cue_times(layer, words):
    """Convierte los `anchor_text` de los cues C/E en tiempos reales."""
    for cue in layer.overlays:
        if not cue.timed:
            hit = _resolve(cue, words, cue.type)
            cue.resolve_times(hit[0], hit[1])

    timed = sorted(layer.overlays, key=lambda c: c.start)
    for prev, nxt in zip(timed, timed[1:]):
        if nxt.start < prev.end - 1e-6:
            raise TextLayerError(
                f"cues {prev.id!r} y {nxt.id!r} se solapan tras resolver los anchor_text "
                f"({prev.start:.2f}-{prev.end:.2f} vs {nxt.start:.2f}-{nxt.end:.2f}). "
                "Nunca puede haber dos capas de texto a la vez."
            )
    return timed


def build_ass(layer, words, offset=0.0):
    """`TextLayer` + alineacion → (texto del .ass, informe).

    `offset` desplaza toda la capa: la narracion empieza despues del teaser.
    """
    if not words:
        raise TextLayerError("No hay alineacion: la capa de texto no se puede colocar en el tiempo.")

    overlays = resolve_cue_times(layer, words)

    # B_LOOP: marca las palabras del guion que van en cursiva ambar.
    loop_flags = {id(w): False for w in words}
    loops_found = []
    for cue in layer.loops:
        hit = _resolve(cue, words, "B_LOOP")
        for w in words[hit[2]:hit[3]]:
            loop_flags[id(w)] = True
        loops_found.append(cue.id)

    b_events = group_b_events(words, loop_flags, S_B,
                              layer.b_style.max_lines, layer.b_style.max_chars_per_line)
    b_events, trimmed = subtract_intervals(b_events, [(c.start, c.end) for c in overlays])

    dialogues, unfitted = [], []
    for ev in b_events:
        style = S_B_LOOP if ev.loop else S_B
        lines = wrap_lines(ev.text, style, layer.b_style.max_lines,
                           layer.b_style.max_chars_per_line)
        if lines is None:
            # Una frase que no cabe en dos lineas no puede tirar el render
            # entero de un video de 20 min: se parte por longitud y se avisa.
            lines = _hard_split(ev.text, layer.b_style.max_chars_per_line,
                                layer.b_style.max_lines)
            unfitted.append(ev.text[:60])
        escaped = [_escape(l) for l in lines]
        dialogues.extend(stacked(L_B, ev.start, ev.end, style, escaped, escaped,
                                 B_BOTTOM, "bottom", offset))

    for cue in overlays:
        dialogues.extend(_c_events(cue, offset) if cue.type == "C"
                         else _e_events(cue, offset))

    report = {
        "b_events": len(b_events),
        "b_loop_events": sum(1 for e in b_events if e.loop),
        "b_events_trimmed": trimmed,
        "b_events_hard_split": unfitted,
        "cues": {t: sum(1 for c in layer.cues if c.type == t)
                 for t in ("B_LOOP", "C", "E", "E_Q")},
        "loops_resolved": loops_found,
        "black_backgrounds": [(round(c.start + offset, 3), round(c.end + offset, 3))
                              for c in overlays if c.background == "black"],
        "words_aligned": len(words),
        "dialogue_lines": len(dialogues),
        "last_event_end_sec": round(max([e.end for e in b_events] +
                                        [c.end for c in overlays]) + offset, 2),
    }
    return header() + "\n".join(dialogues) + "\n", report


def _hard_split(text, max_chars, max_lines):
    """Ultimo recurso cuando ni `wrap_lines` encuentra un corte valido.

    Reparte por longitud y, si aun asi no entra en `max_lines`, mete el resto
    en la ultima linea. NUNCA descarta palabras: un subtitulo que se sale un
    poco es un defecto visible y arreglable; uno al que le faltan palabras es
    una mentira sobre lo que dice la narracion.
    """
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur); cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return lines
