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

# Enhanced client list
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
    """Sanitize filename for safe download."""
    name = SAFE_CHARS.sub("_", name).strip() or "media"
    name = " ".join(name.split())
    if len(name) > 200:
        name = name[:200].rsplit(' ', 1)[0]
    return f"{name}.mp3"


def _base_ydl_opts(out_default: str, cookiefile: str | None, dsid: str | None, client: str, quality: str = "192"):
    """Build optimized yt-dlp options with FLEXIBLE format selection to prevent errors."""
    
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
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality,
            },
            {
                "key": "FFmpegThumbnailsConvertor",
                "format": "jpg",
            },
            {
                "key": "EmbedThumbnail",
                "already_have_thumbnail": False,
            },
            {
                "key": "FFmpegMetadata",
                "add_metadata": True,
            },
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
    """Fetch video title using multiple client fallbacks."""
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
    """Background task to download with multiple client fallbacks."""
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
                    job_queue[job_id].update({
                        "status": "done",
                        "file_path": str(downloaded_file),
                        "title": title
                    })
                    print(f"[{job_id}] ✓ Download completed with client: {client}", flush=True)
                    return
                else:
                    raise FileNotFoundError(f"Downloaded file not found: {downloaded_file}")
        
        except Exception as e:
            last_error = str(e)
            print(f"[{job_id}] ✗ Client {client} failed: {e}", flush=True)
            continue
    
    # All clients failed
    job_queue[job_id].update({
        "status": "error",
        "error": f"All clients failed. Last error: {last_error}"
    })
    print(f"[{job_id}] ✗ All clients exhausted", flush=True)


def batch_download_task(batch_id: str, urls: list, quality: str):
    """Background task to download multiple URLs sequentially."""
    batch = batch_queue[batch_id]
    
    for i, url in enumerate(urls):
        job_id = batch["jobs"][i]["job_id"]
        
        # Update batch progress
        batch["current_index"] = i
        batch["jobs"][i]["status"] = "downloading"
        
        # Create job entry
        job_queue[job_id] = {
            "status": "downloading",
            "url": url,
            "title": "Fetching...",
            "quality": quality,
            "error": None,
            "file_path": None,
            "created_at": datetime.now().isoformat()
        }
        
        # Download
        download_task(job_id, url, quality)
        
        # Update batch job status from job_queue
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
    """Health check endpoint."""
    return jsonify({"ok": True, "status": "online"})


@app.route("/enqueue", methods=["POST"])
def enqueue():
    """Enqueue a single download job."""
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
        "status": "queued",
        "url": url,
        "title": title,
        "quality": quality,
        "error": None,
        "file_path": None,
        "created_at": datetime.now().isoformat()
    }
    
    thread = threading.Thread(target=download_task, args=(job_id, url, quality))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        "job_id": job_id,
        "status": "queued",
        "title": title,
        "quality": quality
    })


@app.route("/batch_enqueue", methods=["POST"])
def batch_enqueue():
    """Enqueue multiple URLs for batch download."""
    urls_raw = request.form.get("urls", "").strip()
    quality = request.form.get("quality", "192").strip()
    
    if not urls_raw:
        return jsonify({"error": "URLs required"}), 400
    
    if quality not in ["128", "192", "256", "320"]:
        quality = "192"
    
    # Parse URLs (split by newlines, commas, or spaces)
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
    
    # Create batch entry
    batch_queue[batch_id] = {
        "status": "processing",
        "total": len(urls),
        "completed": 0,
        "failed": 0,
        "current_index": 0,
        "quality": quality,
        "created_at": datetime.now().isoformat(),
        "jobs": [
            {
                "job_id": str(uuid.uuid4()),
                "url": url,
                "status": "queued",
                "title": "Waiting...",
                "error": None,
                "file_path": None
            }
            for url in urls
        ]
    }
    
    # Start batch download in background
    thread = threading.Thread(target=batch_download_task, args=(batch_id, urls, quality))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        "batch_id": batch_id,
        "total": len(urls),
        "status": "processing",
        "jobs": [{"job_id": j["job_id"], "url": j["url"], "status": "queued"} for j in batch_queue[batch_id]["jobs"]]
    })


@app.route("/batch_status/<batch_id>", methods=["GET"])
def batch_status(batch_id):
    """Get batch download status."""
    batch = batch_queue.get(batch_id)
    if not batch:
        return jsonify({"error": "Batch not found"}), 404
    
    return jsonify({
        "batch_id": batch_id,
        "status": batch["status"],
        "total": batch["total"],
        "completed": batch["completed"],
        "failed": batch["failed"],
        "current_index": batch["current_index"],
        "jobs": [
            {
                "job_id": j["job_id"],
                "url": j["url"],
                "status": j["status"],
                "title": j["title"],
                "error": j.get("error")
            }
            for j in batch["jobs"]
        ]
    })


@app.route("/batch_download/<batch_id>", methods=["GET"])
def batch_download(batch_id):
    """Download all completed files as a ZIP."""
    batch = batch_queue.get(batch_id)
    if not batch:
        return jsonify({"error": "Batch not found"}), 404
    
    # Collect completed files
    files_to_zip = []
    for job in batch["jobs"]:
        if job["status"] == "done" and job.get("file_path"):
            file_path = Path(job["file_path"])
            if file_path.exists():
                files_to_zip.append((file_path, safe_filename(job.get("title", "audio"))))
    
    if not files_to_zip:
        return jsonify({"error": "No completed files to download"}), 400
    
    # Create ZIP file
    zip_path = DOWNLOAD_DIR / f"batch_{batch_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path, filename in files_to_zip:
            zf.write(file_path, filename)
    
    return send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"youtube_mp3_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    )


@app.route("/status/<job_id>", methods=["GET"])
def get_status(job_id):
    """Check job status."""
    job = job_queue.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    return jsonify({
        "job_id": job_id,
        "status": job["status"],
        "title": job.get("title"),
        "error": job.get("error"),
        "quality": job.get("quality")
    })


@app.route("/download_job/<job_id>", methods=["GET"])
def download_job(job_id):
    """Download the completed MP3 file from job queue."""
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
    
    return send_file(
        file_path,
        mimetype="audio/mpeg",
        as_attachment=True,
        download_name=safe_name
    )


HOME_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>YouTube → MP3 | Batch Converter</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #050510;
      --card: rgba(10, 10, 20, 0.8);
      --text: #ffffff;
      --text-dim: #b4b8c5;
      --text-muted: #6b7280;
      --primary: #6366f1;
      --primary-light: #818cf8;
      --primary-dark: #4f46e5;
      --accent: #f0abfc;
      --success: #10b981;
      --warning: #fbbf24;
      --error: #ef4444;
      --border: rgba(255, 255, 255, 0.1);
      --border-light: rgba(255, 255, 255, 0.15);
    }

    * {
      margin: 0;
      padding: 0;
      box-sizing: border-box;
    }

    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg);
      min-height: 100vh;
      color: var(--text);
      overflow-x: hidden;
    }

    /* Animated Background */
    .bg-animation {
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      z-index: -1;
      overflow: hidden;
    }

    .orb {
      position: absolute;
      border-radius: 50%;
      filter: blur(80px);
      opacity: 0.5;
      animation: float 20s infinite ease-in-out;
    }

    .orb:nth-child(1) {
      width: 600px;
      height: 600px;
      background: linear-gradient(135deg, var(--primary), var(--accent));
      top: -200px;
      left: -200px;
    }

    .orb:nth-child(2) {
      width: 400px;
      height: 400px;
      background: linear-gradient(135deg, var(--accent), var(--primary-light));
      bottom: -100px;
      right: -100px;
      animation-delay: -5s;
    }

    @keyframes float {
      0%, 100% { transform: translate(0, 0) scale(1); }
      50% { transform: translate(50px, -50px) scale(1.1); }
    }

    /* Container */
    .container {
      max-width: 700px;
      margin: 0 auto;
      padding: 40px 20px;
    }

    /* Header */
    .header {
      text-align: center;
      margin-bottom: 40px;
    }

    .logo {
      width: 80px;
      height: 80px;
      background: linear-gradient(135deg, var(--primary), var(--accent));
      border-radius: 24px;
      display: flex;
      align-items: center;
      justify-content: center;
      margin: 0 auto 24px;
      box-shadow: 0 10px 40px rgba(99, 102, 241, 0.3);
    }

    .logo svg {
      width: 40px;
      height: 40px;
      fill: white;
    }

    h1 {
      font-size: 32px;
      font-weight: 800;
      margin-bottom: 8px;
      background: linear-gradient(135deg, var(--text) 0%, var(--text-dim) 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }

    .subtitle {
      color: var(--text-muted);
      font-size: 16px;
    }

    /* Card */
    .card {
      background: var(--card);
      backdrop-filter: blur(20px);
      border: 1px solid var(--border);
      border-radius: 24px;
      padding: 32px;
    }

    /* Mode Toggle */
    .mode-toggle {
      display: flex;
      background: rgba(255, 255, 255, 0.05);
      border-radius: 12px;
      padding: 4px;
      margin-bottom: 24px;
    }

    .mode-btn {
      flex: 1;
      padding: 12px 16px;
      background: transparent;
      border: none;
      border-radius: 10px;
      color: var(--text-muted);
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.3s;
    }

    .mode-btn.active {
      background: var(--primary);
      color: white;
    }

    .mode-btn:hover:not(.active) {
      color: var(--text);
    }

    /* Input Styles */
    .input-group {
      margin-bottom: 20px;
    }

    .input-label {
      display: block;
      color: var(--text-dim);
      font-size: 14px;
      font-weight: 500;
      margin-bottom: 8px;
    }

    input, textarea {
      width: 100%;
      padding: 16px;
      background: rgba(255, 255, 255, 0.05);
      border: 2px solid var(--border);
      border-radius: 12px;
      color: var(--text);
      font-size: 15px;
      font-family: inherit;
      transition: all 0.3s;
    }

    textarea {
      min-height: 150px;
      resize: vertical;
      line-height: 1.6;
    }

    input:focus, textarea:focus {
      outline: none;
      border-color: var(--primary);
      background: rgba(99, 102, 241, 0.1);
    }

    input::placeholder, textarea::placeholder {
      color: var(--text-muted);
    }

    /* Quality Selector */
    .quality-row {
      display: flex;
      gap: 12px;
      margin-bottom: 24px;
    }

    .quality-option {
      flex: 1;
    }

    .quality-option input {
      display: none;
    }

    .quality-option label {
      display: block;
      padding: 12px;
      background: rgba(255, 255, 255, 0.05);
      border: 2px solid var(--border);
      border-radius: 10px;
      text-align: center;
      font-size: 13px;
      font-weight: 600;
      color: var(--text-muted);
      cursor: pointer;
      transition: all 0.3s;
    }

    .quality-option input:checked + label {
      background: rgba(99, 102, 241, 0.2);
      border-color: var(--primary);
      color: var(--primary-light);
    }

    .quality-option label:hover {
      border-color: var(--primary);
    }

    /* Button */
    .btn {
      width: 100%;
      padding: 18px 24px;
      background: linear-gradient(135deg, var(--primary), var(--primary-dark));
      border: none;
      border-radius: 14px;
      color: white;
      font-size: 16px;
      font-weight: 700;
      cursor: pointer;
      transition: all 0.3s;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
    }

    .btn:hover {
      transform: translateY(-2px);
      box-shadow: 0 10px 40px rgba(99, 102, 241, 0.4);
    }

    .btn:disabled {
      opacity: 0.6;
      cursor: not-allowed;
      transform: none;
    }

    .btn svg {
      width: 20px;
      height: 20px;
    }

    /* Batch Queue */
    .batch-queue {
      margin-top: 24px;
      display: none;
    }

    .batch-queue.active {
      display: block;
    }

    .batch-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
    }

    .batch-title {
      font-size: 16px;
      font-weight: 600;
      color: var(--text);
    }

    .batch-progress {
      font-size: 14px;
      color: var(--text-muted);
    }

    .batch-list {
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 400px;
      overflow-y: auto;
    }

    .batch-item {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 14px 16px;
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid var(--border);
      border-radius: 12px;
      transition: all 0.3s;
    }

    .batch-item.downloading {
      border-color: var(--warning);
      background: rgba(251, 191, 36, 0.1);
    }

    .batch-item.done {
      border-color: var(--success);
      background: rgba(16, 185, 129, 0.1);
    }

    .batch-item.error {
      border-color: var(--error);
      background: rgba(239, 68, 68, 0.1);
    }

    .batch-status-icon {
      width: 24px;
      height: 24px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }

    .batch-status-icon.queued {
      background: var(--text-muted);
    }

    .batch-status-icon.downloading {
      background: var(--warning);
      animation: pulse 1.5s infinite;
    }

    .batch-status-icon.done {
      background: var(--success);
    }

    .batch-status-icon.error {
      background: var(--error);
    }

    @keyframes pulse {
      0%, 100% { transform: scale(1); opacity: 1; }
      50% { transform: scale(1.1); opacity: 0.7; }
    }

    .batch-status-icon svg {
      width: 14px;
      height: 14px;
      fill: white;
    }

    .batch-info {
      flex: 1;
      min-width: 0;
    }

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
      padding: 8px 12px;
      background: var(--success);
      border: none;
      border-radius: 8px;
      color: white;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      opacity: 0;
      transition: all 0.3s;
    }

    .batch-item.done .batch-item-download {
      opacity: 1;
    }

    /* Download All Button */
    .download-all {
      margin-top: 16px;
      background: linear-gradient(135deg, var(--success), #059669);
      display: none;
    }

    .download-all.active {
      display: flex;
    }

    /* Single Mode Elements */
    .single-input { display: block; }
    .batch-input { display: none; }

    .mode-batch .single-input { display: none; }
    .mode-batch .batch-input { display: block; }

    /* Toast */
    .toast {
      position: fixed;
      bottom: 30px;
      left: 50%;
      transform: translateX(-50%) translateY(100px);
      background: var(--card);
      border: 1px solid var(--border);
      padding: 16px 24px;
      border-radius: 12px;
      color: var(--text);
      font-size: 14px;
      opacity: 0;
      transition: all 0.3s;
      z-index: 1000;
    }

    .toast.show {
      transform: translateX(-50%) translateY(0);
      opacity: 1;
    }

    /* Status */
    .status-display {
      display: none;
      align-items: center;
      gap: 12px;
      padding: 16px;
      background: rgba(255, 255, 255, 0.03);
      border-radius: 12px;
      margin-top: 16px;
    }

    .status-display.active {
      display: flex;
    }

    .status-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--text-muted);
    }

    .status-dot.processing {
      background: var(--warning);
      animation: pulse 1.5s infinite;
    }

    .status-dot.success {
      background: var(--success);
    }

    .status-dot.error {
      background: var(--error);
    }

    .status-text {
      color: var(--text-dim);
      font-size: 14px;
    }

    /* Responsive */
    @media (max-width: 600px) {
      .container { padding: 20px 16px; }
      .card { padding: 24px 20px; }
      h1 { font-size: 26px; }
      .quality-row { flex-wrap: wrap; }
      .quality-option { flex: 1 1 45%; }
    }
  </style>
</head>
<body>
  <div class="bg-animation">
    <div class="orb"></div>
    <div class="orb"></div>
  </div>

  <div class="container">
    <div class="header">
      <div class="logo">
        <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 14.5v-9l6 4.5-6 4.5z"/></svg>
      </div>
      <h1>YouTube → MP3 Converter</h1>
      <p class="subtitle">Convert single videos or batch download multiple at once</p>
    </div>

    <div class="card" id="mainCard">
      <!-- Mode Toggle -->
      <div class="mode-toggle">
        <button class="mode-btn active" data-mode="single">Single URL</button>
        <button class="mode-btn" data-mode="batch">Batch Mode</button>
      </div>

      <form id="form">
        <!-- Single URL Input -->
        <div class="input-group single-input">
          <label class="input-label">YouTube URL</label>
          <input type="url" id="singleUrl" placeholder="https://www.youtube.com/watch?v=..." />
        </div>

        <!-- Batch URLs Input -->
        <div class="input-group batch-input">
          <label class="input-label">YouTube URLs (one per line, max 20)</label>
          <textarea id="batchUrls" placeholder="https://www.youtube.com/watch?v=abc123
https://youtu.be/xyz789
https://www.youtube.com/watch?v=..."></textarea>
        </div>

        <!-- Quality Selection -->
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

        <button type="submit" class="btn" id="convertBtn">
          <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
          </svg>
          <span id="btnText">Convert Now</span>
        </button>
      </form>

      <!-- Single Status -->
      <div class="status-display" id="statusDisplay">
        <div class="status-dot" id="statusDot"></div>
        <span class="status-text" id="statusText">Ready</span>
      </div>

      <!-- Batch Queue -->
      <div class="batch-queue" id="batchQueue">
        <div class="batch-header">
          <span class="batch-title">Download Queue</span>
          <span class="batch-progress" id="batchProgress">0 / 0</span>
        </div>
        <div class="batch-list" id="batchList"></div>
        <button class="btn download-all" id="downloadAll">
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

    // Elements
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

    // Mode toggle
    $$('.mode-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        $$('.mode-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentMode = btn.dataset.mode;
        mainCard.className = 'card' + (currentMode === 'batch' ? ' mode-batch' : '');
        batchQueue.classList.remove('active');
        statusDisplay.classList.remove('active');
      });
    });

    // Toast
    function showToast(msg) {
      toast.textContent = msg;
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 3000);
    }

    // Status
    function setStatus(type, msg) {
      statusDisplay.classList.add('active');
      statusDot.className = 'status-dot ' + type;
      statusText.textContent = msg;
    }

    // Validate YouTube URL
    function isValidYT(url) {
      return /^(https?:\/\/)?(www\.)?(youtube\.com|youtu\.be)\//i.test(url);
    }

    // Single download
    async function handleSingle(url, quality) {
      setStatus('processing', 'Starting conversion...');
      
      const resp = await fetch('/enqueue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ url, quality })
      });
      
      if (!resp.ok) {
        setStatus('error', 'Failed to start conversion');
        return;
      }
      
      const data = await resp.json();
      const jobId = data.job_id;
      
      setStatus('processing', 'Converting: ' + (data.title || 'Loading...'));
      
      // Poll for status
      const poll = setInterval(async () => {
        const statusResp = await fetch('/status/' + jobId);
        const status = await statusResp.json();
        
        if (status.status === 'done') {
          clearInterval(poll);
          setStatus('success', 'Complete! Downloading...');
          showToast('🎉 Download ready!');
          window.location.href = '/download_job/' + jobId;
          convertBtn.disabled = false;
          btnText.textContent = 'Convert Now';
        } else if (status.status === 'error') {
          clearInterval(poll);
          setStatus('error', status.error || 'Conversion failed');
          showToast('❌ Conversion failed');
          convertBtn.disabled = false;
          btnText.textContent = 'Convert Now';
        } else {
          setStatus('processing', 'Converting: ' + (status.title || 'Processing...'));
        }
      }, 2000);
    }

    // Batch download
    async function handleBatch(urls, quality) {
      batchQueue.classList.add('active');
      batchList.innerHTML = '';
      downloadAll.classList.remove('active');
      
      const resp = await fetch('/batch_enqueue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ urls, quality })
      });
      
      if (!resp.ok) {
        const err = await resp.json();
        showToast('❌ ' + (err.error || 'Failed to start batch'));
        convertBtn.disabled = false;
        btnText.textContent = 'Convert Now';
        return;
      }
      
      const data = await resp.json();
      currentBatchId = data.batch_id;
      
      // Initialize list
      data.jobs.forEach((job, i) => {
        const item = document.createElement('div');
        item.className = 'batch-item';
        item.id = 'job-' + job.job_id;
        item.innerHTML = '<div class="batch-status-icon queued"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/></svg></div><div class="batch-info"><div class="batch-item-title">Waiting...</div><div class="batch-item-url">' + job.url + '</div></div><button class="batch-item-download" onclick="window.location.href=\'/download_job/' + job.job_id + '\'">Download</button>';
        batchList.appendChild(item);
      });
      
      batchProgress.textContent = '0 / ' + data.total;
      
      // Poll for batch status
      const poll = setInterval(async () => {
        const statusResp = await fetch('/batch_status/' + currentBatchId);
        const status = await statusResp.json();
        
        batchProgress.textContent = status.completed + ' / ' + status.total;
        
        // Update each job
        status.jobs.forEach(job => {
          const item = $('#job-' + job.job_id);
          if (!item) return;
          
          item.className = 'batch-item ' + job.status;
          item.querySelector('.batch-item-title').textContent = job.title || 'Processing...';
          
          const icon = item.querySelector('.batch-status-icon');
          icon.className = 'batch-status-icon ' + job.status;
          
          if (job.status === 'done') {
            icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>';
          } else if (job.status === 'error') {
            icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>';
          } else if (job.status === 'downloading') {
            icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z"/></svg>';
          }
        });
        
        if (status.status === 'done') {
          clearInterval(poll);
          showToast('✅ Batch complete! ' + status.completed + '/' + status.total + ' successful');
          convertBtn.disabled = false;
          btnText.textContent = 'Convert Now';
          
          if (status.completed > 0) {
            downloadAll.classList.add('active');
          }
        }
      }, 2000);
    }

    // Download all
    downloadAll.addEventListener('click', () => {
      if (currentBatchId) {
        window.location.href = '/batch_download/' + currentBatchId;
      }
    });

    // Form submit
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