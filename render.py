"""
IMPERIOS CAIDOS - Video Render Service
Ken Burns + crossfade + audio mix via FFmpeg.
Uploads result to Google Drive, returns URL.
"""
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import logging
from pathlib import Path

import requests as http_requests
from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("render")

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


@app.route("/render", methods=["POST"])
def render():
    """
    POST /render
    Body JSON:
    {
      "images": [
        {"file_id": "DRIVE_ID", "duration": 12, "filename": "escena_001_a.jpg"},
        {"url": "https://...", "duration": 10, "filename": "escena_002_a.jpg"}
      ],
      "narration_file_id": "DRIVE_ID",       // OR "narration_url": "https://..."
      "music_track": "uprising",             // uprising | the_long_dark | end
      "dynasty_name": "azcarraga",
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
    dynasty = data.get("dynasty_name", "video")
    folder_id = data.get("drive_folder_id")

    work = tempfile.mkdtemp(prefix="render_")
    log.info("Render started: dynasty=%s, images=%d, music=%s", dynasty, len(images), music_key)

    try:
        drive = _get_drive_service()

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

        # 5 ── Mix audio (narration + looped music at -20dB)
        log.info("Mixing audio...")
        out_name = f"{dynasty}_{int(time.time())}.mp4"
        out_path = os.path.join(work, out_name)
        _mix_audio(joined_path, narr_path, music_path, out_path)

        duration_sec = _probe_duration(out_path)
        file_size = os.path.getsize(out_path)
        log.info("Render complete: %s (%.1f min, %.1f MB)",
                 out_name,
                 (duration_sec or 0) / 60,
                 file_size / 1024 / 1024)

        # 6 ── Upload to Drive
        log.info("Uploading to Google Drive (folder=%s)...", folder_id)
        result = _upload_drive(drive, out_path, out_name, folder_id)
        log.info("Upload done: %s", result["url"])

        return jsonify({
            "success": True,
            "file_id": result["file_id"],
            "url": result["url"],
            "filename": out_name,
            "duration_sec": duration_sec,
            "duration_min": round(duration_sec / 60, 1) if duration_sec else None,
            "size_bytes": file_size,
            "images_count": len(img_list),
            "music_track": music_key,
        })

    except Exception as e:
        log.exception("Render failed")
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        shutil.rmtree(work, ignore_errors=True)


# ── Google Drive ───────────────────────────────────────────


def _get_drive_service():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise ValueError(
            "GOOGLE_SERVICE_ACCOUNT_JSON env var not set. "
            "Create a service account in Google Cloud Console, "
            "download the JSON key, and set it as this env var."
        )
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
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


def _upload_drive(service, file_path, filename, folder_id):
    meta = {"name": filename}
    if folder_id:
        meta["parents"] = [folder_id]
    media = MediaFileUpload(file_path, mimetype="video/mp4", resumable=True)
    uploaded = service.files().create(
        body=meta, media_body=media, fields="id,webViewLink",
    ).execute()
    service.permissions().create(
        fileId=uploaded["id"],
        body={"role": "reader", "type": "anyone"},
    ).execute()
    return {
        "file_id": uploaded["id"],
        "url": uploaded.get("webViewLink",
                            f"https://drive.google.com/file/d/{uploaded['id']}/view"),
    }


def _download_url(url, dest):
    r = http_requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)


# ── FFmpeg ─────────────────────────────────────────────────


def _ken_burns(image_path, output_path, duration):
    """Create a Ken Burns clip: slow 3% zoom over duration."""
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
             "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
             output_path], timeout=180)


def _join_clips(clips, output_path, work_dir):
    """Join clips with xfade crossfade, batched for large counts."""
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
        bp = os.path.join(work_dir, f"batch_{j:03d}.mp4")
        if len(batch) == 1:
            shutil.copy(batch[0]["path"], bp)
            dur = batch[0]["duration"]
        else:
            _xfade_batch(batch, bp)
            dur = sum(c["duration"] for c in batch) - (len(batch) - 1) * CROSSFADE_SEC
        merged.append({"path": bp, "duration": dur})

    _join_clips(merged, output_path, work_dir)


def _xfade_batch(clips, output_path):
    """xfade up to XFADE_BATCH clips in a single FFmpeg call."""
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

    # Write filter script to avoid command-line length limits
    fc = ";".join(parts)
    _ffmpeg(inputs + [
        "-filter_complex", fc,
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        output_path,
    ], timeout=1200)


def _mix_audio(video_path, narration_path, music_path, output_path):
    """Mix narration (1.0) + looped music (-20dB ≈ 0.1) onto video."""
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
