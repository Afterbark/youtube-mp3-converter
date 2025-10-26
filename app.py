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