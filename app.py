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
SAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Enhanced client list
CLIENTS_TO_TRY = [
    "android",
    "ios",
    "tv_embedded",
    "mediaconnect",
    "mweb",
    "web",
]

# ---------- Job Queue System ----------
job_queue = {}

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
        "format": "ba/b",  # Best audio, or best overall if audio-only not available
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
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": quality,
        }],
        "extractor_args": {
            "youtube": {
                "player_client": [client],
                "player_skip": ["configs", "webpage"],
            }
        },
        "http_headers": {
            "User-Agent": "com.google.android.youtube/19.09.37 (Linux; U; Android 11) gzip" if client == "android"
                         else "com.google.ios.youtube/19.09.3 (iPhone14,3; U; CPU iOS 15_6 like Mac OS X)" if client == "ios"
                         else "Mozilla/5.0 (SMART-TV; Linux; Tizen 2.4.0) AppleWebKit/538.1 (KHTML, like Gecko) Version/2.4.0 TV Safari/538.1" if client == "tv_embedded"
                         else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
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


def _resolve_mp3_path(job_id: str, url: str, quality: str) -> Path:
    """Resolve the final downloaded MP3 file path."""
    cookiefile = str(COOKIE_PATH) if COOKIE_PATH else None
    dsid = YTDLP_DATA_SYNC_ID
    
    for client in CLIENTS_TO_TRY:
        try:
            opts = _base_ydl_opts(OUT_DEFAULT, cookiefile, dsid, client, quality)
            opts["skip_download"] = True
            opts["quiet"] = True
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                video_id = info.get("id", "unknown")
                return DOWNLOAD_DIR / f"yt_{video_id}.mp3"
        except Exception as e:
            print(f"Client {client} failed for path resolution: {e}", flush=True)
            continue
    
    # Fallback
    return DOWNLOAD_DIR / f"yt_{job_id}.mp3"


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
                        "status": "completed",
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


@app.route("/")
def home():
    return render_template_string(HOME_HTML)


@app.route("/enqueue", methods=["POST"])
def enqueue():
    """Enqueue a download job with quality options."""
    url = request.form.get("url", "").strip()
    quality = request.form.get("quality", "192").strip()
    
    if not url:
        return jsonify({"error": "URL required"}), 400
    
    # Validate quality
    if quality not in ["128", "192", "256", "320"]:
        quality = "192"
    
    job_id = str(uuid.uuid4())
    
    # Fetch title
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
    
    # Start download in background
    thread = threading.Thread(target=download_task, args=(job_id, url, quality))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        "job_id": job_id,
        "status": "queued",
        "title": title,
        "quality": quality
    })


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


@app.route("/download/<job_id>", methods=["GET"])
def download_file(job_id):
    """Download the completed MP3 file."""
    job = job_queue.get(job_id)
    
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    if job["status"] != "completed":
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


# ---------- Magnificent UI ----------
HOME_HTML = """
<!doctype html>
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
      --card-hover: rgba(15, 15, 30, 0.7);
      --text: #ffffff;
      --text-dim: #b4b8c5;
      --text-muted: #6b7280;
      
      --primary: #6366f1;
      --primary-light: #818cf8;
      --primary-dark: #4f46e5;
      
      --accent: #f0abfc;
      --accent-2: #fbbf24;
      --accent-3: #34d399;
      
      --gradient-1: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      --gradient-2: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
      --gradient-3: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
      
      --success: #10b981;
      --warning: #fbbf24;
      --error: #ef4444;
      
      --glow-primary: 0 0 60px rgba(99, 102, 241, 0.5);
      --glow-accent: 0 0 60px rgba(240, 171, 252, 0.4);
      
      --shadow-xl: 0 20px 60px rgba(0, 0, 0, 0.8);
      --shadow-2xl: 0 25px 80px rgba(0, 0, 0, 0.9);
      
      --border: rgba(255, 255, 255, 0.08);
      --border-light: rgba(255, 255, 255, 0.15);
    }
    
    * { box-sizing: border-box; margin: 0; padding: 0; }
    
    body {
      margin: 0;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
      background: var(--bg-dark);
      color: var(--text);
      min-height: 100vh;
      overflow-x: hidden;
      position: relative;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }
    
    /* Animated Background */
    .bg-gradient {
      position: fixed;
      inset: 0;
      z-index: 0;
      background: 
        radial-gradient(circle at 20% 20%, rgba(99, 102, 241, 0.15), transparent 50%),
        radial-gradient(circle at 80% 80%, rgba(240, 171, 252, 0.15), transparent 50%),
        radial-gradient(circle at 50% 50%, rgba(59, 130, 246, 0.1), transparent 70%);
      animation: gradientShift 20s ease infinite;
    }
    
    @keyframes gradientShift {
      0%, 100% { opacity: 1; transform: scale(1) rotate(0deg); }
      50% { opacity: 0.8; transform: scale(1.1) rotate(5deg); }
    }
    
    .orb {
      position: fixed;
      border-radius: 50%;
      filter: blur(80px);
      opacity: 0.3;
      pointer-events: none;
      z-index: 1;
      animation: float 15s ease-in-out infinite;
    }
    
    .orb1 {
      width: 400px;
      height: 400px;
      background: var(--primary);
      top: -200px;
      left: -200px;
      animation-delay: 0s;
    }
    
    .orb2 {
      width: 350px;
      height: 350px;
      background: var(--accent);
      bottom: -150px;
      right: -150px;
      animation-delay: 5s;
    }
    
    .orb3 {
      width: 300px;
      height: 300px;
      background: #3b82f6;
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%);
      animation-delay: 10s;
    }
    
    @keyframes float {
      0%, 100% { transform: translate(0, 0) scale(1); }
      33% { transform: translate(30px, -30px) scale(1.1); }
      66% { transform: translate(-30px, 30px) scale(0.9); }
    }
    
    .container {
      position: relative;
      z-index: 10;
      max-width: 680px;
      margin: 0 auto;
      padding: 60px 24px;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      justify-content: center;
    }
    
    .card {
      background: var(--card);
      backdrop-filter: blur(20px) saturate(180%);
      border-radius: 32px;
      padding: 56px 48px;
      box-shadow: var(--shadow-2xl);
      border: 1px solid var(--border);
      transition: all 0.4s ease;
      position: relative;
      overflow: hidden;
    }
    
    .card::before {
      content: '';
      position: absolute;
      inset: 0;
      background: radial-gradient(circle at 50% 0%, rgba(99, 102, 241, 0.1), transparent 50%);
      opacity: 0;
      transition: opacity 0.4s ease;
    }
    
    .card:hover::before {
      opacity: 1;
    }
    
    /* Elegant Sound Wave Logo */
    .logo-container {
      display: inline-block;
      margin-bottom: 32px;
      position: relative;
    }
    
    .logo-wave {
      width: 120px;
      height: 120px;
      position: relative;
      display: flex;
      align-items: center;
      justify-content: center;
      background: linear-gradient(135deg, rgba(99, 102, 241, 0.1) 0%, rgba(240, 171, 252, 0.1) 100%);
      border-radius: 30px;
      backdrop-filter: blur(10px);
      border: 1px solid rgba(255, 255, 255, 0.1);
      box-shadow: 
        0 8px 32px rgba(99, 102, 241, 0.3),
        inset 0 1px 0 rgba(255, 255, 255, 0.2);
      overflow: hidden;
    }
    
    .logo-wave::before {
      content: '';
      position: absolute;
      inset: 0;
      background: radial-gradient(circle at center, transparent 30%, rgba(99, 102, 241, 0.1) 100%);
      animation: pulseGlow 3s ease-in-out infinite;
    }
    
    @keyframes pulseGlow {
      0%, 100% { opacity: 0.5; transform: scale(1); }
      50% { opacity: 1; transform: scale(1.1); }
    }
    
    .sound-bars {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 4px;
      height: 50px;
      position: relative;
      z-index: 1;
    }
    
    .sound-bar {
      width: 6px;
      background: var(--gradient-1);
      border-radius: 3px;
      animation: soundWave 1.2s ease-in-out infinite;
      box-shadow: 0 0 10px rgba(99, 102, 241, 0.5);
    }
    
    .sound-bar:nth-child(1) { height: 20px; animation-delay: 0s; }
    .sound-bar:nth-child(2) { height: 35px; animation-delay: 0.1s; }
    .sound-bar:nth-child(3) { height: 45px; animation-delay: 0.2s; }
    .sound-bar:nth-child(4) { height: 40px; animation-delay: 0.3s; }
    .sound-bar:nth-child(5) { height: 30px; animation-delay: 0.4s; }
    .sound-bar:nth-child(6) { height: 25px; animation-delay: 0.5s; }
    .sound-bar:nth-child(7) { height: 35px; animation-delay: 0.6s; }
    
    @keyframes soundWave {
      0%, 100% { 
        transform: scaleY(1);
        opacity: 0.8;
      }
      50% { 
        transform: scaleY(1.5);
        opacity: 1;
      }
    }
    
    h1 {
      font-size: 48px;
      font-weight: 900;
      background: linear-gradient(135deg, var(--text) 0%, var(--text-dim) 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      margin-bottom: 12px;
      letter-spacing: -0.02em;
      line-height: 1.1;
    }
    
    .subtitle {
      font-size: 18px;
      color: var(--text-dim);
      margin-bottom: 48px;
      font-weight: 500;
    }
    
    .input-group {
      margin-bottom: 24px;
    }
    
    label {
      display: block;
      font-size: 14px;
      font-weight: 600;
      color: var(--text-dim);
      margin-bottom: 8px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    
    input[type="text"], select {
      width: 100%;
      padding: 16px 20px;
      font-size: 16px;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--border);
      border-radius: 16px;
      color: var(--text);
      font-family: inherit;
      transition: all 0.3s ease;
      backdrop-filter: blur(10px);
    }
    
    input[type="text"]:focus, select:focus {
      outline: none;
      border-color: var(--primary);
      background: rgba(255, 255, 255, 0.08);
      box-shadow: 0 0 0 4px rgba(99, 102, 241, 0.1);
    }
    
    input[type="text"]::placeholder {
      color: var(--text-muted);
    }
    
    select {
      cursor: pointer;
      appearance: none;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24' fill='none' stroke='%23b4b8c5' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");
      background-repeat: no-repeat;
      background-position: right 16px center;
      background-size: 20px;
      padding-right: 48px;
    }
    
    select option {
      background: var(--bg);
      color: var(--text);
    }
    
    .btn-convert {
      width: 100%;
      padding: 18px 32px;
      font-size: 16px;
      font-weight: 700;
      color: white;
      background: var(--gradient-1);
      border: none;
      border-radius: 16px;
      cursor: pointer;
      transition: all 0.3s ease;
      box-shadow: 0 4px 20px rgba(99, 102, 241, 0.4);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      position: relative;
      overflow: hidden;
    }
    
    .btn-convert::before {
      content: '';
      position: absolute;
      inset: 0;
      background: linear-gradient(135deg, rgba(255, 255, 255, 0.2), transparent);
      opacity: 0;
      transition: opacity 0.3s ease;
    }
    
    .btn-convert:hover {
      transform: translateY(-2px);
      box-shadow: 0 8px 30px rgba(99, 102, 241, 0.6);
    }
    
    .btn-convert:hover::before {
      opacity: 1;
    }
    
    .btn-convert:active {
      transform: translateY(0);
    }
    
    #status {
      margin-top: 32px;
      padding: 20px;
      border-radius: 16px;
      font-size: 15px;
      font-weight: 500;
      display: none;
      backdrop-filter: blur(10px);
      border: 1px solid var(--border);
      transition: all 0.3s ease;
    }
    
    #status.show {
      display: block;
      animation: slideDown 0.4s ease;
    }
    
    @keyframes slideDown {
      from {
        opacity: 0;
        transform: translateY(-10px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    
    #status.success {
      background: rgba(16, 185, 129, 0.1);
      border-color: var(--success);
      color: var(--success);
    }
    
    #status.error {
      background: rgba(239, 68, 68, 0.1);
      border-color: var(--error);
      color: var(--error);
    }
    
    #status.loading {
      background: rgba(251, 191, 36, 0.1);
      border-color: var(--warning);
      color: var(--warning);
    }
    
    #status a {
      color: inherit;
      font-weight: 700;
      text-decoration: underline;
      text-underline-offset: 2px;
    }
    
    #status a:hover {
      text-decoration-thickness: 2px;
    }
    
    /* Responsive Design */
    @media (max-width: 768px) {
      .container { padding: 40px 20px; }
      .card { padding: 40px 28px; }
      h1 { font-size: 36px; }
      .subtitle { font-size: 16px; }
      .logo-wave { 
        width: 90px; 
        height: 90px; 
      }
      .sound-bars { height: 40px; }
      .sound-bar:nth-child(1) { height: 15px; }
      .sound-bar:nth-child(2) { height: 25px; }
      .sound-bar:nth-child(3) { height: 35px; }
      .sound-bar:nth-child(4) { height: 30px; }
      .sound-bar:nth-child(5) { height: 22px; }
      .sound-bar:nth-child(6) { height: 18px; }
      .sound-bar:nth-child(7) { height: 26px; }
    }
  </style>
</head>
<body>
  <div class="bg-gradient"></div>
  <div class="orb orb1"></div>
  <div class="orb orb2"></div>
  <div class="orb orb3"></div>
  
  <div class="container">
    <div class="card">
      <div class="logo-container">
        <div class="logo-wave">
          <div class="sound-bars">
            <div class="sound-bar"></div>
            <div class="sound-bar"></div>
            <div class="sound-bar"></div>
            <div class="sound-bar"></div>
            <div class="sound-bar"></div>
            <div class="sound-bar"></div>
            <div class="sound-bar"></div>
          </div>
        </div>
      </div>
      
      <h1>YouTube → MP3</h1>
      <p class="subtitle">Convert videos to high-quality audio in seconds</p>
      
      <div class="input-group">
        <label for="url">YouTube URL</label>
        <input 
          type="text" 
          id="url" 
          placeholder="https://youtube.com/watch?v=..." 
          autocomplete="off"
        />
      </div>
      
      <div class="input-group">
        <label for="quality">Audio Quality</label>
        <select id="quality">
          <option value="192">192 kbps (Recommended)</option>
          <option value="128">128 kbps (Good)</option>
          <option value="256">256 kbps (High)</option>
          <option value="320">320 kbps (Maximum)</option>
        </select>
      </div>
      
      <button class="btn-convert" onclick="convert()">Convert Now</button>
      
      <div id="status"></div>
    </div>
  </div>

  <script>
    async function convert() {
      const url = document.getElementById('url').value;
      const quality = document.getElementById('quality').value;
      const status = document.getElementById('status');
      
      if (!url) {
        status.className = 'error show';
        status.textContent = '⚠️ Please enter a YouTube URL';
        return;
      }
      
      status.className = 'loading show';
      status.textContent = '⏳ Starting conversion...';
      
      try {
        const resp = await fetch('/enqueue', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: `url=${encodeURIComponent(url)}&quality=${quality}`
        });
        
        const data = await resp.json();
        
        if (data.error) {
          status.className = 'error show';
          status.textContent = '❌ Error: ' + data.error;
          return;
        }
        
        const jobId = data.job_id;
        status.className = 'loading show';
        status.textContent = `🎵 Converting: ${data.title || 'Unknown'}...`;
        
        // Poll for status
        const interval = setInterval(async () => {
          const statusResp = await fetch(`/status/${jobId}`);
          const statusData = await statusResp.json();
          
          if (statusData.status === 'completed') {
            clearInterval(interval);
            status.className = 'success show';
            status.innerHTML = `✅ Ready! <a href="/download/${jobId}">Download ${statusData.title}</a>`;
          } else if (statusData.status === 'error') {
            clearInterval(interval);
            status.className = 'error show';
            status.textContent = '❌ Error: ' + (statusData.error || 'Unknown error');
          } else {
            status.className = 'loading show';
            status.textContent = `⏳ ${statusData.status}...`;
          }
        }, 2000);
        
      } catch (e) {
        status.className = 'error show';
        status.textContent = '❌ Error: ' + e.message;
      }
    }
    
    // Parallax effect for orbs
    document.addEventListener('mousemove', (e) => {
      const { clientX, clientY } = e;
      const x = (clientX / window.innerWidth - 0.5) * 2;
      const y = (clientY / window.innerHeight - 0.5) * 2;
      
      document.querySelectorAll('.orb').forEach((orb, index) => {
        const speed = (index + 1) * 10;
        orb.style.transform = `translate(${x * speed}px, ${y * speed}px)`;
      });
    });
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)