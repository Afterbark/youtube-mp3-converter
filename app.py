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
job_queue = {}  # {job_id: {status, url, title, error, file_path, format, created_at}}

def safe_filename(name: str, ext: str = "mp3") -> str:
    """Sanitize filename for safe download - preserves Unicode characters."""
    # Only remove characters that are invalid in filenames
    # Keep Arabic, Chinese, Japanese, Korean, etc.
    name = SAFE_CHARS.sub("_", name).strip() or "media"
    # Remove multiple spaces and leading/trailing spaces
    name = " ".join(name.split())
    # Limit length to 200 chars to be safe
    if len(name) > 200:
        name = name[:200].rsplit(' ', 1)[0]  # Cut at word boundary
    return f"{name}.{ext}"


def _base_ydl_opts(out_default: str, cookiefile: str | None, dsid: str | None, client: str, quality: str = "192", format_type: str = "mp3"):
    """Build optimized yt-dlp options for a specific player client, quality, and format."""
    opts = {
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
    
    # Configure format and postprocessors based on desired output
    if format_type == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": quality,
        }]
    else:  # mp4
        # Quality mapping for video: 360p, 480p, 720p, 1080p
        quality_map = {
            "360": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best[height<=360]",
            "480": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best[height<=480]",
            "720": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
            "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best[height<=1080]",
        }
        opts["format"] = quality_map.get(quality, "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best")
        opts["merge_output_format"] = "mp4"
    
    if cookiefile:
        opts["cookiefile"] = cookiefile
    
    return opts


def _resolve_file_path(ydl: yt_dlp.YoutubeDL, info, ext: str = "mp3") -> Path:
    """Get the final file path after post-processing."""
    try:
        pre = Path(ydl.prepare_filename(info))
        cand = pre.with_suffix(f".{ext}")
        if cand.exists():
            return cand
    except Exception:
        pass

    vid = info.get("id") or "*"
    matches = sorted(
        DOWNLOAD_DIR.glob(f"yt_{vid}*.{ext}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )
    if matches:
        return matches[0]
    raise FileNotFoundError(f"{ext.upper()} not found after postprocessing")


def fetch_title_with_ytdlp(url: str, cookiefile: str | None, dsid: str | None):
    """Metadata-only title fetch using the same cookies/clients."""
    for client in CLIENTS_TO_TRY:
        try:
            opts = _base_ydl_opts(OUT_DEFAULT, cookiefile, dsid, client, "192", "mp3")  # Use default for metadata
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


def download_media_with_fallback(url: str, out_default: str, cookiefile: str | None, dsid: str | None, quality: str = "192", format_type: str = "mp3"):
    """Try multiple clients to avoid SABR/bot checks. Returns (title, file_path:str)."""
    last_err = None
    ext = format_type  # "mp3" or "mp4"
    
    for idx, client in enumerate(CLIENTS_TO_TRY):
        try:
            print(f"[yt-dlp] Attempt {idx+1}/{len(CLIENTS_TO_TRY)}: client={client}, format={format_type}, quality={quality}", flush=True)
            with yt_dlp.YoutubeDL(_base_ydl_opts(out_default, cookiefile, dsid, client, quality, format_type)) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title") or "media"
                file_path = _resolve_file_path(ydl, info, ext)
                print(f"✓ Success with client={client}", flush=True)
                return title, str(file_path)
        except (DownloadError, ExtractorError, FileNotFoundError) as e:
            last_err = e
            print(f"✗ client={client} failed: {str(e)[:100]}", flush=True)
            continue
    if last_err:
        raise last_err
    raise RuntimeError("All extractor attempts failed")


def process_job(job_id: str, url: str, quality: str = "192", format_type: str = "mp3"):
    """Background job processor."""
    try:
        job_queue[job_id]["status"] = "processing"
        cookiefile = str(COOKIE_PATH) if COOKIE_PATH and COOKIE_PATH.exists() else None
        
        # Download with fallback
        title, file_path = download_media_with_fallback(
            url, OUT_DEFAULT, cookiefile=cookiefile, dsid=YTDLP_DATA_SYNC_ID, quality=quality, format_type=format_type
        )
        
        # Try to get better title if needed
        if not title or title.strip().lower() in ["audio", "media"]:
            t2 = fetch_title_with_ytdlp(url, cookiefile, YTDLP_DATA_SYNC_ID)
            if t2:
                title = t2
        
        if not title or title.strip().lower() in ["audio", "media"]:
            t3 = fetch_title_oembed(url)
            if t3:
                title = t3
        
        job_queue[job_id].update({
            "status": "done",
            "title": title or "media",
            "file_path": file_path,
        })
        print(f"✓ Job {job_id} completed: {title}", flush=True)
        
        # Schedule cleanup after 5 minutes
        def cleanup():
            time.sleep(300)
            try:
                Path(file_path).unlink(missing_ok=True)
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
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>YouTube Converter - MP3 & MP4</title>
  <style>
    * {
      margin: 0;
      padding: 0;
      box-sizing: border-box;
    }

    :root {
      --primary: #6366f1;
      --primary-dark: #4f46e5;
      --success: #10b981;
      --error: #ef4444;
      --bg: #0f172a;
      --bg-card: #1e293b;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --border: #334155;
    }

    body {
      font-family: 'Segoe UI', system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      position: relative;
      overflow-x: hidden;
    }

    /* Animated Background */
    .orb {
      position: fixed;
      border-radius: 50%;
      filter: blur(100px);
      opacity: 0.3;
      pointer-events: none;
      z-index: 0;
      animation: float 20s ease-in-out infinite;
    }

    .orb-1 {
      width: 500px;
      height: 500px;
      background: var(--primary);
      top: -250px;
      left: -250px;
    }

    .orb-2 {
      width: 400px;
      height: 400px;
      background: #8b5cf6;
      bottom: -200px;
      right: -200px;
      animation-delay: -10s;
    }

    @keyframes float {
      0%, 100% { transform: translate(0, 0) scale(1); }
      33% { transform: translate(30px, -30px) scale(1.1); }
      66% { transform: translate(-20px, 20px) scale(0.9); }
    }

    .container {
      position: relative;
      z-index: 1;
      max-width: 600px;
      margin: 0 auto;
      padding: 40px 20px;
    }

    .card {
      background: var(--bg-card);
      border-radius: 24px;
      padding: 40px;
      box-shadow: 0 20px 60px rgba(0,0,0,0.3);
      backdrop-filter: blur(10px);
      border: 1px solid var(--border);
    }

    h1 {
      font-size: 2.5rem;
      font-weight: 700;
      text-align: center;
      margin-bottom: 12px;
      background: linear-gradient(135deg, var(--primary) 0%, #8b5cf6 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }

    .subtitle {
      text-align: center;
      color: var(--text-muted);
      margin-bottom: 40px;
      font-size: 1rem;
    }

    .input-group {
      margin-bottom: 24px;
    }

    label {
      display: block;
      margin-bottom: 8px;
      font-weight: 600;
      font-size: 0.875rem;
      color: var(--text);
    }

    input[type="text"], select {
      width: 100%;
      padding: 14px 18px;
      background: var(--bg);
      border: 2px solid var(--border);
      border-radius: 12px;
      color: var(--text);
      font-size: 1rem;
      transition: all 0.3s;
    }

    input[type="text"]:focus, select:focus {
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 4px rgba(99, 102, 241, 0.1);
    }

    .format-selector {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-bottom: 24px;
    }

    .format-option {
      position: relative;
    }

    .format-option input[type="radio"] {
      position: absolute;
      opacity: 0;
    }

    .format-label {
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 16px;
      background: var(--bg);
      border: 2px solid var(--border);
      border-radius: 12px;
      cursor: pointer;
      transition: all 0.3s;
      font-weight: 600;
    }

    .format-option input[type="radio"]:checked + .format-label {
      background: var(--primary);
      border-color: var(--primary);
      color: white;
    }

    .format-label:hover {
      border-color: var(--primary);
    }

    .quality-group {
      margin-bottom: 24px;
    }

    .quality-grid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 8px;
    }

    .quality-option {
      position: relative;
    }

    .quality-option input[type="radio"] {
      position: absolute;
      opacity: 0;
    }

    .quality-btn {
      display: block;
      width: 100%;
      padding: 12px;
      background: var(--bg);
      border: 2px solid var(--border);
      border-radius: 10px;
      cursor: pointer;
      transition: all 0.3s;
      text-align: center;
      font-weight: 600;
      font-size: 0.875rem;
    }

    .quality-option input[type="radio"]:checked + .quality-btn {
      background: var(--primary);
      border-color: var(--primary);
      color: white;
    }

    .convert-btn {
      width: 100%;
      padding: 16px;
      background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
      color: white;
      border: none;
      border-radius: 12px;
      font-size: 1.125rem;
      font-weight: 700;
      cursor: pointer;
      transition: all 0.3s;
      position: relative;
      overflow: hidden;
    }

    .convert-btn:hover:not(:disabled) {
      transform: translateY(-2px);
      box-shadow: 0 10px 30px rgba(99, 102, 241, 0.4);
    }

    .convert-btn:active:not(:disabled) {
      transform: translateY(0);
    }

    .convert-btn:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }

    .progress-wrapper {
      margin-top: 24px;
      display: none;
    }

    .progress-wrapper.active {
      display: block;
    }

    .progress-bar {
      height: 8px;
      background: var(--bg);
      border-radius: 4px;
      overflow: hidden;
      margin-bottom: 12px;
    }

    .progress-fill {
      height: 100%;
      background: linear-gradient(90deg, var(--primary), #8b5cf6);
      border-radius: 4px;
      transition: width 0.3s;
      animation: shimmer 2s infinite;
    }

    @keyframes shimmer {
      0% { background-position: -200px 0; }
      100% { background-position: 200px 0; }
    }

    .status-message {
      text-align: center;
      padding: 12px;
      border-radius: 8px;
      font-weight: 600;
      display: none;
    }

    .status-message.active {
      display: block;
    }

    .status-ready { background: rgba(99, 102, 241, 0.1); color: var(--primary); }
    .status-processing { background: rgba(139, 92, 246, 0.1); color: #8b5cf6; }
    .status-success { background: rgba(16, 185, 129, 0.1); color: var(--success); }
    .status-error { background: rgba(239, 68, 68, 0.1); color: var(--error); }

    .toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      padding: 16px 24px;
      background: var(--bg-card);
      border-radius: 12px;
      box-shadow: 0 10px 40px rgba(0,0,0,0.3);
      border: 1px solid var(--border);
      transform: translateX(400px);
      transition: transform 0.3s;
      z-index: 1000;
    }

    .toast.show {
      transform: translateX(0);
    }

    @media (max-width: 640px) {
      .card {
        padding: 24px;
      }
      h1 {
        font-size: 2rem;
      }
      .quality-grid {
        grid-template-columns: repeat(2, 1fr);
      }
    }
  </style>
</head>
<body>
  <div class="orb orb-1"></div>
  <div class="orb orb-2"></div>

  <div class="container">
    <div class="card">
      <h1>🎵 YouTube Converter</h1>
      <p class="subtitle">Convert videos to MP3 or download as MP4</p>

      <form id="convertForm">
        <div class="input-group">
          <label for="url">YouTube URL</label>
          <input 
            type="text" 
            id="url" 
            name="url" 
            placeholder="https://youtube.com/watch?v=..."
            required
          />
        </div>

        <div class="format-selector">
          <div class="format-option">
            <input type="radio" name="format" id="mp3" value="mp3" checked>
            <label for="mp3" class="format-label">🎵 MP3 Audio</label>
          </div>
          <div class="format-option">
            <input type="radio" name="format" id="mp4" value="mp4">
            <label for="mp4" class="format-label">🎬 MP4 Video</label>
          </div>
        </div>

        <div class="quality-group">
          <label>Quality</label>
          <div class="quality-grid" id="qualityGrid">
            <!-- Quality options will be dynamically inserted here -->
          </div>
        </div>

        <button type="submit" class="convert-btn" id="convertBtn">
          <span id="btnText">Convert Now</span>
        </button>

        <div class="progress-wrapper" id="progressWrapper">
          <div class="progress-bar">
            <div class="progress-fill" id="progressFill" style="width: 0%"></div>
          </div>
          <div class="status-message" id="statusMessage"></div>
        </div>
      </form>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => document.querySelectorAll(sel);
    
    const urlInput = $('#url');
    const convertBtn = $('#convertBtn');
    const btnText = $('#btnText');
    const progressWrapper = $('#progressWrapper');
    const progressFill = $('#progressFill');
    const statusMessage = $('#statusMessage');
    const toast = $('#toast');
    const qualityGrid = $('#qualityGrid');

    const mp3Qualities = ['128', '192', '256', '320'];
    const mp4Qualities = ['360', '480', '720', '1080'];

    function updateQualityOptions() {
      const format = document.querySelector('input[name="format"]:checked').value;
      const qualities = format === 'mp3' ? mp3Qualities : mp4Qualities;
      const suffix = format === 'mp3' ? 'kbps' : 'p';
      const defaultQuality = format === 'mp3' ? '192' : '720';

      qualityGrid.innerHTML = qualities.map((q, i) => `
        <div class="quality-option">
          <input type="radio" name="quality" id="q${q}" value="${q}" ${q === defaultQuality ? 'checked' : ''}>
          <label for="q${q}" class="quality-btn">${q}${suffix}</label>
        </div>
      `).join('');
    }

    // Initialize quality options
    updateQualityOptions();

    // Update quality options when format changes
    $$('input[name="format"]').forEach(radio => {
      radio.addEventListener('change', updateQualityOptions);
    });

    function showToast(message) {
      toast.textContent = message;
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 3000);
    }

    function setStatus(type, message) {
      statusMessage.className = `status-message active status-${type}`;
      statusMessage.textContent = message;
    }

    $('#convertForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      
      const url = urlInput.value.trim();
      const format = document.querySelector('input[name="format"]:checked').value;
      const quality = document.querySelector('input[name="quality"]:checked').value;
      
      if (!url) {
        showToast('❌ Please enter a YouTube URL');
        return;
      }

      convertBtn.disabled = true;
      btnText.textContent = 'Processing...';
      progressWrapper.classList.add('active');
      setStatus('processing', 'Starting conversion...');
      progressFill.style.width = '30%';

      try {
        const formData = new FormData();
        formData.append('url', url);
        formData.append('quality', quality);
        formData.append('format', format);

        const enqueueRes = await fetch('/enqueue', {
          method: 'POST',
          body: formData
        });

        if (!enqueueRes.ok) throw new Error('Failed to start conversion');

        const { job_id } = await enqueueRes.json();
        setStatus('processing', 'Converting your video...');
        progressFill.style.width = '60%';

        let attempts = 0;
        const maxAttempts = 120;

        const checkStatus = async () => {
          if (attempts >= maxAttempts) {
            throw new Error('Conversion timeout');
          }

          const statusRes = await fetch(`/status/${job_id}`);
          const statusData = await statusRes.json();

          if (statusData.status === 'done') {
            progressFill.style.width = '100%';
            setStatus('success', `✓ ${statusData.title || 'Media'} ready!`);
            
            const downloadUrl = `/download_job/${job_id}`;
            const a = document.createElement('a');
            a.href = downloadUrl;
            a.download = '';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);

            showToast(`✓ Downloaded as ${format.toUpperCase()}`);
            
            setTimeout(() => {
              progressWrapper.classList.remove('active');
              convertBtn.disabled = false;
              btnText.textContent = 'Convert Now';
            }, 2000);
          } else if (statusData.status === 'error') {
            throw new Error(statusData.error || 'Conversion failed');
          } else {
            attempts++;
            setTimeout(checkStatus, 1000);
          }
        };

        await checkStatus();

      } catch (error) {
        console.error(error);
        setStatus('error', error.message || 'Conversion failed');
        showToast('❌ ' + (error.message || 'Failed to convert'));
        convertBtn.disabled = false;
        btnText.textContent = 'Convert Now';
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
        progressWrapper.classList.add('active');
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
    quality = request.form.get("quality", "192")
    format_type = request.form.get("format", "mp3")  # "mp3" or "mp4"
    
    if not url:
        return jsonify({"error": "missing url"}), 400
    
    # Validate format
    if format_type not in ["mp3", "mp4"]:
        format_type = "mp3"
    
    # Validate quality based on format
    if format_type == "mp3":
        valid_qualities = ["128", "192", "256", "320"]
        if quality not in valid_qualities:
            quality = "192"
    else:  # mp4
        valid_qualities = ["360", "480", "720", "1080"]
        if quality not in valid_qualities:
            quality = "720"
    
    job_id = str(uuid.uuid4())
    job_queue[job_id] = {
        "status": "queued",
        "url": url,
        "quality": quality,
        "format": format_type,
        "title": None,
        "error": None,
        "file_path": None,
        "created_at": datetime.utcnow().isoformat()
    }
    
    thread = threading.Thread(target=process_job, args=(job_id, url, quality, format_type), daemon=True)
    thread.start()
    
    print(f"✓ Job {job_id} queued for URL: {url[:50]}... ({format_type} @ {quality})", flush=True)
    return jsonify({"job_id": job_id, "status": "queued", "quality": quality, "format": format_type})


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
        "format": job.get("format"),
        "created_at": job.get("created_at")
    })


@app.get("/download_job/<job_id>")
def download_job(job_id: str):
    """Download the completed file."""
    if job_id not in job_queue:
        return jsonify({"error": "job not found"}), 404
    
    job = job_queue[job_id]
    
    if job["status"] != "done":
        return jsonify({"error": f"job is {job['status']}"}), 400
    
    file_path = job["file_path"]
    if not file_path or not Path(file_path).exists():
        return jsonify({"error": "file not found"}), 404
    
    format_type = job.get("format", "mp3")
    safe_name = safe_filename(job["title"] or "media", format_type)
    
    # Set appropriate MIME type
    mime_type = "audio/mpeg" if format_type == "mp3" else "video/mp4"
    
    resp = send_file(
        file_path,
        mimetype=mime_type,
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
    format_type = request.args.get("format") or request.form.get("format", "mp3")
    
    if not url:
        return jsonify({"error": "missing url"}), 400
    
    # Validate format
    if format_type not in ["mp3", "mp4"]:
        format_type = "mp3"
    
    # Validate quality based on format
    if format_type == "mp3":
        valid_qualities = ["128", "192", "256", "320"]
        if quality not in valid_qualities:
            quality = "192"
    else:  # mp4
        valid_qualities = ["360", "480", "720", "1080"]
        if quality not in valid_qualities:
            quality = "720"

    try:
        cookiefile = str(COOKIE_PATH) if COOKIE_PATH and COOKIE_PATH.exists() else None

        title, file_path = download_media_with_fallback(
            url,
            OUT_DEFAULT,
            cookiefile=cookiefile,
            dsid=YTDLP_DATA_SYNC_ID,
            quality=quality,
            format_type=format_type
        )

        if not title or title.strip().lower() in ["audio", "media"]:
            t2 = fetch_title_with_ytdlp(url, cookiefile, YTDLP_DATA_SYNC_ID)
            if t2:
                title = t2

        if not title or title.strip().lower() in ["audio", "media"]:
            t3 = fetch_title_oembed(url)
            if t3:
                title = t3

        safe_name = safe_filename(title or "media", format_type)
        
        # Set appropriate MIME type
        mime_type = "audio/mpeg" if format_type == "mp3" else "video/mp4"
        
        resp = send_file(
            file_path,
            mimetype=mime_type,
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

        threading.Thread(target=_cleanup, args=(file_path,), daemon=True).start()
        return resp

    except Exception as e:
        print(f"✗ Download error: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))