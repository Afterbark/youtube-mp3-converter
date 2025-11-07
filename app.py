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
SAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')  # Only remove invalid filename chars

# Enhanced client list - prioritize clients that work best
CLIENTS_TO_TRY = [
    "android",       # Best option currently
    "ios",
    "tv_embedded",
    "mediaconnect",
    "mweb",
    "web",
]

# ---------- Job Queue System ----------
job_queue = {}  # {job_id: {status, url, title, error, mp3_path, created_at}}

def safe_filename(name: str, ext: str = "mp3") -> str:
    """Sanitize filename for safe download - preserves Unicode characters."""
    # Only remove characters that are invalid in filenames
    # Keep Arabic, Chinese, Japanese, Korean, etc.
    name = SAFE_CHARS.sub("_", name).strip() or "audio"
    # Remove multiple spaces and leading/trailing spaces
    name = " ".join(name.split())
    # Limit length to 200 chars to be safe
    if len(name) > 200:
        name = name[:200].rsplit(' ', 1)[0]  # Cut at word boundary
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
            "preferredquality": "192",
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
    """Metadata-only title fetch using the same cookies/clients."""
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
    """Last-resort title via YouTube oEmbed (no cookies)."""
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
    """Try multiple clients to avoid SABR/bot checks. Returns (title, mp3_path:str)."""
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
        
        # Download with fallback
        title, mp3_path = download_audio_with_fallback(
            url, OUT_DEFAULT, cookiefile=cookiefile, dsid=YTDLP_DATA_SYNC_ID
        )
        
        # Try to get better title if needed
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
        
        # Schedule cleanup after 5 minutes
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


# ---------- Enhanced HTML with Magnificent UI ----------
HOME_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>YouTube → MP3 Converter</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {
      --bg: #0a0e27;
      --card: rgba(17, 24, 39, 0.7);
      --text: #f9fafb;
      --muted: #9ca3af;
      --brand1: #8b5cf6;
      --brand2: #ec4899;
      --brand3: #3b82f6;
      --ok: #10b981;
      --warn: #f59e0b;
      --err: #ef4444;
      --glow: rgba(139, 92, 246, 0.4);
      --glow2: rgba(236, 72, 153, 0.3);
    }
    
    * { box-sizing: border-box; margin: 0; padding: 0; }
    
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      overflow-x: hidden;
      position: relative;
    }
    
    .bg-gradient {
      position: fixed;
      inset: 0;
      z-index: 0;
      background: 
        radial-gradient(circle at 20% 20%, rgba(139, 92, 246, 0.15), transparent 50%),
        radial-gradient(circle at 80% 80%, rgba(236, 72, 153, 0.15), transparent 50%),
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
      background: var(--brand1);
      top: -200px;
      left: -200px;
      animation-delay: 0s;
    }
    
    .orb2 {
      width: 350px;
      height: 350px;
      background: var(--brand2);
      bottom: -150px;
      right: -150px;
      animation-delay: 5s;
    }
    
    .orb3 {
      width: 300px;
      height: 300px;
      background: var(--brand3);
      top: 40%;
      right: 10%;
      animation-delay: 10s;
    }
    
    @keyframes float {
      0%, 100% { transform: translate(0, 0) scale(1); }
      33% { transform: translate(50px, -50px) scale(1.1); }
      66% { transform: translate(-30px, 30px) scale(0.9); }
    }
    
    .container {
      position: relative;
      z-index: 10;
      max-width: 1000px;
      margin: 0 auto;
      padding: 60px 24px;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      justify-content: center;
    }
    
    .header {
      text-align: center;
      margin-bottom: 60px;
      animation: fadeInDown 0.8s ease;
    }
    
    @keyframes fadeInDown {
      from { opacity: 0; transform: translateY(-30px); }
      to { opacity: 1; transform: translateY(0); }
    }
    
    .logo-container {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      margin-bottom: 24px;
      position: relative;
    }
    
    .logo {
      width: 80px;
      height: 80px;
      background: linear-gradient(135deg, var(--brand1), var(--brand2));
      border-radius: 24px;
      display: grid;
      place-items: center;
      font-size: 32px;
      font-weight: 900;
      letter-spacing: 1px;
      box-shadow: 
        0 0 60px var(--glow),
        0 20px 40px rgba(0, 0, 0, 0.4);
      animation: logoFloat 3s ease-in-out infinite;
      position: relative;
      overflow: hidden;
    }
    
    .logo::before {
      content: '';
      position: absolute;
      inset: 0;
      background: linear-gradient(45deg, transparent, rgba(255, 255, 255, 0.1), transparent);
      transform: translateX(-100%);
      animation: shine 3s ease infinite;
    }
    
    @keyframes logoFloat {
      0%, 100% { transform: translateY(0) rotate(0deg); }
      50% { transform: translateY(-10px) rotate(2deg); }
    }
    
    @keyframes shine {
      0% { transform: translateX(-100%); }
      50%, 100% { transform: translateX(200%); }
    }
    
    h1 {
      font-size: clamp(32px, 5vw, 48px);
      font-weight: 800;
      margin-bottom: 12px;
      background: linear-gradient(135deg, var(--brand1), var(--brand2), var(--brand3));
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      animation: gradientText 5s ease infinite;
      background-size: 200% 200%;
    }
    
    @keyframes gradientText {
      0%, 100% { background-position: 0% 50%; }
      50% { background-position: 100% 50%; }
    }
    
    .subtitle {
      font-size: 18px;
      color: var(--muted);
      font-weight: 500;
    }
    
    .card {
      background: var(--card);
      backdrop-filter: blur(20px) saturate(180%);
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 32px;
      padding: 48px;
      box-shadow: 
        0 20px 60px rgba(0, 0, 0, 0.5),
        inset 0 1px 0 rgba(255, 255, 255, 0.1);
      animation: fadeInUp 0.8s ease 0.2s both;
      position: relative;
      overflow: hidden;
    }
    
    .card::before {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 1px;
      background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.5), transparent);
    }
    
    @keyframes fadeInUp {
      from { opacity: 0; transform: translateY(30px); }
      to { opacity: 1; transform: translateY(0); }
    }
    
    .form-group {
      margin-bottom: 24px;
    }
    
    .input-wrapper {
      position: relative;
      display: flex;
      gap: 12px;
    }
    
    input[type="url"] {
      flex: 1;
      padding: 18px 24px;
      background: rgba(0, 0, 0, 0.3);
      border: 2px solid rgba(255, 255, 255, 0.1);
      border-radius: 16px;
      color: var(--text);
      font-size: 16px;
      outline: none;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    
    input[type="url"]::placeholder {
      color: var(--muted);
    }
    
    input[type="url"]:focus {
      border-color: var(--brand1);
      background: rgba(0, 0, 0, 0.4);
      box-shadow: 0 0 0 4px var(--glow), 0 8px 24px rgba(0, 0, 0, 0.3);
      transform: translateY(-2px);
    }
    
    button {
      padding: 18px 36px;
      background: linear-gradient(135deg, var(--brand1), var(--brand2));
      border: none;
      border-radius: 16px;
      color: white;
      font-size: 16px;
      font-weight: 700;
      cursor: pointer;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      box-shadow: 0 8px 24px var(--glow2);
      position: relative;
      overflow: hidden;
      white-space: nowrap;
    }
    
    button::before {
      content: '';
      position: absolute;
      inset: 0;
      background: linear-gradient(135deg, rgba(255, 255, 255, 0.2), transparent);
      opacity: 0;
      transition: opacity 0.3s;
    }
    
    button:hover {
      transform: translateY(-2px);
      box-shadow: 0 12px 32px var(--glow2);
    }
    
    button:hover::before {
      opacity: 1;
    }
    
    button:active {
      transform: translateY(0);
    }
    
    button:disabled {
      opacity: 0.5;
      cursor: not-allowed;
      transform: none;
    }
    
    .status {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 16px 20px;
      background: rgba(0, 0, 0, 0.3);
      border-radius: 12px;
      margin-top: 24px;
      border: 1px solid rgba(255, 255, 255, 0.05);
      min-height: 60px;
      transition: all 0.3s ease;
    }
    
    .status-dot {
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background: var(--muted);
      position: relative;
      flex-shrink: 0;
    }
    
    .status-dot::after {
      content: '';
      position: absolute;
      inset: -4px;
      border-radius: 50%;
      border: 2px solid currentColor;
      opacity: 0;
      animation: ping 2s cubic-bezier(0, 0, 0.2, 1) infinite;
    }
    
    .status-dot.active::after {
      opacity: 0.75;
    }
    
    @keyframes ping {
      75%, 100% { transform: scale(2); opacity: 0; }
    }
    
    .status-dot.ok { background: var(--ok); color: var(--ok); }
    .status-dot.warn { background: var(--warn); color: var(--warn); }
    .status-dot.err { background: var(--err); color: var(--err); }
    
    .status-text {
      flex: 1;
      font-size: 15px;
      font-weight: 500;
    }
    
    .spinner {
      width: 20px;
      height: 20px;
      border: 3px solid rgba(255, 255, 255, 0.2);
      border-top-color: var(--brand1);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
    
    .progress-container {
      margin-top: 24px;
      display: none;
      opacity: 0;
      transition: opacity 0.3s ease;
    }
    
    .progress-container.show {
      display: block;
      opacity: 1;
    }
    
    .progress-bar {
      height: 6px;
      background: rgba(255, 255, 255, 0.1);
      border-radius: 999px;
      overflow: hidden;
      position: relative;
    }
    
    .progress-fill {
      height: 100%;
      background: linear-gradient(90deg, var(--brand1), var(--brand2), var(--brand3));
      background-size: 200% 100%;
      border-radius: 999px;
      animation: progressFlow 1.5s ease infinite;
      width: 100%;
    }
    
    @keyframes progressFlow {
      0% { transform: translateX(-100%); }
      100% { transform: translateX(100%); }
    }
    
    .features {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 16px;
      margin-top: 32px;
    }
    
    .feature {
      padding: 16px 20px;
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 12px;
      font-size: 14px;
      color: var(--muted);
      text-align: center;
      transition: all 0.3s ease;
      animation: fadeInUp 0.6s ease backwards;
    }
    
    .feature:nth-child(1) { animation-delay: 0.3s; }
    .feature:nth-child(2) { animation-delay: 0.4s; }
    .feature:nth-child(3) { animation-delay: 0.5s; }
    .feature:nth-child(4) { animation-delay: 0.6s; }
    
    .feature:hover {
      background: rgba(255, 255, 255, 0.05);
      border-color: rgba(255, 255, 255, 0.15);
      transform: translateY(-2px);
    }
    
    .actions {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-top: 24px;
      flex-wrap: wrap;
    }
    
    .link {
      color: var(--brand1);
      text-decoration: none;
      font-weight: 600;
      font-size: 14px;
      transition: all 0.2s ease;
      position: relative;
    }
    
    .link::after {
      content: '';
      position: absolute;
      bottom: -2px;
      left: 0;
      width: 0;
      height: 2px;
      background: var(--brand1);
      transition: width 0.3s ease;
    }
    
    .link:hover::after {
      width: 100%;
    }
    
    .health-badge {
      padding: 8px 16px;
      background: rgba(16, 185, 129, 0.1);
      border: 1px solid rgba(16, 185, 129, 0.3);
      border-radius: 999px;
      font-size: 13px;
      color: var(--ok);
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    
    .health-badge.loading {
      background: rgba(156, 163, 175, 0.1);
      border-color: rgba(156, 163, 175, 0.3);
      color: var(--muted);
    }
    
    .health-badge.error {
      background: rgba(239, 68, 68, 0.1);
      border-color: rgba(239, 68, 68, 0.3);
      color: var(--err);
    }
    
    .toast {
      position: fixed;
      bottom: 32px;
      left: 50%;
      transform: translateX(-50%) translateY(100px);
      padding: 16px 24px;
      background: rgba(17, 24, 39, 0.95);
      backdrop-filter: blur(20px);
      border: 1px solid rgba(255, 255, 255, 0.2);
      border-radius: 16px;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5);
      color: var(--text);
      font-weight: 500;
      z-index: 1000;
      opacity: 0;
      transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
    }
    
    .toast.show {
      opacity: 1;
      transform: translateX(-50%) translateY(0);
    }
    
    .footer {
      text-align: center;
      margin-top: 48px;
      color: var(--muted);
      font-size: 14px;
      animation: fadeInUp 0.8s ease 0.4s both;
    }
    
    @media (max-width: 768px) {
      .container { padding: 40px 16px; }
      .card { padding: 32px 24px; }
      h1 { font-size: 32px; }
      .input-wrapper { flex-direction: column; }
      button { width: 100%; }
      .actions { flex-direction: column; align-items: stretch; }
      .features { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="bg-gradient"></div>
  <div class="orb orb1"></div>
  <div class="orb orb2"></div>
  <div class="orb orb3"></div>

  <div class="container">
    <div class="header">
      <div class="logo-container">
        <div class="logo">MP3</div>
      </div>
      <h1>YouTube → MP3 Converter</h1>
      <p class="subtitle">Convert any YouTube video to high-quality MP3 in seconds</p>
    </div>

    <div class="card">
      <form id="form">
        <div class="form-group">
          <div class="input-wrapper">
            <input 
              id="url" 
              type="url" 
              required 
              placeholder="Paste YouTube URL here..."
              autocomplete="off"
            />
            <button id="convertBtn" type="submit">
              <span id="btnText">Convert</span>
            </button>
          </div>
        </div>

        <div class="actions">
          <div style="display: flex; gap: 16px; align-items: center; flex-wrap: wrap;">
            <a href="#" id="sampleLink" class="link">Try sample video</a>
            <span class="health-badge loading" id="healthBadge">
              <span class="spinner" style="width: 12px; height: 12px; border-width: 2px;"></span>
              Checking...
            </span>
          </div>
          <a href="chrome://extensions" class="link" target="_blank">Chrome Extension</a>
        </div>

        <div class="progress-container" id="progressContainer">
          <div class="progress-bar">
            <div class="progress-fill"></div>
          </div>
        </div>

        <div class="status">
          <div class="status-dot" id="statusDot"></div>
          <span class="status-text" id="statusText">Ready to convert</span>
        </div>
      </form>

      <div class="features">
        <div class="feature">🎵 192 kbps MP3</div>
        <div class="feature">⚡ Lightning Fast</div>
        <div class="feature">🔄 Smart Fallbacks</div>
        <div class="feature">📱 Mobile Friendly</div>
      </div>
    </div>

    <div class="footer">
      <p>Powered by yt-dlp • FFmpeg • Flask</p>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    const $ = (sel) => document.querySelector(sel);
    const statusDot = $('#statusDot');
    const statusText = $('#statusText');
    const progressContainer = $('#progressContainer');
    const toast = $('#toast');
    const form = $('#form');
    const urlInput = $('#url');
    const convertBtn = $('#convertBtn');
    const btnText = $('#btnText');
    const healthBadge = $('#healthBadge');
    const sampleLink = $('#sampleLink');

    function setStatus(type, message, showSpinner = false) {
      statusText.textContent = message;
      statusDot.className = 'status-dot';
      statusDot.classList.remove('active');
      
      if (type === 'ok') statusDot.classList.add('ok');
      else if (type === 'warn') { statusDot.classList.add('warn'); statusDot.classList.add('active'); }
      else if (type === 'err') statusDot.classList.add('err');
      
      if (showSpinner) {
        statusText.innerHTML = '<div class="spinner" style="display: inline-block; vertical-align: middle; margin-right: 8px;"></div>' + message;
      }
    }

    function showToast(message) {
      toast.textContent = message;
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 3000);
    }

    function isValidYouTubeURL(url) {
      return /^(https?:\/\/)?(www\.)?(youtube\.com|youtu\.be)\//i.test(url);
    }

    fetch('/health')
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data && data.ok) {
          healthBadge.className = 'health-badge';
          healthBadge.innerHTML = '<div style="width: 8px; height: 8px; background: var(--ok); border-radius: 50%;"></div>Server Online';
        } else {
          throw new Error();
        }
      })
      .catch(() => {
        healthBadge.className = 'health-badge error';
        healthBadge.textContent = 'Server Offline';
      });

    sampleLink.addEventListener('click', (e) => {
      e.preventDefault();
      urlInput.value = 'http://www.youtube.com/watch?v=JK_hBk2f01k';
      showToast('✨ Sample video loaded');
      urlInput.focus();
    });

    async function tryEnqueue(url) {
      try {
        const resp = await fetch('/enqueue', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ url })
        });
        if (!resp.ok) return null;
        const data = await resp.json();
        return data.job_id || null;
      } catch (e) {
        return null;
      }
    }

    async function pollJob(jobId) {
      progressContainer.classList.add('show');
      const startTime = Date.now();
      
      const interval = setInterval(async () => {
        try {
          const resp = await fetch('/status/' + jobId);
          if (!resp.ok) {
            clearInterval(interval);
            setStatus('err', 'Status check failed');
            progressContainer.classList.remove('show');
            convertBtn.disabled = false;
            btnText.textContent = 'Convert';
            return;
          }
          
          const status = await resp.json();
          const elapsed = Math.floor((Date.now() - startTime) / 1000);
          
          if (status.status === 'done') {
            clearInterval(interval);
            progressContainer.classList.remove('show');
            setStatus('ok', '✓ Ready! Starting download...');
            convertBtn.disabled = false;
            btnText.textContent = 'Convert';
            showToast('🎉 Conversion complete!');
            
            // Use direct location instead of window.open to avoid popup blocker
            window.location.href = '/download_job/' + jobId;
          } else if (status.status === 'error') {
            clearInterval(interval);
            progressContainer.classList.remove('show');
            setStatus('err', status.error || 'Conversion failed');
            convertBtn.disabled = false;
            btnText.textContent = 'Convert';
          } else {
            const minutes = Math.floor(elapsed / 60);
            const seconds = elapsed % 60;
            const timeStr = minutes > 0 ? minutes + 'm ' + seconds + 's' : seconds + 's';
            setStatus('warn', 'Converting... ' + timeStr, true);
          }
        } catch (e) {
          clearInterval(interval);
          progressContainer.classList.remove('show');
          setStatus('err', 'Connection error');
          convertBtn.disabled = false;
          btnText.textContent = 'Convert';
        }
      }, 2000);
    }

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      
      const url = urlInput.value.trim();
      if (!url) {
        setStatus('warn', 'Please paste a YouTube URL');
        urlInput.focus();
        return;
      }
      
      if (!isValidYouTubeURL(url)) {
        setStatus('warn', 'Invalid YouTube URL');
        showToast('❌ Please enter a valid YouTube link');
        return;
      }

      convertBtn.disabled = true;
      btnText.textContent = 'Processing...';
      setStatus('warn', 'Starting conversion...', true);

      // Try queue system first (for long videos)
      const jobId = await tryEnqueue(url);
      
      if (jobId) {
        showToast('✓ Conversion queued - this may take a few minutes');
        await pollJob(jobId);
      } else {
        // Fallback to direct download (for short videos)
        setStatus('warn', 'Converting directly...', true);
        progressContainer.classList.add('show');
        
        const downloadUrl = '/download?url=' + encodeURIComponent(url);
        window.open(downloadUrl, '_blank');
        
        setTimeout(() => {
          progressContainer.classList.remove('show');
          setStatus('ok', 'Download started in new tab');
          convertBtn.disabled = false;
          btnText.textContent = 'Convert';
        }, 3000);
      }
    });

    try {
      const params = new URLSearchParams(location.search);
      const urlParam = params.get('url');
      if (urlParam) {
        urlInput.value = urlParam;
        setStatus('ok', 'URL loaded from link');
      }
    } catch (e) {}
  </script>
</body>
</html>
"""


# ---------- API Routes ----------

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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))