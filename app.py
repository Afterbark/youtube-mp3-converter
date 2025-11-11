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


def _base_ydl_opts(out_default: str, cookiefile: str | None, dsid: str | None, client: str, quality: str = "192"):
    """Build optimized yt-dlp options for a specific player client and quality."""
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
            opts = _base_ydl_opts(OUT_DEFAULT, cookiefile, dsid, client, "192")  # Use default quality for metadata
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


def download_audio_with_fallback(url: str, out_default: str, cookiefile: str | None, dsid: str | None, quality: str = "192"):
    """Try multiple clients to avoid SABR/bot checks. Returns (title, mp3_path:str)."""
    last_err = None
    for idx, client in enumerate(CLIENTS_TO_TRY):
        try:
            print(f"[yt-dlp] Attempt {idx+1}/{len(CLIENTS_TO_TRY)}: client={client}, quality={quality}kbps", flush=True)
            with yt_dlp.YoutubeDL(_base_ydl_opts(out_default, cookiefile, dsid, client, quality)) as ydl:
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


def process_job(job_id: str, url: str, quality: str = "192"):
    """Background job processor."""
    try:
        job_queue[job_id]["status"] = "processing"
        cookiefile = str(COOKIE_PATH) if COOKIE_PATH and COOKIE_PATH.exists() else None
        
        # Download with fallback
        title, mp3_path = download_audio_with_fallback(
            url, OUT_DEFAULT, cookiefile=cookiefile, dsid=YTDLP_DATA_SYNC_ID, quality=quality
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
      --gradient-rainbow: linear-gradient(90deg, #ff6b6b, #feca57, #48dbfb, #ff9ff3, #6c5ce7);
      
      --success: #10b981;
      --warning: #fbbf24;
      --error: #ef4444;
      
      --glow-primary: 0 0 60px rgba(99, 102, 241, 0.5);
      --glow-accent: 0 0 60px rgba(240, 171, 252, 0.4);
      --glow-intense: 0 0 120px rgba(99, 102, 241, 0.6), 0 0 200px rgba(99, 102, 241, 0.3);
      
      --shadow-xl: 0 20px 60px rgba(0, 0, 0, 0.8);
      --shadow-2xl: 0 25px 80px rgba(0, 0, 0, 0.9);
      --shadow-glow: 0 0 100px rgba(99, 102, 241, 0.2);
      
      --border: rgba(255, 255, 255, 0.08);
      --border-light: rgba(255, 255, 255, 0.15);
      
      --transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      --transition-slow: all 0.6s cubic-bezier(0.4, 0, 0.2, 1);
      --transition-bounce: all 0.5s cubic-bezier(0.68, -0.55, 0.265, 1.55);
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
    .universe-bg {
      position: fixed;
      inset: 0;
      z-index: 0;
      background: 
        radial-gradient(ellipse at top left, rgba(99, 102, 241, 0.15) 0%, transparent 40%),
        radial-gradient(ellipse at bottom right, rgba(240, 171, 252, 0.15) 0%, transparent 40%),
        radial-gradient(ellipse at center, rgba(79, 70, 229, 0.08) 0%, transparent 60%),
        linear-gradient(180deg, var(--bg-dark) 0%, var(--bg) 100%);
    }
    
    /* Animated Stars */
    .stars {
      position: fixed;
      inset: 0;
      z-index: 1;
    }
    
    .star {
      position: absolute;
      width: 2px;
      height: 2px;
      background: white;
      border-radius: 50%;
      animation: twinkle 3s ease-in-out infinite;
      box-shadow: 0 0 6px white;
    }
    
    @keyframes twinkle {
      0%, 100% { opacity: 0; transform: scale(0.5); }
      50% { opacity: 1; transform: scale(1); }
    }
    
    /* Floating Particles */
    .particles {
      position: fixed;
      inset: 0;
      z-index: 2;
      pointer-events: none;
    }
    
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
      0% { 
        transform: translateY(100vh) translateX(0) scale(0);
        opacity: 0;
      }
      10% {
        opacity: 0.8;
      }
      90% {
        opacity: 0.8;
      }
      100% { 
        transform: translateY(-100vh) translateX(100px) scale(1.5);
        opacity: 0;
      }
    }
    
    /* Gradient Orbs */
    .gradient-orbs {
      position: fixed;
      inset: 0;
      z-index: 1;
      filter: blur(100px);
      opacity: 0.5;
    }
    
    .orb {
      position: absolute;
      border-radius: 50%;
      mix-blend-mode: screen;
    }
    
    .orb1 {
      width: 600px;
      height: 600px;
      background: radial-gradient(circle, var(--primary) 0%, transparent 70%);
      top: -300px;
      left: -300px;
      animation: floatOrb1 25s ease-in-out infinite;
    }
    
    .orb2 {
      width: 500px;
      height: 500px;
      background: radial-gradient(circle, var(--accent) 0%, transparent 70%);
      bottom: -250px;
      right: -250px;
      animation: floatOrb2 30s ease-in-out infinite;
    }
    
    .orb3 {
      width: 400px;
      height: 400px;
      background: radial-gradient(circle, var(--accent-2) 0%, transparent 70%);
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%);
      animation: floatOrb3 35s ease-in-out infinite;
    }
    
    @keyframes floatOrb1 {
      0%, 100% { transform: translate(0, 0) scale(1) rotate(0deg); }
      33% { transform: translate(100px, 50px) scale(1.1) rotate(120deg); }
      66% { transform: translate(-50px, 100px) scale(0.9) rotate(240deg); }
    }
    
    @keyframes floatOrb2 {
      0%, 100% { transform: translate(0, 0) scale(1) rotate(0deg); }
      33% { transform: translate(-100px, -50px) scale(1.2) rotate(-120deg); }
      66% { transform: translate(50px, -100px) scale(0.8) rotate(-240deg); }
    }
    
    @keyframes floatOrb3 {
      0%, 100% { transform: translate(-50%, -50%) scale(1) rotate(0deg); }
      25% { transform: translate(-45%, -55%) scale(1.1) rotate(90deg); }
      50% { transform: translate(-55%, -45%) scale(0.9) rotate(180deg); }
      75% { transform: translate(-45%, -50%) scale(1.05) rotate(270deg); }
    }
    
    /* Grid Effect */
    .grid-bg {
      position: fixed;
      inset: 0;
      z-index: 1;
      background-image: 
        linear-gradient(rgba(99, 102, 241, 0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(99, 102, 241, 0.03) 1px, transparent 1px);
      background-size: 50px 50px;
      animation: gridMove 20s linear infinite;
    }
    
    @keyframes gridMove {
      0% { transform: translate(0, 0); }
      100% { transform: translate(50px, 50px); }
    }
    
    /* Main Container */
    .container {
      position: relative;
      z-index: 10;
      max-width: 1200px;
      margin: 0 auto;
      padding: 60px 24px;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      justify-content: center;
    }
    
    /* Header */
    .header {
      text-align: center;
      margin-bottom: 60px;
      animation: fadeInDown 1s ease;
    }
    
    @keyframes fadeInDown {
      from { 
        opacity: 0; 
        transform: translateY(-40px);
      }
      to { 
        opacity: 1; 
        transform: translateY(0);
      }
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
    
    .sound-bar:nth-child(1) { 
      height: 20px; 
      animation-delay: 0s;
    }
    .sound-bar:nth-child(2) { 
      height: 35px; 
      animation-delay: 0.1s;
    }
    .sound-bar:nth-child(3) { 
      height: 45px; 
      animation-delay: 0.2s;
    }
    .sound-bar:nth-child(4) { 
      height: 40px; 
      animation-delay: 0.3s;
    }
    .sound-bar:nth-child(5) { 
      height: 30px; 
      animation-delay: 0.4s;
    }
    .sound-bar:nth-child(6) { 
      height: 25px; 
      animation-delay: 0.5s;
    }
    .sound-bar:nth-child(7) { 
      height: 35px; 
      animation-delay: 0.6s;
    }
    
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
    
    /* Animated Title */
    h1 {
      font-size: clamp(40px, 6vw, 72px);
      font-weight: 900;
      margin-bottom: 16px;
      background: var(--gradient-rainbow);
      background-size: 200% auto;
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      animation: shimmer 3s linear infinite;
      letter-spacing: -2px;
      line-height: 1;
    }
    
    @keyframes shimmer {
      0% { background-position: 0% center; }
      100% { background-position: 200% center; }
    }
    
    .subtitle {
      font-size: 20px;
      color: var(--text-dim);
      font-weight: 500;
      letter-spacing: 0.5px;
      animation: fadeIn 1s ease 0.3s both;
    }
    
    @keyframes fadeIn {
      from { opacity: 0; }
      to { opacity: 1; }
    }
    
    /* Glassmorphism Card */
    .card {
      background: linear-gradient(135deg, rgba(255, 255, 255, 0.05) 0%, rgba(255, 255, 255, 0.02) 100%);
      backdrop-filter: blur(20px) saturate(180%);
      -webkit-backdrop-filter: blur(20px) saturate(180%);
      border: 1px solid var(--border);
      border-radius: 32px;
      padding: 56px;
      box-shadow: 
        var(--shadow-2xl),
        var(--shadow-glow),
        inset 0 1px 0 rgba(255, 255, 255, 0.1);
      animation: cardEntrance 0.8s ease 0.2s both;
      position: relative;
      overflow: hidden;
      transition: var(--transition);
    }
    
    @keyframes cardEntrance {
      from { 
        opacity: 0; 
        transform: translateY(40px) scale(0.95);
      }
      to { 
        opacity: 1; 
        transform: translateY(0) scale(1);
      }
    }
    
    .card::before {
      content: '';
      position: absolute;
      top: 0;
      left: -100%;
      width: 100%;
      height: 2px;
      background: var(--gradient-rainbow);
      animation: scanLine 3s linear infinite;
    }
    
    @keyframes scanLine {
      0% { left: -100%; }
      100% { left: 100%; }
    }
    
    .card:hover {
      transform: translateY(-2px);
      box-shadow: 
        var(--shadow-2xl),
        var(--glow-intense),
        inset 0 1px 0 rgba(255, 255, 255, 0.15);
      border-color: var(--border-light);
    }
    
    /* Input Group */
    .input-group {
      margin-bottom: 32px;
      position: relative;
    }
    
    .input-wrapper {
      display: flex;
      gap: 16px;
      position: relative;
    }
    
    .input-field {
      flex: 1;
      position: relative;
    }
    
    input[type="url"] {
      width: 100%;
      padding: 20px 24px;
      padding-left: 56px;
      background: rgba(0, 0, 0, 0.4);
      border: 2px solid var(--border);
      border-radius: 20px;
      color: var(--text);
      font-size: 16px;
      font-weight: 500;
      outline: none;
      transition: var(--transition);
      letter-spacing: 0.3px;
    }
    
    input[type="url"]::placeholder {
      color: var(--text-muted);
      font-weight: 400;
    }
    
    input[type="url"]:focus {
      border-color: var(--primary);
      background: rgba(0, 0, 0, 0.6);
      box-shadow: 
        0 0 0 4px rgba(99, 102, 241, 0.1),
        var(--glow-primary);
      transform: translateY(-1px);
    }
    
    /* Quality Selector */
    .quality-selector {
      padding: 20px 16px;
      background: rgba(0, 0, 0, 0.4);
      border: 2px solid var(--border);
      border-radius: 20px;
      color: var(--text);
      font-size: 15px;
      font-weight: 600;
      outline: none;
      transition: var(--transition);
      cursor: pointer;
      min-width: 120px;
    }
    
    .quality-selector:hover {
      border-color: var(--primary-light);
      background: rgba(0, 0, 0, 0.5);
    }
    
    .quality-selector:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 4px rgba(99, 102, 241, 0.1);
    }
    
    .quality-selector option {
      background: var(--bg-dark);
      color: var(--text);
      padding: 10px;
    }
    
    /* Input Icon */
    .input-icon {
      position: absolute;
      left: 20px;
      top: 50%;
      transform: translateY(-50%);
      width: 24px;
      height: 24px;
      color: var(--text-muted);
      transition: var(--transition);
    }
    
    input:focus ~ .input-icon {
      color: var(--primary);
    }
    
    /* Animated Button */
    .btn-convert {
      padding: 20px 48px;
      background: var(--gradient-1);
      border: none;
      border-radius: 20px;
      color: white;
      font-size: 16px;
      font-weight: 700;
      cursor: pointer;
      transition: var(--transition-bounce);
      box-shadow: 
        0 10px 30px rgba(99, 102, 241, 0.4),
        inset 0 1px 0 rgba(255, 255, 255, 0.2);
      position: relative;
      overflow: hidden;
      text-transform: uppercase;
      letter-spacing: 1px;
      white-space: nowrap;
    }
    
    .btn-convert::before {
      content: '';
      position: absolute;
      top: 0;
      left: -100%;
      width: 100%;
      height: 100%;
      background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.4), transparent);
      transition: left 0.5s;
    }
    
    .btn-convert:hover::before {
      left: 100%;
    }
    
    .btn-convert:hover {
      transform: translateY(-3px) scale(1.02);
      box-shadow: 
        0 15px 40px rgba(99, 102, 241, 0.5),
        inset 0 1px 0 rgba(255, 255, 255, 0.3);
    }
    
    .btn-convert:active {
      transform: translateY(-1px) scale(1);
    }
    
    .btn-convert:disabled {
      opacity: 0.6;
      cursor: not-allowed;
      transform: none;
    }
    
    /* Status Display */
    .status-display {
      display: flex;
      align-items: center;
      gap: 16px;
      padding: 20px 24px;
      background: linear-gradient(135deg, rgba(0, 0, 0, 0.4) 0%, rgba(0, 0, 0, 0.2) 100%);
      border-radius: 16px;
      border: 1px solid var(--border);
      margin-top: 32px;
      min-height: 70px;
      transition: var(--transition);
      position: relative;
      overflow: hidden;
    }
    
    .status-display::after {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 1px;
      background: linear-gradient(90deg, transparent, var(--primary-light), transparent);
      opacity: 0;
      transition: opacity 0.3s;
    }
    
    .status-display.active::after {
      opacity: 1;
      animation: shimmerLine 2s linear infinite;
    }
    
    @keyframes shimmerLine {
      0% { transform: translateX(-100%); }
      100% { transform: translateX(100%); }
    }
    
    /* Status Indicator */
    .status-indicator {
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background: var(--text-muted);
      position: relative;
      flex-shrink: 0;
      transition: var(--transition);
    }
    
    .status-indicator::before {
      content: '';
      position: absolute;
      inset: -6px;
      border-radius: 50%;
      background: inherit;
      opacity: 0.3;
      animation: pulse 2s ease-in-out infinite;
    }
    
    @keyframes pulse {
      0%, 100% { transform: scale(1); opacity: 0.3; }
      50% { transform: scale(1.5); opacity: 0; }
    }
    
    .status-indicator.ready { background: var(--text-muted); }
    .status-indicator.processing { background: var(--warning); }
    .status-indicator.success { background: var(--success); }
    .status-indicator.error { background: var(--error); }
    
    .status-text {
      flex: 1;
      font-size: 15px;
      font-weight: 500;
      color: var(--text-dim);
      letter-spacing: 0.3px;
    }
    
    /* Progress Bar */
    .progress-wrapper {
      margin-top: 32px;
      opacity: 0;
      transform: translateY(20px);
      transition: var(--transition);
    }
    
    .progress-wrapper.active {
      opacity: 1;
      transform: translateY(0);
    }
    
    .progress-bar {
      height: 8px;
      background: rgba(255, 255, 255, 0.05);
      border-radius: 999px;
      overflow: hidden;
      position: relative;
      border: 1px solid var(--border);
    }
    
    .progress-fill {
      height: 100%;
      background: var(--gradient-rainbow);
      background-size: 200% 100%;
      border-radius: 999px;
      animation: progressMove 2s linear infinite, shimmer 2s linear infinite;
      width: 100%;
      transform-origin: left;
    }
    
    @keyframes progressMove {
      0% { transform: scaleX(0) translateX(0); }
      50% { transform: scaleX(1) translateX(0); }
      100% { transform: scaleX(1) translateX(100%); }
    }
    
    /* Feature Cards */
    .features-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
      gap: 20px;
      margin-top: 40px;
    }
    
    .feature-card {
      padding: 24px;
      background: linear-gradient(135deg, rgba(255, 255, 255, 0.03) 0%, rgba(255, 255, 255, 0.01) 100%);
      border: 1px solid var(--border);
      border-radius: 20px;
      text-align: center;
      transition: var(--transition);
      animation: featureFloat 6s ease-in-out infinite;
      animation-delay: calc(var(--i) * 0.2s);
      position: relative;
      overflow: hidden;
    }
    
    .feature-card::before {
      content: '';
      position: absolute;
      top: 50%;
      left: 50%;
      width: 100%;
      height: 100%;
      background: radial-gradient(circle, var(--primary) 0%, transparent 70%);
      transform: translate(-50%, -50%) scale(0);
      opacity: 0;
      transition: var(--transition);
    }
    
    .feature-card:hover::before {
      transform: translate(-50%, -50%) scale(2);
      opacity: 0.1;
    }
    
    @keyframes featureFloat {
      0%, 100% { transform: translateY(0); }
      50% { transform: translateY(-10px); }
    }
    
    .feature-card:hover {
      transform: translateY(-5px) scale(1.02);
      background: linear-gradient(135deg, rgba(255, 255, 255, 0.06) 0%, rgba(255, 255, 255, 0.02) 100%);
      border-color: var(--primary);
      box-shadow: var(--glow-primary);
    }
    
    .feature-icon {
      font-size: 36px;
      margin-bottom: 12px;
      filter: drop-shadow(0 4px 8px rgba(0, 0, 0, 0.3));
      animation: iconRotate 4s ease-in-out infinite;
      animation-delay: calc(var(--i) * 0.3s);
    }
    
    @keyframes iconRotate {
      0%, 100% { transform: rotate(0deg) scale(1); }
      25% { transform: rotate(5deg) scale(1.1); }
      75% { transform: rotate(-5deg) scale(1.1); }
    }
    
    .feature-title {
      font-size: 16px;
      font-weight: 600;
      color: var(--text);
      margin-bottom: 8px;
    }
    
    .feature-desc {
      font-size: 14px;
      color: var(--text-muted);
      line-height: 1.5;
    }
    
    /* Quick Actions */
    .quick-actions {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 20px;
      margin-top: 32px;
      flex-wrap: wrap;
    }
    
    .action-group {
      display: flex;
      gap: 16px;
      align-items: center;
    }
    
    .action-link {
      color: var(--primary-light);
      text-decoration: none;
      font-weight: 600;
      font-size: 14px;
      transition: var(--transition);
      position: relative;
      padding: 8px 16px;
      border-radius: 8px;
      background: rgba(99, 102, 241, 0.1);
      border: 1px solid transparent;
    }
    
    .action-link:hover {
      background: rgba(99, 102, 241, 0.2);
      border-color: var(--primary);
      transform: translateY(-2px);
      box-shadow: 0 4px 12px rgba(99, 102, 241, 0.3);
    }
    
    /* Health Badge */
    .health-badge {
      padding: 10px 20px;
      background: linear-gradient(135deg, rgba(16, 185, 129, 0.1) 0%, rgba(16, 185, 129, 0.05) 100%);
      border: 1px solid rgba(16, 185, 129, 0.3);
      border-radius: 999px;
      font-size: 13px;
      color: var(--success);
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      transition: var(--transition);
    }
    
    .health-badge.loading {
      background: linear-gradient(135deg, rgba(156, 163, 175, 0.1) 0%, rgba(156, 163, 175, 0.05) 100%);
      border-color: rgba(156, 163, 175, 0.3);
      color: var(--text-muted);
    }
    
    .health-badge.error {
      background: linear-gradient(135deg, rgba(239, 68, 68, 0.1) 0%, rgba(239, 68, 68, 0.05) 100%);
      border-color: rgba(239, 68, 68, 0.3);
      color: var(--error);
    }
    
    .health-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: currentColor;
      animation: blink 2s ease-in-out infinite;
    }
    
    @keyframes blink {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.3; }
    }
    
    /* Spinner */
    .spinner {
      width: 20px;
      height: 20px;
      border: 3px solid rgba(255, 255, 255, 0.1);
      border-top-color: var(--primary);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
    
    /* Toast Notification */
    .toast {
      position: fixed;
      bottom: 40px;
      left: 50%;
      transform: translateX(-50%) translateY(100px) scale(0.9);
      padding: 20px 32px;
      background: linear-gradient(135deg, rgba(17, 24, 39, 0.95) 0%, rgba(10, 10, 20, 0.95) 100%);
      backdrop-filter: blur(20px);
      border: 1px solid var(--border-light);
      border-radius: 20px;
      box-shadow: 
        var(--shadow-2xl),
        var(--glow-primary);
      color: var(--text);
      font-weight: 500;
      z-index: 1000;
      opacity: 0;
      transition: var(--transition-bounce);
      font-size: 15px;
      letter-spacing: 0.3px;
    }
    
    .toast.show {
      opacity: 1;
      transform: translateX(-50%) translateY(0) scale(1);
    }
    
    /* Footer */
    .footer {
      text-align: center;
      margin-top: 60px;
      padding-top: 40px;
      border-top: 1px solid var(--border);
      animation: fadeInUp 0.8s ease 0.6s both;
    }
    
    .footer-text {
      color: var(--text-muted);
      font-size: 14px;
      margin-bottom: 16px;
    }
    
    .footer-links {
      display: flex;
      justify-content: center;
      gap: 24px;
      flex-wrap: wrap;
    }
    
    .footer-link {
      color: var(--text-dim);
      text-decoration: none;
      font-size: 13px;
      transition: var(--transition);
      position: relative;
    }
    
    .footer-link:hover {
      color: var(--primary-light);
    }
    
    /* Responsive Design */
    @media (max-width: 768px) {
      .container { padding: 40px 20px; }
      .card { padding: 40px 28px; }
      h1 { font-size: 36px; }
      .input-wrapper { flex-direction: column; }
      .btn-convert { width: 100%; }
      .quick-actions { flex-direction: column; align-items: stretch; }
      .action-group { flex-direction: column; width: 100%; }
      .action-link { width: 100%; text-align: center; }
      .features-grid { grid-template-columns: 1fr; }
      .logo-wave { 
        width: 90px; 
        height: 90px; 
      }
      .sound-bars {
        height: 40px;
      }
      .sound-bar:nth-child(1) { height: 15px; }
      .sound-bar:nth-child(2) { height: 25px; }
      .sound-bar:nth-child(3) { height: 35px; }
      .sound-bar:nth-child(4) { height: 30px; }
      .sound-bar:nth-child(5) { height: 22px; }
      .sound-bar:nth-child(6) { height: 18px; }
      .sound-bar:nth-child(7) { height: 26px; }
    }
    
    @keyframes fadeInUp {
      from { 
        opacity: 0; 
        transform: translateY(30px);
      }
      to { 
        opacity: 1; 
        transform: translateY(0);
      }
    }
    
    /* Loading Animation */
    .loading-dots {
      display: inline-flex;
      gap: 4px;
    }
    
    .loading-dot {
      width: 8px;
      height: 8px;
      background: var(--primary);
      border-radius: 50%;
      animation: loadingBounce 1.4s ease-in-out infinite;
    }
    
    .loading-dot:nth-child(1) { animation-delay: -0.32s; }
    .loading-dot:nth-child(2) { animation-delay: -0.16s; }
    
    @keyframes loadingBounce {
      0%, 80%, 100% { 
        transform: scale(0);
        opacity: 0.5;
      }
      40% { 
        transform: scale(1);
        opacity: 1;
      }
    }
  </style>
</head>
<body>
  <!-- Animated Background Layers -->
  <div class="universe-bg"></div>
  <div class="grid-bg"></div>
  <div class="gradient-orbs">
    <div class="orb orb1"></div>
    <div class="orb orb2"></div>
    <div class="orb orb3"></div>
  </div>
  
  <!-- Stars Background -->
  <div class="stars" id="stars"></div>
  
  <!-- Floating Particles -->
  <div class="particles" id="particles"></div>

  <!-- Main Content -->
  <div class="container">
    <div class="header">
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
      <h1>YouTube → MP3 Converter</h1>
      <p class="subtitle">Transform any YouTube video into premium quality audio instantly</p>
    </div>

    <div class="card">
      <form id="form">
        <div class="input-group">
          <div class="input-wrapper">
            <div class="input-field">
              <input 
                id="url" 
                type="url" 
                required 
                placeholder="Paste your YouTube URL here..."
                autocomplete="off"
              />
              <svg class="input-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"></path>
              </svg>
            </div>
            <select class="quality-selector" id="qualitySelect">
              <option value="128">128 kbps</option>
              <option value="192" selected>192 kbps</option>
              <option value="256">256 kbps</option>
              <option value="320">320 kbps</option>
            </select>
            <button class="btn-convert" id="convertBtn" type="submit">
              <span id="btnText">Convert Now</span>
            </button>
          </div>
        </div>

        <div class="quick-actions">
          <div class="action-group">
            <a href="#" id="sampleLink" class="action-link">✨ Try Sample</a>
            <span class="health-badge loading" id="healthBadge">
              <span class="spinner" style="width: 12px; height: 12px; border-width: 2px;"></span>
              <span>Checking...</span>
            </span>
          </div>
          <a href="/chrome-extension" class="action-link" download>🚀 Browser Extension</a>
        </div>

        <div class="progress-wrapper" id="progressWrapper">
          <div class="progress-bar">
            <div class="progress-fill"></div>
          </div>
        </div>

        <div class="status-display" id="statusDisplay">
          <div class="status-indicator ready" id="statusIndicator"></div>
          <span class="status-text" id="statusText">Ready to convert your audio</span>
        </div>
      </form>

      <div class="features-grid">
        <div class="feature-card" style="--i: 0;">
          <div class="feature-icon">🎵</div>
          <div class="feature-title">Premium Quality</div>
          <div class="feature-desc">Crystal clear 192kbps MP3 audio extraction</div>
        </div>
        <div class="feature-card" style="--i: 1;">
          <div class="feature-icon">⚡</div>
          <div class="feature-title">Lightning Fast</div>
          <div class="feature-desc">Optimized processing with smart caching</div>
        </div>
        <div class="feature-card" style="--i: 2;">
          <div class="feature-icon">🔄</div>
          <div class="feature-title">Smart Fallbacks</div>
          <div class="feature-desc">Multiple extraction methods for reliability</div>
        </div>
        <div class="feature-card" style="--i: 3;">
          <div class="feature-icon">📱</div>
          <div class="feature-title">Universal Support</div>
          <div class="feature-desc">Works perfectly on all devices</div>
        </div>
      </div>
    </div>

    <div class="footer">
      <p class="footer-text">Powered by advanced audio extraction technology</p>
      <div class="footer-links">
        <a href="#" class="footer-link">Privacy Policy</a>
        <a href="#" class="footer-link">Terms of Service</a>
        <a href="#" class="footer-link">API Access</a>
        <a href="#" class="footer-link">Support</a>
      </div>
    </div>
  </div>

  <!-- Toast Notification -->
  <div class="toast" id="toast"></div>

  <script>
    // Selectors
    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => document.querySelectorAll(sel);
    
    // Elements
    const statusIndicator = $('#statusIndicator');
    const statusText = $('#statusText');
    const statusDisplay = $('#statusDisplay');
    const progressWrapper = $('#progressWrapper');
    const toast = $('#toast');
    const form = $('#form');
    const urlInput = $('#url');
    const convertBtn = $('#convertBtn');
    const btnText = $('#btnText');
    const healthBadge = $('#healthBadge');
    const sampleLink = $('#sampleLink');

    // Generate random stars
    function createStars() {
      const starsContainer = $('#stars');
      const numberOfStars = 100;
      
      for (let i = 0; i < numberOfStars; i++) {
        const star = document.createElement('div');
        star.className = 'star';
        star.style.left = Math.random() * 100 + '%';
        star.style.top = Math.random() * 100 + '%';
        star.style.animationDelay = Math.random() * 3 + 's';
        star.style.animationDuration = 3 + Math.random() * 2 + 's';
        starsContainer.appendChild(star);
      }
    }

    // Generate floating particles
    function createParticles() {
      const particlesContainer = $('#particles');
      const numberOfParticles = 30;
      
      for (let i = 0; i < numberOfParticles; i++) {
        const particle = document.createElement('div');
        particle.className = 'particle';
        particle.style.left = Math.random() * 100 + '%';
        particle.style.animationDelay = Math.random() * 20 + 's';
        particle.style.animationDuration = 20 + Math.random() * 10 + 's';
        particlesContainer.appendChild(particle);
      }
    }

    // Initialize background effects
    createStars();
    createParticles();

    // Status management
    function setStatus(type, message, showSpinner = false) {
      statusText.textContent = message;
      statusIndicator.className = 'status-indicator ' + type;
      statusDisplay.classList.toggle('active', type === 'processing');
      
      if (showSpinner) {
        const loadingHtml = '<div class="loading-dots"><div class="loading-dot"></div><div class="loading-dot"></div><div class="loading-dot"></div></div>';
        statusText.innerHTML = loadingHtml + ' ' + message;
      }
    }

    // Toast notifications
    function showToast(message) {
      toast.textContent = message;
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 4000);
    }

    // URL validation
    function isValidYouTubeURL(url) {
      return /^(https?:\/\/)?(www\.)?(youtube\.com|youtu\.be)\//i.test(url);
    }

    // Health check
    fetch('/health')
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data && data.ok) {
          healthBadge.className = 'health-badge';
          healthBadge.innerHTML = '<div class="health-dot"></div><span>Online</span>';
        } else {
          throw new Error();
        }
      })
      .catch(() => {
        healthBadge.className = 'health-badge error';
        healthBadge.innerHTML = '<span>Offline</span>';
      });

    // Sample video
    sampleLink.addEventListener('click', (e) => {
      e.preventDefault();
      urlInput.value = 'http://www.youtube.com/watch?v=JK_hBk2f01k';
      showToast('✨ Sample video loaded - Click Convert Now!');
      urlInput.focus();
      
      // Add visual feedback
      urlInput.style.animation = 'pulse 0.5s';
      setTimeout(() => {
        urlInput.style.animation = '';
      }, 500);
    });

    // Queue system
    async function tryEnqueue(url, quality = '192') {
      try {
        const resp = await fetch('/enqueue', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ url, quality })
        });
        if (!resp.ok) return null;
        const data = await resp.json();
        return data.job_id || null;
      } catch (e) {
        return null;
      }
    }

    // Job polling
    async function pollJob(jobId) {
      progressWrapper.classList.add('active');
      const startTime = Date.now();
      
      const interval = setInterval(async () => {
        try {
          const resp = await fetch('/status/' + jobId);
          if (!resp.ok) {
            clearInterval(interval);
            setStatus('error', 'Status check failed');
            progressWrapper.classList.remove('active');
            convertBtn.disabled = false;
            btnText.textContent = 'Convert Now';
            return;
          }
          
          const status = await resp.json();
          const elapsed = Math.floor((Date.now() - startTime) / 1000);
          
          if (status.status === 'done') {
            clearInterval(interval);
            progressWrapper.classList.remove('active');
            setStatus('success', '✓ Conversion complete! Downloading...');
            convertBtn.disabled = false;
            btnText.textContent = 'Convert Now';
            showToast('🎉 Your MP3 is ready!');
            
            window.location.href = '/download_job/' + jobId;
          } else if (status.status === 'error') {
            clearInterval(interval);
            progressWrapper.classList.remove('active');
            setStatus('error', status.error || 'Conversion failed');
            convertBtn.disabled = false;
            btnText.textContent = 'Convert Now';
            showToast('❌ Conversion failed. Please try again.');
          } else {
            const minutes = Math.floor(elapsed / 60);
            const seconds = elapsed % 60;
            const timeStr = minutes > 0 ? minutes + 'm ' + seconds + 's' : seconds + 's';
            setStatus('processing', 'Converting... ' + timeStr, true);
          }
        } catch (e) {
          clearInterval(interval);
          progressWrapper.classList.remove('active');
          setStatus('error', 'Connection error');
          convertBtn.disabled = false;
          btnText.textContent = 'Convert Now';
        }
      }, 2000);
    }

    // Form submission
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      
      const url = urlInput.value.trim();
      const quality = $('#qualitySelect').value;
      
      if (!url) {
        setStatus('error', 'Please paste a YouTube URL');
        showToast('⚠️ URL field is empty');
        urlInput.focus();
        return;
      }
      
      if (!isValidYouTubeURL(url)) {
        setStatus('error', 'Invalid YouTube URL');
        showToast('❌ Please enter a valid YouTube link');
        return;
      }

      convertBtn.disabled = true;
      btnText.textContent = 'Processing...';
      setStatus('processing', `Initializing conversion (${quality}kbps)...`, true);

      const jobId = await tryEnqueue(url, quality);
      
      if (jobId) {
        showToast(`✓ Processing at ${quality}kbps - Please wait...`);
        await pollJob(jobId);
      } else {
        setStatus('processing', `Direct conversion at ${quality}kbps...`, true);
        progressWrapper.classList.add('active');
        
        const downloadUrl = '/download?url=' + encodeURIComponent(url) + '&quality=' + quality;
        window.open(downloadUrl, '_blank');
        
        setTimeout(() => {
          progressWrapper.classList.remove('active');
          setStatus('success', 'Download started in new tab');
          convertBtn.disabled = false;
          btnText.textContent = 'Convert Now';
          showToast('✓ Download initiated');
        }, 3000);
      }
    });

    // URL parameter handling
    try {
      const params = new URLSearchParams(location.search);
      const urlParam = params.get('url');
      if (urlParam) {
        urlInput.value = urlParam;
        setStatus('ready', 'URL loaded - Click Convert Now');
        showToast('✨ URL loaded from link');
      }
    } catch (e) {}

    // Add subtle parallax effect on mouse move
    document.addEventListener('mousemove', (e) => {
      const x = e.clientX / window.innerWidth;
      const y = e.clientY / window.innerHeight;
      
      const orbs = $$('.orb');
      orbs.forEach((orb, index) => {
        const speed = (index + 1) * 10;
        orb.style.transform = `translate(${x * speed}px, ${y * speed}px)`;
      });
    });
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


@app.get("/chrome-extension")
def download_extension():
    """Download the Chrome extension ZIP file."""
    import os
    import io
    
    # Try to find the extension ZIP file
    extension_paths = [
        "/home/claude/youtube-mp3-extension.zip",
        "./youtube-mp3-extension.zip",
        "/tmp/youtube-mp3-extension.zip",
        "youtube-mp3-extension.zip"
    ]
    
    for path in extension_paths:
        if os.path.exists(path):
            try:
                # Read the file into memory
                with open(path, 'rb') as f:
                    zip_data = f.read()
                
                # Create response with proper headers
                response = send_file(
                    io.BytesIO(zip_data),
                    mimetype="application/zip",
                    as_attachment=True,
                    download_name="youtube-mp3-chrome-extension.zip"
                )
                # Force download headers
                response.headers["Content-Type"] = "application/zip"
                response.headers["Content-Disposition"] = 'attachment; filename="youtube-mp3-chrome-extension.zip"'
                return response
            except Exception as e:
                print(f"Error serving extension: {e}")
                continue
    
    # If no zip file found, return error
    return jsonify({"error": "Extension file not found"}), 404


@app.route("/enqueue", methods=["POST"])
def enqueue():
    """Add conversion job to queue for async processing."""
    url = request.form.get("url")
    quality = request.form.get("quality", "192")  # Default to 192 kbps
    
    if not url:
        return jsonify({"error": "missing url"}), 400
    
    # Validate quality
    valid_qualities = ["128", "192", "256", "320"]
    if quality not in valid_qualities:
        quality = "192"
    
    job_id = str(uuid.uuid4())
    job_queue[job_id] = {
        "status": "queued",
        "url": url,
        "quality": quality,
        "title": None,
        "error": None,
        "mp3_path": None,
        "created_at": datetime.utcnow().isoformat()
    }
    
    thread = threading.Thread(target=process_job, args=(job_id, url, quality), daemon=True)
    thread.start()
    
    print(f"✓ Job {job_id} queued for URL: {url[:50]}... at {quality}kbps", flush=True)
    return jsonify({"job_id": job_id, "status": "queued", "quality": quality})


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
    quality = request.args.get("quality") or request.form.get("quality", "192")
    
    if not url:
        return jsonify({"error": "missing url"}), 400
    
    # Validate quality
    valid_qualities = ["128", "192", "256", "320"]
    if quality not in valid_qualities:
        quality = "192"

    try:
        cookiefile = str(COOKIE_PATH) if COOKIE_PATH and COOKIE_PATH.exists() else None

        title, mp3_path = download_audio_with_fallback(
            url,
            OUT_DEFAULT,
            cookiefile=cookiefile,
            dsid=YTDLP_DATA_SYNC_ID,
            quality=quality
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