"""Alineacion por palabras: {word, start, end} para toda la narracion.

Tres piezas:

* `words_from_elevenlabs` — via de produccion. `/with-timestamps` devuelve
  tiempos por CARACTER; aqui se agrupan en palabras.
* `words_from_whisper` — via del test y fallback. faster-whisper con
  `word_timestamps=True`. Coste 0.
* `align_script_to_words` — corrige la transcripcion con el guion real, para
  que el subtitulo B muestre siempre lo que dice el guion y no lo que "oyo"
  Whisper. Sin esto, el estilo documental hereda las erratas del ASR.

Se usa `difflib` (stdlib) y no rapidfuzz: la comparacion es sobre listas de
tokens normalizados de unos pocos miles de elementos, donde SequenceMatcher
sobra, y el renderer no gana nada arrastrando otra dependencia.
"""

import difflib
import re
from dataclasses import dataclass
from typing import List, Optional

from .schema import TextLayerError, normalize

# Puntuacion que corta frase (fuerte) y que corta linea (debil).
STRONG_PUNCT = (".", "!", "?", "…", "…")
WEAK_PUNCT = (",", ";", ":", "—", "–")


@dataclass
class Word:
    word: str
    start: float
    end: float

    @property
    def norm(self):
        return normalize(self.word)


# ── Fuentes de alineacion ──────────────────────────────────


def words_from_whisper(model, audio_path, language="es"):
    """faster-whisper con tiempos por palabra.

    Se mantienen las mismas guardas que ya usa el renderer para narraciones
    largas: `condition_on_previous_text=False` y VAD, porque en una toma de
    20+ min Whisper deriva y deja de emitir segmentos a media narracion.
    """
    segments, info = model.transcribe(
        audio_path,
        language=language,
        beam_size=1,
        best_of=1,
        vad_filter=True,
        condition_on_previous_text=False,
        word_timestamps=True,
    )
    words = []
    for seg in segments:
        for w in (seg.words or []):
            text = w.word.strip()
            if text:
                words.append(Word(text, float(w.start), float(w.end)))
    return words, info


def words_from_elevenlabs(alignment):
    """`alignment` de ElevenLabs `/with-timestamps` → lista de Word.

    Espera `characters`, `character_start_times_seconds` y
    `character_end_times_seconds`. Los caracteres se acumulan hasta un espacio.
    """
    chars = alignment.get("characters")
    starts = alignment.get("character_start_times_seconds")
    ends = alignment.get("character_end_times_seconds")
    if not chars or starts is None or ends is None:
        raise TextLayerError(
            "alignment de ElevenLabs incompleto: faltan characters o los arrays de tiempos."
        )
    if not (len(chars) == len(starts) == len(ends)):
        raise TextLayerError(
            f"alignment de ElevenLabs descuadrado: {len(chars)} caracteres, "
            f"{len(starts)} inicios, {len(ends)} finales."
        )

    words, buf, w_start, w_end = [], "", None, None
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if buf:
                words.append(Word(buf, w_start, w_end))
                buf, w_start, w_end = "", None, None
            continue
        if not buf:
            w_start = float(s)
        buf += ch
        w_end = float(e)
    if buf:
        words.append(Word(buf, w_start, w_end))
    return words


def words_from_payload(alignment):
    """`alignment` tal y como puede llegar en el body de /render.

    Admite `{"words": [...]}` (nuestro formato) o el objeto crudo de
    ElevenLabs `{"characters": [...], ...}`.
    """
    if not isinstance(alignment, dict):
        raise TextLayerError(f"alignment: se esperaba un objeto, llego {type(alignment).__name__}")
    if "characters" in alignment:
        return words_from_elevenlabs(alignment)
    raw = alignment.get("words")
    if not isinstance(raw, list) or not raw:
        raise TextLayerError("alignment.words: hace falta una lista de palabras no vacia.")
    words = []
    for i, w in enumerate(raw):
        try:
            words.append(Word(str(w["word"]), float(w["start"]), float(w["end"])))
        except (KeyError, TypeError, ValueError) as e:
            raise TextLayerError(f"alignment.words[{i}]: {w!r} no es {{word, start, end}} ({e})")
    return words


# ── Correccion con el guion ────────────────────────────────


def _script_tokens(script_text):
    """Trocea el guion en tokens conservando la puntuacion pegada a la palabra."""
    cleaned = re.sub(r"<break[^>]*/?>", " ", script_text or "")   # tags TTS de EP-03B
    cleaned = cleaned.replace("…", "…")
    return [t for t in cleaned.split() if t.strip()]


def align_script_to_words(script_text, words):
    """Devuelve los tokens del GUION con los tiempos de la narracion real.

    Empareja token a token contra la transcripcion por similitud. Los tramos
    que Whisper no acerto (o que se comio) se reparten proporcionalmente entre
    los anclajes que si casaron, asi que ningun token del guion se queda sin
    tiempo.
    """
    tokens = _script_tokens(script_text)
    if not tokens:
        raise TextLayerError("script_text vacio: no hay guion con el que corregir la transcripcion.")
    if not words:
        raise TextLayerError("alignment vacio: no hay palabras con tiempos.")

    a = [normalize(t) for t in tokens]
    b = [w.norm for w in words]

    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    aligned: List[Optional[Word]] = [None] * len(tokens)
    matched = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                aligned[i1 + k] = Word(tokens[i1 + k], words[j1 + k].start, words[j1 + k].end)
            matched += i2 - i1
        elif tag == "replace" and (i2 - i1) == (j2 - j1):
            # Mismo numero de palabras a los dos lados: Whisper oyo mal la
            # palabra pero ocupa la misma casilla ("Amalia" por "Amelia"), asi
            # que su tiempo es el bueno. Interpolar aqui seria perder precision
            # a cambio de nada.
            for k in range(i2 - i1):
                aligned[i1 + k] = Word(tokens[i1 + k], words[j1 + k].start, words[j1 + k].end)

    if matched < max(3, len(tokens) * 0.30):
        raise TextLayerError(
            f"El guion y la narracion no casan: solo {matched} de {len(tokens)} palabras "
            "coinciden. Comprueba que script_text es el guion de ESTE audio."
        )

    _interpolate(aligned, tokens, words)
    return [w for w in aligned if w is not None]


def _interpolate(aligned, tokens, words):
    """Rellena los huecos de `aligned` repartiendo el tiempo entre anclajes."""
    n = len(aligned)
    anchors = [i for i, w in enumerate(aligned) if w is not None]
    first, last = anchors[0], anchors[-1]

    # Cabeza y cola: fuera del rango emparejado, se reparte contra los extremos
    # reales del audio para no inventar tiempos negativos ni pasarse del final.
    if first > 0:
        head_start = words[0].start
        span = max(aligned[first].start - head_start, 0.0) or 0.001
        for k in range(first):
            aligned[k] = Word(tokens[k],
                              head_start + span * k / first,
                              head_start + span * (k + 1) / first)
    if last < n - 1:
        tail_end = words[-1].end
        span = max(tail_end - aligned[last].end, 0.0) or 0.001
        tail_n = n - 1 - last
        for k in range(last + 1, n):
            idx = k - last - 1
            aligned[k] = Word(tokens[k],
                              aligned[last].end + span * idx / tail_n,
                              aligned[last].end + span * (idx + 1) / tail_n)

    # Huecos interiores
    for left, right in zip(anchors, anchors[1:]):
        gap = right - left - 1
        if gap <= 0:
            continue
        t0, t1 = aligned[left].end, aligned[right].start
        span = max(t1 - t0, 0.0)
        for k in range(gap):
            aligned[left + 1 + k] = Word(tokens[left + 1 + k],
                                         t0 + span * k / gap,
                                         t0 + span * (k + 1) / gap)


# ── Busqueda de anchor_text ────────────────────────────────


def find_anchor(anchor_text, words, min_ratio=0.72):
    """Localiza una frase del guion en la alineacion → (start, end, i, j).

    Devuelve None si no la encuentra por encima de `min_ratio`. Quien llama
    decide si eso es un error (lo es: el encargo prohibe el fallo silencioso).
    """
    target = normalize(anchor_text)
    if not target:
        return None
    target_tokens = target.split()
    n = len(target_tokens)
    if n == 0 or not words:
        return None

    norms = [w.norm for w in words]
    best, best_ratio = None, 0.0
    # Se prueban ventanas de longitud parecida a la frase buscada: el ASR puede
    # partir o unir alguna palabra, asi que +-2 tokens de holgura.
    for width in range(max(1, n - 2), n + 3):
        for i in range(0, len(words) - width + 1):
            window = " ".join(norms[i:i + width])
            ratio = difflib.SequenceMatcher(None, target, window, autojunk=False).ratio()
            if ratio > best_ratio:
                best_ratio, best = ratio, (i, i + width)
    if best is None or best_ratio < min_ratio:
        return None
    i, j = best
    return words[i].start, words[j - 1].end, i, j


def anchor_candidates(anchor_text, words, k=3):
    """Las `k` ventanas mas parecidas, para el mensaje de error."""
    target = normalize(anchor_text)
    n = max(1, len(target.split()))
    norms = [w.norm for w in words]
    scored = []
    for i in range(0, max(0, len(words) - n + 1)):
        window = " ".join(norms[i:i + n])
        scored.append((difflib.SequenceMatcher(None, target, window, autojunk=False).ratio(),
                       " ".join(w.word for w in words[i:i + n])))
    scored.sort(reverse=True)
    return scored[:k]
