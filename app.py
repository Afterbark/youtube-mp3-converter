import os
import base64
import re
import time
import json
import urllib.parse
import urllib.request
import threading
from pathlib import Path
from uuid import uuid4
from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

app = Flask(__name__)
CORS(app)

# ---------- Temp dir ----------
DOWNLOAD_DIR = Path("/tmp")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---------- Cookies (optional but recommended) ----------
COOKIE_PATH = None
_b64 = os.getenv("YTDLP_COOKIES_B64")
if _b64:
    try:
        COOKIE_PATH = DOWNLOAD_DIR / "youtube_cookies.txt"
        COOKIE_PATH.write_bytes(base64.b64decode(_b64))
        print(f"[init] Loaded cookies to {COOKIE_PATH}", flush=True)
    except Exception as e:
        print(f"[init] Failed to load cookies: {e}", flush=True)

# ---------- Optional DSID to quiet web-client warnings ----------
YTDLP_DATA_SYNC_ID = os.getenv("YTDLP_DATA_SYNC_ID")  # optional

# Default output pattern (yt-dlp will write into /tmp via `paths`)
OUT_DEFAULT = "yt_%(id)s.%(ext)s"
SAFE_CHARS = re.compile(r"[^A-Za-z0-9 _.-]+")

# Clients to try (broadest first to work around SABR/throttling)
CLIENTS_TO_TRY = ["web", "web_safari", "web_embedded", "tv", "ios", "android"]

def safe_filename(name: str, ext: str = "mp3") -> str:
    name = SAFE_CHARS.sub("", name).strip() or "audio"
    return f"{name}.{ext}"

def _base_ydl_opts(out_default: str, cookiefile: str | None, dsid: str | None, client: str):
    opts = {
        "format": "bestaudio/best",
        "paths": {"home": str(DOWNLOAD_DIR), "temp": str(DOWNLOAD_DIR)},
        "outtmpl": {"default": out_default},
        "noprogress": True,
        "quiet": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 1,
        "geo_bypass": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "extractor_args": {
            "youtube": {
                "player_client": [client],
                "player_skip": ["webpage"],
                **({"data_sync_id": [dsid]} if (dsid and client.startswith("web")) else {}),
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.youtube.com/",
        },
        # "force_ip": "0.0.0.0",
        # "throttledratelimit": 102400,
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts

def _resolve_mp3_path(ydl: yt_dlp.YoutubeDL, info) -> Path:
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
    try:
        q = urllib.parse.quote(url, safe="")
        oembed = f"https://www.youtube.com/oembed?url={q}&format=json"
        with urllib.request.urlopen(oembed, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            t = data.get("title")
            if t:
                return t
    except Exception:
        pass
    return None

def download_audio_with_fallback(url: str, out_default: str, cookiefile: str | None, dsid: str | None):
    last_err = None
    for client in CLIENTS_TO_TRY:
        try:
            print(f"[yt-dlp] trying client={client}", flush=True)
            with yt_dlp.YoutubeDL(_base_ydl_opts(out_default, cookiefile, dsid, client)) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title") or "audio"
                mp3_path = _resolve_mp3_path(ydl, info)
                return title, str(mp3_path)
        except (DownloadError, ExtractorError, FileNotFoundError) as e:
            last_err = e
            print(f"[yt-dlp] client={client} failed: {e}", flush=True)
            continue
    if last_err:
        raise last_err
    raise RuntimeError("All extractor attempts failed")

# ===================== Minimal UI =====================
HOME_HTML = """(unchanged HTML from your message)"""

@app.get("/")
def home():
    return render_template_string(HOME_HTML)

@app.get("/health")
def health():
    return jsonify({"ok": True})

# ===================== Async Job System =====================
JOBS = {}  # job_id -> {"status": "queued|working|done|error", "title": str|None, "path": str|None, "error": str|None, "ts": float}
JOBS_LOCK = threading.Lock()

def _set_job(job_id, **kwargs):
    with JOBS_LOCK:
        job = JOBS.get(job_id, {})
        job.update(kwargs)
        JOBS[job_id] = job

def _cleanup_file_later(path: str, delay: int = 30):
    def _runner(p=path, d=delay):
        try:
            time.sleep(d)
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass
    threading.Thread(target=_runner, daemon=True).start()

def _purge_old_jobs(max_age_seconds: int = 3600):
    now = time.time()
    with JOBS_LOCK:
        to_del = [jid for jid, j in JOBS.items() if now - j.get("ts", now) > max_age_seconds]
        for jid in to_del:
            JOBS.pop(jid, None)

def _worker(job_id: str, url: str):
    _set_job(job_id, status="working", ts=time.time())
    cookiefile = str(COOKIE_PATH) if COOKIE_PATH and COOKIE_PATH.exists() else None
    try:
        # 1) download
        title, mp3_path = download_audio_with_fallback(url, OUT_DEFAULT, cookiefile, YTDLP_DATA_SYNC_ID)

        # 2) improve title if needed
        if not title or title.strip().lower() == "audio":
            t2 = fetch_title_with_ytdlp(url, cookiefile, YTDLP_DATA_SYNC_ID)
            if t2: title = t2
        if not title or title.strip().lower() == "audio":
            t3 = fetch_title_oembed(url)
            if t3: title = t3
        title = title or "audio"

        _set_job(job_id, status="done", title=title, path=mp3_path)
        _purge_old_jobs()
    except Exception as e:
        _set_job(job_id, status="error", error=str(e))
        _purge_old_jobs()

@app.post("/enqueue")
def enqueue():
    url = request.form.get("url") or (request.json or {}).get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    job_id = uuid4().hex
    _set_job(job_id, status="queued", title=None, path=None, error=None, ts=time.time())
    threading.Thread(target=_worker, args=(job_id, url), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.get("/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"status": "error", "error": "job not found"}), 404
    return jsonify({
        "status": job.get("status"),
        "title": job.get("title"),
        "error": job.get("error"),
    })

@app.get("/download_job/<job_id>")
def download_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    if job.get("status") != "done" or not job.get("path"):
        return jsonify({"error": "job not ready"}), 409

    mp3_path = job["path"]
    title = job.get("title") or "audio"
    safe_name = safe_filename(title, "mp3")
    try:
        resp = send_file(mp3_path, mimetype="audio/mpeg", as_attachment=True, download_name=safe_name)
        resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
        _cleanup_file_later(mp3_path, delay=30)
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ===================== (Optional) direct /download kept for bare calls =====================
@app.route("/download", methods=["GET", "POST"])
def download():
    url = request.args.get("url") or request.form.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    try:
        cookiefile = str(COOKIE_PATH) if COOKIE_PATH and COOKIE_PATH.exists() else None
        title, mp3_path = download_audio_with_fallback(url, OUT_DEFAULT, cookiefile=cookiefile, dsid=YTDLP_DATA_SYNC_ID)
        if not title or title.strip().lower() == "audio":
            t2 = fetch_title_with_ytdlp(url, cookiefile, YTDLP_DATA_SYNC_ID)
            if t2: title = t2
        if not title or title.strip().lower() == "audio":
            t3 = fetch_title_oembed(url)
            if t3: title = t3
        safe_name = safe_filename(title or "audio", "mp3")
        resp = send_file(mp3_path, mimetype="audio/mpeg", as_attachment=True, download_name=safe_name)
        resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
        _cleanup_file_later(mp3_path, delay=30)
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
