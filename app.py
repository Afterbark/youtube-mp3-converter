import os
import uuid
import base64
import threading
import traceback
from pathlib import Path
from flask import Flask, request, send_file, make_response, jsonify
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
# Allow your extension to call this API and read filename headers
CORS(app, resources={r"/*": {"origins": "*"}}, expose_headers=["Content-Disposition"])

# Heroku writes are allowed only in /tmp
DOWNLOAD_DIR = Path("/tmp")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---- Optional: load YouTube cookies from Heroku config var (recommended if you hit bot checks)
# Run locally or on Heroku:
#   (Windows PowerShell)
#     $b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes("youtube_cookies.txt"))
#     heroku config:set YTDLP_COOKIES_B64=$b64 -a <your-app>
COOKIE_PATH = None
_b64 = os.getenv("YTDLP_COOKIES_B64")
if _b64:
    try:
        COOKIE_PATH = DOWNLOAD_DIR / "youtube_cookies.txt"
        COOKIE_PATH.write_bytes(base64.b64decode(_b64))
        print(f"[init] Loaded cookies to {COOKIE_PATH}", flush=True)
    except Exception as e:
        print(f"[init] Failed to load cookies: {e}", flush=True)
        COOKIE_PATH = None


def _safe_filename(name: str) -> str:
    # Basic filename sanitizer
    bad = r'\/:*?"<>|'
    for ch in bad:
        name = name.replace(ch, "-")
    name = name.strip().strip(".")
    return name or "audio"


@app.route("/")
def home():
    # Simple test form so you can hit the app in a browser
    return """
    <h2>YouTube → MP3</h2>
    <form action="/download" method="post">
      <input type="text" name="url" placeholder="Enter YouTube URL" style="width: 360px" required>
      <button type="submit">Convert</button>
    </form>
    <p style="color:#666">Tip: If you get a bot check in logs, set YTDLP_COOKIES_B64 on Heroku.</p>
    """, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/download", methods=["POST"])
def download():
    url = (request.form.get("url") or "").strip()
    if not url:
        return jsonify(error="missing_url"), 400

    file_id = str(uuid.uuid4())
    outtmpl = str(DOWNLOAD_DIR / f"{file_id}.%(ext)s")

    # yt-dlp options tuned for Heroku reliability
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noprogress": True,
        "quiet": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        # Try to avoid anti-bot by using web client and fewer fetches
        "extractor_args": {
            "youtube": {
                "player_client": ["web"],
                "player_skip": ["webpage"],  # skip extra webpage parsing
            }
        },
        "concurrent_fragment_downloads": 1,
        # Uncomment if you specifically need IPv4 only:
        # "force_ip": "0.0.0.0",
    }

    # Use cookiefile if provided (helps when YouTube challenges “sign in to confirm you're not a bot”)
    if COOKIE_PATH and COOKIE_PATH.exists():
        ydl_opts["cookiefile"] = str(COOKIE_PATH)

    # If ffmpeg isn't found even after adding the buildpack, you can point to a path:
    # ydl_opts["ffmpeg_location"] = "/app/.heroku/bin"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title") or "audio"

        title = _safe_filename(title)
        mp3_path = DOWNLOAD_DIR / f"{file_id}.mp3"

        if not mp3_path.exists():
            # Some edge cases: postprocessor failed or YouTube blocked the download
            return jsonify(error="no_mp3_generated"), 500

        resp = make_response(
            send_file(
                str(mp3_path),
                as_attachment=True,
                download_name=f"{title}.mp3",
                mimetype="audio/mpeg",
                max_age=0,
            )
        )

        # Schedule cleanup
        def _cleanup():
            try:
                if mp3_path.exists():
                    mp3_path.unlink()
            except Exception:
                pass

        threading.Timer(60.0, _cleanup).start()
        return resp

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        # Surface common anti-bot hint to the client
        if "Sign in to confirm" in msg or "cookies" in msg.lower():
            return jsonify(
                error="youtube_antibot",
                hint="Set YTDLP_COOKIES_B64 with your youtube.com cookies, or try again later.",
                detail=msg[:500],
            ), 502  # Bad gateway-ish to indicate upstream block
        print("DOWNLOAD_ERROR(yt-dlp):\n" + traceback.format_exc(), flush=True)
        return jsonify(error="download_error", detail=msg[:500]), 500
    except Exception:
        print("DOWNLOAD_ERROR:\n" + traceback.format_exc(), flush=True)
        return jsonify(error="conversion_failed"), 500


if __name__ == "__main__":
    # Local dev
    app.run(host="127.0.0.1", port=5000, debug=True)
