import os
import base64
import re
import time
import json
import uuid
import urllib.parse
import urllib.request
import threading
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

app = Flask(__name__)
CORS(app)

# ---------- Configuration ----------
DOWNLOAD_DIR = Path("/tmp")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Cookie handling
COOKIE_PATH = None
_b64 = os.getenv("YTDLP_COOKIES_B64")
if _b64:
    try:
        COOKIE_PATH = DOWNLOAD_DIR / "youtube_cookies.txt"
        COOKIE_PATH.write_bytes(base64.b64decode(_b64))
        print(f"✓ Loaded cookies to {COOKIE_PATH}", flush=True)
    except Exception as e:
        print(f"✗ Failed to load cookies: {e}", flush=True)

YTDLP_DATA_SYNC_ID = os.getenv("YTDLP_DATA_SYNC_ID")
OUT_DEFAULT = "yt_%(id)s.%(ext)s"
SAFE_CHARS = re.compile(r"[^A-Za-z0-9 _.-]+")

# Prioritize mobile clients to avoid SABR
CLIENTS_TO_TRY = [
    "ios",
    "android",
    "tv_embedded",
    "web_creator",
    "mediaconnect",
    "web",
    "web_safari",
    "web_embedded",
]

# Job queue
job_queue = {}

def safe_filename(name: str, ext: str = "mp3") -> str:
    """Sanitize filename for safe download."""
    name = SAFE_CHARS.sub("", name).strip() or "audio"
    return f"{name}.{ext}"


def _base_ydl_opts(out_default: str, cookiefile: str | None, dsid: str | None, client: str):
    """Build optimized yt-dlp options for a specific player client."""
    opts = {
        "format": "bestaudio/best",
        "paths": {"home": str(DOWNLOAD_DIR), "temp": str(DOWNLOAD_DIR)},
        "outtmpl": {"default": out_default},
        "noprogress": True,
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 10,
        "extractor_retries": 5,
        "concurrent_fragment_downloads": 1,
        "geo_bypass": True,
        "socket_timeout": 30,
        "http_chunk_size": 10485760,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "extractor_args": {
            "youtube": {
                "player_client": [client],
                "player_skip": ["webpage", "configs"],
                "skip": ["hls", "dash"],
                **({"data_sync_id": [dsid]} if (dsid and client.startswith("web")) else {}),
            }
        },
        "http_headers": {
            "User-Agent": "com.google.ios.youtube/19.29.1 (iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X;)" if client == "ios" 
                         else "com.google.android.youtube/19.29.37 (Linux; U; Android 14; en_US)" if client == "android"
                         else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        },
    }
    
    if client.startswith("web"):
        opts["http_headers"]["Origin"] = "https://www.youtube.com"
        opts["http_headers"]["Referer"] = "https://www.youtube.com/"
    
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts


def _resolve_mp3_path(ydl: yt_dlp.YoutubeDL, info) -> Path:
    """Get the final MP3 path after post-processing."""
    try:
        pre = Path(ydl.prepare_filename(info))
        cand = pre.with_suffix(".mp3")
        if cand.exists():
            return cand
    except Exception:
        pass

    vid = info.get("id") or "*"
    matches = sorted(
        DOWNLOAD_DIR.glob(f"yt_{vid}*.mp3"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )
    if matches:
        return matches[0]
    raise FileNotFoundError("MP3 not found after postprocessing")


def fetch_title_with_ytdlp(url: str, cookiefile: str | None, dsid: str | None):
    """Metadata-only title fetch."""
    for client in CLIENTS_TO_TRY:
        try:
            opts = _base_ydl_opts(OUT_DEFAULT, cookiefile, dsid, client)
            opts.update({"skip_download": True})
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                t = (info or {}).get("title")
                if t:
                    return t
        except Exception:
            continue
    return None


def fetch_title_oembed(url: str):
    """Last-resort title via YouTube oEmbed."""
    try:
        q = urllib.parse.quote(url, safe="")
        oembed = f"https://www.youtube.com/oembed?url={q}&format=json"
        with urllib.request.urlopen(oembed, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            return data.get("title")
    except Exception:
        pass
    return None


def download_audio_with_fallback(url: str, out_default: str, cookiefile: str | None, dsid: str | None):
    """Try multiple clients to avoid SABR/bot checks."""
    last_err = None
    for idx, client in enumerate(CLIENTS_TO_TRY):
        try:
            print(f"[yt-dlp] Attempt {idx+1}/{len(CLIENTS_TO_TRY)}: client={client}", flush=True)
            with yt_dlp.YoutubeDL(_base_ydl_opts(out_default, cookiefile, dsid, client)) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title") or "audio"
                mp3_path = _resolve_mp3_path(ydl, info)
                print(f"✓ Success with client={client}", flush=True)
                return title, str(mp3_path)
        except (DownloadError, ExtractorError, FileNotFoundError) as e:
            last_err = e
            print(f"✗ client={client} failed: {str(e)[:100]}", flush=True)
            continue
    if last_err:
        raise last_err
    raise RuntimeError("All extractor attempts failed")


def process_job(job_id: str, url: str):
    """Background job processor."""
    try:
        job_queue[job_id]["status"] = "processing"
        cookiefile = str(COOKIE_PATH) if COOKIE_PATH and COOKIE_PATH.exists() else None
        
        title, mp3_path = download_audio_with_fallback(
            url, OUT_DEFAULT, cookiefile=cookiefile, dsid=YTDLP_DATA_SYNC_ID
        )
        
        if not title or title.strip().lower() == "audio":
            t2 = fetch_title_with_ytdlp(url, cookiefile, YTDLP_DATA_SYNC_ID)
            if t2:
                title = t2
        
        if not title or title.strip().lower() == "audio":
            t3 = fetch_title_oembed(url)
            if t3:
                title = t3
        
        job_queue[job_id].update({
            "status": "done",
            "title": title or "audio",
            "mp3_path": mp3_path,
        })
        print(f"✓ Job {job_id} completed: {title}", flush=True)
        
        def cleanup():
            time.sleep(300)
            try:
                Path(mp3_path).unlink(missing_ok=True)
                if job_id in job_queue:
                    del job_queue[job_id]
                print(f"🧹 Cleaned up job {job_id}", flush=True)
            except Exception as e:
                print(f"⚠ Cleanup error for {job_id}: {e}", flush=True)
        
        threading.Thread(target=cleanup, daemon=True).start()
        
    except Exception as e:
        job_queue[job_id].update({
            "status": "error",
            "error": str(e)
        })
        print(f"✗ Job {job_id} failed: {e}", flush=True)


# ---------- Routes ----------

@app.get("/")
def home():
    return render_template_string(HOME_HTML)


@app.get("/health")
def health():
    """Health check endpoint."""
    return jsonify({
        "ok": True,
        "timestamp": datetime.utcnow().isoformat(),
        "active_jobs": len([j for j in job_queue.values() if j["status"] == "processing"])
    })


@app.route("/enqueue", methods=["POST"])
def enqueue():
    """Add conversion job to queue for async processing."""
    url = request.form.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    
    job_id = str(uuid.uuid4())
    job_queue[job_id] = {
        "status": "queued",
        "url": url,
        "title": None,
        "error": None,
        "mp3_path": None,
        "created_at": datetime.utcnow().isoformat()
    }
    
    thread = threading.Thread(target=process_job, args=(job_id, url), daemon=True)
    thread.start()
    
    print(f"✓ Job {job_id} queued for URL: {url[:50]}...", flush=True)
    return jsonify({"job_id": job_id, "status": "queued"})


@app.get("/status/<job_id>")
def status(job_id: str):
    """Check status of a queued job."""
    if job_id not in job_queue:
        return jsonify({"error": "job not found"}), 404
    
    job = job_queue[job_id]
    return jsonify({
        "status": job["status"],
        "title": job.get("title"),
        "error": job.get("error"),
        "created_at": job.get("created_at")
    })


@app.get("/download_job/<job_id>")
def download_job(job_id: str):
    """Download the completed MP3 file."""
    if job_id not in job_queue:
        return jsonify({"error": "job not found"}), 404
    
    job = job_queue[job_id]
    
    if job["status"] != "done":
        return jsonify({"error": f"job is {job['status']}"}), 400
    
    mp3_path = job["mp3_path"]
    if not mp3_path or not Path(mp3_path).exists():
        return jsonify({"error": "file not found"}), 404
    
    safe_name = safe_filename(job["title"] or "audio", "mp3")
    
    resp = send_file(
        mp3_path,
        mimetype="audio/mpeg",
        as_attachment=True,
        download_name=safe_name
    )
    resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
    
    return resp


@app.route("/download", methods=["GET", "POST"])
def download():
    """Direct download endpoint (synchronous)."""
    url = request.args.get("url") or request.form.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400

    try:
        cookiefile = str(COOKIE_PATH) if COOKIE_PATH and COOKIE_PATH.exists() else None

        title, mp3_path = download_audio_with_fallback(
            url,
            OUT_DEFAULT,
            cookiefile=cookiefile,
            dsid=YTDLP_DATA_SYNC_ID
        )

        if not title or title.strip().lower() == "audio":
            t2 = fetch_title_with_ytdlp(url, cookiefile, YTDLP_DATA_SYNC_ID)
            if t2:
                title = t2

        if not title or title.strip().lower() == "audio":
            t3 = fetch_title_oembed(url)
            if t3:
                title = t3

        safe_name = safe_filename(title or "audio", "mp3")
        resp = send_file(
            mp3_path,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=safe_name
        )
        resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition"

        def _cleanup(path):
            try:
                time.sleep(30)
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass

        threading.Thread(target=_cleanup, args=(mp3_path,), daemon=True).start()
        return resp

    except Exception as e:
        print(f"✗ Download error: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


# Minimal UI (same as before - keeping it short for space)
HOME_HTML = """<!doctype html><html><head><meta charset="utf-8"/><title>YouTube MP3</title></head><body><h1>YouTube MP3 Converter</h1><p>Backend is running. Use the Chrome extension or access via API.</p></body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))