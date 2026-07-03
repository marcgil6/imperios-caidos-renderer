"""
ENIGMAS DEL PASADO / IMPERIOS CAIDOS - Video Render Service
Ken Burns + crossfade + audio mix via FFmpeg.
Word-by-word subtitles (Whisper) + CTA overlay in last 60s.
"""
import base64
import io
import json
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

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("render")


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

RENDERS_DIR = Path("/app/renders")
RENDERS_DIR.mkdir(exist_ok=True)

WHISPER_MODEL = None

_FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"


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
    })


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
      "drive_folder_id": "FOLDER_ID"
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

        # 5 ── Mix audio
        log.info("Mixing audio...")
        mixed_path = os.path.join(work, "mixed.mp4")
        _mix_audio(joined_path, narr_path, music_path, mixed_path)

        duration_sec = _probe_duration(mixed_path)

        # QC ── duration drift check
        if narr_dur and duration_sec:
            drift = abs(duration_sec - narr_dur)
            log.info("QC duration: video=%.1fs narration=%.1fs drift=%.1fs",
                     duration_sec, narr_dur, drift)
            if drift > 5:
                raise RuntimeError(
                    f"QC FAILED: video {duration_sec:.1f}s vs narration {narr_dur:.1f}s "
                    f"(drift {drift:.1f}s > 5s)."
                )

        # 6 ── Subtitles + CTA overlay
        out_name = f"{dynasty}_{int(time.time())}.mp4"
        out_path = os.path.join(work, out_name)
        if WHISPER_MODEL is not None:
            try:
                ass_path = _transcribe_to_ass(narr_path, work)
                _burn_subtitles_and_cta(mixed_path, ass_path, out_path, duration_sec)
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


def _transcribe_to_ass(narr_path, work):
    """
    Whisper phrase-level ASS subtitles.
    Each segment fades in/out with \\fad(200,200).
    Font: Liberation Sans Bold 72px, 3px black outline, bottom-centre (15%).
    Max 12 words per line — wraps with \\N at natural phrase break.
    """
    log.info("Transcribing narration (Whisper base, phrase-level, CPU)...")
    segments, info = WHISPER_MODEL.transcribe(
        narr_path,
        language="es",
        beam_size=1,
        best_of=1,
        vad_filter=True,
    )
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
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        text = _wrap_phrase(text, max_words=12)
        dialogues.append(
            f"Dialogue: 0,{_tc(seg.start)},{_tc(seg.end)},Default,,0,0,0,,{{\\fad(200,200)}}{text}"
        )

    if not dialogues:
        raise RuntimeError("Whisper returned 0 segments — audio may be silent")

    ass_path = os.path.join(work, "subtitles.ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(dialogues))

    log.info("ASS written: %d phrases → %s", len(dialogues), ass_path)
    return ass_path


# ── CTA overlay (last 60 seconds) ─────────────────────────


def _build_cta_filters(duration_sec):
    """
    FFmpeg drawtext filters for like/subscribe CTA overlay.
    Like appears at T-60s, Subscribe at T-55s.
    Positioned bottom-left (x=80), above subtitle area.
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
            f":x=80:y=H-295"
            f":box=1:boxcolor=black@0.65:boxborderw=14"
            f":enable='gte(t,{t_like:.1f})'"
        ),
        (
            f"drawtext=fontfile={f}:text='Dale like'"
            f":fontsize=30:fontcolor=white"
            f":x=80:y=H-242"
            f":box=1:boxcolor=black@0.50:boxborderw=10"
            f":enable='gte(t,{t_like:.1f})'"
        ),
        # ── Subscribe CTA (T-55s) ─────────────────────────
        (
            f"drawtext=fontfile={f}:text='SUSCRIBETE'"
            f":fontsize=44:fontcolor=white"
            f":x=80:y=H-175"
            f":box=1:boxcolor=red@0.70:boxborderw=14"
            f":enable='gte(t,{t_sub:.1f})'"
        ),
        (
            f"drawtext=fontfile={f}:text='Suscribete al canal'"
            f":fontsize=26:fontcolor=white"
            f":x=80:y=H-122"
            f":box=1:boxcolor=black@0.50:boxborderw=8"
            f":enable='gte(t,{t_sub:.1f})'"
        ),
    ]


def _burn_subtitles_and_cta(video_path, ass_path, output_path, duration_sec=None):
    """
    Single FFmpeg pass: burn ASS word subtitles + CTA drawtext overlay.
    Uses filter_complex to safely chain ass= and multiple drawtext= filters.
    """
    cta = _build_cta_filters(duration_sec)

    if cta:
        # Chain: ass subtitles → CTA drawtext filters → output pad
        chain = "ass=" + ass_path + "," + ",".join(cta)
        _ffmpeg([
            "-i", video_path,
            "-filter_complex", f"[0:v]{chain}[vout]",
            "-map", "[vout]",
            "-map", "0:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            output_path,
        ], timeout=1800)
    else:
        _ffmpeg([
            "-i", video_path,
            "-vf", f"ass={ass_path}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            output_path,
        ], timeout=1800)


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


def _mix_audio(video_path, narration_path, music_path, output_path):
    fc = (
        "[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[narr];"
        "[2:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
        "volume=0.1[mus];"
        "[narr][mus]amix=inputs=2:duration=first:normalize=0[aout]"
    )
    _ffmpeg([
        "-i", video_path,
        "-i", narration_path,
        "-stream_loop", "-1", "-i", music_path,
        "-filter_complex", fc,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest",
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
