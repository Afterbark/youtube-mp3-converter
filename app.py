import os
import base64
import re
import time
import json
import uuid
import urllib.parse
import urllib.request
import threading
import zipfile
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
SAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

CLIENTS_TO_TRY = [
    "web",
    "mweb",
    "mediaconnect",
    "tv_embedded",
]

# ---------- Job Queue System ----------
job_queue = {}
batch_queue = {}

def safe_filename(name: str) -> str:
    name = SAFE_CHARS.sub("_", name).strip() or "media"
    name = " ".join(name.split())
    if len(name) > 200:
        name = name[:200].rsplit(' ', 1)[0]
    return f"{name}.mp3"


def _base_ydl_opts(out_default: str, cookiefile: str | None, dsid: str | None, client: str, quality: str = "192"):
    opts = {
        "format": "ba/b",
        "paths": {"home": str(DOWNLOAD_DIR), "temp": str(DOWNLOAD_DIR)},
        "outtmpl": {"default": out_default},
        "noprogress": True,
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 10,
        "concurrent_fragment_downloads": 1,
        "geo_bypass": True,
        "socket_timeout": 30,
        "http_chunk_size": 10485760,
        "age_limit": None,
        "nocheckcertificate": True,
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
        "extractor_args": {
            "youtube": {
                "player_client": [client],
                "player_skip": ["configs", "webpage"],
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if dsid:
        opts["extractor_args"]["youtube"]["data_sync_id"] = [dsid]
    return opts


def fetch_title_with_ytdlp(url: str) -> str:
    cookiefile = str(COOKIE_PATH) if COOKIE_PATH else None
    dsid = YTDLP_DATA_SYNC_ID
    for client in CLIENTS_TO_TRY:
        try:
            opts = _base_ydl_opts(OUT_DEFAULT, cookiefile, dsid, client, "192")
            opts["skip_download"] = True
            opts["quiet"] = True
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info.get("title", "Unknown")
        except Exception as e:
            print(f"Client {client} failed for title fetch: {e}", flush=True)
            continue
    return "Unknown"


def download_task(job_id: str, url: str, quality: str):
    job_queue[job_id]["status"] = "downloading"
    cookiefile = str(COOKIE_PATH) if COOKIE_PATH else None
    dsid = YTDLP_DATA_SYNC_ID
    last_error = None
    for client in CLIENTS_TO_TRY:
        try:
            print(f"[{job_id}] Trying client: {client}", flush=True)
            opts = _base_ydl_opts(OUT_DEFAULT, cookiefile, dsid, client, quality)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "Unknown")
                video_id = info.get("id", "unknown")
                downloaded_file = DOWNLOAD_DIR / f"yt_{video_id}.mp3"
                if not downloaded_file.exists():
                    pattern = f"yt_{video_id}.*"
                    matches = list(DOWNLOAD_DIR.glob(pattern))
                    if matches:
                        downloaded_file = matches[0]
                if downloaded_file.exists():
                    job_queue[job_id].update({"status": "done", "file_path": str(downloaded_file), "title": title})
                    print(f"[{job_id}] ✓ Download completed with client: {client}", flush=True)
                    return
                else:
                    raise FileNotFoundError(f"Downloaded file not found: {downloaded_file}")
        except Exception as e:
            last_error = str(e)
            print(f"[{job_id}] ✗ Client {client} failed: {e}", flush=True)
            continue
    job_queue[job_id].update({"status": "error", "error": f"All clients failed. Last error: {last_error}"})
    print(f"[{job_id}] ✗ All clients exhausted", flush=True)


def batch_download_task(batch_id: str, urls: list, quality: str):
    batch = batch_queue[batch_id]
    for i, url in enumerate(urls):
        job_id = batch["jobs"][i]["job_id"]
        batch["current_index"] = i
        batch["jobs"][i]["status"] = "downloading"
        job_queue[job_id] = {
            "status": "downloading", "url": url, "title": "Fetching...",
            "quality": quality, "error": None, "file_path": None,
            "created_at": datetime.now().isoformat()
        }
        download_task(job_id, url, quality)
        job = job_queue[job_id]
        batch["jobs"][i]["status"] = job["status"]
        batch["jobs"][i]["title"] = job.get("title", "Unknown")
        batch["jobs"][i]["error"] = job.get("error")
        batch["jobs"][i]["file_path"] = job.get("file_path")
        if job["status"] == "done":
            batch["completed"] += 1
        elif job["status"] == "error":
            batch["failed"] += 1
    batch["status"] = "done"
    print(f"[BATCH {batch_id}] ✓ Completed: {batch['completed']}/{batch['total']}, Failed: {batch['failed']}", flush=True)


@app.route("/")
def home():
    return render_template_string(HOME_HTML)


@app.route("/health")
def health():
    return jsonify({"ok": True, "status": "online"})


@app.route("/enqueue", methods=["POST"])
def enqueue():
    url = request.form.get("url", "").strip()
    quality = request.form.get("quality", "192").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    if quality not in ["128", "192", "256", "320"]:
        quality = "192"
    job_id = str(uuid.uuid4())
    try:
        title = fetch_title_with_ytdlp(url)
    except:
        title = "Unknown"
    job_queue[job_id] = {
        "status": "queued", "url": url, "title": title, "quality": quality,
        "error": None, "file_path": None, "created_at": datetime.now().isoformat()
    }
    thread = threading.Thread(target=download_task, args=(job_id, url, quality))
    thread.daemon = True
    thread.start()
    return jsonify({"job_id": job_id, "status": "queued", "title": title, "quality": quality})


@app.route("/batch_enqueue", methods=["POST"])
def batch_enqueue():
    urls_raw = request.form.get("urls", "").strip()
    quality = request.form.get("quality", "192").strip()
    if not urls_raw:
        return jsonify({"error": "URLs required"}), 400
    if quality not in ["128", "192", "256", "320"]:
        quality = "192"
    urls = []
    for line in urls_raw.replace(",", "\n").split("\n"):
        url = line.strip()
        if url and ("youtube.com" in url or "youtu.be" in url):
            urls.append(url)
    if not urls:
        return jsonify({"error": "No valid YouTube URLs found"}), 400
    if len(urls) > 20:
        return jsonify({"error": "Maximum 20 URLs per batch"}), 400
    batch_id = str(uuid.uuid4())
    batch_queue[batch_id] = {
        "status": "processing", "total": len(urls), "completed": 0, "failed": 0,
        "current_index": 0, "quality": quality, "created_at": datetime.now().isoformat(),
        "jobs": [{"job_id": str(uuid.uuid4()), "url": url, "status": "queued", "title": "Waiting...", "error": None, "file_path": None} for url in urls]
    }
    thread = threading.Thread(target=batch_download_task, args=(batch_id, urls, quality))
    thread.daemon = True
    thread.start()
    return jsonify({"batch_id": batch_id, "total": len(urls), "status": "processing", "jobs": [{"job_id": j["job_id"], "url": j["url"], "status": "queued"} for j in batch_queue[batch_id]["jobs"]]})


@app.route("/batch_status/<batch_id>", methods=["GET"])
def batch_status(batch_id):
    batch = batch_queue.get(batch_id)
    if not batch:
        return jsonify({"error": "Batch not found"}), 404
    return jsonify({
        "batch_id": batch_id, "status": batch["status"], "total": batch["total"],
        "completed": batch["completed"], "failed": batch["failed"], "current_index": batch["current_index"],
        "jobs": [{"job_id": j["job_id"], "url": j["url"], "status": j["status"], "title": j["title"], "error": j.get("error")} for j in batch["jobs"]]
    })


@app.route("/batch_download/<batch_id>", methods=["GET"])
def batch_download(batch_id):
    batch = batch_queue.get(batch_id)
    if not batch:
        return jsonify({"error": "Batch not found"}), 404
    files_to_zip = []
    for job in batch["jobs"]:
        if job["status"] == "done" and job.get("file_path"):
            file_path = Path(job["file_path"])
            if file_path.exists():
                files_to_zip.append((file_path, safe_filename(job.get("title", "audio"))))
    if not files_to_zip:
        return jsonify({"error": "No completed files to download"}), 400
    zip_path = DOWNLOAD_DIR / f"batch_{batch_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path, filename in files_to_zip:
            zf.write(file_path, filename)
    return send_file(zip_path, mimetype="application/zip", as_attachment=True, download_name=f"youtube_mp3_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")


@app.route("/status/<job_id>", methods=["GET"])
def get_status(job_id):
    job = job_queue.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job_id": job_id, "status": job["status"], "title": job.get("title"), "error": job.get("error"), "quality": job.get("quality")})


@app.route("/download_job/<job_id>", methods=["GET"])
def download_job(job_id):
    job = job_queue.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] != "done":
        return jsonify({"error": f"Job status: {job['status']}"}), 400
    file_path = Path(job["file_path"])
    if not file_path.exists():
        return jsonify({"error": "File not found"}), 404
    title = job.get("title", "media")
    safe_name = safe_filename(title)
    return send_file(file_path, mimetype="audio/mpeg", as_attachment=True, download_name=safe_name)


HOME_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>YouTube → MP3 | Premium Audio Converter</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #050510;
      --bg-dark: #010104;
      --card: rgba(10, 10, 20, 0.6);
      --text: #ffffff;
      --text-dim: #b4b8c5;
      --text-muted: #6b7280;
      --primary: #6366f1;
      --primary-light: #818cf8;
      --primary-dark: #4f46e5;
      --accent: #f0abfc;
      --accent-2: #fbbf24;
      --gradient-1: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      --gradient-rainbow: linear-gradient(90deg, #ff6b6b, #feca57, #48dbfb, #ff9ff3, #6c5ce7);
      --success: #10b981;
      --warning: #fbbf24;
      --error: #ef4444;
      --glow-primary: 0 0 60px rgba(99, 102, 241, 0.5);
      --shadow-2xl: 0 25px 80px rgba(0, 0, 0, 0.9);
      --border: rgba(255, 255, 255, 0.08);
      --border-light: rgba(255, 255, 255, 0.15);
      --transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg-dark);
      color: var(--text);
      min-height: 100vh;
      overflow-x: hidden;
    }
    .universe-bg {
      position: fixed;
      inset: 0;
      z-index: 0;
      background: 
        radial-gradient(ellipse at top left, rgba(99, 102, 241, 0.15) 0%, transparent 40%),
        radial-gradient(ellipse at bottom right, rgba(240, 171, 252, 0.15) 0%, transparent 40%),
        linear-gradient(180deg, var(--bg-dark) 0%, var(--bg) 100%);
    }
    .stars { position: fixed; inset: 0; z-index: 1; }
    .star {
      position: absolute;
      width: 2px;
      height: 2px;
      background: white;
      border-radius: 50%;
      animation: twinkle 3s ease-in-out infinite;
    }
    @keyframes twinkle {
      0%, 100% { opacity: 0; transform: scale(0.5); }
      50% { opacity: 1; transform: scale(1); }
    }
    .particles { position: fixed; inset: 0; z-index: 2; pointer-events: none; }
    .particle {
      position: absolute;
      width: 4px;
      height: 4px;
      background: var(--primary-light);
      border-radius: 50%;
      filter: blur(1px);
      animation: floatUp 20s linear infinite;
    }
    @keyframes floatUp {
      0% { transform: translateY(100vh) scale(0); opacity: 0; }
      10% { opacity: 0.8; }
      90% { opacity: 0.8; }
      100% { transform: translateY(-100vh) translateX(100px) scale(1.5); opacity: 0; }
    }
    .gradient-orbs { position: fixed; inset: 0; z-index: 1; filter: blur(100px); opacity: 0.5; }
    .orb { position: absolute; border-radius: 50%; }
    .orb1 {
      width: 600px; height: 600px;
      background: radial-gradient(circle, var(--primary) 0%, transparent 70%);
      top: -300px; left: -300px;
      animation: floatOrb1 25s ease-in-out infinite;
    }
    .orb2 {
      width: 500px; height: 500px;
      background: radial-gradient(circle, var(--accent) 0%, transparent 70%);
      bottom: -250px; right: -250px;
      animation: floatOrb2 30s ease-in-out infinite;
    }
    @keyframes floatOrb1 {
      0%, 100% { transform: translate(0, 0); }
      50% { transform: translate(100px, 50px); }
    }
    @keyframes floatOrb2 {
      0%, 100% { transform: translate(0, 0); }
      50% { transform: translate(-100px, -50px); }
    }
    .grid-bg {
      position: fixed;
      inset: 0;
      z-index: 1;
      background-image: 
        linear-gradient(rgba(99, 102, 241, 0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(99, 102, 241, 0.03) 1px, transparent 1px);
      background-size: 50px 50px;
    }
    .container {
      position: relative;
      z-index: 10;
      max-width: 800px;
      margin: 0 auto;
      padding: 60px 24px;
      min-height: 100vh;
    }
    .header { text-align: center; margin-bottom: 40px; }
    .logo-wave {
      width: 100px; height: 100px;
      margin: 0 auto 24px;
      display: flex;
      align-items: center;
      justify-content: center;
      background: linear-gradient(135deg, rgba(99, 102, 241, 0.2) 0%, rgba(240, 171, 252, 0.2) 100%);
      border-radius: 24px;
      border: 1px solid var(--border-light);
    }
    .sound-bars { display: flex; align-items: center; gap: 4px; height: 50px; }
    .sound-bar {
      width: 6px;
      background: var(--gradient-1);
      border-radius: 3px;
      animation: soundWave 1.2s ease-in-out infinite;
    }
    .sound-bar:nth-child(1) { height: 20px; animation-delay: 0s; }
    .sound-bar:nth-child(2) { height: 35px; animation-delay: 0.1s; }
    .sound-bar:nth-child(3) { height: 45px; animation-delay: 0.2s; }
    .sound-bar:nth-child(4) { height: 40px; animation-delay: 0.3s; }
    .sound-bar:nth-child(5) { height: 30px; animation-delay: 0.4s; }
    @keyframes soundWave {
      0%, 100% { transform: scaleY(1); }
      50% { transform: scaleY(1.5); }
    }
    h1 {
      font-size: 42px;
      font-weight: 900;
      margin-bottom: 12px;
      background: var(--gradient-rainbow);
      background-size: 200% auto;
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      animation: shimmer 3s linear infinite;
    }
    @keyframes shimmer {
      0% { background-position: 0% center; }
      100% { background-position: 200% center; }
    }
    .subtitle { font-size: 18px; color: var(--text-dim); }
    .card {
      background: linear-gradient(135deg, rgba(255, 255, 255, 0.05) 0%, rgba(255, 255, 255, 0.02) 100%);
      backdrop-filter: blur(20px);
      border: 1px solid var(--border);
      border-radius: 24px;
      padding: 40px;
      box-shadow: var(--shadow-2xl);
    }
    .mode-toggle {
      display: flex;
      background: rgba(0, 0, 0, 0.3);
      border-radius: 16px;
      padding: 6px;
      margin-bottom: 28px;
      border: 1px solid var(--border);
    }
    .mode-btn {
      flex: 1;
      padding: 14px 20px;
      background: transparent;
      border: none;
      border-radius: 12px;
      color: var(--text-muted);
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      transition: var(--transition);
    }
    .mode-btn.active {
      background: var(--gradient-1);
      color: white;
      box-shadow: 0 4px 15px rgba(99, 102, 241, 0.4);
    }
    .mode-btn:hover:not(.active) { color: var(--text); background: rgba(255,255,255,0.05); }
    .input-group { margin-bottom: 24px; }
    .input-label { display: block; color: var(--text-dim); font-size: 14px; font-weight: 500; margin-bottom: 10px; }
    input[type="url"], input[type="text"], textarea {
      width: 100%;
      padding: 18px 20px;
      background: rgba(0, 0, 0, 0.4);
      border: 2px solid var(--border);
      border-radius: 16px;
      color: var(--text);
      font-size: 15px;
      font-family: inherit;
      transition: var(--transition);
    }
    textarea { min-height: 140px; resize: vertical; line-height: 1.6; }
    input:focus, textarea:focus {
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 4px rgba(99, 102, 241, 0.15);
    }
    input::placeholder, textarea::placeholder { color: var(--text-muted); }
    .quality-row { display: flex; gap: 12px; margin-bottom: 28px; flex-wrap: wrap; }
    .quality-option { flex: 1; min-width: 80px; }
    .quality-option input[type="radio"] { display: none; }
    .quality-option label {
      display: block;
      padding: 14px 12px;
      background: rgba(0, 0, 0, 0.3);
      border: 2px solid var(--border);
      border-radius: 12px;
      text-align: center;
      font-size: 14px;
      font-weight: 600;
      color: var(--text-muted);
      cursor: pointer;
      transition: var(--transition);
    }
    .quality-option input[type="radio"]:checked + label {
      background: rgba(99, 102, 241, 0.2);
      border-color: var(--primary);
      color: var(--primary-light);
      box-shadow: 0 0 20px rgba(99, 102, 241, 0.2);
    }
    .quality-option label:hover { border-color: var(--primary-light); }
    .btn-convert {
      width: 100%;
      padding: 20px 32px;
      background: var(--gradient-1);
      border: none;
      border-radius: 16px;
      color: white;
      font-size: 17px;
      font-weight: 700;
      cursor: pointer;
      transition: var(--transition);
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 12px;
      text-transform: uppercase;
      letter-spacing: 1px;
      box-shadow: 0 8px 30px rgba(99, 102, 241, 0.4);
    }
    .btn-convert:hover { transform: translateY(-2px); box-shadow: 0 12px 40px rgba(99, 102, 241, 0.5); }
    .btn-convert:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
    .btn-convert svg { width: 22px; height: 22px; }
    .single-input { display: block; }
    .batch-input { display: none; }
    .mode-batch .single-input { display: none; }
    .mode-batch .batch-input { display: block; }
    .status-display {
      display: none;
      align-items: center;
      gap: 14px;
      padding: 18px 20px;
      background: rgba(0, 0, 0, 0.3);
      border-radius: 14px;
      margin-top: 20px;
      border: 1px solid var(--border);
    }
    .status-display.active { display: flex; }
    .status-dot {
      width: 12px; height: 12px;
      border-radius: 50%;
      background: var(--text-muted);
      flex-shrink: 0;
    }
    .status-dot.processing { background: var(--warning); animation: pulse 1.5s infinite; }
    .status-dot.success { background: var(--success); }
    .status-dot.error { background: var(--error); }
    @keyframes pulse {
      0%, 100% { transform: scale(1); opacity: 1; }
      50% { transform: scale(1.2); opacity: 0.7; }
    }
    .status-text { color: var(--text-dim); font-size: 15px; }
    .batch-queue { margin-top: 24px; display: none; }
    .batch-queue.active { display: block; }
    .batch-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
    .batch-title { font-size: 16px; font-weight: 600; color: var(--text); }
    .batch-progress { font-size: 14px; color: var(--text-muted); }
    .batch-list { display: flex; flex-direction: column; gap: 10px; max-height: 350px; overflow-y: auto; }
    .batch-item {
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 16px 18px;
      background: rgba(0, 0, 0, 0.25);
      border: 1px solid var(--border);
      border-radius: 14px;
      transition: var(--transition);
    }
    .batch-item.downloading { border-color: var(--warning); background: rgba(251, 191, 36, 0.1); }
    .batch-item.done { border-color: var(--success); background: rgba(16, 185, 129, 0.1); }
    .batch-item.error { border-color: var(--error); background: rgba(239, 68, 68, 0.1); }
    .batch-status-icon {
      width: 28px; height: 28px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }
    .batch-status-icon.queued { background: var(--text-muted); }
    .batch-status-icon.downloading { background: var(--warning); animation: pulse 1.5s infinite; }
    .batch-status-icon.done { background: var(--success); }
    .batch-status-icon.error { background: var(--error); }
    .batch-status-icon svg { width: 14px; height: 14px; fill: white; }
    .batch-info { flex: 1; min-width: 0; }
    .batch-item-title {
      font-size: 14px;
      font-weight: 500;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .batch-item-url {
      font-size: 12px;
      color: var(--text-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .batch-item-download {
      padding: 10px 14px;
      background: var(--success);
      border: none;
      border-radius: 10px;
      color: white;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      opacity: 0;
      transition: var(--transition);
    }
    .batch-item.done .batch-item-download { opacity: 1; }
    .batch-item-download:hover { transform: scale(1.05); }
    .download-all {
      margin-top: 18px;
      background: linear-gradient(135deg, var(--success), #059669);
      display: none;
    }
    .download-all.active { display: flex; }
    .toast {
      position: fixed;
      bottom: 30px;
      left: 50%;
      transform: translateX(-50%) translateY(100px);
      background: rgba(10, 10, 20, 0.95);
      backdrop-filter: blur(20px);
      border: 1px solid var(--border-light);
      padding: 18px 28px;
      border-radius: 16px;
      color: var(--text);
      font-size: 15px;
      font-weight: 500;
      opacity: 0;
      transition: var(--transition);
      z-index: 1000;
      box-shadow: 0 10px 40px rgba(0,0,0,0.5);
    }
    .toast.show { transform: translateX(-50%) translateY(0); opacity: 1; }
    @media (max-width: 600px) {
      .container { padding: 30px 16px; }
      .card { padding: 28px 20px; }
      h1 { font-size: 28px; }
      .quality-row { gap: 8px; }
      .quality-option { flex: 1 1 45%; }
    }
  </style>
</head>
<body>
  <div class="universe-bg"></div>
  <div class="grid-bg"></div>
  <div class="gradient-orbs">
    <div class="orb orb1"></div>
    <div class="orb orb2"></div>
  </div>
  <div class="stars" id="stars"></div>
  <div class="particles" id="particles"></div>

  <div class="container">
    <div class="header">
      <div class="logo-wave">
        <div class="sound-bars">
          <div class="sound-bar"></div>
          <div class="sound-bar"></div>
          <div class="sound-bar"></div>
          <div class="sound-bar"></div>
          <div class="sound-bar"></div>
        </div>
      </div>
      <h1>YouTube → MP3 Converter</h1>
      <p class="subtitle">Transform any YouTube video into premium quality audio</p>
    </div>

    <div class="card" id="mainCard">
      <div class="mode-toggle">
        <button type="button" class="mode-btn active" data-mode="single">Single URL</button>
        <button type="button" class="mode-btn" data-mode="batch">Batch Mode</button>
      </div>

      <form id="form">
        <div class="input-group single-input">
          <label class="input-label">YouTube URL</label>
          <input type="url" id="singleUrl" placeholder="https://www.youtube.com/watch?v=..." />
        </div>

        <div class="input-group batch-input">
          <label class="input-label">YouTube URLs (one per line, max 20)</label>
          <textarea id="batchUrls" placeholder="https://www.youtube.com/watch?v=abc123
https://youtu.be/xyz789
https://www.youtube.com/watch?v=..."></textarea>
        </div>

        <div class="input-group">
          <label class="input-label">Audio Quality</label>
          <div class="quality-row">
            <div class="quality-option">
              <input type="radio" name="quality" id="q128" value="128">
              <label for="q128">128 kbps</label>
            </div>
            <div class="quality-option">
              <input type="radio" name="quality" id="q192" value="192" checked>
              <label for="q192">192 kbps</label>
            </div>
            <div class="quality-option">
              <input type="radio" name="quality" id="q256" value="256">
              <label for="q256">256 kbps</label>
            </div>
            <div class="quality-option">
              <input type="radio" name="quality" id="q320" value="320">
              <label for="q320">320 kbps</label>
            </div>
          </div>
        </div>

        <button type="submit" class="btn-convert" id="convertBtn">
          <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
          </svg>
          <span id="btnText">Convert Now</span>
        </button>
      </form>

      <div class="status-display" id="statusDisplay">
        <div class="status-dot" id="statusDot"></div>
        <span class="status-text" id="statusText">Ready</span>
      </div>

      <div class="batch-queue" id="batchQueue">
        <div class="batch-header">
          <span class="batch-title">Download Queue</span>
          <span class="batch-progress" id="batchProgress">0 / 0</span>
        </div>
        <div class="batch-list" id="batchList"></div>
        <button type="button" class="btn-convert download-all" id="downloadAll">
          <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
          </svg>
          Download All as ZIP
        </button>
      </div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    const $ = s => document.querySelector(s);
    const $$ = s => document.querySelectorAll(s);

    const mainCard = $('#mainCard');
    const form = $('#form');
    const singleUrl = $('#singleUrl');
    const batchUrls = $('#batchUrls');
    const convertBtn = $('#convertBtn');
    const btnText = $('#btnText');
    const statusDisplay = $('#statusDisplay');
    const statusDot = $('#statusDot');
    const statusText = $('#statusText');
    const batchQueue = $('#batchQueue');
    const batchList = $('#batchList');
    const batchProgress = $('#batchProgress');
    const downloadAll = $('#downloadAll');
    const toast = $('#toast');

    let currentMode = 'single';
    let currentBatchId = null;

    // Create stars
    (function createStars() {
      const container = $('#stars');
      for (let i = 0; i < 80; i++) {
        const star = document.createElement('div');
        star.className = 'star';
        star.style.left = Math.random() * 100 + '%';
        star.style.top = Math.random() * 100 + '%';
        star.style.animationDelay = Math.random() * 3 + 's';
        container.appendChild(star);
      }
    })();

    // Create particles
    (function createParticles() {
      const container = $('#particles');
      for (let i = 0; i < 20; i++) {
        const p = document.createElement('div');
        p.className = 'particle';
        p.style.left = Math.random() * 100 + '%';
        p.style.animationDelay = Math.random() * 20 + 's';
        container.appendChild(p);
      }
    })();

    // Mode toggle
    $$('.mode-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        $$('.mode-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentMode = btn.dataset.mode;
        mainCard.className = 'card' + (currentMode === 'batch' ? ' mode-batch' : '');
        batchQueue.classList.remove('active');
        statusDisplay.classList.remove('active');
      });
    });

    function showToast(msg) {
      toast.textContent = msg;
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 3500);
    }

    function setStatus(type, msg) {
      statusDisplay.classList.add('active');
      statusDot.className = 'status-dot ' + type;
      statusText.textContent = msg;
    }

    function isValidYT(url) {
      return /^(https?:\\/\\/)?(www\\.)?(youtube\\.com|youtu\\.be)\\//i.test(url);
    }

    async function handleSingle(url, quality) {
      setStatus('processing', 'Starting conversion...');
      try {
        const resp = await fetch('/enqueue', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ url, quality })
        });
        if (!resp.ok) throw new Error('Failed to start');
        const data = await resp.json();
        const jobId = data.job_id;
        setStatus('processing', 'Converting: ' + (data.title || 'Loading...'));
        const poll = setInterval(async () => {
          const sr = await fetch('/status/' + jobId);
          const st = await sr.json();
          if (st.status === 'done') {
            clearInterval(poll);
            setStatus('success', 'Complete! Downloading...');
            showToast('🎉 Download ready!');
            window.location.href = '/download_job/' + jobId;
            convertBtn.disabled = false;
            btnText.textContent = 'Convert Now';
          } else if (st.status === 'error') {
            clearInterval(poll);
            setStatus('error', st.error || 'Conversion failed');
            showToast('❌ Conversion failed');
            convertBtn.disabled = false;
            btnText.textContent = 'Convert Now';
          } else {
            setStatus('processing', 'Converting: ' + (st.title || 'Processing...'));
          }
        }, 2000);
      } catch (e) {
        setStatus('error', 'Failed to start conversion');
        convertBtn.disabled = false;
        btnText.textContent = 'Convert Now';
      }
    }

    async function handleBatch(urls, quality) {
      batchQueue.classList.add('active');
      batchList.innerHTML = '';
      downloadAll.classList.remove('active');
      try {
        const resp = await fetch('/batch_enqueue', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ urls, quality })
        });
        if (!resp.ok) {
          const err = await resp.json();
          showToast('❌ ' + (err.error || 'Failed'));
          convertBtn.disabled = false;
          btnText.textContent = 'Convert Now';
          return;
        }
        const data = await resp.json();
        currentBatchId = data.batch_id;
        data.jobs.forEach(job => {
          const item = document.createElement('div');
          item.className = 'batch-item';
          item.id = 'job-' + job.job_id;
          item.innerHTML = '<div class="batch-status-icon queued"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/></svg></div><div class="batch-info"><div class="batch-item-title">Waiting...</div><div class="batch-item-url">' + job.url + '</div></div><button type="button" class="batch-item-download" onclick="window.location.href=\\'/download_job/' + job.job_id + '\\'">Download</button>';
          batchList.appendChild(item);
        });
        batchProgress.textContent = '0 / ' + data.total;
        const poll = setInterval(async () => {
          const sr = await fetch('/batch_status/' + currentBatchId);
          const st = await sr.json();
          batchProgress.textContent = st.completed + ' / ' + st.total;
          st.jobs.forEach(job => {
            const item = $('#job-' + job.job_id);
            if (!item) return;
            item.className = 'batch-item ' + job.status;
            item.querySelector('.batch-item-title').textContent = job.title || 'Processing...';
            const icon = item.querySelector('.batch-status-icon');
            icon.className = 'batch-status-icon ' + job.status;
            if (job.status === 'done') icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>';
            else if (job.status === 'error') icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>';
            else if (job.status === 'downloading') icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z"/></svg>';
          });
          if (st.status === 'done') {
            clearInterval(poll);
            showToast('✅ Batch complete! ' + st.completed + '/' + st.total + ' successful');
            convertBtn.disabled = false;
            btnText.textContent = 'Convert Now';
            if (st.completed > 0) downloadAll.classList.add('active');
          }
        }, 2000);
      } catch (e) {
        showToast('❌ Error starting batch');
        convertBtn.disabled = false;
        btnText.textContent = 'Convert Now';
      }
    }

    downloadAll.addEventListener('click', (e) => {
      e.preventDefault();
      if (currentBatchId) window.location.href = '/batch_download/' + currentBatchId;
    });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const quality = document.querySelector('input[name="quality"]:checked').value;
      convertBtn.disabled = true;
      btnText.textContent = 'Processing...';
      if (currentMode === 'single') {
        const url = singleUrl.value.trim();
        if (!url) {
          showToast('⚠️ Please enter a URL');
          convertBtn.disabled = false;
          btnText.textContent = 'Convert Now';
          return;
        }
        if (!isValidYT(url)) {
          showToast('❌ Invalid YouTube URL');
          convertBtn.disabled = false;
          btnText.textContent = 'Convert Now';
          return;
        }
        await handleSingle(url, quality);
      } else {
        const urls = batchUrls.value.trim();
        if (!urls) {
          showToast('⚠️ Please enter URLs');
          convertBtn.disabled = false;
          btnText.textContent = 'Convert Now';
          return;
        }
        await handleBatch(urls, quality);
      }
    });
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)