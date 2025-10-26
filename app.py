import os
import base64
import re
import time
import json
import uuid
import urllib.parse
import urllib.request
import threading
from dataclasses import dataclass, asdict
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
        # Prefer m4a (usually quickest) before generic best
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "paths": {"home": str(DOWNLOAD_DIR), "temp": str(DOWNLOAD_DIR)},
        "outtmpl": {"default": out_default},
        "noprogress": True,
        "quiet": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,   # retry metadata extraction too
        "concurrent_fragment_downloads": 3,  # a bit faster on long videos
        "geo_bypass": True,
        # MP3 postprocess (keep for compatibility)
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
        # "throttledratelimit": 256000,  # 250 KiB/s (helps yt-dlp detect throttling)
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
<html>
<head>
  <meta charset="utf-8"/>
  <title>YouTube → MP3</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root{
      --bg:#0b0c10;--panel:#0f1115;--text:#e6e8eb;--muted:#9aa3ad;--brand:#6aa6ff;--brand-strong:#4285f4;--border:#1b1e26;--radius:16px;--shadow:0 18px 40px rgba(0,0,0,.35);--focus:0 0 0 2px rgba(106,166,255,.45);
    }
    @media (prefers-color-scheme: light){
      :root{
        --bg:#f6f7fb;--panel:#fff;--text:#0f1115;--muted:#5f6b76;--brand:#3b7cff;--brand-strong:#1f6bff;--border:#e8ecf2;--shadow:0 18px 40px rgba(10,22,37,.08);--focus:0 0 0 2px rgba(31,107,255,.28);
      }
    }
    *{box-sizing:border-box}
    html,body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}
    .wrap{max-width:760px;margin:48px auto;padding:0 16px}
    .card{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);padding:22px}
    .brand{display:flex;align-items:center;gap:12px;margin-bottom:10px}
    .logo{width:34px;height:34px;border-radius:12px;background:linear-gradient(135deg,var(--brand),var(--brand-strong));display:grid;place-items:center;color:#fff;font-weight:800}
    h1{margin:0;font-size:20px}
    p{color:var(--muted);margin:6px 0 16px}
    .row{display:flex;gap:10px;flex-wrap:wrap}
    input[type="url"]{flex:1;min-width:260px;padding:12px 14px;border:1px solid var(--border);background:transparent;color:var(--text);border-radius:12px;outline:none;transition:box-shadow .2s,border .2s}
    input::placeholder{color:var(--muted)}
    input:focus{box-shadow:var(--focus);border-color:transparent}
    button{padding:12px 16px;border:none;border-radius:12px;background:var(--brand);color:#fff;font-weight:700;cursor:pointer}
    button:hover{background:var(--brand-strong)}
    .muted{font-size:13px;color:var(--muted);margin-top:10px}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="brand">
        <div class="logo">MP3</div>
        <h1>YouTube → MP3 (Heroku)</h1>
      </div>
      <p>Paste a YouTube link and we’ll download a 192kbps MP3. This page is mainly for quick manual tests and debugging.</p>

      <form id="f" class="row">
        <input id="u" type="url" required placeholder="https://www.youtube.com/watch?v=..." />
        <button>Convert</button>
      </form>

      <div id="msg" class="muted"></div>
      <div class="muted">Tip: Use the Chrome extension for one-click conversion from YouTube.</div>
    </div>
  </div>

  <script>
    const f = document.getElementById('f');
    const u = document.getElementById('u');
    const msg = document.getElementById('msg');
    f.addEventListener('submit', (e)=>{
      e.preventDefault();
      const url = u.value.trim();
      if(!url){ msg.textContent = "Please enter a valid URL."; return; }
      // Use async flow for long videos too
      fetch('/enqueue', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:new URLSearchParams({url})})
        .then(r=>r.json()).then(({job_id})=>{
          if(!job_id){ msg.textContent = "Failed to queue job"; return; }
          msg.textContent = "Queued. Working…";
          const iv = setInterval(()=>{
            fetch('/status/' + job_id).then(r=>r.json()).then(s=>{
              if(s.status==='done'){ clearInterval(iv); msg.textContent="Ready. Starting download…"; window.open('/download_job/'+job_id,'_blank'); }
              else if(s.status==='error'){ clearInterval(iv); msg.textContent = "Error: " + (s.error||'unknown'); }
            }).catch(()=>{ clearInterval(iv); msg.textContent="Status error"; });
          }, 3000);
        }).catch(()=>{ msg.textContent = "Queue error"; });
    });
  </script>
</body>
</html>
"""

# ----------------- Async job registry -----------------
@dataclass
class Job:
    id: str
    url: str
    status: str  # "queued"|"working"|"done"|"error"
    title: str | None = None
    path: str | None = None
    error: str | None = None

JOBS_DIR = DOWNLOAD_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

def _job_path(jid: str) -> Path:
    return JOBS_DIR / f"{jid}.json"

def _save_job(job: Job):
    _job_path(job.id).write_text(json.dumps(asdict(job)))

def _load_job(jid: str) -> Job | None:
    p = _job_path(jid)
    if not p.exists():
        return None
    return Job(**json.loads(p.read_text()))

def _update_job(j: Job, **kw):
    for k, v in kw.items():
        setattr(j, k, v)
    _save_job(j)

def _worker(job_id: str):
    j = _load_job(job_id)
    if not j:
        return
    try:
        _update_job(j, status="working")
        cookiefile = str(COOKIE_PATH) if COOKIE_PATH and COOKIE_PATH.exists() else None
        title, mp3_path = download_audio_with_fallback(
            j.url,
            OUT_DEFAULT,
            cookiefile=cookiefile,
            dsid=YTDLP_DATA_SYNC_ID
        )
        _update_job(j, status="done", title=title, path=mp3_path)
    except Exception as e:
        _update_job(j, status="error", error=str(e))


# ----------------- Routes -----------------
@app.get("/")
def home():
    return render_template_string(HOME_HTML)

@app.get("/health")
def health():
    return jsonify({"ok": True})

# Synchronous path remains for short clips
@app.route("/download", methods=["GET", "POST"])
def download():
    url = request.args.get("url") or request.form.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    try:
        cookiefile = str(COOKIE_PATH) if COOKIE_PATH and COOKIE_PATH.exists() else None
        title, mp3_path = download_audio_with_fallback(url, OUT_DEFAULT, cookiefile, YTDLP_DATA_SYNC_ID)

        if not title or title.strip().lower() == "audio":
            t2 = fetch_title_with_ytdlp(url, cookiefile, YTDLP_DATA_SYNC_ID)
            if t2: title = t2
        if not title or title.strip().lower() == "audio":
            t3 = fetch_title_oembed(url)
            if t3: title = t3

        safe_name = safe_filename(title or "audio", "mp3")
        resp = send_file(mp3_path, mimetype="audio/mpeg", as_attachment=True, download_name=safe_name)
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
        return jsonify({"error": str(e)}), 500

# ---------- Async endpoints for long videos ----------
@app.post("/enqueue")
def enqueue():
    url = request.form.get("url") or (request.json.get("url") if request.is_json else None)
    if not url:
        return jsonify({"error": "missing url"}), 400
    jid = uuid.uuid4().hex[:12]
    job = Job(id=jid, url=url, status="queued")
    _save_job(job)
    threading.Thread(target=_worker, args=(jid,), daemon=True).start()
    return jsonify({"job_id": jid})

@app.get("/status/<job_id>")
def status(job_id):
    j = _load_job(job_id)
    if not j:
        return jsonify({"error": "not found"}), 404
    return jsonify(asdict(j))

@app.get("/download_job/<job_id>")
def download_job(job_id):
    j = _load_job(job_id)
    if not j:
        return jsonify({"error": "not found"}), 404
    if j.status != "done" or not j.path:
        return jsonify({"error": "not ready"}), 409
    safe_name = safe_filename(j.title or "audio", "mp3")
    resp = send_file(j.path, mimetype="audio/mpeg", as_attachment=True, download_name=safe_name)
    resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition"

    def _cleanup(p, jfile):
        try:
            time.sleep(60)
            Path(p).unlink(missing_ok=True)
            Path(jfile).unlink(missing_ok=True)
        except Exception:
            pass
    threading.Thread(target=_cleanup, args=(j.path, _job_path(j.id)), daemon=True).start()
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
