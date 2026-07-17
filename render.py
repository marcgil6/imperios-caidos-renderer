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

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("render")

# Bump this string on every render.py change that affects output —
# exposed via /health and in the /render response so a stale EasyPanel
# deploy can be spotted without shell access to the container.
BUILD_VERSION = "2026-07-17-thumb-html"


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
        if (d / "music_01_uprising.mp3").exists():
            return d
    return candidates[0]

MUSIC_DIR = _find_music_dir()
MUSIC = {
    "uprising": MUSIC_DIR / "music_01_uprising.mp3",
    "the_long_dark": MUSIC_DIR / "music_02_the_long_dark.mp3",
    "end": MUSIC_DIR / "music_03_end.mp3",
}

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
LOGO_WIDTH = 140        # px, height keeps aspect ratio
LOGO_OPACITY = 0.7
LOGO_MARGIN = 40        # px from top and right edges
LOGO_HOOK_END = 30.0    # gancho = first 30s exactly (MasterTube block 1)
LOGO_FADE = 0.5

FPS = 25
CROSSFADE_SEC = 1.0
DEFAULT_DURATION = 12
ZOOM_TOTAL = 0.03
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
        "logo_found": LOGO_PATH is not None,
        "riser_found": RISER_PATH is not None,
        "playwright": _playwright_available(),
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
            per_clip = max(per_clip, 5.0)
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
            teaser_path = os.path.join(work, "teaser.mp4")
            _build_teaser_video(work, teaser["frases"], freeze_path, teaser_path)
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
        _mix_audio(joined_path, narr_path, music_path, mixed_path,
                   teaser_sec=teaser_sec, hook_end=hook_end,
                   teaser_voice_path=teaser_voice_path)

        duration_sec = _probe_duration(mixed_path)

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

        # 6 ── Subtitles + CTA overlay
        out_name = f"{dynasty}_{int(time.time())}.mp4"
        out_path = os.path.join(work, out_name)
        subtitle_coverage = None
        if WHISPER_MODEL is not None:
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
            "build_version": BUILD_VERSION,
            "subtitle_coverage": subtitle_coverage,
            "teaser_duration_sec": teaser_sec or None,
            "teaser_hook_block_sec": (round(teaser_sec + teaser["gancho_sec_est"], 1)
                                      if teaser else None),
            "teaser_voiced": teaser["voiced"] if teaser else None,
            "hook_end_sec": hook_end if teaser else None,
        })

    except Exception as e:
        log.exception("Render failed")
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
    if not LOGO_PATH:
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
                            duration_sec=None, hook_end=LOGO_HOOK_END):
    """
    Single FFmpeg pass: logo watermark (hook block) + ASS subtitles + CTA
    drawtext overlay. Uses filter_complex to safely chain everything.
    """
    cta = _build_cta_filters(duration_sec)
    logo_inputs, logo_pre, src_pad = _build_logo_overlay(hook_end)

    chain = "ass=" + ass_path
    if cta:
        chain += "," + ",".join(cta)

    _ffmpeg([
        "-i", video_path,
        *logo_inputs,
        "-filter_complex", f"{logo_pre}{src_pad}{chain}[vout]",
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


def _build_teaser_video(work, frases, freeze_path, out_path):
    """
    Teaser video track: cuts (one per text fragment, each over its frase's
    item image, each frase paced by its own "cut" duration) + final
    ambiguous freeze frame with no text.
    Encoded with the same params as the Ken Burns clips so the final
    concat with the main video can be a lossless stream copy.
    """
    inputs, chains, pads = [], [], []
    idx = 0
    for fi, frase in enumerate(frases):
        for gi, frag in enumerate(frase["fragmentos"]):
            frame = os.path.join(work, f"teaser_frame_{fi}_{gi}.jpg")
            _compose_teaser_frame(frase["image_path"], frag, frame)
            inputs += ["-loop", "1", "-t", f"{frase['cut']:.3f}", "-i", frame]
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

    vf = (
        "hflip,"
        "scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,setsar=1,"
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
