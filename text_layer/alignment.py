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


# Whisper deja de emitir segmentos a media narracion en tomas de 20+ min: no
# da error, simplemente se calla. El renderer ya lo sabia y por eso troceaba
# la narracion en ventanas de 5 min antes de transcribir. Aqui hay que hacer
# lo mismo.
WHISPER_CHUNK_SEC = 300


def _transcribe(model, audio_path, language):
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


def words_from_whisper(model, audio_path, language="es", duration=None, cut=None,
                       chunk_sec=WHISPER_CHUNK_SEC, log=None):
    """faster-whisper con tiempos por palabra.

    Tres guardas, y las TRES hacen falta:

    * `condition_on_previous_text=False` — si no, Whisper se realimenta de su
      propia salida y deriva.
    * `vad_filter=True`.
    * **Trocear la narracion en ventanas de `chunk_sec`.** Esta es la que de
      verdad importa y la que faltaba: en una toma de 19 min, Whisper emitio
      solo las primeras ~600 palabras de 2.282 y se callo, sin dar error. El
      render del video 7 se detuvo con "el guion y la narracion no casan: solo
      599 de 2282 palabras coinciden" — que al menos fallo a la vista, pero
      fallo.

    Para trocear hacen falta `duration` y `cut(inicio, duracion) -> ruta`, que
    los pone quien llama (el renderer, que ya tiene ffmpeg a mano). Sin ellos
    se hace una sola pasada, que vale para audios cortos.
    """
    import math

    if duration and cut and duration > chunk_sec * 1.2:
        n = math.ceil(duration / chunk_sec)
        if log:
            log("Transcribiendo en %d ventanas de ~%ds (audio %.0fs)", n, chunk_sec, duration)
        words, info = [], None
        for i in range(n):
            inicio = i * chunk_sec
            trozo = cut(inicio, min(chunk_sec, duration - inicio))
            parciales, info = _transcribe(model, trozo, language)
            words.extend(Word(w.word, w.start + inicio, w.end + inicio) for w in parciales)
            if log:
                log("  ventana %d/%d [%.0f-%.0fs]: %d palabras",
                    i + 1, n, inicio, inicio + chunk_sec, len(parciales))
        return words, info

    return _transcribe(model, audio_path, language)


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


def align_script_to_words(script_text, words, ventana=150, holgura=90,
                          minimo_alineado=0.25):
    """Devuelve los tokens del GUION con los tiempos de la narracion real.

    Se alinea por VENTANAS, no de una sola pasada. `difflib` sobre las 2.300
    palabras de golpe alinea muy mal: en el render del video 7 dio por
    coincidentes 599 de 2.282, y en una prueba controlada un 13,9%, marcando
    como no encontradas palabras tan comunes como "de" o "con" que estaban en
    los dos lados. `SequenceMatcher` busca bloques largos, y cuando el ASR se
    equivoca cada pocas palabras los bloques se fragmentan y el emparejamiento
    se descoloca por completo.

    Recorriendo el guion en ventanas de ~150 tokens y buscando cada una en el
    tramo de transcripcion donde toca (mas una holgura), las secuencias que ve
    `difflib` son cortas y las alinea bien. Ademas el avance es monotono, asi
    que una frase repetida no puede emparejarse con la ocurrencia equivocada
    del otro extremo del video — que es lo que descartaba la alternativa de
    buscar n-gramas unicos.
    """
    tokens = _script_tokens(script_text)
    if not tokens:
        raise TextLayerError("script_text vacio: no hay guion con el que corregir la transcripcion.")
    if not words:
        raise TextLayerError("alignment vacio: no hay palabras con tiempos.")

    a = [normalize(t) for t in tokens]
    b = [w.norm for w in words]
    aligned: List[Optional[Word]] = [None] * len(tokens)

    ai, bi, alineados = 0, 0, 0
    while ai < len(a):
        a_fin = min(ai + ventana, len(a))
        # Donde deberia caer esta ventana en la transcripcion: por donde se
        # quedo la anterior, o proporcional al avance si aun no hay nada.
        b_est = bi if bi else int(ai * len(b) / max(len(a), 1))
        b_ini = max(0, b_est - holgura // 2)
        b_fin = min(len(b), b_ini + (a_fin - ai) + holgura)

        avance_a, avance_b = ai, bi
        if b_ini < b_fin:
            sm = difflib.SequenceMatcher(None, a[ai:a_fin], b[b_ini:b_fin], autojunk=False)
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                # 'equal' son coincidencias reales. 'replace' con el mismo
                # numero de palabras a los dos lados tambien vale: el ASR oyo
                # mal la palabra pero ocupa la misma casilla ("Amalia" por
                # "Amelia"), asi que su tiempo es el bueno e interpolarlo
                # seria perder precision a cambio de nada.
                if tag == "equal" or (tag == "replace" and (i2 - i1) == (j2 - j1)):
                    for k in range(i2 - i1):
                        aligned[ai + i1 + k] = Word(tokens[ai + i1 + k],
                                                    words[b_ini + j1 + k].start,
                                                    words[b_ini + j1 + k].end)
                    if tag == "equal":
                        alineados += i2 - i1
                    avance_a, avance_b = ai + i2, b_ini + j2

        # La ventana siempre avanza, coincida o no: si no, bucle infinito.
        ai = avance_a if avance_a > ai else a_fin
        bi = avance_b if avance_b > bi else min(len(b), b_fin)

    fraccion = alineados / len(tokens)
    if fraccion < minimo_alineado:
        raise TextLayerError(
            f"El guion y la narracion no casan: solo {alineados} de {len(tokens)} "
            f"palabras del guion ({fraccion:.0%}) se localizan en la transcripcion. "
            "Comprueba que script_text es el guion de ESTE audio."
        )

    _interpolate(aligned, tokens, words)
    resultado = [w for w in aligned if w is not None]

    # Los tiempos tienen que ir hacia delante siempre.
    for x, y in zip(resultado, resultado[1:]):
        if y.start < x.start - 1e-6:
            y.start = x.start
        if y.end < y.start:
            y.end = y.start
    return resultado


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
