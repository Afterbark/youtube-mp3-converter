import os
import base64
import re
import time
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


def safe_filename(name: str, ext: str = "mp3") -> str:
    name = SAFE_CHARS.sub("", name).strip() or "audio"
    return f"{name}.{ext}"


def _base_ydl_opts(out_default: str, cookiefile: str | None, dsid: str | None, client: str):
    """Build yt-dlp options for a specific player client."""
    opts = {
        "format": "bestaudio/best",
        # Force everything into /tmp on Heroku
        "paths": {"home": str(DOWNLOAD_DIR), "temp": str(DOWNLOAD_DIR)},
        "outtmpl": {"default": out_default},
        "noprogress": True,
        "quiet": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 1,
        "geo_bypass": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "extractor_args": {
            "youtube": {
                "player_client": [client],      # "web" | "ios" | "android"
                "player_skip": ["webpage"],
                **({"data_sync_id": [dsid]} if (dsid and client == "web") else {}),
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
        # If IPv6 egress causes issues, uncomment:
        # "force_ip": "0.0.0.0",
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts


def _resolve_mp3_path(ydl: yt_dlp.YoutubeDL, info) -> Path:
    """
    Determine the final MP3 path after post-processing.
    1) Try prepare_filename(info) and swap extension to .mp3
    2) Fallback: glob for yt_<id>*.mp3 in /tmp
    """
    try:
        pre = Path(ydl.prepare_filename(info))  # pre-postproc path (e.g., .webm/.m4a)
        cand = pre.with_suffix(".mp3")
        if cand.exists():
            return cand
    except Exception:
        pass

    vid = info.get("id") or "*"
    matches = sorted(DOWNLOAD_DIR.glob(f"yt_{vid}*.mp3"),
                     key=lambda p: p.stat().st_mtime,
                     reverse=True)
    if matches:
        return matches[0]
    raise FileNotFoundError("MP3 not found after postprocessing")


def download_audio_with_fallback(url: str, out_default: str, cookiefile: str | None, dsid: str | None):
    """Try web -> ios -> android to avoid SABR/bot checks. Returns (title, mp3_path:str)."""
    last_err = None
    for client in ["web", "ios", "android"]:
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
    <style>
      body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:700px;margin:40px auto;padding:0 16px;}
      input,button{font-size:16px;padding:10px;border:1px solid #ccc;border-radius:10px}
      button{cursor:pointer}
      .row{display:flex;gap:8px}
      #msg{margin-top:12px;color:#555}
    </style>
  </head>
  <body>
    <h1>YouTube → MP3 (Heroku)</h1>
    <form id="f" class="row">
      <input id="u" type="url" required placeholder="https://www.youtube.com/watch?v=..." style="flex:1">
      <button>Convert</button>
    </form>
    <div id="msg"></div>
    <script>
      const f = document.getElementById('f');
      const u = document.getElementById('u');
      const msg = document.getElementById('msg');
      f.addEventListener('submit', (e)=>{
        e.preventDefault();
        const url = u.value.trim();
        if(!url) return;
        const dl = location.origin + "/download?url=" + encodeURIComponent(url);
        window.open(dl, "_blank");
        msg.textContent = "Starting download...";
      });
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
    title, mp3_path = download_audio_with_fallback(
        url,
        OUT_DEFAULT,
        cookiefile=cookiefile,
        dsid=YTDLP_DATA_SYNC_ID
    )

    # If title is missing, try extracting it from yt-dlp metadata file or the URL
    if not title or title.lower() == "audio":
        try:
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                title = info.get('title', 'audio')
        except Exception:
            title = "audio"

    safe_name = safe_filename(title, "mp3")
    resp = send_file(mp3_path, mimetype="audio/mpeg", as_attachment=True, download_name=safe_name)

            # Optional: expose for POST clients that parse filename
            resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
            return resp

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
