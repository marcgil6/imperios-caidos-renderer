"""
ENIGMAS DEL PASADO / IMPERIOS CAIDOS - Video Render Service
Ken Burns + crossfade + audio mix via FFmpeg.
Word-by-word subtitles (Whisper) + CTA overlay in last 60s.
"""
import base64
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import logging
from pathlib import Path

import requests as http_requests
from flask import Flask, request, jsonify, send_file
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PIL import Image, ImageDraw, ImageFont

import audio_mix
from text_layer import (TextLayerError, build_ass, build_video_filters,
                        parse_text_layer, words_from_payload)
from text_layer.alignment import align_script_to_words, words_from_whisper
from text_layer.burn import find_fonts_dir

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("render")

# Bump this string on every render.py change that affects output —
# exposed via /health and in the /render response so a stale EasyPanel
# deploy can be spotted without shell access to the container.
# Subir esto en CADA cambio que se despliegue. El 2026-09-08 se arreglo la
# alineacion sin tocarlo, se redesplego, y /health seguia diciendo lo mismo:
# no habia forma de saber que el arreglo no habia entrado hasta lanzar un
# render de 23 minutos y verlo fallar igual.
BUILD_VERSION = "2026-09-09-capa-texto-3-teaser"


def _parse_creds(raw):
    """Parse service account JSON — accepts raw JSON or base64-encoded JSON."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(base64.b64decode(raw.strip()).decode())
    except Exception:
        return None


def _check_google_env():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    length = len(raw)
    preview = repr(raw[:20]) if raw else "(empty)"
    log.info("STARTUP — GOOGLE_SERVICE_ACCOUNT_JSON: len=%d, preview=%s", length, preview)
    if not raw:
        log.warning("STARTUP — variable is empty or not set!")
        return
    info = _parse_creds(raw)
    if info:
        log.info("STARTUP — creds OK: type=%s, project_id=%s, client_email=%s",
                 info.get("type"), info.get("project_id"), info.get("client_email"))
    else:
        log.error("STARTUP — creds parse FAILED")

_check_google_env()

def _writable_dir(container_path, fallback_name):
    p = Path(container_path)
    try:
        p.mkdir(exist_ok=True)
        return p
    except OSError:  # outside the container (local test run)
        p = Path(tempfile.gettempdir()) / fallback_name
        p.mkdir(exist_ok=True)
        return p

RENDERS_DIR = _writable_dir("/app/renders", "renders")
THUMBS_DIR = _writable_dir("/app/thumbs", "thumbs")

WHISPER_MODEL = None

_FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"


def _find_anton_font():
    candidates = [
        Path("/app/fonts/Anton-Regular.ttf"),
        Path(__file__).resolve().parent / "fonts" / "Anton-Regular.ttf",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return _FONT_BOLD  # fallback so thumbnail generation never hard-fails on a missing font

_FONT_ANTON = _find_anton_font()


def _load_whisper():
    global WHISPER_MODEL
    try:
        from faster_whisper import WhisperModel
        for ct in ("int8", "float32"):
            try:
                WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type=ct)
                log.info("Whisper base model loaded (compute_type=%s).", ct)
                return
            except Exception as e:
                log.warning("Whisper compute_type=%s failed: %s — trying next", ct, e)
        log.error("Whisper: all compute_type options failed — subtitles disabled.")
    except Exception as e:
        log.warning("Whisper could not be loaded — subtitles disabled: %s", e)

_load_whisper()


def _find_music_dir():
    candidates = [
        Path("/app/music"),
        Path(__file__).resolve().parent / "music",
    ]
    for d in candidates:
        if (d / "library").is_dir():
            return d
    return candidates[0]

MUSIC_DIR = _find_music_dir()

# Biblioteca musical del canal: cualquier archivo de audio que se deje en
# music/library/ con el mood en el nombre (mystery_low_01.mp3) entra solo,
# sin tocar codigo. Desde el 2026-08-28 la biblioteca es la UNICA fuente de
# musica: 24 pistas de la Biblioteca de audio de YouTube con el filtro
# "no requiere atribucion" (licencia YouTube, sin Content ID). Las tres
# pistas de Scott Buckley que vivian aqui se retiraron: su Smart Content ID
# reclamaba los videos aunque el credito estuviera puesto, y la reclamacion
# habia que levantarla a mano vídeo por vídeo.
MUSIC_LIBRARY_DIR = MUSIC_DIR / "library"

# Las claves se conservan porque el payload de EP-07 puede traer
# `audio.music_track`; ahora apuntan a pistas de la biblioteca.
MUSIC = {
    "uprising": MUSIC_LIBRARY_DIR / "tension_medium_02.mp3",
    "the_long_dark": MUSIC_LIBRARY_DIR / "dark_low_01.mp3",
    "end": MUSIC_LIBRARY_DIR / "emotional_low_01.mp3",
}

# Red de seguridad si music/library/ llegara vacia (imagen mal construida).
LEGACY_LIBRARY = [
    {"id": "tension_medium_02", "path": str(MUSIC["uprising"]),
     "mood": "tension", "intensity": "medium", "title": "Standoff",
     "lufs": -12.19, "source": "YouTube Audio Library",
     "attribution": None},
    {"id": "dark_low_01", "path": str(MUSIC["the_long_dark"]),
     "mood": "dark", "intensity": "low", "title": "Surface of the Moon",
     "lufs": -16.29, "source": "YouTube Audio Library",
     "attribution": None},
    {"id": "emotional_low_01", "path": str(MUSIC["end"]),
     "mood": "emotional", "intensity": "low",
     "title": "Things I Could Have Said",
     "lufs": -11.84, "source": "YouTube Audio Library",
     "attribution": None},
]
_MUSIC_LUFS_CACHE = {}

def _find_riser():
    candidates = [
        Path("/app/sfx/riser_01_mixkit_1144.mp3"),
        Path(__file__).resolve().parent / "sfx" / "riser_01_mixkit_1144.mp3",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None

RISER_PATH = _find_riser()
RISER_VOLUME = 0.9

# ── Teaser (trailer-style cold open inside the MasterTube hook) ──
# Silent mode: the whole hook block (teaser + spoken hook) must stay inside
# HOOK_END, so the teaser length is derived from the hook word count.
# Voiced mode (frases carry narration_file_id): the cuts follow the real
# duration of each spoken frase and the hook block end becomes dynamic
# (teaser + spoken hook, capped at HOOK_MAX).
HOOK_END = 30.0
HOOK_MAX = 45.0            # cap for the dynamic hook block (voiced teaser)
TEASER_MIN = 4.0
TEASER_MAX = 8.0
TEASER_MAX_VOICED = 18.0   # voiced teaser budget (3 preguntas + cierre);
                           # drops weakest frase beyond
TEASER_FREEZE = 1.0        # final ambiguous still, no text
TEASER_SILENCE = 0.5       # dead-silence beat at the end of the freeze
TEASER_CUT_MIN = 0.30      # per text-fragment cut
TEASER_CUT_MAX = 0.55
TEASER_GAP = 0.30          # breath between voiced frases
SPOKEN_RATE_FALLBACK = 2.3  # words/s of "El Faraón" if rate can't be derived

MUSIC_BASE_VOL = 0.13      # channel standard for the whole video
MUSIC_HOOK_VOL = 0.30      # elevated presence during the 0-30s hook (+7.3 dB)
MUSIC_DUCK_FADE = 1.5      # crossfade back to base level at t=30s


def _find_logo():
    candidates = [
        Path("/app/branding/logo_ep.png"),
        Path(__file__).resolve().parent / "branding" / "logo_ep.png",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None

LOGO_PATH = _find_logo()
# Marca de agua del canal RETIRADA del render (orden de Marc 2026-08-06): el
# logo pasa a ser solo marca de agua nativa de YouTube, que se configura en el
# canal y no toca el archivo. El PNG se conserva (avatar del canal) y el
# overlay sigue en el código: LOGO_ENABLED=1 en el entorno lo devuelve.
LOGO_ENABLED = os.environ.get("LOGO_ENABLED", "0") == "1"
LOGO_WIDTH = 140        # px, height keeps aspect ratio
LOGO_OPACITY = 0.7
LOGO_MARGIN = 40        # px from top and right edges
LOGO_HOOK_END = 30.0    # gancho = first 30s exactly (MasterTube block 1)
LOGO_FADE = 0.5

FPS = 25
CROSSFADE_SEC = 1.0
DEFAULT_DURATION = 12
# Ken Burns suavizado (orden de Marc 2026-08-06: "que parezca más soft").
# ZOOM_TOTAL 0.03 → 0.02: el recorrido del zoom es 1,5 veces más lento.
# 2026-09-07 (ritmo calmado): 0.02 → 0.01, la mitad de velocidad de movimiento.
# zf = ZOOM_TOTAL / frames, así que el recorrido ya se reparte por la duración
# del clip: halvar ZOOM_TOTAL halva los px/s reales sea cual sea la escena.
# KB_SUPERSAMPLE arregla el TEMBLOR: zoompan trunca el origen del recorte a
# píxeles ENTEROS de la imagen de entrada, así que con entrada de 1920 px el
# encuadre se queda quieto y luego salta un píxel entero de salida. Ampliando
# la entrada ×2 antes del zoompan, ese salto vale medio píxel de salida.
# Medido sobre una imagen real de EP-04 (desplazamiento subpíxel por
# correlación de fase, std de la aceleración): 0,479 → 0,111 px/f² (−77%).
# Subir a ×3 o ×4 apenas mejora (0,095 / 0,085) y encarece el render.
ZOOM_TOTAL = 0.01
KB_SUPERSAMPLE = 2
XFADE_BATCH = 10


# ── Endpoints ──────────────────────────────────────────────


@app.route("/health", methods=["GET"])
def health():
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    music_ok = {k: v.exists() for k, v in MUSIC.items()}
    return jsonify({
        "status": "ok" if ffmpeg_ok and all(music_ok.values()) else "degraded",
        "ffmpeg": ffmpeg_ok,
        "music": music_ok,
        "build_version": BUILD_VERSION,
        "whisper_loaded": WHISPER_MODEL is not None,
        "fonts_dir": find_fonts_dir(),
        "fonts": _fonts_report(),
        "logo_found": LOGO_PATH is not None,
        "logo_enabled": LOGO_ENABLED,
        "riser_found": RISER_PATH is not None,
        "playwright": _playwright_available(),
        "music_library": audio_mix.library_summary(
            audio_mix.load_library(str(MUSIC_LIBRARY_DIR), LEGACY_LIBRARY)),
    })


def _playwright_available():
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


@app.route("/debug-env", methods=["GET"])
def debug_env():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    result = {
        "len": len(raw),
        "empty": len(raw) == 0,
        "preview_20": repr(raw[:20]) if raw else "(empty)",
    }
    if raw:
        try:
            parsed = json.loads(raw)
            result["json_ok"] = True
            result["type"] = parsed.get("type")
            result["project_id"] = parsed.get("project_id")
            result["client_email"] = parsed.get("client_email")
            result["private_key_starts"] = parsed.get("private_key", "")[:40]
        except Exception as e:
            result["json_ok"] = False
            result["json_error"] = str(e)
    return jsonify(result)


@app.route("/test-subs", methods=["GET"])
def test_subs():
    """Quick diagnostic: reports Whisper load status and libass availability."""
    import subprocess as sp
    whisper_ok = WHISPER_MODEL is not None
    r = sp.run(["ffmpeg", "-filters"], capture_output=True, text=True)
    libass_ok = "ass" in r.stdout
    fc = sp.run(["fc-list", ":family=Liberation Sans"], capture_output=True, text=True)
    font_ok = "Liberation" in fc.stdout
    return jsonify({
        "whisper_loaded": whisper_ok,
        "libass_available": libass_ok,
        "liberation_sans_found": font_ok,
        # Las cuatro caras de la capa de texto. Cualquiera en false significa
        # que libass caeria en una fuente de sustitucion sin avisar.
        "text_layer_fonts": _fonts_report(),
        "fonts_dir": find_fonts_dir(),
        "fc_list_output": fc.stdout[:500],
    })


@app.route("/render", methods=["POST"])
def render():
    """
    POST /render
    Body JSON:
    {
      "images": [{"file_id": "...", "duration": 12, "filename": "..."}],
      "narration_file_id": "DRIVE_ID",
      "music_track": "uprising",
      "dynasty_name": "enigmas",
      "drive_folder_id": "FOLDER_ID",
      # Capa de texto (opcional). Si no viene, el render se comporta
      # exactamente como antes: subtitulo Whisper + Liberation Sans.
      "text_layer": {"version": 1, "cues": [...], "b_style": {...}},
      "alignment": {"words": [{"word": "El", "start": 0.31, "end": 0.44}]},
      #   o el objeto crudo de ElevenLabs /with-timestamps, o una URL a un JSON.
      #   Si falta, se calcula con Whisper (word_timestamps=True).
      "script_text": "El 2 de julio de 1937...",
      #   guion real: hace que el subtitulo B muestre el guion y no lo que
      #   "oyo" Whisper. Sin el, se avisa por log y se usa la transcripcion.
      "teaser": {                          # optional trailer-style cold open
        "frases": [{"fragmentos": ["Los jeroglíficos—", "..."],
                     "image_file_id": "DRIVE_ID", "filename": "ITEM_4_IMG_3.jpg",
                     "narration_file_id": "DRIVE_ID"}],  # optional: voiced teaser
        "freeze_image_file_id": "DRIVE_ID",
        "gancho_words": 55, "words_total": 2560
      }
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "JSON body required"}), 400

    images = data.get("images", [])
    if not images:
        return jsonify({"success": False, "error": "No images provided"}), 400

    music_key = data.get("music_track", "uprising")
    music_path = str(MUSIC.get(music_key, MUSIC["uprising"]))
    dynasty = data.get("dynasty_name", data.get("output_filename", "video"))
    if dynasty.lower().endswith(".mp4"):
        dynasty = dynasty[:-4]
    folder_id = data.get("drive_folder_id")
    google_creds = data.get("google_credentials_json")

    work = tempfile.mkdtemp(prefix="render_")
    log.info("Render started: dynasty=%s, images=%d, music=%s", dynasty, len(images), music_key)

    try:
        drive = _get_drive_service(creds_override=google_creds)

        # 1 ── Download narration
        narr_path = os.path.join(work, "narration.mp3")
        if "narration_file_id" in data:
            log.info("Downloading narration from Drive...")
            _download_drive(drive, data["narration_file_id"], narr_path)
        elif "narration_url" in data:
            log.info("Downloading narration from URL...")
            _download_url(data["narration_url"], narr_path)
        else:
            return jsonify({"success": False, "error": "narration_file_id or narration_url required"}), 400

        narr_dur = _probe_duration(narr_path)

        # 1b ── Teaser plan (optional). Voiced frases (narration_file_id)
        # are downloaded and probed first so the cuts follow the speech.
        teaser_cfg = data.get("teaser") or {}
        for i, frase in enumerate(teaser_cfg.get("frases") or []):
            vid = frase.get("narration_file_id")
            if not vid:
                continue
            vp = os.path.join(work, f"teaser_voice_{i}.mp3")
            try:
                _download_drive(drive, vid, vp)
                frase["voice_path"] = vp
                frase["voice_dur"] = _probe_duration(vp)
            except Exception as e:
                log.warning("Teaser voice %d download failed (%s) — frase "
                            "falls back to silent handling", i, e)
        teaser = _teaser_timing(teaser_cfg, narr_dur) if teaser_cfg else None
        teaser_sec = teaser["total"] if teaser else 0.0
        teaser_cues = []
        hook_end = teaser["hook_end"] if teaser else HOOK_END
        if teaser:
            log.info("Teaser (%s): %d frases, total=%.2fs (gancho est. %.1fs → "
                     "hook block ends %.1fs)",
                     "voiced" if teaser["voiced"] else "silent",
                     len(teaser["frases"]), teaser_sec,
                     teaser["gancho_sec_est"], hook_end)

        n_clips = len(images)
        if narr_dur and n_clips > 0:
            crossfades_total = (n_clips - 1) * CROSSFADE_SEC
            per_clip = (narr_dur + crossfades_total) / n_clips
            # Suelo de duración por escena. 5,0s era el valor histórico; con el
            # ritmo calmado (2026-09-07) ninguna imagen puede estar menos de 8s
            # en pantalla. Nota: este cálculo IGNORA a propósito las duraciones
            # que manda EP-07 y las recalcula contra la narración real, para que
            # vídeo y audio no se desincronicen.
            per_clip = max(per_clip, 8.0)
            log.info("Narration: %.1fs (%.1fmin) → %d clips × %.2fs each",
                     narr_dur, narr_dur / 60, n_clips, per_clip)
            for img in images:
                img["duration"] = per_clip

        # 2 ── Download images
        log.info("Downloading %d images...", len(images))
        img_list = []
        for i, img in enumerate(images):
            ext = Path(img.get("filename", "img.jpg")).suffix or ".jpg"
            path = os.path.join(work, f"img_{i:04d}{ext}")
            if "file_id" in img:
                _download_drive(drive, img["file_id"], path)
            elif "url" in img:
                _download_url(img["url"], path)
            else:
                log.warning("Image %d has no file_id or url, skipping", i)
                continue
            img_list.append({
                "path": path,
                "duration": float(img.get("duration", DEFAULT_DURATION)),
            })

        if not img_list:
            return jsonify({"success": False, "error": "No images downloaded successfully"}), 400

        # 3 ── Ken Burns clips
        log.info("Creating %d Ken Burns clips...", len(img_list))
        clips = []
        for i, im in enumerate(img_list):
            clip_path = os.path.join(work, f"clip_{i:04d}.mp4")
            _ken_burns(im["path"], clip_path, im["duration"])
            clips.append({"path": clip_path, "duration": im["duration"]})
            if (i + 1) % 10 == 0:
                log.info("  %d/%d clips done", i + 1, len(img_list))
        log.info("All %d clips created", len(clips))

        # 4 ── Crossfade join
        log.info("Joining clips with %.1fs crossfade...", CROSSFADE_SEC)
        joined_path = os.path.join(work, "joined.mp4")
        _join_clips(clips, joined_path, work)

        # 4b ── Teaser cold open, prepended via lossless concat
        if teaser:
            log.info("Building teaser (%d frases, %.2fs)...", len(teaser["frases"]), teaser_sec)
            for i, frase in enumerate(teaser["frases"]):
                p = os.path.join(work, f"teaser_img_{i}.jpg")
                _download_drive(drive, frase["image_file_id"], p)
                frase["image_path"] = p
            freeze_path = os.path.join(work, "teaser_freeze.jpg")
            freeze_id = teaser_cfg.get("freeze_image_file_id")
            if freeze_id:
                _download_drive(drive, freeze_id, freeze_path)
            else:
                freeze_path = teaser["frases"][-1]["image_path"]
            # Con capa de texto, el teaser va sin letras quemadas por PIL: las
            # pone la capa ASS sincronizada con la voz. Los cues se calculan
            # AQUI porque de paso fijan la duracion de cada corte de imagen.
            if data.get("text_layer"):
                teaser_cues = _teaser_cues(teaser, work)
                log.info("Teaser: %d fragmentos sincronizados con su voz", len(teaser_cues))
            teaser_path = os.path.join(work, "teaser.mp4")
            _build_teaser_video(work, teaser["frases"], freeze_path, teaser_path,
                                draw_text=not data.get("text_layer"))
            with_teaser_path = os.path.join(work, "with_teaser.mp4")
            _concat_copy([teaser_path, joined_path], with_teaser_path, work)
            joined_path = with_teaser_path

        # 5 ── Mix audio
        log.info("Mixing audio...")
        teaser_voice_path = None
        if teaser and teaser["voiced"]:
            teaser_voice_path = os.path.join(work, "teaser_voice.wav")
            _build_teaser_voice(teaser["frases"], teaser_voice_path)
        mixed_path = os.path.join(work, "mixed.mp4")
        music_info = _mix_audio_v2(
            work, joined_path, narr_path, mixed_path,
            narr_dur=narr_dur, teaser_sec=teaser_sec, hook_end=hook_end,
            teaser_voice_path=teaser_voice_path,
            music_plan=data.get("music_plan"),
            music_avoid=data.get("music_avoid") or [],
            seed=data.get("airtable_id") or dynasty,
            legacy_track=music_path,
            bed_offset_db=data.get("bed_offset_db") or 0.0)

        duration_sec = _probe_duration(mixed_path)
        audio_qc = audio_mix.measure_loudness(mixed_path, timeout=900) or {}

        # QC ── duration drift check
        if narr_dur and duration_sec:
            drift = abs(duration_sec - (narr_dur + teaser_sec))
            log.info("QC duration: video=%.1fs narration+teaser=%.1fs drift=%.1fs",
                     duration_sec, narr_dur + teaser_sec, drift)
            if drift > 5:
                raise RuntimeError(
                    f"QC FAILED: video {duration_sec:.1f}s vs narration+teaser "
                    f"{narr_dur + teaser_sec:.1f}s (drift {drift:.1f}s > 5s)."
                )

        # 6 ── Capa de texto (o subtitulo clasico) + CTA overlay
        out_name = f"{dynasty}_{int(time.time())}.mp4"
        out_path = os.path.join(work, out_name)
        subtitle_coverage = None
        text_layer_report = None

        if data.get("text_layer"):
            # Camino nuevo: B documental + golpes C + rotulos E, con el estilo
            # del canal (Cormorant/Anton, sin borde negro). Un JSON invalido
            # tiene que ABORTAR: si cayera al subtitulo viejo, el video saldria
            # con el estilo que este trabajo vino a eliminar y nadie se
            # enteraria hasta verlo.
            ass_path, pre_filters, text_layer_report = _build_text_layer(
                narr_path, work, data, offset=teaser_sec, pre_cues=teaser_cues)
            _burn_subtitles_and_cta(mixed_path, ass_path, out_path, duration_sec,
                                    hook_end=hook_end, pre_filters=pre_filters,
                                    fonts_dir=text_layer_report["fonts_dir"])
            log.info("Capa de texto + CTA quemadas.")
        elif WHISPER_MODEL is not None:
            # Camino clasico, intacto: Liberation Sans con borde negro. Se
            # mantiene para que un render sin `text_layer` se comporte
            # exactamente igual que antes de este cambio.
            try:
                ass_path, subtitle_coverage = _transcribe_to_ass(narr_path, work, offset=teaser_sec)
                _burn_subtitles_and_cta(mixed_path, ass_path, out_path,
                                        duration_sec, hook_end=hook_end)
                log.info("Subtitles + CTA burned successfully.")
            except Exception as e:
                log.warning("Subtitle/CTA burn failed (non-fatal): %s — using plain video", e)
                shutil.copy(mixed_path, out_path)
        else:
            log.warning("Whisper not available — skipping subtitles.")
            shutil.copy(mixed_path, out_path)

        file_size = os.path.getsize(out_path)
        log.info("Render complete: %s (%.1f min, %.1f MB)",
                 out_name, (duration_sec or 0) / 60, file_size / 1024 / 1024)

        # 7 ── Save to renders dir and upload directly to Drive
        token = str(uuid.uuid4())
        persistent = RENDERS_DIR / f"{token}.mp4"
        shutil.move(out_path, str(persistent))
        log.info("Render saved as token=%s", token)

        drive_file_id = None
        drive_webViewLink = None
        if folder_id:
            try:
                from googleapiclient.http import MediaFileUpload
                log.info("Uploading to Drive folder %s...", folder_id)
                file_metadata = {"name": out_name, "parents": [folder_id]}
                media = MediaFileUpload(
                    str(persistent), mimetype="video/mp4",
                    resumable=True, chunksize=10 * 1024 * 1024
                )
                result = drive.files().create(
                    body=file_metadata, media_body=media, fields="id,webViewLink"
                ).execute()
                drive_file_id = result.get("id")
                drive_webViewLink = result.get("webViewLink")
                log.info("Uploaded to Drive: id=%s", drive_file_id)
                persistent.unlink()
                log.info("Local render deleted after Drive upload.")
            except Exception as e:
                log.error("Drive upload failed (keeping local for /download): %s", e)

        return jsonify({
            "success": True,
            "download_token": token,
            "drive_file_id": drive_file_id,
            "drive_webViewLink": drive_webViewLink,
            "filename": out_name,
            "duration_sec": duration_sec,
            "duration_min": round(duration_sec / 60, 1) if duration_sec else None,
            "narration_duration_sec": narr_dur,
            "narration_duration_min": round(narr_dur / 60, 1) if narr_dur else None,
            "size_bytes": file_size,
            "images_count": len(img_list),
            "music_track": music_key,
            "music_engine": music_info.get("engine"),
            "music_tracks": music_info.get("tracks"),
            "music_attribution": music_info.get("attribution"),
            "music_warnings": music_info.get("warnings"),
            "audio_qc": {
                "integrated_lufs": audio_qc.get("lufs"),
                "true_peak_dbtp": audio_qc.get("tp"),
                "lra": audio_qc.get("lra"),
                "voice_gain_db": music_info.get("voice_gain_db"),
                "bed_gain_db": music_info.get("bed_gain_db"),
                "bed_offset_db": music_info.get("bed_offset_db"),
            },
            "build_version": BUILD_VERSION,
            "subtitle_coverage": subtitle_coverage,
            "text_layer": text_layer_report,
            "teaser_duration_sec": teaser_sec or None,
            "teaser_hook_block_sec": (round(teaser_sec + teaser["gancho_sec_est"], 1)
                                      if teaser else None),
            "teaser_voiced": teaser["voiced"] if teaser else None,
            "hook_end_sec": hook_end if teaser else None,
        })

    except TextLayerError as e:
        log.error("text_layer invalido: %s", e)
        return jsonify({"success": False, "error": str(e),
                        "error_type": "text_layer"}), 400

    except Exception as e:
        log.exception("Render failed")
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.route("/remix", methods=["POST"])
def remix():
    """
    POST /remix — cambia SOLO la musica de un video ya renderizado.

    Pensado para reparar videos ya publicados cuya cama musical genera
    reclamaciones de Content ID: el stream de video se copia tal cual
    (`-c:v copy` dentro de audio_mix.mix_final), asi que imagenes, Ken Burns,
    subtitulos quemados y CTA quedan intactos y no se regenera NADA (ni
    imagenes, ni narracion, ni voz del teaser). Solo se reconstruye el audio:
    narracion original + voz del teaser (los mismos ficheros de Drive que uso
    el render) + riser + cama nueva de la biblioteca, con el motor v2.

    Body JSON:
    {
      "video_file_id": "DRIVE_ID",          # o "video_url"
      "narration_file_id": "DRIVE_ID",      # o "narration_url"
      "teaser": {"frases": [{"narration_file_id": "..."}, ...],
                 "gancho_words": 55, "words_total": 2560},   # opcional
      "teaser_sec": 17.8,        # opcional; por defecto se MIDE (video - narracion)
      "hook_end": 42.75,         # opcional
      "music_plan": [...], "music_avoid": [...],
      "airtable_id": "rec...",   # semilla: mismo id -> misma musica
      "drive_folder_id": "...", "google_credentials_json": "...",
      "output_filename": "REMIX_rec....mp4"
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "JSON body required"}), 400

    folder_id = data.get("drive_folder_id")
    google_creds = data.get("google_credentials_json")
    seed = data.get("airtable_id") or data.get("output_filename") or "remix"

    work = tempfile.mkdtemp(prefix="remix_")
    log.info("Remix started: seed=%s", seed)
    try:
        drive = _get_drive_service(creds_override=google_creds)

        # 1 ── Video ya renderizado (solo se usa su stream de video)
        video_path = os.path.join(work, "source.mp4")
        if data.get("video_file_id"):
            log.info("Downloading rendered video from Drive...")
            _download_drive(drive, data["video_file_id"], video_path)
        elif data.get("video_url"):
            _download_url(data["video_url"], video_path)
        else:
            return jsonify({"success": False,
                            "error": "video_file_id or video_url required"}), 400
        video_dur = _probe_duration(video_path)

        # 2 ── Narracion original
        narr_path = os.path.join(work, "narration.mp3")
        if data.get("narration_file_id"):
            _download_drive(drive, data["narration_file_id"], narr_path)
        elif data.get("narration_url"):
            _download_url(data["narration_url"], narr_path)
        else:
            return jsonify({"success": False,
                            "error": "narration_file_id or narration_url required"}), 400
        narr_dur = _probe_duration(narr_path)

        # 3 ── Voz del teaser: los MISMOS mp3 que uso el render original.
        teaser_cfg = data.get("teaser") or {}
        frases = [f for f in (teaser_cfg.get("frases") or []) if f.get("narration_file_id")]
        for i, frase in enumerate(frases):
            vp = os.path.join(work, f"teaser_voice_{i}.mp3")
            try:
                _download_drive(drive, frase["narration_file_id"], vp)
                frase["voice_path"] = vp
                frase["voice_dur"] = _probe_duration(vp)
            except Exception as e:
                log.warning("Teaser voice %d download failed (%s)", i, e)
                frase["voice_path"] = None

        voiced = [f for f in frases if f.get("voice_path")]
        teaser_voice_path = None
        if voiced:
            teaser_voice_path = os.path.join(work, "teaser_voice.wav")
            _build_teaser_voice(voiced, teaser_voice_path)

        # 4 ── teaser_sec: se MIDE del material real (video - narracion). Es lo
        # que mantiene la narracion en sincronia con los subtitulos quemados;
        # recalcularlo con _teaser_timing podria dar otro valor si el render
        # original recorto alguna frase.
        measured = round((video_dur or 0) - (narr_dur or 0), 3)
        teaser_sec = data.get("teaser_sec")
        if teaser_sec is None:
            if voiced:
                # Hay teaser: la diferencia ES el teaser que va delante.
                teaser_sec = measured if measured > 0.2 else 0.0
            else:
                # Sin voces de teaser (videos anteriores a esa funcion) la
                # diferencia es cola de video / redondeo de crossfades, no un
                # bloque delante: retrasar la narracion la desincronizaria de
                # los subtitulos ya quemados. Se rellena por el final (abajo).
                teaser_sec = 0.0
                if measured > 0.2:
                    log.info("Sin voces de teaser y %.2fs de diferencia: se "
                             "trata como cola, la narracion NO se retrasa.",
                             measured)
        teaser_sec = float(teaser_sec)
        if voiced and abs(teaser_sec - measured) > 0.35:
            log.warning("teaser_sec=%.2fs no cuadra con lo medido (%.2fs)",
                        teaser_sec, measured)

        # El audio tiene que durar exactamente lo que el video: mix_final lleva
        # -shortest, asi que si la narracion se queda corta se perderia la cola
        # del video (CTA final). Se rellena con silencio al final.
        falta = (video_dur or 0) - (teaser_sec + (narr_dur or 0))
        if falta > 0.05:
            padded = os.path.join(work, "narration_padded.wav")
            _ffmpeg(["-i", narr_path, "-af", "apad",
                     "-t", f"{(video_dur - teaser_sec):.3f}",
                     "-c:a", "pcm_s16le", padded], timeout=600)
            narr_path = padded
            narr_dur = _probe_duration(narr_path) or (video_dur - teaser_sec)
            log.info("Narracion rellenada con %.2fs de silencio final", falta)

        hook_end = data.get("hook_end")
        if hook_end is None:
            if teaser_cfg:
                try:
                    hook_end = _teaser_timing(teaser_cfg, narr_dur)["hook_end"]
                except Exception:
                    hook_end = None
            if hook_end is None:
                hook_end = min(HOOK_MAX, max(HOOK_END, teaser_sec + 25.0))
        hook_end = float(hook_end)

        log.info("Remix: video=%.1fs narracion=%.1fs teaser=%.2fs hook_end=%.1fs "
                 "voces_teaser=%d", video_dur or 0, narr_dur or 0, teaser_sec,
                 hook_end, len(voiced))

        # 5 ── Mezcla nueva (mismo motor que el render; el video se copia)
        out_path = os.path.join(work, "remix.mp4")
        music_info = _mix_audio_v2(
            work, video_path, narr_path, out_path,
            narr_dur=narr_dur, teaser_sec=teaser_sec, hook_end=hook_end,
            teaser_voice_path=teaser_voice_path,
            music_plan=data.get("music_plan"),
            music_avoid=data.get("music_avoid") or [],
            seed=seed,
            legacy_track=str(MUSIC["uprising"]),
            bed_offset_db=data.get("bed_offset_db") or 0.0)
        if music_info.get("engine") != "v2":
            log.warning("Remix sin motor v2 (%s): %s",
                        music_info.get("engine"), music_info.get("warnings"))

        duration_sec = _probe_duration(out_path)
        audio_qc = audio_mix.measure_loudness(out_path, timeout=900) or {}
        drift = abs((duration_sec or 0) - (video_dur or 0))
        if drift > 1.0:
            log.warning("Remix duracion %.1fs vs original %.1fs (drift %.1fs)",
                        duration_sec or 0, video_dur or 0, drift)

        out_name = data.get("output_filename") or f"REMIX_{seed}.mp4"
        if not out_name.lower().endswith(".mp4"):
            out_name += ".mp4"

        token = str(uuid.uuid4())
        persistent = RENDERS_DIR / f"{token}.mp4"
        shutil.move(out_path, str(persistent))
        file_size = os.path.getsize(str(persistent))

        drive_file_id = None
        drive_webViewLink = None
        if folder_id:
            try:
                from googleapiclient.http import MediaFileUpload
                media = MediaFileUpload(str(persistent), mimetype="video/mp4",
                                        resumable=True, chunksize=10 * 1024 * 1024)
                result = drive.files().create(
                    body={"name": out_name, "parents": [folder_id]},
                    media_body=media, fields="id,webViewLink").execute()
                drive_file_id = result.get("id")
                drive_webViewLink = result.get("webViewLink")
                persistent.unlink()
                log.info("Remix subido a Drive: id=%s", drive_file_id)
            except Exception as e:
                log.error("Drive upload failed (queda en /download): %s", e)

        return jsonify({
            "success": True,
            "download_token": token,
            "drive_file_id": drive_file_id,
            "drive_webViewLink": drive_webViewLink,
            "filename": out_name,
            "duration_sec": duration_sec,
            "source_duration_sec": video_dur,
            "duration_drift_sec": round(drift, 2),
            "narration_duration_sec": narr_dur,
            "teaser_sec": teaser_sec,
            "hook_end_sec": hook_end,
            "teaser_voices": len(voiced),
            "size_bytes": file_size,
            "music_engine": music_info.get("engine"),
            "music_tracks": music_info.get("tracks"),
            "music_attribution": music_info.get("attribution"),
            "music_warnings": music_info.get("warnings"),
            "audio_qc": {
                "integrated_lufs": audio_qc.get("lufs"),
                "true_peak_dbtp": audio_qc.get("tp"),
                "lra": audio_qc.get("lra"),
                "voice_gain_db": music_info.get("voice_gain_db"),
                "bed_gain_db": music_info.get("bed_gain_db"),
                "bed_offset_db": music_info.get("bed_offset_db"),
            },
            "build_version": BUILD_VERSION,
        })

    except Exception as e:
        log.exception("Remix failed")
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.route("/download/<token>", methods=["GET"])
def download_render(token):
    if not re.match(r'^[0-9a-f\-]+$', token):
        return jsonify({"error": "invalid token"}), 400
    path = RENDERS_DIR / f"{token}.mp4"
    if not path.exists():
        return jsonify({"error": "render not found"}), 404
    log.info("Serving render token=%s", token)
    return send_file(str(path), mimetype="video/mp4", as_attachment=True,
                     download_name=f"{token}.mp4")


@app.route("/text-layer/preview", methods=["POST"])
def text_layer_preview():
    """
    POST /text-layer/preview — un PNG por cue, en su instante medio.

    Sirve para revisar la capa de texto de un video sin renderizar los 20
    minutos. Body:
    {
      "text_layer": {...},                  # obligatorio
      "alignment": {...} | "https://...",   # obligatorio (aqui no hay audio)
      "background": "#1A1408" | "https://..." | {"file_id": "..."},
      "cues": ["c01", "q02"]                # opcional: solo estos
    }
    Responde {"success": true, "download_token": "...", "frames": [...]}.
    El ZIP se recoge en /text-layer/preview-download/<token>.
    """
    data = request.get_json() or {}
    if not data.get("text_layer"):
        return jsonify({"success": False, "error": "text_layer requerido"}), 400
    if not data.get("alignment"):
        return jsonify({"success": False, "error":
                        "alignment requerido: sin audio no hay de donde sacar los tiempos"}), 400

    work = tempfile.mkdtemp(prefix="preview_")
    try:
        layer = parse_text_layer(data["text_layer"])
        words, _ = _words_for_text_layer(None, work, data)
        ass_text, report = build_ass(layer, words, offset=0.0)
        ass_path = os.path.join(work, "layer.ass")
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_text)

        # Fondo: un color plano, una imagen de Drive o una URL.
        bg = data.get("background") or "#1A1408"
        bg_input = None
        if isinstance(bg, dict) and bg.get("file_id"):
            bg_input = os.path.join(work, "bg.jpg")
            _download_drive(_get_drive_service(data.get("google_credentials_json")),
                            bg["file_id"], bg_input)
        elif isinstance(bg, str) and bg.startswith("http"):
            bg_input = os.path.join(work, "bg.jpg")
            _download_url(bg, bg_input)

        wanted = set(data.get("cues") or [])
        filters = build_video_filters(ass_path, report["black_backgrounds"],
                                      fonts_dir=find_fonts_dir())
        chain = ",".join(filters)
        end = report["last_event_end_sec"] + 2

        frames, out_dir = [], os.path.join(work, "frames")
        os.makedirs(out_dir, exist_ok=True)
        for cue in sorted(layer.overlays, key=lambda c: c.start):
            if wanted and cue.id not in wanted:
                continue
            t = (cue.start + cue.end) / 2
            png = os.path.join(out_dir, f"{cue.id}.png")
            source = (["-loop", "1", "-t", str(end), "-i", bg_input] if bg_input
                      else ["-f", "lavfi", "-i", f"color=c={bg}:s=1920x1080:d={end}:r=25"])
            _ffmpeg([*source, "-vf", chain, "-ss", f"{t:.3f}",
                     "-frames:v", "1", png], timeout=180)
            frames.append({"cue": cue.id, "type": cue.type, "t": round(t, 2),
                           "start": round(cue.start, 2), "end": round(cue.end, 2)})

        token = str(uuid.uuid4())
        zip_base = str(THUMBS_DIR / token)
        shutil.make_archive(zip_base, "zip", out_dir)
        return jsonify({"success": True, "download_token": token,
                        "frames": frames, "text_layer": report,
                        "build_version": BUILD_VERSION})

    except TextLayerError as e:
        log.error("preview: text_layer invalido: %s", e)
        return jsonify({"success": False, "error": str(e), "error_type": "text_layer"}), 400
    except Exception as e:
        log.exception("preview failed")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.route("/text-layer/preview-download/<token>", methods=["GET"])
def text_layer_preview_download(token):
    path = THUMBS_DIR / f"{token}.zip"
    if not path.exists():
        return jsonify({"success": False, "error": "token no encontrado"}), 404
    return send_file(str(path), mimetype="application/zip", as_attachment=True,
                     download_name=f"text_layer_preview_{token[:8]}.zip")


@app.route("/thumbnail", methods=["POST"])
def thumbnail():
    """
    Generate 3 YouTube thumbnail variants (1280x720 JPEG) from a base image
    + a brief_miniatura brief. Same text layout across variants; each one
    places the attention-marker ring at a different candidate position
    (A=left, B=center, C=right) so the best composition can be picked by eye.

    multipart/form-data:
      'image'            - base image file
      'brief_text'       - raw brief_miniatura content (used to derive
                            main_text if main_text_override isn't given)
      'main_text_override'      - optional, wins over brief_text parsing
      'secondary_text_override' - optional, wins over brief_text parsing
                                   (use this for the LLM-generated specific
                                   hook instead of a generic label)
      'record_id'        - optional

    Alternative JSON mode (EP-08 template thumbnails):
      POST application/json {"html": "<full template html>"} →
      Playwright/Chromium renders it at 1280x720, waits for
      body[data-render-ready="1"], captures #canvas, returns the PNG.
    """
    if request.is_json:
        return _thumbnail_from_html(request.get_json(silent=True) or {})
    if "image" not in request.files:
        return jsonify({"success": False, "error": "image file required (multipart 'image' field)"}), 400
    brief_text = request.form.get("brief_text", "")
    record_id = request.form.get("record_id", "thumb")
    main_override = request.form.get("main_text_override", "").strip()
    secondary_override = request.form.get("secondary_text_override", "").strip()

    main_text, secondary_text = "", ""
    if brief_text.strip():
        main_text, secondary_text = _parse_brief_miniatura(brief_text)
    if main_override:
        main_text = main_override
    if secondary_override:
        secondary_text = secondary_override
    if not main_text:
        return jsonify({"success": False, "error": "Could not resolve main_text (need brief_text or main_text_override)"}), 400

    work = tempfile.mkdtemp(prefix="thumb_")
    try:
        img_path = os.path.join(work, "base.jpg")
        request.files["image"].save(img_path)

        tokens = {}
        for variant in ("A", "B", "C"):
            token = str(uuid.uuid4())
            _generate_thumbnail(img_path, main_text, secondary_text, variant, str(THUMBS_DIR / f"{token}.jpg"))
            tokens[variant] = token

        log.info("Thumbnails generated for record=%s: main=%r secondary=%r",
                 record_id, main_text, secondary_text)
        return jsonify({
            "success": True,
            "build_version": BUILD_VERSION,
            "record_id": record_id,
            "main_text": main_text,
            "secondary_text": secondary_text,
            "variant_a_token": tokens["A"],
            "variant_b_token": tokens["B"],
            "variant_c_token": tokens["C"],
        })
    except Exception as e:
        log.exception("Thumbnail generation failed")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.route("/thumbnail-download/<token>", methods=["GET"])
def thumbnail_download(token):
    if not re.match(r'^[0-9a-f\-]+$', token):
        return jsonify({"error": "invalid token"}), 400
    path = THUMBS_DIR / f"{token}.jpg"
    if not path.exists():
        return jsonify({"error": "thumbnail not found"}), 404
    return send_file(str(path), mimetype="image/jpeg", as_attachment=True,
                     download_name=f"{token}.jpg")


# ── Thumbnails ─────────────────────────────────────────────


def _thumbnail_from_html(payload):
    """EP-08: render the thumbnail template HTML to a 1280x720 PNG."""
    html = payload.get("html", "")
    if not html or not isinstance(html, str):
        return jsonify({"success": False, "error": "html string required"}), 400
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return jsonify({"success": False, "build_version": BUILD_VERSION,
                        "error": "playwright not installed in this build"}), 501
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage", "--force-color-profile=srgb"])
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 720},
                                        device_scale_factor=1)
                page.set_content(html, wait_until="load")
                page.wait_for_selector('body[data-render-ready="1"]', timeout=20000)
                # data-render-ready fires on fonts.ready — the bg image (a
                # remote URL) may still be loading, so wait for it too.
                try:
                    page.wait_for_function(
                        "() => { const i = document.getElementById('bg');"
                        " return !i || i.complete; }", timeout=20000)
                except Exception:
                    log.warning("HTML thumbnail: bg image still loading after 20s, capturing anyway")
                bg_ok = page.evaluate(
                    "() => { const i = document.getElementById('bg');"
                    " return !!(i && i.complete && i.naturalWidth > 0); }")
                canvas = page.query_selector("#canvas")
                if canvas is None:
                    return jsonify({"success": False, "error": "#canvas not found in html"}), 400
                png = canvas.screenshot(type="png")
            finally:
                browser.close()
        log.info("HTML thumbnail rendered: %d bytes, bg_loaded=%s", len(png), bg_ok)
        resp = send_file(io.BytesIO(png), mimetype="image/png", as_attachment=True,
                         download_name="thumbnail.png")
        resp.headers["X-Build-Version"] = BUILD_VERSION
        resp.headers["X-Bg-Loaded"] = "1" if bg_ok else "0"
        return resp
    except Exception as e:
        log.exception("HTML thumbnail render failed")
        return jsonify({"success": False, "error": str(e)}), 500


def _parse_brief_miniatura(brief_text):
    """Extract TEXTO PRINCIPAL / TEXTO SECUNDARIO from the structured brief_miniatura field."""
    main_text, secondary_text = "", ""
    for line in brief_text.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().upper()
        val = val.strip()
        if "TEXTO PRINCIPAL" in key:
            main_text = val
        elif "TEXTO SECUNDARIO" in key:
            secondary_text = val
    if not main_text:
        for line in brief_text.splitlines():
            if line.strip():
                main_text = line.strip()
                break
    return main_text, secondary_text


def _cover_resize(img, target_w, target_h):
    """Resize+crop an image to exactly fill target_w x target_h (cover, not stretch)."""
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def _fit_font(draw, text, font_path, max_width, start_size, min_size):
    size = start_size
    while size > min_size:
        font = ImageFont.truetype(font_path, size)
        stroke_w = max(2, size // 14)
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_w)
        if (bbox[2] - bbox[0]) <= max_width:
            return font, size
        size -= 4
    return ImageFont.truetype(font_path, min_size), min_size



def _generate_thumbnail(image_path, main_text, secondary_text, variant, out_path):
    """
    1280x720 YouTube thumbnail: uppercase Anton text, top-center, vivid
    yellow w/ dark outline. No attention marker — the background image must
    carry the drama, and prompts must leave the top third free so text never
    covers subjects. `variant` is kept for endpoint compatibility; all
    variants currently render identically.
    """
    W, H = 1280, 720
    GOLD = (255, 222, 0)
    WHITE = (255, 255, 255)
    OUTLINE = (12, 10, 8)

    img = Image.open(image_path).convert("RGB")
    img = _cover_resize(img, W, H)
    draw = ImageDraw.Draw(img)

    main_text = main_text.upper()
    secondary_text = (secondary_text or "").upper()

    margin = 64
    max_w = W - 2 * margin

    main_font, main_size = _fit_font(draw, main_text, _FONT_ANTON, max_w, 130, 56)
    stroke_main = max(4, main_size // 12)

    sec_font, sec_size, stroke_sec = None, 0, 0
    if secondary_text:
        sec_font, sec_size = _fit_font(draw, secondary_text, _FONT_ANTON, max_w, 62, 30)
        stroke_sec = max(3, sec_size // 12)

    cx = W // 2
    main_y = int(H * 0.10)
    draw.text((cx, main_y), main_text, font=main_font, fill=GOLD,
              stroke_width=stroke_main, stroke_fill=OUTLINE, anchor="ma", align="center")
    if secondary_text:
        sec_y = main_y + main_size + 24
        draw.text((cx, sec_y), secondary_text, font=sec_font, fill=WHITE,
                  stroke_width=stroke_sec, stroke_fill=OUTLINE, anchor="ma", align="center")

    img.save(out_path, "JPEG", quality=92)


# ── Google Drive ───────────────────────────────────────────


def _get_drive_service(creds_override=None):
    raw = creds_override or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if isinstance(raw, dict):
        info = raw
    else:
        info = _parse_creds(raw)
    if not info:
        raise ValueError(
            "Google credentials not available. Set GOOGLE_SERVICE_ACCOUNT_JSON env var "
            "or pass google_credentials_json in the request body."
        )
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _download_drive(service, file_id, dest):
    req = service.files().get_media(fileId=file_id)
    with open(dest, "wb") as f:
        dl = MediaIoBaseDownload(f, req)
        done = False
        while not done:
            _, done = dl.next_chunk()


def _download_url(url, dest):
    r = http_requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)


# ── Subtitles (word-by-word sliding window) ────────────────


def _tc(t):
    """Seconds → ASS timecode  H:MM:SS.cc"""
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    cs = int(round((t % 1) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _wrap_phrase(text, max_words=12):
    """Insert ASS hard line-break (\\N) if phrase exceeds max_words."""
    words = text.split()
    if len(words) <= max_words:
        return text
    # Prefer splitting after punctuation near the midpoint
    split_at = max_words
    for i in range(min(max_words, len(words) - 1), max(0, max_words - 4), -1):
        if words[i - 1].endswith((',', ';', ':', '—', '.')):
            split_at = i
            break
    return " ".join(words[:split_at]) + "\\N" + " ".join(words[split_at:])


def _extract_audio_chunk(src_path, dest_path, start_sec, dur_sec):
    """Cut [start_sec, start_sec+dur_sec) out of src_path into a 16kHz mono wav."""
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
         "-ss", str(start_sec), "-t", str(dur_sec), "-i", str(src_path),
         "-ar", "16000", "-ac", "1", str(dest_path)],
        capture_output=True, text=True, timeout=120, check=True,
    )


def _whisper_transcribe_segments(audio_path):
    segments, info = WHISPER_MODEL.transcribe(
        audio_path,
        language="es",
        beam_size=1,
        best_of=1,
        vad_filter=True,
        # Long single-take narration (20+ min) makes Whisper drift/hallucinate
        # and silently stop emitting segments partway through when it keeps
        # conditioning on its own (increasingly wrong) previous output.
        condition_on_previous_text=False,
    )
    return list(segments), info


# Chunk narrations longer than this before transcribing — faster-whisper
# (base model, greedy decode) has a known failure mode on single-pass
# 20+ min audio where it silently stops emitting segments partway through.
# Splitting into ~5 min windows keeps each pass short enough to avoid it.
WHISPER_CHUNK_SEC = 300


def _transcribe_to_ass(narr_path, work, offset=0.0):
    """
    Whisper phrase-level ASS subtitles.
    Each segment fades in/out with \\fad(200,200).
    Font: Liberation Sans Bold 72px, 3px black outline, bottom-centre (15%).
    Max 12 words per line — wraps with \\N at natural phrase break.
    `offset` shifts every cue (narration starts after the teaser).
    Returns (ass_path, coverage_dict).
    """
    real_dur = _probe_duration(narr_path)
    all_segments = []  # (start, end, text) with offsets relative to full narration

    if real_dur and real_dur > WHISPER_CHUNK_SEC * 1.2:
        n_chunks = math.ceil(real_dur / WHISPER_CHUNK_SEC)
        log.info("Transcribing narration in %d chunks of ~%ds (audio=%.1fs)...",
                  n_chunks, WHISPER_CHUNK_SEC, real_dur)
        for i in range(n_chunks):
            chunk_start = i * WHISPER_CHUNK_SEC
            chunk_dur = min(WHISPER_CHUNK_SEC, real_dur - chunk_start)
            chunk_path = os.path.join(work, f"narr_chunk_{i:02d}.wav")
            _extract_audio_chunk(narr_path, chunk_path, chunk_start, chunk_dur)
            segments, info = _whisper_transcribe_segments(chunk_path)
            chunk_segment_count = 0
            for seg in segments:
                text = seg.text.strip()
                if text:
                    all_segments.append((seg.start + chunk_start, seg.end + chunk_start, text))
                    chunk_segment_count += 1
            log.info("Chunk %d/%d [%.0fs-%.0fs]: %d segments, lang=%s (%.0f%%)",
                      i + 1, n_chunks, chunk_start, chunk_start + chunk_dur,
                      chunk_segment_count, info.language, info.language_probability * 100)
    else:
        log.info("Transcribing narration (Whisper base, single pass, CPU)...")
        segments, info = _whisper_transcribe_segments(narr_path)
        for seg in segments:
            text = seg.text.strip()
            if text:
                all_segments.append((seg.start, seg.end, text))
        log.info("Whisper: lang=%s (%.0f%%)", info.language, info.language_probability * 100)

    # MarginV=162 ≈ 15% from bottom (1080 × 0.15)
    # Outline=3 (3px black), Shadow=2, Alignment=2 (bottom-centre)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n"
        "WrapStyle: 1\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Liberation Sans,72,&H00FFFFFF,&H000000FF,"
        "&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,2,2,80,80,162,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    dialogues = []
    last_end = 0.0
    for start, end, text in all_segments:
        text = _wrap_phrase(text, max_words=12)
        dialogues.append(
            f"Dialogue: 0,{_tc(start + offset)},{_tc(end + offset)},Default,,0,0,0,,"
            f"{{\\fad(200,200)}}{text}"
        )
        last_end = end

    if not dialogues:
        raise RuntimeError("Whisper returned 0 segments — audio may be silent")

    ass_path = os.path.join(work, "subtitles.ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(dialogues))

    log.info("ASS written: %d phrases → %s", len(dialogues), ass_path)
    coverage = {"phrases": len(dialogues), "last_subtitle_end_sec": last_end,
                "audio_duration_sec": real_dur, "coverage_gap_sec": None}
    if real_dur:
        coverage_gap = real_dur - last_end
        coverage["coverage_gap_sec"] = round(coverage_gap, 1)
        log.info("Subtitle coverage: last subtitle ends at %.1fs, audio is %.1fs (gap %.1fs)",
                  last_end, real_dur, coverage_gap)
        if coverage_gap > 30:
            log.warning("Subtitles stop %.1fs before audio ends — Whisper may have "
                        "drifted/stopped early on this render.", coverage_gap)
    return ass_path, coverage


# ── Capa de texto (B documental / C golpe / E rotulo) ──────


def _words_for_text_layer(narr_path, work, data):
    """Alineacion por palabras: la del payload si viene, si no Whisper.

    Via de produccion: EP-03B pedira a ElevenLabs
    `POST /v1/text-to-speech/{voice_id}/with-timestamps` y mandara el objeto
    `alignment` tal cual. Via de respaldo y de test: faster-whisper con
    `word_timestamps=True`, coste 0.
    """
    alignment = data.get("alignment")
    if isinstance(alignment, str):
        path = os.path.join(work, "alignment.json")
        _download_url(alignment, path)
        with open(path, encoding="utf-8") as f:
            alignment = json.load(f)
    if alignment:
        words = words_from_payload(alignment)
        log.info("Alineacion recibida en el payload: %d palabras", len(words))
    else:
        if WHISPER_MODEL is None:
            raise TextLayerError(
                "No hay alignment en el payload y Whisper no esta cargado: "
                "la capa de texto no se puede colocar en el tiempo."
            )
        log.info("Sin alignment en el payload — transcribiendo con Whisper "
                 "(word_timestamps=True)...")
        dur = _probe_duration(narr_path)

        def cortar(inicio, duracion, _n=[0]):
            _n[0] += 1
            destino = os.path.join(work, f"align_chunk_{_n[0]:02d}.wav")
            _extract_audio_chunk(narr_path, destino, inicio, duracion)
            return destino

        words, info = words_from_whisper(WHISPER_MODEL, narr_path, duration=dur,
                                         cut=cortar, log=log.info)
        log.info("Whisper: %d palabras en %.0fs de audio, lang=%s (%.0f%%)",
                 len(words), dur or 0, info.language, info.language_probability * 100)
        if dur and len(words) < dur * 1.2:
            log.warning("Solo %d palabras para %.0fs de narracion (~%.1f palabras/s). "
                        "Whisper puede haberse callado antes de tiempo.",
                        len(words), dur, len(words) / dur)

    script = data.get("script_text")
    if script:
        before = len(words)
        words = align_script_to_words(script, words)
        log.info("Guion aplicado sobre la transcripcion: %d palabras del ASR → "
                 "%d tokens del guion", before, len(words))
    else:
        log.warning("Sin script_text: el subtitulo B mostrara la transcripcion "
                    "de Whisper, con sus erratas, en vez del guion.")
    return words, bool(script)


def _build_text_layer(narr_path, work, data, offset, pre_cues=()):
    """→ (ass_path, filtros previos, informe). Lanza TextLayerError si el JSON falla."""
    layer = parse_text_layer(data["text_layer"])
    words, script_used = _words_for_text_layer(narr_path, work, data)
    ass_text, report = build_ass(layer, words, offset=offset, pre_cues=pre_cues)

    ass_path = os.path.join(work, "layer.ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_text)

    from text_layer.burn import black_background_filters
    pre = black_background_filters(report["black_backgrounds"])
    report["script_applied"] = script_used
    report["fonts_dir"] = find_fonts_dir()
    report["fonts_resolved"] = _fonts_report()
    log.info("Capa de texto: %d lineas de dialogo, %d eventos B (%d recortados "
             "bajo cues), cues=%s", report["dialogue_lines"], report["b_events"],
             report["b_events_trimmed"], report["cues"])
    return ass_path, pre, report


def _fonts_report():
    """Que caras resuelve fontconfig. Una fuente en fallback es un fallo."""
    import subprocess as sp
    familias = ["Anton", "Cormorant Garamond Medium",
                "Cormorant Garamond Medium Italic", "Cormorant Garamond SemiBold"]
    out = {}
    for fam in familias:
        try:
            r = sp.run(["fc-list", f":family={fam}", "file"],
                       capture_output=True, text=True, timeout=10)
            out[fam] = bool(r.stdout.strip())
        except Exception:
            out[fam] = None
    return out


def _teaser_cues(teaser, work):
    """Cues de la capa de texto para el teaser, sincronizados con su voz.

    Antes el teaser dibujaba su texto con PIL y repartia los fragmentos de una
    frase en trozos de duracion IDENTICA (`cut = (voice_dur + gap) / n`). Como
    "¿Por qué desaparece el comercio—" y "que las ciudades?" no se tardan lo
    mismo en decir, el texto se despegaba de la voz. Ademas iba con borde negro
    grueso, que es justo el acabado que este trabajo vino a quitar.

    Ahora cada fragmento sale cuando de verdad se pronuncia: se transcribe la
    voz de la frase con tiempos por palabra, se alinea contra el texto de los
    fragmentos (la misma maquinaria que corrige el subtitulo B con el guion) y
    de ahi salen los tiempos reales de cada trozo.

    Los tiempos son ABSOLUTOS del video final: el teaser va delante de todo.
    """
    from text_layer.schema import Cue

    frases = teaser["frases"]
    cues, base = [], 0.0
    for i, frase in enumerate(frases):
        fragmentos = [f for f in frase["fragmentos"] if f and f.strip()]
        if not fragmentos:
            continue
        bloque = (frase.get("voice_dur") or 0) + TEASER_GAP if teaser["voiced"] \
            else frase["cut"] * len(fragmentos)
        # El cierre (la ultima frase) va entero en ambar, como el remate del
        # gancho en el mock.
        es_cierre = (i == len(frases) - 1)

        tramos = None
        if teaser["voiced"] and frase.get("voice_path") and WHISPER_MODEL is not None:
            try:
                tramos = _tramos_por_voz(frase["voice_path"], fragmentos, work, i)
            except Exception as e:
                log.warning("Teaser frase %d: no se pudo sincronizar con la voz "
                            "(%s). Se reparte por igual.", i + 1, e)

        if not tramos:
            # Sin voz o sin Whisper: reparto uniforme, como antes.
            paso = bloque / len(fragmentos)
            tramos = [(k * paso, (k + 1) * paso) for k in range(len(fragmentos))]

        # Duraciones del CORTE DE IMAGEN, para que la imagen cambie con el
        # fragmento y no en una rejilla aparte. CLAUDE_EP.md ya decia que los
        # cortes siguen el ritmo real de la locucion; el codigo repartia por
        # igual. Suman exactamente el bloque, que es lo que mantiene el video
        # cuadrado con la pista de voz.
        arranques = [t[0] for t in tramos] + [bloque]
        frase["frag_durs"] = [round(max(0.2, arranques[k + 1] - arranques[k]), 3)
                              for k in range(len(tramos))]

        for k, (frag, (ini, fin)) in enumerate(zip(fragmentos, tramos)):
            texto = frag.strip().rstrip("—-").strip().upper()
            if not texto:
                continue
            cue = Cue(id=f"t{i + 1:02d}_{k + 1}", type="C", placement="mid",
                      style_hint="teaser")
            cue.lines = [texto]
            cue.accent = texto if es_cierre else None
            cue.start = round(base + ini, 3)
            cue.end = round(base + fin, 3)
            cues.append(cue)
        base += bloque

    # Que no se pisen ni se salgan del teaser.
    for a, b in zip(cues, cues[1:]):
        if a.end > b.start:
            a.end = b.start
    for c in cues:
        c.end = min(c.end, teaser["total"])
        if c.end - c.start < 0.25:
            c.end = c.start + 0.25
    return [c for c in cues if c.start < teaser["total"]]


def _tramos_por_voz(voice_path, fragmentos, work, indice):
    """(inicio, fin) de cada fragmento dentro de su frase, segun la voz real."""
    wav = os.path.join(work, f"teaser_voz_{indice}.wav")
    _extract_audio_chunk(voice_path, wav, 0, 60)
    palabras, _ = words_from_whisper(WHISPER_MODEL, wav)
    if not palabras:
        raise RuntimeError("Whisper no devolvio palabras")

    texto = " ".join(f.strip().rstrip("—-").strip() for f in fragmentos)
    alineadas = align_script_to_words(texto, palabras)

    tramos, i = [], 0
    for frag in fragmentos:
        n = len(frag.strip().rstrip("—-").strip().split())
        trozo = alineadas[i:i + n]
        if not trozo:
            raise RuntimeError("el reparto de fragmentos no cuadra con la alineacion")
        tramos.append((trozo[0].start, trozo[-1].end))
        i += n
    return tramos


# ── CTA overlay (last 60 seconds) ─────────────────────────


def _build_cta_filters(duration_sec):
    """
    FFmpeg drawtext filters for like/subscribe CTA overlay.
    Like appears at T-60s, Subscribe at T-55s.
    Centered on screen, both horizontally and vertically, as a stacked block.
    """
    if duration_sec is None or duration_sec < 65:
        return []

    t_like = duration_sec - 60.0
    t_sub = duration_sec - 55.0
    f = _FONT_BOLD

    return [
        # ── Like CTA (T-60s) ──────────────────────────────
        (
            f"drawtext=fontfile={f}:text='LIKE'"
            f":fontsize=44:fontcolor=white"
            f":x=(w-text_w)/2:y=(h-text_h)/2-140"
            f":box=1:boxcolor=black@0.65:boxborderw=14"
            f":enable='gte(t,{t_like:.1f})'"
        ),
        (
            f"drawtext=fontfile={f}:text='Dale like'"
            f":fontsize=30:fontcolor=white"
            f":x=(w-text_w)/2:y=(h-text_h)/2-82"
            f":box=1:boxcolor=black@0.50:boxborderw=10"
            f":enable='gte(t,{t_like:.1f})'"
        ),
        # ── Subscribe CTA (T-55s) ─────────────────────────
        (
            f"drawtext=fontfile={f}:text='SUSCRIBETE'"
            f":fontsize=44:fontcolor=white"
            f":x=(w-text_w)/2:y=(h-text_h)/2+6"
            f":box=1:boxcolor=red@0.70:boxborderw=14"
            f":enable='gte(t,{t_sub:.1f})'"
        ),
        (
            f"drawtext=fontfile={f}:text='Suscribete al canal'"
            f":fontsize=26:fontcolor=white"
            f":x=(w-text_w)/2:y=(h-text_h)/2+66"
            f":box=1:boxcolor=black@0.50:boxborderw=8"
            f":enable='gte(t,{t_sub:.1f})'"
        ),
    ]


def _build_logo_overlay(hook_end=LOGO_HOOK_END):
    """
    Filter_complex snippet for the channel-logo watermark during the gancho
    (the hook block only): small, top-right, semi-transparent, 0.5s fade
    in/out. Returns (extra_inputs, pre_chain, src_pad) — the logo is input
    [1] and must be overlaid BEFORE subtitles/CTA so text always draws on
    top.
    """
    if not LOGO_PATH or not LOGO_ENABLED:
        # Sin logo: la entrada de vídeo entra directa a subtítulos/CTA, así que
        # el filtergraph queda "[0:v]ass=...,drawtext=...[vout]" — bien formado.
        return [], "", "[0:v]"
    fade_out_start = hook_end - LOGO_FADE
    # -loop 1 makes the still PNG a timed stream so the fades can play out;
    # -t bounds it just past the fade-out so the input isn't infinite.
    extra_inputs = ["-loop", "1", "-t", str(hook_end + 1), "-i", LOGO_PATH]
    pre = (
        f"[1:v]scale={LOGO_WIDTH}:-1,format=rgba,"
        f"colorchannelmixer=aa={LOGO_OPACITY},"
        f"fade=t=in:st=0:d={LOGO_FADE}:alpha=1,"
        f"fade=t=out:st={fade_out_start}:d={LOGO_FADE}:alpha=1[logo];"
        f"[0:v][logo]overlay=x=W-w-{LOGO_MARGIN}:y={LOGO_MARGIN}"
        f":enable='between(t,0,{hook_end})'[vlogo];"
    )
    return extra_inputs, pre, "[vlogo]"


def _burn_subtitles_and_cta(video_path, ass_path, output_path,
                            duration_sec=None, hook_end=LOGO_HOOK_END,
                            pre_filters=(), fonts_dir=None):
    """
    Single FFmpeg pass: ASS subtitles + CTA drawtext overlay (+ logo
    watermark only if LOGO_ENABLED). Uses filter_complex to safely chain
    everything.

    `pre_filters` se aplican ANTES del ASS: son los `drawbox` de fondo negro
    de los cues C con `background: "black"`, que tienen que quedar debajo del
    titular. `fonts_dir` se pasa a libass para que resuelva Anton y Cormorant
    sin depender de que fontconfig las haya indexado.
    """
    cta = _build_cta_filters(duration_sec)
    logo_inputs, logo_pre, src_pad = _build_logo_overlay(hook_end)

    chain_parts = list(pre_filters)
    ass_filter = "ass=" + ass_path
    if fonts_dir:
        ass_filter += ":fontsdir=" + fonts_dir
    chain_parts.append(ass_filter)
    chain = ",".join(chain_parts)
    if cta:
        chain += "," + ",".join(cta)

    filter_complex = f"{logo_pre}{src_pad}{chain}[vout]"
    # Logged at INFO so the live filtergraph is auditable from the container
    # logs — this is the pass that burns the subtitles.
    log.info("Subs/CTA filter_complex (logo_enabled=%s): %s",
             LOGO_ENABLED, filter_complex)

    _ffmpeg([
        "-i", video_path,
        *logo_inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "0:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path,
    ], timeout=1800)


# ── Teaser (cold open) ─────────────────────────────────────


def _teaser_timing(teaser_cfg, narr_dur):
    """
    Derive the teaser timing. Silent mode: cut grid sized so that teaser +
    spoken hook fits in HOOK_END (the spoken-hook length is estimated from
    its word count at the narration's real words/s rate); drops the weakest
    (first) frase if the budget is tight. Voiced mode (frases carry a probed
    "voice_dur"): each frase block lasts its own narration + TEASER_GAP and
    the hook block end becomes dynamic (capped at HOOK_MAX).
    Returns {"frases", "total", "gancho_sec_est", "voiced", "hook_end"}
    with a per-frase "cut" on each frase, or None.
    """
    frases = [f for f in (teaser_cfg.get("frases") or [])
              if f.get("fragmentos") and f.get("image_file_id")]
    if not frases:
        return None
    words_total = float(teaser_cfg.get("words_total") or 0)
    gancho_words = float(teaser_cfg.get("gancho_words") or 0)
    rate = (words_total / narr_dur) if (words_total and narr_dur) else SPOKEN_RATE_FALLBACK
    gancho_sec = (gancho_words / rate) if gancho_words else 25.0

    voiced = [f for f in frases if f.get("voice_dur")]
    if voiced:
        # Voiced: cuts follow the real speech; budget only trims extremes.
        frases = voiced
        while len(frases) > 1:
            total = sum(f["voice_dur"] + TEASER_GAP for f in frases) + TEASER_FREEZE
            if total <= TEASER_MAX_VOICED:
                break
            frases = frases[1:]   # ascending intensity: first one is weakest
        for f in frases:
            block = f["voice_dur"] + TEASER_GAP
            f["cut"] = block / max(1, len(f["fragmentos"]))
        total = round(sum(f["cut"] * len(f["fragmentos"]) for f in frases)
                      + TEASER_FREEZE, 2)
        hook_end = min(HOOK_MAX, max(HOOK_END, total + gancho_sec + 0.2))
        return {"frases": frases, "total": total, "voiced": True,
                "gancho_sec_est": round(gancho_sec, 1),
                "hook_end": round(hook_end, 2)}

    target = max(TEASER_MIN, min(TEASER_MAX, HOOK_END - gancho_sec - 0.2))
    cut = TEASER_CUT_MIN
    while frases:
        n_frags = sum(len(f["fragmentos"]) for f in frases)
        cut = (target - TEASER_FREEZE) / n_frags
        if cut >= TEASER_CUT_MIN or len(frases) == 1:
            break
        frases = frases[1:]   # ascending intensity: first one is the weakest
    cut = max(TEASER_CUT_MIN, min(TEASER_CUT_MAX, cut))
    for f in frases:
        f["cut"] = cut
    n_frags = sum(len(f["fragmentos"]) for f in frases)
    total = round(n_frags * cut + TEASER_FREEZE, 2)
    return {"frases": frases, "total": total, "voiced": False,
            "gancho_sec_est": round(gancho_sec, 1), "hook_end": HOOK_END}


def _compose_teaser_frame(image_path, text, out_path):
    """
    1920x1080 teaser frame: cover-resized item image with the fragment text
    centered in Anton (white, dark stroke), rendered with PIL — same text
    machinery as the thumbnails, so no ffmpeg drawtext/escaping involved.
    `text=None` renders the bare frame (final freeze).
    """
    W, H = 1920, 1080
    img = Image.open(image_path).convert("RGB")
    img = _cover_resize(img, W, H)
    if text:
        draw = ImageDraw.Draw(img)
        text = text.upper()
        margin = 140
        font, size = _fit_font(draw, text, _FONT_ANTON, W - 2 * margin, 112, 48)
        stroke = max(4, size // 12)
        draw.text((W // 2, H // 2), text, font=font, fill=(255, 255, 255),
                  stroke_width=stroke, stroke_fill=(10, 8, 6), anchor="mm",
                  align="center")
    img.save(out_path, "JPEG", quality=92)


def _build_teaser_video(work, frases, freeze_path, out_path, draw_text=True):
    """
    Teaser video track: cuts (one per text fragment, each over its frase's
    item image, each frase paced by its own "cut" duration) + final
    ambiguous freeze frame with no text.

    `draw_text=False` deja los fotogramas limpios: el texto lo pone la capa
    ASS, sincronizado con la voz y con el estilo del canal. El troceado en
    fragmentos se mantiene igual porque marca el RITMO VISUAL del teaser (un
    corte por fragmento); lo que cambia es quien dibuja las letras.
    Encoded with the same params as the Ken Burns clips so the final
    concat with the main video can be a lossless stream copy.
    """
    inputs, chains, pads = [], [], []
    idx = 0
    for fi, frase in enumerate(frases):
        for gi, frag in enumerate(frase["fragmentos"]):
            frame = os.path.join(work, f"teaser_frame_{fi}_{gi}.jpg")
            _compose_teaser_frame(frase["image_path"], frag if draw_text else None, frame)
            dur = (frase.get("frag_durs") or [None] * 99)[gi] or frase["cut"]
            inputs += ["-loop", "1", "-t", f"{dur:.3f}", "-i", frame]
            chains.append(f"[{idx}:v]setsar=1,fps={FPS}[v{idx}]")
            pads.append(f"[v{idx}]")
            idx += 1
    freeze_frame = os.path.join(work, "teaser_frame_freeze.jpg")
    _compose_teaser_frame(freeze_path, None, freeze_frame)
    inputs += ["-loop", "1", "-t", f"{TEASER_FREEZE:.3f}", "-i", freeze_frame]
    chains.append(f"[{idx}:v]setsar=1,fps={FPS}[v{idx}]")
    pads.append(f"[v{idx}]")

    fc = ";".join(chains) + ";" + "".join(pads) + f"concat=n={len(pads)}:v=1:a=0[vout]"
    _ffmpeg(inputs + [
        "-filter_complex", fc,
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        out_path,
    ], timeout=300)


def _build_teaser_voice(frases, out_path):
    """
    Single voice track for the voiced teaser: each frase's narration in
    order, TEASER_GAP of silence after every one, so the voice lines up
    with its frase's visual block (which lasts voice_dur + TEASER_GAP).
    """
    fmt = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
    inputs, chains, pads = [], [], []
    for i, frase in enumerate(frases):
        inputs += ["-i", frase["voice_path"]]
        chains.append(f"[{i}:a]{fmt}[a{i}]")
        chains.append(
            f"anullsrc=r=44100:cl=stereo,atrim=duration={TEASER_GAP:.3f},{fmt}[g{i}]"
        )
        pads.append(f"[a{i}][g{i}]")
    fc = (";".join(chains) + ";" + "".join(pads)
          + f"concat=n={2 * len(frases)}:v=0:a=1[aout]")
    _ffmpeg(inputs + [
        "-filter_complex", fc,
        "-map", "[aout]",
        "-c:a", "pcm_s16le", "-f", "wav",
        out_path,
    ], timeout=120)


def _concat_copy(paths, out_path, work):
    """Lossless concat of clips that share identical encoding params."""
    list_path = os.path.join(work, "concat_list.txt")
    with open(list_path, "w") as f:
        for p in paths:
            f.write(f"file '{p}'\n")
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", list_path,
             "-c", "copy", out_path], timeout=300)


# ── FFmpeg ─────────────────────────────────────────────────


def _ken_burns(image_path, output_path, duration):
    frames = max(int(duration * FPS), 1)
    zf = ZOOM_TOTAL / frames
    # La entrada se amplía KB_SUPERSAMPLE veces para que el redondeo a píxeles
    # enteros de zoompan no se note como temblor (ver KB_SUPERSAMPLE arriba).
    sw, sh = 1920 * KB_SUPERSAMPLE, 1080 * KB_SUPERSAMPLE

    vf = (
        "hflip,"
        f"scale={sw}:{sh}:force_original_aspect_ratio=increase,"
        f"crop={sw}:{sh},setsar=1,"
        f"zoompan=z='min(zoom+{zf:.10f},1.5)'"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s=1920x1080:fps={FPS}"
    )
    _ffmpeg(["-i", image_path, "-vf", vf,
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             output_path], timeout=180)


def _join_clips(clips, output_path, work_dir, _level=0):
    if len(clips) == 1:
        shutil.copy(clips[0]["path"], output_path)
        return

    if len(clips) <= XFADE_BATCH:
        _xfade_batch(clips, output_path)
        return

    batches = [clips[i:i + XFADE_BATCH]
               for i in range(0, len(clips), XFADE_BATCH)]
    merged = []
    for j, batch in enumerate(batches):
        bp = os.path.join(work_dir, f"batch_L{_level}_{j:03d}.mp4")
        if len(batch) == 1:
            shutil.copy(batch[0]["path"], bp)
            dur = batch[0]["duration"]
        else:
            _xfade_batch(batch, bp)
            dur = sum(c["duration"] for c in batch) - (len(batch) - 1) * CROSSFADE_SEC
        merged.append({"path": bp, "duration": dur})

    _join_clips(merged, output_path, work_dir, _level=_level + 1)


def _xfade_batch(clips, output_path):
    inputs = []
    for c in clips:
        inputs += ["-i", c["path"]]

    parts = []
    n = len(clips)
    for i in range(n - 1):
        cum = sum(c["duration"] for c in clips[:i + 1])
        offset = max(0, cum - (i + 1) * CROSSFADE_SEC)

        in1 = "[0:v]" if i == 0 else f"[v{i - 1}]"
        in2 = f"[{i + 1}:v]"
        out = "[vout]" if i == n - 2 else f"[v{i}]"

        parts.append(
            f"{in1}{in2}xfade=transition=fade:duration={CROSSFADE_SEC}:offset={offset:.3f}{out}"
        )

    fc = ";".join(parts)
    _ffmpeg(inputs + [
        "-filter_complex", fc,
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        output_path,
    ], timeout=1200)


def _mix_audio_v2(work, video_path, narration_path, output_path, *,
                  narr_dur, teaser_sec, hook_end, teaser_voice_path,
                  music_plan, music_avoid, seed, legacy_track,
                  bed_offset_db=0.0):
    """
    Motor de audio nuevo (audio_mix): plan musical por tramos, cama con
    crossfades, ducking con la narracion como llave y master a -14 LUFS.

    Cualquier fallo cae a la mezcla clasica y, si esa tambien falla, a la
    narracion sola: un problema con la musica no puede impedir que salga el
    video. El motor usado se reporta en la respuesta (`music_engine`).
    """
    info = {"engine": "v2", "tracks": [], "attribution": [], "warnings": []}
    try:
        library = audio_mix.load_library(str(MUSIC_LIBRARY_DIR), LEGACY_LIBRARY)
        segments = audio_mix.normalize_plan(music_plan, narr_dur or 0, teaser_sec)
        chosen = audio_mix.select_tracks(library, segments,
                                         avoid=music_avoid, seed=seed)
        total = (narr_dur or 0) + teaser_sec
        bed = audio_mix.build_bed(work, chosen, total, _MUSIC_LUFS_CACHE)

        bm = audio_mix.measure_loudness(bed, timeout=900)
        vm = audio_mix.measure_loudness(narration_path, timeout=900)
        # Topes: si una medicion sale rara (pista corrupta, silencio) no se
        # amplifica sin freno, se deja el nivel tal cual.
        bed_gain = max(-24.0, min(24.0, audio_mix.MUSIC_OPEN_LUFS - bm["lufs"])) if bm else 0.0
        # Ajuste fino por peticion: negativo = musica mas discreta. Se aplica
        # DESPUES de la normalizacion, asi que el resultado es predecible
        # (-3 dB aqui son -3 dB en la mezcla final).
        bed_offset_db = max(-12.0, min(6.0, float(bed_offset_db or 0.0)))
        bed_gain += bed_offset_db
        voice_gain = max(-12.0, min(12.0, audio_mix.TARGET_VOICE_LUFS - vm["lufs"])) if vm else 0.0

        audio_mix.mix_final(video_path, narration_path, bed, output_path,
                            teaser_sec=teaser_sec, hook_end=hook_end,
                            teaser_voice_path=teaser_voice_path,
                            riser_path=RISER_PATH, riser_volume=RISER_VOLUME,
                            bed_gain_db=bed_gain, voice_gain_db=voice_gain)

        info["tracks"] = [
            {"id": c["track"]["id"], "title": c["track"].get("title"),
             "mood": c["mood"], "intensity": c["intensity"],
             "start": round(c["start"], 1), "end": round(c["end"], 1)}
            for c in chosen]
        info["attribution"] = sorted({c["track"]["attribution"] for c in chosen
                                      if c["track"].get("attribution")})
        info["voice_gain_db"] = round(voice_gain, 2)
        info["bed_gain_db"] = round(bed_gain, 2)
        info["bed_offset_db"] = round(bed_offset_db, 2)
        info["narration_lufs"] = round(vm["lufs"], 1) if vm else None
        log.info("Audio v2: %d tramos, pistas=%s, voz %+.1f dB, cama %+.1f dB",
                 len(chosen), [t["id"] for t in info["tracks"]],
                 voice_gain, bed_gain)
        return info
    except Exception as e:
        log.warning("Motor de audio v2 fallo (%s) - se usa la mezcla clasica", e)

    info = {"engine": "legacy", "tracks": [], "attribution": [],
            "warnings": ["motor v2 no disponible en este render"]}
    try:
        _mix_audio(video_path, narration_path, legacy_track, output_path,
                   teaser_sec=teaser_sec, hook_end=hook_end,
                   teaser_voice_path=teaser_voice_path)
        return info
    except Exception as e:
        log.error("Mezcla clasica tambien fallo (%s) - video con narracion sola", e)

    info["engine"] = "narration_only"
    info["warnings"].append("sin musica: fallaron los dos motores de mezcla")
    # El teaser va delante del video, asi que la narracion sigue necesitando
    # su retardo aunque no haya musica: si no, se desincroniza.
    delay = (f"adelay=delays={int(round(teaser_sec * 1000))}:all=1"
             if teaser_sec > 0 else "anull")
    _ffmpeg(["-i", video_path, "-i", narration_path,
             "-filter_complex", f"[1:a]{delay}[aout]",
             "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac",
             "-b:a", "192k", "-shortest", "-movflags", "+faststart",
             output_path], timeout=600)
    return info


def _music_envelope(teaser_sec, hook_end=HOOK_END):
    """
    Music volume over the video, per the hook dynamics spec:
      0 → teaser_end-0.5s : MUSIC_HOOK_VOL (trailer presence)
      teaser_end-0.5 → teaser_end : 0 (total-silence beat before narration)
      teaser_end → hook_end : MUSIC_HOOK_VOL (hook narration)
      hook_end → +fade   : linear crossfade down
      rest of the video  : MUSIC_BASE_VOL (unchanged channel standard)
    hook_end is 30s in silent mode, dynamic (teaser + spoken hook) in
    voiced mode.
    """
    g, b = MUSIC_HOOK_VOL, MUSIC_BASE_VOL
    dip_start = teaser_sec - TEASER_SILENCE
    fade_end = hook_end + MUSIC_DUCK_FADE
    return (
        f"if(lt(t,{dip_start:.3f}),{g},"
        f"if(lt(t,{teaser_sec:.3f}),0,"
        f"if(lt(t,{hook_end}),{g},"
        f"if(lt(t,{fade_end}),{g}-({g}-{b})*(t-{hook_end})/{MUSIC_DUCK_FADE},{b}))))"
    )


def _mix_audio(video_path, narration_path, music_path, output_path,
               teaser_sec=0.0, hook_end=HOOK_END, teaser_voice_path=None):
    """
    Narration + looped music. With a teaser, the narration is delayed by the
    teaser length, the music follows the hook envelope (elevated until
    hook_end, dip to silence right before the narration, crossfade back to
    base at hook_end) and the riser plays under the teaser cuts, peaking at
    the silence cut. A voiced teaser adds its own voice track at t=0.
    """
    fmt = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
    if teaser_sec <= 0:
        fc = (
            f"[1:a]{fmt}[narr];"
            f"[2:a]{fmt},volume={MUSIC_BASE_VOL}[mus];"
            "[narr][mus]amix=inputs=2:duration=first:normalize=0[aout]"
        )
        extra_inputs = []
        n_mix = 2
    else:
        delay_ms = int(round(teaser_sec * 1000))
        fc = (
            f"[1:a]{fmt},adelay=delays={delay_ms}:all=1[narr];"
            f"[2:a]{fmt},volume='{_music_envelope(teaser_sec, hook_end)}':eval=frame[mus];"
        )
        extra_inputs = []
        pads = "[narr][mus]"
        n_mix = 2
        next_in = 3
        if RISER_PATH:
            hit = teaser_sec - TEASER_SILENCE   # riser must peak at the cut to silence
            riser_dur = _probe_duration(RISER_PATH) or 0
            if riser_dur > hit:
                align = f"atrim=start={riser_dur - hit:.3f},asetpts=PTS-STARTPTS"
            else:
                align = f"adelay=delays={int(round((hit - riser_dur) * 1000))}:all=1"
            fc += f"[{next_in}:a]{fmt},{align},volume={RISER_VOLUME},apad[ris];"
            extra_inputs += ["-i", RISER_PATH]
            pads += "[ris]"
            n_mix += 1
            next_in += 1
        if teaser_voice_path:
            fc += f"[{next_in}:a]{fmt},apad[tvoz];"
            extra_inputs += ["-i", teaser_voice_path]
            pads += "[tvoz]"
            n_mix += 1
            next_in += 1
        fc += f"{pads}amix=inputs={n_mix}:duration=first:normalize=0[aout]"

    _ffmpeg([
        "-i", video_path,
        "-i", narration_path,
        "-stream_loop", "-1", "-i", music_path,
        *extra_inputs,
        "-filter_complex", fc,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ], timeout=600)


def _probe_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=10,
        )
        return round(float(r.stdout.strip()), 1)
    except Exception:
        return None


def _ffmpeg(args, timeout=300):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"] + args
    log.debug("FFmpeg: %s", " ".join(cmd[:6]) + " ...")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        stderr_tail = r.stderr[-1000:] if r.stderr else "(no stderr)"
        raise RuntimeError(f"FFmpeg exit {r.returncode}: {stderr_tail}")


# ── Main ───────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
