"""Esquema y validacion estricta de `text_layer`.

Se valida a mano en vez de con pydantic a proposito: todas las reglas que
importan aqui (el `accent` tiene que aparecer en `lines`, `anchor_text` XOR
`start`/`end`, duraciones minimas por tipo, tope de palabras de un golpe C)
son de dominio, no de tipos, y el encargo pide que un JSON malo falle con un
mensaje explicito y nunca en silencio. Ademas el renderer no tiene ninguna
dependencia de validacion hoy y no merece la pena arrastrar una por esto.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional

# ── Reglas de la FASE 1 ────────────────────────────────────

CUE_TYPES = ("B_LOOP", "C", "E", "E_Q")

MIN_DUR = {"B": 1.0, "C": 2.0, "E": 3.0, "E_Q": 3.0}
# El encargo pedia un tope de 5 palabras, pero NINGUN ejemplo lo cumple: el
# golpe C del storyboard son 6 ("NADIE VOLVERA / A VERLA CON VIDA") y el corte
# sobre negro son 7 ("PERO HAY TRES COSAS / QUE NO ENCAJAN"), y los dos vienen
# tal cual en el JSON de ejemplo del propio encargo. Lo que de verdad hay que
# impedir no es contar palabras sino que el titular desborde: de eso se encarga
# la medida real con las metricas de Anton en ass_builder._check_width. Este
# tope se queda solo como red contra el error tonto de pegar una frase entera.
C_MAX_WORDS = 8
C_MAX_LINES = 2

B_MAX_LINES_DEFAULT = 2
B_MAX_CHARS_DEFAULT = 42


class TextLayerError(ValueError):
    """JSON de capa de texto invalido. El mensaje es para que lo lea un humano."""


def normalize(text):
    """Minusculas, sin tildes, sin puntuacion, espacios colapsados.

    Es la forma en que se comparan los `anchor_text` contra la alineacion:
    el guion trae mayusculas y tildes que Whisper no siempre reproduce.
    """
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


# ── Cues ───────────────────────────────────────────────────


@dataclass
class Cue:
    id: str
    type: str
    start: Optional[float] = None
    end: Optional[float] = None
    anchor_text: Optional[str] = None

    # C
    lines: List[str] = field(default_factory=list)
    accent: Optional[str] = None
    placement: Optional[str] = None      # None | "mid"
    background: Optional[str] = None     # None | "black"

    # Pista de estilo para cues que no vienen del JSON: "teaser".
    style_hint: Optional[str] = None

    # E / E_Q
    big: str = ""
    big_accent: str = ""
    accent_first: bool = False
    small: str = ""
    counter: Optional[str] = None

    @property
    def timed(self):
        return self.start is not None and self.end is not None

    @property
    def duration(self):
        return None if not self.timed else self.end - self.start

    def resolve_times(self, start, end):
        """Fija los tiempos calculados desde `anchor_text` y aplica el minimo."""
        self.start = start
        self.end = max(end, start + MIN_DUR[self.type])


@dataclass
class BStyle:
    max_lines: int = B_MAX_LINES_DEFAULT
    max_chars_per_line: int = B_MAX_CHARS_DEFAULT


@dataclass
class TextLayer:
    version: int = 1
    cues: List[Cue] = field(default_factory=list)
    b_style: BStyle = field(default_factory=BStyle)

    @property
    def loops(self):
        return [c for c in self.cues if c.type == "B_LOOP"]

    @property
    def overlays(self):
        """Cues que ocupan pantalla y por tanto suprimen el subtitulo B."""
        return [c for c in self.cues if c.type != "B_LOOP"]


# ── Parser ─────────────────────────────────────────────────


def _num(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TextLayerError(f"{where}: se esperaba un numero de segundos, llego {value!r}")
    return float(value)


def _str_list(value, where):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise TextLayerError(f"{where}: se esperaba una lista de lineas no vacia, llego {value!r}")
    out = []
    for line in value:
        if not isinstance(line, str) or not line.strip():
            raise TextLayerError(f"{where}: linea vacia o no textual ({line!r})")
        out.append(line.strip())
    return out


def _parse_cue(raw, index):
    if not isinstance(raw, dict):
        raise TextLayerError(f"cues[{index}]: se esperaba un objeto, llego {type(raw).__name__}")

    cue_id = raw.get("id") or f"cue{index:02d}"
    where = f"cue {cue_id!r}"

    ctype = raw.get("type")
    if ctype not in CUE_TYPES:
        raise TextLayerError(
            f"{where}: type={ctype!r} no valido. Tiene que ser uno de {', '.join(CUE_TYPES)}. "
            "El estilo B normal NO se declara cue a cue: se genera de la alineacion."
        )

    cue = Cue(id=str(cue_id), type=ctype)

    # ── Tiempos: start+end XOR anchor_text ──
    has_times = "start" in raw or "end" in raw
    anchor = raw.get("anchor_text")

    if ctype == "B_LOOP":
        if has_times:
            raise TextLayerError(
                f"{where}: un B_LOOP no lleva tiempos, se identifica solo por anchor_text."
            )
        if not isinstance(anchor, str) or not anchor.strip():
            raise TextLayerError(f"{where}: un B_LOOP necesita anchor_text con la frase del guion.")
        cue.anchor_text = anchor.strip()
        return cue

    if has_times and anchor:
        raise TextLayerError(
            f"{where}: lleva start/end Y anchor_text. Elige uno: o los tiempos son "
            "explicitos, o los calcula el builder desde la alineacion."
        )
    if has_times:
        if "start" not in raw or "end" not in raw:
            raise TextLayerError(f"{where}: si das tiempos tienen que ser start Y end.")
        cue.start = _num(raw["start"], f"{where}.start")
        cue.end = _num(raw["end"], f"{where}.end")
        if cue.end <= cue.start:
            raise TextLayerError(f"{where}: end ({cue.end}) no es posterior a start ({cue.start}).")
        minimum = MIN_DUR[ctype]
        if cue.duration < minimum - 1e-6:
            raise TextLayerError(
                f"{where}: dura {cue.duration:.2f}s y un cue {ctype} necesita al menos "
                f"{minimum:.1f}s en pantalla."
            )
    elif isinstance(anchor, str) and anchor.strip():
        cue.anchor_text = anchor.strip()
    else:
        raise TextLayerError(
            f"{where}: hace falta start/end o anchor_text. Sin uno de los dos el cue "
            "no se puede colocar en el tiempo."
        )

    # ── Contenido por tipo ──
    if ctype == "C":
        cue.lines = _str_list(raw.get("lines"), f"{where}.lines")
        if len(cue.lines) > C_MAX_LINES:
            raise TextLayerError(
                f"{where}: {len(cue.lines)} lineas. Un golpe C admite como maximo {C_MAX_LINES}."
            )
        n_words = sum(len(l.split()) for l in cue.lines)
        if n_words > C_MAX_WORDS:
            raise TextLayerError(
                f"{where}: {n_words} palabras ({' / '.join(cue.lines)}). Un golpe C admite "
                f"como maximo {C_MAX_WORDS}: es un titular, no una transcripcion."
            )

        accent = raw.get("accent")
        if accent is not None:
            if not isinstance(accent, str) or not accent.strip():
                raise TextLayerError(f"{where}.accent: si va, tiene que ser texto.")
            cue.accent = accent.strip()
            joined = normalize(" ".join(cue.lines))
            if normalize(cue.accent) not in joined:
                raise TextLayerError(
                    f"{where}: accent={cue.accent!r} no aparece en lines "
                    f"({' / '.join(cue.lines)}). No se puede poner en ambar lo que no esta."
                )

        placement = raw.get("placement")
        if placement not in (None, "mid"):
            raise TextLayerError(f"{where}.placement: solo se admite 'mid' (o nada).")
        cue.placement = placement

        background = raw.get("background")
        if background not in (None, "black"):
            raise TextLayerError(f"{where}.background: solo se admite 'black' (o nada).")
        if background == "black":
            cue.background = "black"
            # El corte a negro es siempre un titular centrado: el mock no tiene
            # ninguna variante de fondo negro con el texto abajo.
            cue.placement = "mid"

    else:  # E / E_Q
        for forbidden in ("lines", "accent", "background"):
            if forbidden in raw:
                raise TextLayerError(f"{where}: '{forbidden}' es de los cues C, no de {ctype}.")

        cue.big = (raw.get("big") or "").strip()
        cue.big_accent = (raw.get("big_accent") or "").strip()
        cue.small = (raw.get("small") or "").strip()
        cue.accent_first = bool(raw.get("accent_first"))

        if not (cue.big or cue.big_accent):
            raise TextLayerError(f"{where}: un rotulo {ctype} necesita big o big_accent.")
        if not cue.small:
            raise TextLayerError(
                f"{where}: falta 'small'. El rotulo lleva siempre la linea en serif debajo."
            )

        counter = raw.get("counter")
        if ctype == "E_Q":
            if not isinstance(counter, str) or not counter.strip():
                raise TextLayerError(
                    f"{where}: un E_Q es una de las tres incognitas del gancho y necesita "
                    "counter (p. ej. '1 / 3')."
                )
            cue.counter = counter.strip()
        elif counter is not None:
            raise TextLayerError(f"{where}: 'counter' es solo de los E_Q.")

    return cue


def parse_text_layer(raw):
    """dict → TextLayer validado. Lanza TextLayerError con el motivo exacto."""
    if not isinstance(raw, dict):
        raise TextLayerError(f"text_layer: se esperaba un objeto, llego {type(raw).__name__}")

    version = raw.get("version", 1)
    if version != 1:
        raise TextLayerError(f"text_layer.version={version!r}: esta build solo entiende la 1.")

    cues_raw = raw.get("cues")
    if not isinstance(cues_raw, list) or not cues_raw:
        raise TextLayerError("text_layer.cues: hace falta una lista de cues no vacia.")

    cues = [_parse_cue(c, i) for i, c in enumerate(cues_raw)]

    seen = set()
    for cue in cues:
        if cue.id in seen:
            raise TextLayerError(f"cue id {cue.id!r} repetido: los ids tienen que ser unicos.")
        seen.add(cue.id)

    bs_raw = raw.get("b_style") or {}
    if not isinstance(bs_raw, dict):
        raise TextLayerError("text_layer.b_style: se esperaba un objeto.")
    b_style = BStyle(
        max_lines=int(bs_raw.get("max_lines", B_MAX_LINES_DEFAULT)),
        max_chars_per_line=int(bs_raw.get("max_chars_per_line", B_MAX_CHARS_DEFAULT)),
    )
    if not 1 <= b_style.max_lines <= 2:
        raise TextLayerError(
            f"b_style.max_lines={b_style.max_lines}: el estandar del canal es 2 como maximo."
        )
    if not 20 <= b_style.max_chars_per_line <= 60:
        raise TextLayerError(
            f"b_style.max_chars_per_line={b_style.max_chars_per_line}: fuera del rango sensato (20-60)."
        )

    layer = TextLayer(version=1, cues=cues, b_style=b_style)

    # Solapes entre cues ya temporizados. Los que vienen por anchor_text se
    # comprueban despues, en el builder, cuando ya tienen tiempo.
    timed = sorted([c for c in layer.overlays if c.timed], key=lambda c: c.start)
    for prev, nxt in zip(timed, timed[1:]):
        if nxt.start < prev.end - 1e-6:
            raise TextLayerError(
                f"cues {prev.id!r} y {nxt.id!r} se solapan ({prev.start:.2f}-{prev.end:.2f} "
                f"vs {nxt.start:.2f}-{nxt.end:.2f}). Nunca puede haber dos capas de texto a la vez."
            )

    return layer
