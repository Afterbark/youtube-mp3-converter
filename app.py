import os
import base64
import re
import time
import json
import urllib.parse
import urllib.request
import threading
from pathlib import Path
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
CLIENTS_TO_TRY = [
    "web",
    "web_safari",
    "web_embedded",
    "tv",
    "ios",
    "android",
]


def safe_filename(name: str, ext: str = "mp3") -> str:
    name = SAFE_CHARS.sub("", name).strip() or "audio"
    return f"{name}.{ext}"


def _base_ydl_opts(out_default: str, cookiefile: str | None, dsid: str | None, client: str):
    """Build yt-dlp options for a specific player client."""
    opts = {
        "format": "bestaudio/best",
        "paths": {"home": str(DOWNLOAD_DIR), "temp": str(DOWNLOAD_DIR)},
        "outtmpl": {"default": out_default},
        "noprogress": True,
        "quiet": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,   # retry metadata extraction too
        "concurrent_fragment_downloads": 1,
        "geo_bypass": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "extractor_args": {
            "youtube": {
                "player_client": [client],      # try multiple clients
                "player_skip": ["webpage"],
                **({"data_sync_id": [dsid]} if (dsid and client.startswith("web")) else {}),
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.youtube.com/",
        },
        # If IPv6 egress causes issues, uncomment:
        # "force_ip": "0.0.0.0",
        # If you still see throttling, you can try:
        # "throttledratelimit": 102400,  # 100 KiB/s (lets yt-dlp detect & handle throttling)
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts


def _resolve_mp3_path(ydl: yt_dlp.YoutubeDL, info) -> Path:
    """Get the final MP3 path after post-processing."""
    try:
        pre = Path(ydl.prepare_filename(info))  # pre-postproc path (.webm/.m4a)
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
            t = data.get("title")
            if t:
                return t
    except Exception:
        pass
    return None


def download_audio_with_fallback(url: str, out_default: str, cookiefile: str | None, dsid: str | None):
    """Try multiple clients to avoid SABR/bot checks. Returns (title, mp3_path:str)."""
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


# ---------- Minimal UI for manual tests ----------
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
      position: fixed; inset: 0; z-index: 0;
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
    .orb { position: fixed; border-radius: 50%; filter: blur(80px); opacity: 0.3; pointer-events: none; z-index: 1; animation: float 15s ease-in-out infinite; }
    .orb1 { width: 400px; height: 400px; background: var(--brand1); top: -200px; left: -200px; animation-delay: 0s; }
    .orb2 { width: 350px; height: 350px; background: var(--brand2); bottom: -150px; right: -150px; animation-delay: 5s; }
    .orb3 { width: 300px; height: 300px; background: var(--brand3); top: 40%; right: 10%; animation-delay: 10s; }
    @keyframes float {
      0%, 100% { transform: translate(0, 0) scale(1); }
      33% { transform: translate(50px, -50px) scale(1.1); }
      66% { transform: translate(-30px, 30px) scale(0.9); }
    }
    .container { position: relative; z-index: 10; max-width: 1000px; margin: 0 auto; padding: 60px 24px;
      min-height: 100vh; display: flex; flex-direction: column; justify-content: center; }
    .header { text-align: center; margin-bottom: 60px; animation: fadeInDown 0.8s ease; }
    @keyframes fadeInDown { from { opacity: 0; transform: translateY(-30px); } to { opacity: 1; transform: translateY(0); } }
    .logo-container { display: inline-flex; align-items: center; justify-content: center; margin-bottom: 24px; position: relative; }
    .logo {
      width: 80px; height: 80px; background: linear-gradient(135deg, var(--brand1), var(--brand2));
      border-radius: 24px; display: grid; place-items: center; font-size: 32px; font-weight: 900; letter-spacing: 1px;
      box-shadow: 0 0 60px var(--glow), 0 20px 40px rgba(0, 0, 0, 0.4);
      animation: logoFloat 3s ease-in-out infinite; position: relative; overflow: hidden;
    }
    .logo::before {
      content: ''; position: absolute; inset: 0;
      background: linear-gradient(45deg, transparent, rgba(255, 255, 255, 0.1), transparent);
      transform: translateX(-100%); animation: shine 3s ease infinite;
    }
    @keyframes logoFloat { 0%, 100% { transform: translateY(0) rotate(0deg); } 50% { transform: translateY(-10px) rotate(2deg); } }
    @keyframes shine { 0% { transform: translateX(-100%); } 50%, 100% { transform: translateX(200%); } }
    h1 {
      font-size: clamp(32px, 5vw, 48px); font-weight: 800; margin-bottom: 12px;
      background: linear-gradient(135deg, var(--brand1), var(--brand2), var(--brand3));
      -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
      animation: gradientText 5s ease infinite; background-size: 200% 200%;
    }
    @keyframes gradientText { 0%, 100% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } }
    .subtitle { font-size: 18px; color: var(--muted); font-weight: 500; }
    .card {
      background: var(--card); backdrop-filter: blur(20px) saturate(180%);
      border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 32px; padding: 48px;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5), inset 0 1px 0 rgba(255, 255, 255, 0.1);
      animation: fadeInUp 0.8s ease 0.2s both; position: relative; overflow: hidden;
    }
    .card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px;
      background: linear-gradient(90deg, transparent, rgba(255,255,255,.5), transparent); }
    @keyframes fadeInUp { from { opacity: 0; transform: translateY(30px); } to { opacity: 1; transform: translateY(0); } }
    .form-group { margin-bottom: 24px; }
    .input-wrapper { position: relative; display: flex; gap: 12px; }
    input[type="url"] {
      flex: 1; padding: 18px 24px; background: rgba(0, 0, 0, 0.3);
      border: 2px solid rgba(255, 255, 255, 0.1); border-radius: 16px; color: var(--text);
      font-size: 16px; outline: none; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    input[type="url"]::placeholder { color: var(--muted); }
    input[type="url"]:focus {
      border-color: var(--brand1); background: rgba(0, 0, 0, 0.4);
      box-shadow: 0 0 0 4px var(--glow), 0 8px 24px rgba(0, 0, 0, 0.3); transform: translateY(-2px);
    }
    button {
      padding: 18px 36px; background: linear-gradient(135deg, var(--brand1), var(--brand2));
      border: none; border-radius: 16px; color: white; font-size: 16px; font-weight: 700; cursor: pointer;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); box-shadow: 0 8px 24px var(--glow2);
      position: relative; overflow: hidden; white-space: nowrap;
    }
    button::before {
      content: ''; position: absolute; inset: 0; background: linear-gradient(135deg, rgba(255,255,255,0.2), transparent);
      opacity: 0; transition: opacity 0.3s;
    }
    button:hover { transform: translateY(-2px); box-shadow: 0 12px 32px var(--glow2); }
    button:hover::before { opacity: 1; }
    button:active { transform: translateY(0); }
    button:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
    .status {
      display: flex; align-items: center; gap: 12px; padding: 16px 20px; background: rgba(0,0,0,0.3);
      border-radius: 12px; margin-top: 24px; border: 1px solid rgba(255,255,255,0.05);
      min-height: 60px; transition: all 0.3s ease;
    }
    .status-dot { width: 12px; height: 12px; border-radius: 50%; background: var(--muted); position: relative; flex-shrink: 0; }
    .status-dot::after {
      content: ''; position: absolute; inset: -4px; border-radius: 50%; border: 2px solid currentColor;
      opacity: 0; animation: ping 2s cubic-bezier(0, 0, 0.2, 1) infinite;
    }
    .status-dot.active::after { opacity: 0.75; }
    @keyframes ping { 75%, 100% { transform: scale(2); opacity: 0; } }
    .status-dot.ok { background: var(--ok); color: var(--ok); }
    .status-dot.warn { background: var(--warn); color: var(--warn); }
    .status-dot.err { background: var(--err); color: var(--err); }
    .status-text { flex: 1; font-size: 15px; font-weight: 500; }
    .spinner { width: 20px; height: 20px; border: 3px solid rgba(255,255,255,0.2); border-top-color: var(--brand1);
      border-radius: 50%; animation: spin 0.8s linear infinite; display: inline-block; vertical-align: middle; margin-right: 8px; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .progress-container { margin-top: 24px; display: none; opacity: 0; transition: opacity 0.3s ease; }
    .progress-container.show { display: block; opacity: 1; }
    .progress-bar { height: 6px; background: rgba(255,255,255,0.1); border-radius: 999px; overflow: hidden; position: relative; }
    .progress-fill { height: 100%; background: linear-gradient(90deg, var(--brand1), var(--brand2), var(--brand3));
      background-size: 200% 100%; border-radius: 999px; animation: progressFlow 1.5s ease infinite; width: 100%; }
    @keyframes progressFlow { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }
    .features { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-top: 32px; }
    .feature {
      padding: 16px 20px; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08);
      border-radius: 12px; font-size: 14px; color: var(--muted); text-align: center; transition: all 0.3s ease; animation: fadeInUp 0.6s ease backwards;
    }
    .feature:nth-child(1) { animation-delay: 0.3s; } .feature:nth-child(2) { animation-delay: 0.4s; }
    .feature:nth-child(3) { animation-delay: 0.5s; } .feature:nth-child(4) { animation-delay: 0.6s; }
    .feature:hover { background: rgba(255,255,255,0.05); border-color: rgba(255,255,255,0.15); transform: translateY(-2px); }
    .actions { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-top: 24px; flex-wrap: wrap; }
    .link { color: var(--brand1); text-decoration: none; font-weight: 600; font-size: 14px; transition: all 0.2s ease; position: relative; }
    .link::after { content: ''; position: absolute; bottom: -2px; left: 0; width: 0; height: 2px; background: var(--brand1); transition: width 0.3s ease; }
    .link:hover::after { width: 100%; }
    .health-badge { padding: 8px 16px; background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.3); border-radius: 999px;
      font-size: 13px; color: var(--ok); font-weight: 600; display: inline-flex; align-items: center; gap: 8px; }
    .health-badge.loading { background: rgba(156,163,175,0.1); border-color: rgba(156,163,175,0.3); color: var(--muted); }
    .health-badge.error { background: rgba(239,68,68,0.1); border-color: rgba(239,68,68,0.3); color: var(--err); }
    .toast {
      position: fixed; bottom: 32px; left: 50%; transform: translateX(-50%) translateY(100px);
      padding: 16px 24px; background: rgba(17,24,39,0.95); backdrop-filter: blur(20px);
      border: 1px solid rgba(255,255,255,0.2); border-radius: 16px; box-shadow: 0 20px 40px rgba(0,0,0,0.5);
      color: var(--text); font-weight: 500; z-index: 1000; opacity: 0; transition: all 0.4s cubic-bezier(0.4,0,0.2,1);
    }
    .toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
    .footer { text-align: center; margin-top: 48px; color: var(--muted); font-size: 14px; animation: fadeInUp 0.8s ease 0.4s both; }
    @media (max-width: 768px) {
      .container { padding: 40px 16px; } .card { padding: 32px 24px; }
      h1 { font-size: 32px; } .input-wrapper { flex-direction: column; } button { width: 100%; }
      .actions { flex-direction: column; align-items: stretch; } .features { grid-template-columns: 1fr; }
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
      <div class="logo-container"><div class="logo">MP3</div></div>
      <h1>YouTube → MP3 Converter</h1>
      <p class="subtitle">Convert any YouTube video to high-quality MP3</p>
    </div>

    <div class="card">
      <form id="form">
        <div class="form-group">
          <div class="input-wrapper">
            <input id="url" type="url" required placeholder="Paste YouTube URL here..." autocomplete="off"/>
            <button id="convertBtn" type="submit"><span id="btnText">Convert</span></button>
          </div>
        </div>

        <div class="actions">
          <div style="display:flex; gap:16px; align-items:center;">
            <a href="#" id="sampleLink" class="link">Try sample video</a>
            <span class="health-badge loading" id="healthBadge">
              <span class="spinner" style="width:12px;height:12px;border-width:2px;"></span>Checking...
            </span>
          </div>
          <a href="chrome://extensions" class="link" target="_blank">Chrome Extension</a>
        </div>

        <div class="progress-container" id="progressContainer">
          <div class="progress-bar"><div class="progress-fill"></div></div>
        </div>

        <div class="status">
          <div class="status-dot" id="statusDot"></div>
          <span class="status-text" id="statusText">Ready to convert</span>
        </div>
      </form>

      <div class="features">
        <div class="feature">🎵 192 kbps MP3</div>
        <div class="feature">⚡ Fast Processing</div>
        <div class="feature">🔄 Smart Fallbacks</div>
        <div class="feature">📱 Mobile Friendly</div>
      </div>
    </div>

    <div class="footer"><p>Powered by yt-dlp • FFmpeg • Heroku</p></div>
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
        statusText.innerHTML = `<span class="spinner"></span>${message}`;
      }
    }

    function showToast(message) {
      toast.textContent = message;
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 3000);
    }

    function isValidYouTubeURL(url) {
      return /^(https?:\\/\\/)?(www\\.)?(youtube\\.com|youtu\\.be)\\//i.test(url);
    }

    // Health check
    fetch('/health')
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data && data.ok) {
          healthBadge.className = 'health-badge';
          healthBadge.innerHTML = '<div style="width:8px;height:8px;background:var(--ok);border-radius:50%;"></div>Server Online';
        } else { throw new Error(); }
      })
      .catch(() => {
        healthBadge.className = 'health-badge error';
        healthBadge.textContent = 'Server Offline';
      });

    // Sample video
    sampleLink.addEventListener('click', (e) => {
      e.preventDefault();
      urlInput.value = 'https://youtu.be/JK_hBk2f01k?list=RDJK_hBk2f01k';
      showToast('✨ Sample video loaded');
      urlInput.focus();
    });

    // Queue system
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
      } catch {
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
            progressContainer.classList.remove('show');
            setStatus('err', 'Status check failed');
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
            setTimeout(() => { window.open('/download_job/' + jobId, '_blank'); }, 500);
          } else if (status.status === 'error') {
            clearInterval(interval);
            progressContainer.classList.remove('show');
            setStatus('err', status.error || 'Conversion failed');
            convertBtn.disabled = false;
            btnText.textContent = 'Convert';
          } else {
            const minutes = Math.floor(elapsed / 60);
            const seconds = elapsed % 60;
            const timeStr = minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
            setStatus('warn', `Converting... ${timeStr}`, true);
          }
        } catch {
          clearInterval(interval);
          progressContainer.classList.remove('show');
          setStatus('err', 'Connection error');
          convertBtn.disabled = false;
          btnText.textContent = 'Convert';
        }
      }, 2000);
    }

    // Form submit
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const url = urlInput.value.trim();
      if (!url) { setStatus('warn', 'Please paste a YouTube URL'); urlInput.focus(); return; }
      if (!isValidYouTubeURL(url)) { setStatus('warn', 'Invalid YouTube URL'); showToast('❌ Please enter a valid YouTube link'); return; }
      convertBtn.disabled = true;
      btnText.textContent = 'Processing...';
      setStatus('warn', 'Queuing conversion...', true);

      const jobId = await tryEnqueue(url);
      if (jobId) {
        showToast('✓ Added to queue');
        await pollJob(jobId);
      } else {
        setStatus('warn', 'Starting direct download...', true);
        progressContainer.classList.add('show');
        const downloadUrl = '/download?url=' + encodeURIComponent(url);
        window.open(downloadUrl, '_blank');
        setTimeout(() => {
          progressContainer.classList.remove('show');
          setStatus('ok', 'Download started in new tab');
          convertBtn.disabled = false;
          btnText.textContent = 'Convert';
        }, 2000);
      }
    });

    // Prefill from ?url=
    try {
      const params = new URLSearchParams(location.search);
      const urlParam = params.get('url');
      if (urlParam) { urlInput.value = urlParam; setStatus('ok', 'URL loaded from link'); }
    } catch {}
  </script>
</body>
</html>
"""


@app.get("/")
def home():
    return render_template_string(HOME_HTML)


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.route("/download", methods=["GET", "POST"])
def download():
    # Accept URL from GET ?url=... or POST form body
    url = request.args.get("url") or request.form.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400

    try:
        cookiefile = str(COOKIE_PATH) if COOKIE_PATH and COOKIE_PATH.exists() else None

        # 1) Download + initial title
        title, mp3_path = download_audio_with_fallback(
            url,
            OUT_DEFAULT,
            cookiefile=cookiefile,
            dsid=YTDLP_DATA_SYNC_ID
        )

        # 2) If title missing/too generic -> try metadata-only yt-dlp
        if not title or title.strip().lower() == "audio":
            t2 = fetch_title_with_ytdlp(url, cookiefile, YTDLP_DATA_SYNC_ID)
            if t2:
                title = t2

        # 3) If still missing -> try oEmbed (no cookies)
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

        # optional background cleanup
        def _cleanup(path):
            try:
                time.sleep(30)
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass

        threading.Thread(target=_cleanup, args=(mp3_path,), daemon=True).start()
        return resp

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))