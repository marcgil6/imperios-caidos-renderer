"""
Motor de audio del render: biblioteca musical, plan musical por tramos,
construcción de la cama sonora y mezcla final con ducking y master.

Se mantiene aparte de render.py a propósito: no depende de flask, whisper ni
de las librerías de Google, así que se puede validar en local con ffmpeg y
material real sin levantar el servicio.

Cadena de mezcla (calibrada 2026-08-27 sobre narración real de El Faraón y
las pistas del canal):

  voz     → ganancia estática medida (a TARGET_VOICE_LUFS). Sin compresión:
            la narración de ElevenLabs ya llega con LRA ≈ 3 LU.
  música  → normalización por pista + acompressor suave (dome los subidones,
            LRA 11 LU → ~6) + envolvente del gancho + ducking sidechain con
            la voz como llave (≈6 dB, release 1500 ms).
  master  → suma + alimiter a -1 dBTP.

El release largo del ducking es deliberado: la narración no tiene pausas
largas (194 pausas medidas, todas entre 0,30 y 1,16 s), así que un release
corto haría bombear la música en cada coma. Con 1500 ms la cama se mantiene
estable bajo la voz y solo respira en los huecos estructurales (teaser,
transiciones del plan musical y cierre).
"""
import json
import math
import os
import random
import re
import subprocess

# ── Objetivos de mezcla ────────────────────────────────────
TARGET_VOICE_LUFS = -14.5   # YouTube normaliza a -14 LUFS; la voz manda
MUSIC_OPEN_LUFS = -30.0     # cama sin voz encima (≈15,5 dB bajo la voz).
                            # Era -26,5 hasta el 28/08/2026: Marc escuchó los
                            # primeros remixes y pidió la música más discreta,
                            # así que los -3,5 dB que se aplicaban a mano por
                            # payload pasan a ser el valor por defecto.
HOOK_BOOST_DB = 6.0         # presencia extra en el bloque de gancho
LIMITER_TP = -1.0           # dBTP del master

BED_COMP = "acompressor=threshold=0.05:ratio=3:attack=50:release=600:makeup=1"
DUCK = ("sidechaincompress=threshold=0.1:ratio=3:attack=20:"
        "release=1500:makeup=1:level_sc=1")

BED_XFADE = 3.0             # crossfade entre tramos del plan musical
LOOP_XFADE = 4.0            # crossfade al reciclar una pista sobre sí misma
BED_FADE_IN = 2.0
BED_FADE_OUT = 6.0
MIN_SEGMENT = 25.0          # tramos más cortos se funden con el anterior

INTENSITY_DB = {"low": -2.0, "medium": 0.0, "high": 2.0}

MOODS = ("mystery", "tension", "ancient", "dark", "discovery",
         "emotional", "atmospheric", "neutral")

# Si no hay pista del mood pedido se cae al vecino más cercano antes que a
# "cualquiera": una tensión sonando donde tocaba misterio es mucho menos
# grave que una pieza emocional en mitad de un clímax.
MOOD_NEIGHBOURS = {
    "mystery": ("atmospheric", "dark", "ancient"),
    "tension": ("dark", "mystery"),
    "ancient": ("atmospheric", "mystery", "emotional"),
    "dark": ("tension", "mystery"),
    "discovery": ("emotional", "neutral", "ancient"),
    "emotional": ("ancient", "discovery", "atmospheric"),
    "atmospheric": ("mystery", "ancient", "neutral"),
    "neutral": ("atmospheric", "ancient"),
}

AUDIO_EXT = (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac")


class AudioMixError(RuntimeError):
    pass


# ── ffmpeg / ffprobe ───────────────────────────────────────

def _run(args, timeout=1800):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise AudioMixError(f"ffmpeg exit {r.returncode}: {(r.stderr or '')[-800:]}")
    return r


def probe_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return None


def measure_loudness(path, timeout=900):
    """Integrated LUFS y true peak de un archivo. None si no se puede medir."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
             "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout)
        m = re.findall(r"\{[^{}]*input_i[^{}]*\}", r.stderr, re.S)
        if not m:
            return None
        d = json.loads(m[-1])
        return {"lufs": float(d["input_i"]), "tp": float(d["input_tp"]),
                "lra": float(d["input_lra"])}
    except Exception:
        return None


# ── Biblioteca musical ─────────────────────────────────────

def _mood_from_name(name):
    """`mystery_low_02.mp3` → ('mystery', 'low'). Convención de nombres para
    que ampliar la biblioteca sea copiar archivos a la carpeta, sin tocar
    ni código ni manifiesto."""
    stem = os.path.splitext(os.path.basename(name))[0].lower()
    parts = re.split(r"[^a-z0-9]+", stem)
    mood = next((p for p in parts if p in MOODS), None)
    intensity = next((p for p in parts if p in INTENSITY_DB), "medium")
    return mood, intensity


def load_library(library_dir, legacy_tracks=None):
    """
    Biblioteca = archivos de `library_dir` (mood/intensidad por nombre) +
    metadatos opcionales de `library.json` (título, licencia, atribución).
    Si la carpeta está vacía se cae a las pistas heredadas para que el render
    nunca se quede sin música.
    """
    meta = {}
    manifest = os.path.join(library_dir, "library.json")
    if os.path.isfile(manifest):
        try:
            with open(manifest, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for t in data.get("tracks", []):
                if t.get("file"):
                    meta[t["file"]] = t
        except Exception:
            meta = {}

    tracks = []
    if os.path.isdir(library_dir):
        for fn in sorted(os.listdir(library_dir)):
            if not fn.lower().endswith(AUDIO_EXT):
                continue
            m = meta.get(fn, {})
            mood, intensity = _mood_from_name(fn)
            mood = m.get("mood") or mood
            if not mood:
                continue          # sin mood no entra: evita colar un SFX
            tracks.append({
                "id": os.path.splitext(fn)[0],
                "path": os.path.join(library_dir, fn),
                "mood": mood,
                "intensity": m.get("intensity") or intensity,
                "title": m.get("title") or os.path.splitext(fn)[0],
                "source": m.get("source", "biblioteca del canal"),
                "attribution": m.get("attribution"),
                "lufs": m.get("lufs"),
            })

    if not tracks and legacy_tracks:
        tracks = [dict(t) for t in legacy_tracks]
    return tracks


def library_summary(tracks):
    by_mood = {}
    for t in tracks:
        by_mood[t["mood"]] = by_mood.get(t["mood"], 0) + 1
    return {"tracks": len(tracks), "moods": by_mood,
            "needs_attribution": sum(1 for t in tracks if t.get("attribution"))}


# ── Plan musical ───────────────────────────────────────────

def normalize_plan(plan, narration_dur, teaser_sec=0.0):
    """
    Convierte el plan que manda EP-07 (tramos en fracciones 0-1 del guion) en
    tramos absolutos del vídeo final. Tolera fracciones o segundos, ordena,
    tapa huecos y descarta tramos ridículamente cortos. Sin plan válido
    devuelve un único tramo neutro: la música nunca es motivo de fallo.
    """
    total = teaser_sec + narration_dur
    segs = []
    for s in (plan or []):
        try:
            start = float(s.get("start", s.get("desde_fraccion", 0)))
            end = float(s.get("end", s.get("hasta_fraccion", 1)))
        except (TypeError, ValueError):
            continue
        if 0 <= start <= 1 and 0 <= end <= 1 and end <= 1.0001:
            start = teaser_sec + start * narration_dur
            end = teaser_sec + end * narration_dur
        mood = str(s.get("mood", "")).lower().strip()
        if mood not in MOODS:
            mood = "mystery"
        intensity = str(s.get("intensity", "medium")).lower().strip()
        if intensity not in INTENSITY_DB:
            intensity = "medium"
        segs.append({"start": max(0.0, start), "end": min(total, end),
                     "mood": mood, "intensity": intensity})

    segs = [s for s in segs if s["end"] > s["start"]]
    segs.sort(key=lambda s: s["start"])

    merged = []
    for s in segs:
        if merged and s["start"] < merged[-1]["end"]:
            s["start"] = merged[-1]["end"]          # sin solapes
        if s["end"] - s["start"] < MIN_SEGMENT:
            if merged:
                merged[-1]["end"] = max(merged[-1]["end"], s["end"])
            continue
        merged.append(s)

    if not merged:
        return [{"start": 0.0, "end": total, "mood": "mystery",
                 "intensity": "low"}]

    merged[0]["start"] = 0.0                        # cubre desde el teaser
    for a, b in zip(merged, merged[1:]):
        a["end"] = b["start"]                       # sin huecos
    merged[-1]["end"] = total
    return merged


def select_tracks(tracks, segments, avoid=None, seed=None):
    """
    Una pista por tramo. Evita repetir dentro del vídeo y las usadas en los
    últimos vídeos (`avoid`, más reciente primero). Con la misma semilla
    (el record de Airtable) un re-render suena exactamente igual.
    """
    if not tracks:
        raise AudioMixError("biblioteca musical vacía")
    rng = random.Random(seed)
    used = []

    # El veto por historial se recorta POR MOOD. Si de un mood solo hay tres
    # pistas y se vetan las de los últimos cinco vídeos, no queda ninguna y el
    # selector acaba poniendo un mood equivocado. Se vetan como mucho (n-1)
    # por mood, las más recientes: así siempre queda al menos una opción del
    # mood correcto y el sistema se autoajusta según crece la biblioteca.
    by_mood = {}
    for t in tracks:
        by_mood.setdefault(t["mood"], []).append(t["id"])
    mood_of = {t["id"]: t["mood"] for t in tracks}
    seen = {}
    capped = []
    for tid in (avoid or []):          # llega ordenado: más reciente primero
        mood = mood_of.get(tid)
        if mood is None:
            continue                   # pista que ya no está en la biblioteca
        limit = max(len(by_mood[mood]) - 1, 0)
        if seen.get(mood, 0) < limit:
            seen[mood] = seen.get(mood, 0) + 1
            capped.append(tid)
    avoid = capped

    def pick(mood, intensity):
        exact = lambda t: t["mood"] == mood and t["intensity"] == intensity
        same = lambda t: t["mood"] == mood
        near = lambda t: t["mood"] in MOOD_NEIGHBOURS.get(mood, ())
        anyt = lambda t: True
        fresh = set(used) | set(avoid)      # ni repetida aquí ni en los
        here = set(used)                    # últimos vídeos
        # Orden deliberado: acertar el mood pesa MÁS que no repetir. Repetir
        # la pista de tensión del vídeo anterior es preferible a colocar una
        # pieza emocional donde el guion pide tensión.
        tiers = [(exact, fresh), (same, fresh),
                 (exact, here), (same, here),
                 (near, fresh), (near, here),
                 (anyt, here), (same, set()), (anyt, set())]
        for f, excl in tiers:
            pool = [t for t in tracks if f(t) and t["id"] not in excl]
            if pool:
                return rng.choice(pool)
        return rng.choice(tracks)

    out = []
    for s in segments:
        t = pick(s["mood"], s["intensity"])
        used.append(t["id"])
        out.append(dict(s, track=t))
    return out


# ── Cama sonora ────────────────────────────────────────────

def _track_lufs(track, cache):
    if track.get("lufs") is not None:
        return float(track["lufs"])
    if track["id"] in cache:
        return cache[track["id"]]
    m = measure_loudness(track["path"], timeout=300)
    v = m["lufs"] if m and math.isfinite(m["lufs"]) else -16.0
    cache[track["id"]] = v
    return v


def _render_segment(track, gain_db, need, out_path):
    """Un tramo de `need` segundos de una pista, reciclándola con crossfade
    si es más corta (nada de cortes secos como el -stream_loop actual)."""
    dur = probe_duration(track["path"]) or 0
    if dur <= 0:
        raise AudioMixError(f"pista ilegible: {track['path']}")
    fmt = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"

    if dur >= need + 0.5:
        _run(["-i", track["path"], "-t", f"{need:.3f}",
              "-af", f"{fmt},volume={gain_db:.2f}dB", out_path])
        return

    step = max(dur - LOOP_XFADE, 1.0)
    copies = min(int(math.ceil(need / step)) + 1, 40)
    args, fc, prev = [], "", None
    for i in range(copies):
        args += ["-i", track["path"]]
        fc += f"[{i}:a]{fmt}[c{i}];"
        if prev is None:
            prev = f"[c{i}]"
        else:
            fc += (f"{prev}[c{i}]acrossfade=d={LOOP_XFADE}:c1=tri:c2=tri"
                   f"[x{i}];")
            prev = f"[x{i}]"
    fc += f"{prev}atrim=0:{need:.3f},asetpts=PTS-STARTPTS,volume={gain_db:.2f}dB[out]"
    _run(args + ["-filter_complex", fc, "-map", "[out]", out_path])


def build_bed(work, chosen, total_dur, lufs_cache=None):
    """Encadena los tramos con crossfade y devuelve el WAV de la cama."""
    lufs_cache = lufs_cache if lufs_cache is not None else {}
    fmt = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
    paths = []
    for i, seg in enumerate(chosen):
        dur = seg["end"] - seg["start"]
        need = dur + (BED_XFADE if i < len(chosen) - 1 else 0.0)
        gain = (MUSIC_OPEN_LUFS - _track_lufs(seg["track"], lufs_cache)
                + INTENSITY_DB.get(seg["intensity"], 0.0))
        p = os.path.join(work, f"bed_seg_{i:02d}.wav")
        _render_segment(seg["track"], gain, max(need, 2.0), p)
        paths.append(p)

    bed = os.path.join(work, "music_bed.wav")
    if len(paths) == 1:
        args = ["-i", paths[0]]
        chain = f"[0:a]{fmt}"
    else:
        args, fc, prev = [], "", None
        for i, p in enumerate(paths):
            args += ["-i", p]
            fc += f"[{i}:a]{fmt}[s{i}];"
            if prev is None:
                prev = f"[s{i}]"
            else:
                fc += (f"{prev}[s{i}]acrossfade=d={BED_XFADE}:c1=tri:c2=tri"
                       f"[j{i}];")
                prev = f"[j{i}]"
        chain = fc + f"{prev}anull"

    fade_out_start = max(total_dur - BED_FADE_OUT, 0.1)
    chain += (f",{BED_COMP}"
              f",afade=t=in:st=0:d={BED_FADE_IN}"
              f",afade=t=out:st={fade_out_start:.2f}:d={BED_FADE_OUT}"
              f",apad[bed]")
    _run(args + ["-filter_complex", chain, "-map", "[bed]",
                 "-t", f"{total_dur:.3f}", bed])
    return bed


# ── Envolvente del gancho ──────────────────────────────────

def hook_envelope(teaser_sec, hook_end, dip=0.5, fade=1.5):
    """
    Multiplicador sobre la cama ya normalizada (1.0 = nivel de crucero):
    presencia de tráiler durante teaser+gancho, silencio total medio segundo
    antes de que entre la narración y bajada suave al nivel base.
    """
    g = 10 ** (HOOK_BOOST_DB / 20.0)
    if teaser_sec <= 0:
        return None
    dip_start = max(teaser_sec - dip, 0.0)
    fade_end = hook_end + fade
    return (f"if(lt(t,{dip_start:.3f}),{g:.4f},"
            f"if(lt(t,{teaser_sec:.3f}),0,"
            f"if(lt(t,{hook_end:.3f}),{g:.4f},"
            f"if(lt(t,{fade_end:.3f}),{g:.4f}-({g:.4f}-1)*(t-{hook_end:.3f})/{fade},1))))")


# ── Mezcla final ───────────────────────────────────────────

def mix_final(video_path, narration_path, bed_path, output_path,
              teaser_sec=0.0, hook_end=30.0, teaser_voice_path=None,
              riser_path=None, riser_volume=0.9, bed_gain_db=0.0,
              voice_gain_db=0.0, timeout=1800):
    """Voz normalizada + cama con ducking (+ riser y voz del teaser) → MP4."""
    fmt = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
    delay = f",adelay=delays={int(round(teaser_sec * 1000))}:all=1" if teaser_sec > 0 else ""
    fc = f"[1:a]{fmt}{delay},volume={voice_gain_db:.2f}dB,asplit=2[vout][vkey];"

    env = hook_envelope(teaser_sec, hook_end)
    bed_chain = f"[2:a]{fmt},volume={bed_gain_db:.2f}dB"
    if env:
        bed_chain += f",volume='{env}':eval=frame"
    fc += bed_chain + "[bedlvl];"
    fc += f"[bedlvl][vkey]{DUCK}[mus];"

    args = ["-i", video_path, "-i", narration_path, "-i", bed_path]
    pads = "[vout][mus]"
    n = 2
    nxt = 3
    if riser_path and teaser_sec > 0:
        hit = max(teaser_sec - 0.5, 0.1)
        rdur = probe_duration(riser_path) or 0
        if rdur > hit:
            align = f"atrim=start={rdur - hit:.3f},asetpts=PTS-STARTPTS"
        else:
            align = f"adelay=delays={int(round((hit - rdur) * 1000))}:all=1"
        fc += f"[{nxt}:a]{fmt},{align},volume={riser_volume},apad[ris];"
        args += ["-i", riser_path]
        pads += "[ris]"
        n += 1
        nxt += 1
    if teaser_voice_path:
        fc += f"[{nxt}:a]{fmt},apad[tvoz];"
        args += ["-i", teaser_voice_path]
        pads += "[tvoz]"
        n += 1
        nxt += 1

    limit = 10 ** (LIMITER_TP / 20.0)
    fc += (f"{pads}amix=inputs={n}:duration=first:normalize=0,"
           f"alimiter=limit={limit:.4f}:attack=5:release=50:level=disabled[aout]")

    _run(args + ["-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                 "-shortest", "-movflags", "+faststart", output_path],
         timeout=timeout)
