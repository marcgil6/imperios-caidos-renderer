#!/usr/bin/env python3
"""Video largo de EP (1920x1080) → Short vertical 1080x1920 a 30 fps.

    python scripts/make_short.py video.mp4 --start 252.3 --duration 48.5 \\
        --text "Hay una puerta en el sur de la India..." -o EP1_short1.mp4

    (o --text-file corte.txt en lugar de --text)

Composicion:

    * fondo: el mismo fotograma (ya recortado) escalado a pantalla completa con gblur sigma 40
    * encima, el video original a 1080 de ancho, centrado (608 px de alto).
      Con --keep-top N solo se usan las N filas de arriba del original: los
      renders anteriores a la capa de texto llevan el subtitulo de Whisper
      quemado en y=714..930 y con --keep-top 700 no sale duplicado.
    * subtitulos quemados en la franja de debajo del video: Anton 85 px (o
      Liberation Sans Bold si no esta Anton), blanco, contorno negro de 5 px,
      una linea cada vez y como mucho 4 palabras por linea
    * H.264 + AAC 128k, faststart

Tiempos de los subtitulos: el texto llega sin tiempos, asi que se transcribe el
audio del corte con faster-whisper y se alinea el texto contra esa
transcripcion (la misma `align_script_to_words` del render largo). Asi el
subtitulo muestra lo que dice el guion con el tiempo de la voz real. Si
Whisper no esta instalado o el texto no casa con el audio, se reparte el
tiempo en proporcion a los caracteres y se avisa por stderr.

Este script NO toca Drive ni Airtable: lee un fichero y escribe otro.

Candado de musica: el video de origen tiene que llevar el sello de musica libre
(sello_musica) y el Short hereda ese sello. Asi un corte sacado de un render
viejo con musica reclamable no llega a existir (los Shorts de Otzi y Qin del
22/09/2026 salieron del render del 05/08 del video 4, con Scott Buckley).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from text_layer.alignment import STRONG_PUNCT, WEAK_PUNCT, Word, align_script_to_words
from text_layer.burn import ass_filter, find_fonts_dir
from text_layer.metrics import ass_fontsize, text_width
from text_layer.schema import TextLayerError
import sello_musica

OUT_W, OUT_H, FPS = 1080, 1920, 30
MIN_DUR, MAX_DUR = 45.0, 55.0
BLUR_SIGMA = 40

FONT_PX = 85                  # px de CSS (cuadratin), no Fontsize de ASS
OUTLINE_PX = 5
MAX_WORDS = 4
SIDE_MARGIN = 60              # ancho util de linea: 1080 - 2*60 = 960 px

# El video de 16:9 ocupa y=656..1264 (menos si se recorta con --keep-top). La
# linea se centra en y=1400: dentro de la franja inferior y por encima de la
# zona que tapan el titulo y el canal en la interfaz de Shorts (el ultimo ~25%
# de la pantalla).
SUB_CENTER_Y = 1400

# Si la palabra que cerraria la linea es una de estas, pasa a la siguiente:
# "EN EL FONDO DEL" / "MAR" se lee peor que "EN EL FONDO" / "DEL MAR".
FUNCTION_WORDS = {
    "a", "al", "con", "de", "del", "el", "en", "la", "las", "lo", "los", "o",
    "para", "por", "que", "se", "su", "sus", "un", "una", "y", "e", "ni", "sin",
}

GAP_FILL = 0.6                # huecos de voz menores que esto no dejan pantalla vacia
TAIL = 0.25                   # la ultima linea aguanta un poco tras su palabra


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def run(cmd):
    subprocess.run(cmd, check=True)


def pick_font():
    """(familia, Bold) — Anton si su fichero esta en fonts/, si no Liberation Sans Bold."""
    fonts = find_fonts_dir()
    if fonts and os.path.exists(os.path.join(fonts, "Anton-Regular.ttf")):
        return "Anton", 0
    log("AVISO: no encuentro Anton-Regular.ttf, uso Liberation Sans Bold")
    return "Liberation Sans", 1


# ── Tiempos por palabra ────────────────────────────────────


def _tokens(text):
    return [t for t in re.sub(r"\s+", " ", text).strip().split(" ") if t]


def words_by_whisper(text, wav):
    from faster_whisper import WhisperModel   # solo se importa si se usa

    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(wav, language="es", beam_size=1, best_of=1,
                                   vad_filter=True, condition_on_previous_text=False,
                                   word_timestamps=True)
    heard = [Word(w.word.strip(), float(w.start), float(w.end))
             for seg in segments for w in (seg.words or []) if w.word.strip()]
    return align_script_to_words(text, heard)


def words_by_proportion(text, duration):
    tokens = _tokens(text)
    total = sum(len(t) + 1 for t in tokens)
    words, t = [], 0.0
    for tok in tokens:
        step = duration * (len(tok) + 1) / total
        words.append(Word(tok, t, t + step))
        t += step
    return words


def timed_words(text, wav, duration, mode):
    if mode in ("auto", "whisper"):
        try:
            return words_by_whisper(text, wav), "whisper"
        except (ImportError, TextLayerError) as e:
            if mode == "whisper":
                raise
            log(f"AVISO: sin alineacion con Whisper ({e}); reparto proporcional")
    return words_by_proportion(text, duration), "proportional"


# ── Lineas ─────────────────────────────────────────────────


def split_lines(words, family, max_width):
    """Agrupa palabras en lineas de <= MAX_WORDS que quepan en `max_width`."""
    lines, cur = [], []

    def fits(ws):
        width = text_width(" ".join(w.word for w in ws), family, FONT_PX)
        return width is None or width <= max_width

    for w in words:
        if cur and (len(cur) >= MAX_WORDS or not fits(cur + [w])):
            # No cerrar la linea en articulo o preposicion si se puede evitar.
            carry = []
            while len(cur) > 1 and cur[-1].word.lower() in FUNCTION_WORDS:
                carry.insert(0, cur.pop())
            lines.append(cur)
            cur = carry
        cur.append(w)
        if w.word.rstrip('"»”)').endswith(STRONG_PUNCT + WEAK_PUNCT):
            lines.append(cur)
            cur = []
    if cur:
        lines.append(cur)
    return lines


def cue_times(lines, duration):
    """[(inicio, fin, texto)] — una entrada por linea en pantalla."""
    cues = []
    for i, line in enumerate(lines):
        start = max(0.0, line[0].start)
        end = line[-1].end + TAIL
        if i + 1 < len(lines):
            nxt = lines[i + 1][0].start
            end = nxt if nxt - line[-1].end < GAP_FILL else min(end, nxt)
        end = min(end, duration)
        if end > start:
            cues.append((start, end, " ".join(w.word for w in line)))
    return cues


def ass_time(t):
    cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_escape(text):
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def build_ass(cues, family, bold):
    fontsize = ass_fontsize(family, FONT_PX) if family == "Anton" else FONT_PX
    margin_v = round(OUT_H - SUB_CENTER_Y - fontsize / 2)
    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {OUT_W}",
        f"PlayResY: {OUT_H}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Short,{family},{fontsize},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        f"{bold},0,0,0,100,100,0,0,1,{OUTLINE_PX},0,2,{SIDE_MARGIN},{SIDE_MARGIN},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = [f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Short,,0,0,0,,{ass_escape(t)}"
              for s, e, t in cues]
    return "\n".join(head + events) + "\n"


# ── Respaldo sin libass ────────────────────────────────────
#
# El FFmpeg de Homebrew no trae libass (ni drawtext), asi que en local `ass=`
# no existe. En ese caso cada linea se dibuja con Pillow en un PNG transparente
# y se superpone con `overlay` durante su intervalo. Mismo aspecto: Pillow toma
# el tamano como cuadratin, que es justo lo que son los 85 px.

LIBERATION_BOLD = (
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
)


def has_libass():
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                         capture_output=True, text=True).stdout
    return re.search(r"^\s*\S+\s+ass\s", out, re.M) is not None


def font_file(family):
    fonts = find_fonts_dir()
    candidates = [os.path.join(fonts, "Anton-Regular.ttf")] if family == "Anton" and fonts else []
    for path in candidates + list(LIBERATION_BOLD):
        if os.path.exists(path):
            return path
    raise SystemExit("No hay libass y no encuentro ni Anton ni Liberation Sans Bold para Pillow")


def render_line_pngs(cues, family, work):
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(font_file(family), FONT_PX)
    box_h = FONT_PX * 2
    paths = []
    for i, (_s, _e, text) in enumerate(cues):
        img = Image.new("RGBA", (OUT_W, box_h), (0, 0, 0, 0))
        ImageDraw.Draw(img).text((OUT_W / 2, box_h / 2), text, font=font, anchor="mm",
                                 fill="white", stroke_width=OUTLINE_PX, stroke_fill="black")
        path = os.path.join(work, f"line_{i:03d}.png")
        img.save(path)
        paths.append(path)
    return paths, SUB_CENTER_Y - box_h // 2


def overlay_chain(cues, first_input, y, src_label):
    parts, label = [], src_label
    for i, (s, e, _t) in enumerate(cues):
        out = f"s{i}"
        parts.append(f"[{label}][{first_input + i}:v]overlay=0:{y}:"
                     f"enable='between(t,{s:.3f},{e - 0.001:.3f})'[{out}]")
        label = out
    return parts, label


# ── Render ─────────────────────────────────────────────────


def probe_duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                   "format=duration", "-of", "csv=p=0", path])
    return float(out)


def probe_size(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                   "-show_entries", "stream=width,height", "-of", "csv=p=0", path])
    w, h = out.decode().strip().split(",")[:2]
    return int(w), int(h)


def fg_height(src_w, src_h):
    return round(OUT_W * src_h / src_w / 2) * 2


def make_short(src, start, duration, text, out, timing="auto", keep_top=None,
               marca=None, banda_alto=None, banda_cy=None):
    if not MIN_DUR <= duration <= MAX_DUR:
        raise SystemExit(f"--duration debe estar entre {MIN_DUR:g} y {MAX_DUR:g} s (llego {duration:g})")
    total = probe_duration(src)
    if start < 0 or start + duration > total + 0.05:
        raise SystemExit(f"El corte {start:.2f}+{duration:.2f}s se sale del video ({total:.2f}s)")
    if not text.strip():
        raise SystemExit("Texto de subtitulos vacio")
    try:
        sello = sello_musica.exigir_sello(src)
    except sello_musica.MusicaNoCertificada as e:
        raise SystemExit(f"Short bloqueado: {e}")
    pistas = [t for t in sello.get("tracks", "").split(",") if t]
    src_w, full_h = probe_size(src)
    src_h = min(keep_top or full_h, full_h)

    family, bold = pick_font()
    with tempfile.TemporaryDirectory(prefix="short_") as work:
        wav = os.path.join(work, "cut.wav")
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
             "-i", src, "-vn", "-ac", "1", "-ar", "16000", wav])
        words, mode = timed_words(text, wav, duration, timing)
        lines = split_lines(words, family, OUT_W - 2 * SIDE_MARGIN)
        cues = cue_times(lines, duration)

        graph = [
            f"[0:v]fps={FPS},crop=iw:{src_h}:0:0,split=2[bg][fg]",
            f"[bg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
            f"crop={OUT_W}:{OUT_H},gblur=sigma={BLUR_SIGMA}[bgb]",
            *( [f"[fg]scale=-2:{banda_alto},crop={OUT_W}:{banda_alto}[fgs]",
                f"[bgb][fgs]overlay=(W-w)/2:{(banda_cy or OUT_H // 2) - banda_alto // 2},setsar=1[comp]"]
               if banda_alto else
               [f"[fg]scale={OUT_W}:{fg_height(src_w, src_h)}[fgs]",
                "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[comp]"] ),
        ]
        extra_inputs = []
        if marca and has_libass():
            extra_inputs += ["-i", marca]
        if has_libass():
            burner = "libass"
            ass_path = os.path.join(work, "subs.ass")
            with open(ass_path, "w", encoding="utf-8") as f:
                f.write(build_ass(cues, family, bold))
            if marca:
                graph.append(f"[comp]{ass_filter(ass_path)}[subs]")
                graph.append("[subs][1:v]overlay=0:0,format=yuv420p[v]")
            else:
                graph.append(f"[comp]{ass_filter(ass_path)},format=yuv420p[v]")
        else:
            burner = "pillow"
            pngs, y = render_line_pngs(cues, family, work)
            for p in pngs:
                extra_inputs += ["-i", p]
            if marca:
                extra_inputs += ["-i", marca]
            chain, last = overlay_chain(cues, 1, y, "comp")
            graph += chain
            if marca:
                graph.append(f"[{last}][{1 + len(pngs)}:v]overlay=0:0[mk]")
                last = "mk"
            graph.append(f"[{last}]format=yuv420p[v]")

        run(["ffmpeg", "-y", "-v", "error", "-stats",
             "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", src, *extra_inputs,
             "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "0:a:0",
             "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-profile:v", "high", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
             "-movflags", "+faststart", "-shortest",
             *sello_musica.args_sello(sello["ep-music"], pistas, "short"), out])

    return {"output": out, "start": start, "duration": duration, "font": family,
            "keep_top": src_h, "marca": marca, "banda_alto": banda_alto, "banda_cy": banda_cy,
            "timing": mode, "subtitles": burner, "lines": len(cues), "words": len(words),
            "output_duration": round(probe_duration(out), 3)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", help="MP4 de origen (1920x1080)")
    ap.add_argument("--start", type=float, required=True, help="segundo de inicio")
    ap.add_argument("--duration", type=float, required=True, help="duracion, 45-55 s")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="texto de los subtitulos")
    group.add_argument("--text-file", help="fichero con el texto de los subtitulos")
    ap.add_argument("-o", "--output", required=True, help="MP4 vertical de salida")
    ap.add_argument("--keep-top", type=int,
                    help="usar solo las N filas superiores del original (p. ej. 700 para "
                         "quitar el subtitulo ya quemado)")
    ap.add_argument("--marca", help="PNG transparente 1080x1920 con la marca del canal "
                                    "(lo genera enigmas-del-pasado/scripts/marca_short.py)")
    ap.add_argument("--banda-alto", type=int,
                    help="alto en px de la banda de video; recorta los lados si hace falta")
    ap.add_argument("--banda-centro-y", type=int, help="centro vertical de la banda de video")
    ap.add_argument("--timing", choices=("auto", "whisper", "proportional"), default="auto",
                    help="auto: Whisper y, si falla, proporcional (por defecto)")
    args = ap.parse_args()

    text = args.text
    if args.text_file:
        with open(args.text_file, encoding="utf-8") as f:
            text = f.read()
    report = make_short(args.video, args.start, args.duration, text, args.output,
                        args.timing, args.keep_top,
                        args.marca, args.banda_alto, args.banda_centro_y)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
