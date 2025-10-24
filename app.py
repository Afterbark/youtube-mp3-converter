import os
import re
import io
import zipfile
import tempfile
import shutil
import datetime
from flask import Flask, request, send_file, abort, jsonify, Response
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)


def sanitize(name: str) -> str:
    """Make a safe, short filename."""
    name = re.sub(r'[\\/*?:"<>|]+', '_', name).strip()
    # remove control chars
    name = re.sub(r'[\x00-\x1f\x7f]+', '', name)
    return name[:80] or 'download'


@app.get("/")
def index():
    # Minimal built-in UI for quick testing
    return Response(
        """
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>YouTube → MP3</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:24px;max-width:720px}
  h1{margin:0 0 12px}
  input,button{font-size:16px;padding:10px;border:1px solid #ccc;border-radius:10px}
  input[type=url]{width:100%;box-sizing:border-box;margin:6px 0 10px}
  .row{display:flex;gap:12px;align-items:center;margin:8px 0}
  .row input[type=number]{width:120px}
  button{cursor:pointer}
  small{color:#666}
</style>
</head>
<body>
  <h1>YouTube → MP3</h1>
  <form method="get" action="/download">
    <label>Video / Playlist / Channel / Topic URL</label>
    <input type="url" name="url" placeholder="https://www.youtube.com/watch?v=..." required />
    <div class="row">
      <label><input type="checkbox" name="mode" value="zip"> ZIP if URL has multiple videos</label>
      <label>Limit <input type="number" name="limit" value="50" min="1" max="500" /></label>
    </div>
    <button type="submit">Convert</button>
  </form>
  <div style="height:10px"></div>
  <small>Tip: Leave ZIP unchecked to grab just the first video from multi-video pages.</small>
</body>
</html>
        """,
        mimetype="text/html",
    )


@app.get("/health")
def health():
    return jsonify(ok=True)


def build_common_ydl_opts(tmpdir: str) -> dict:
    """Common yt-dlp options for MP3 conversion."""
    opts = {
        # file name: Title [ID].ext to avoid clashes and keep titles legible
        "outtmpl": os.path.join(tmpdir, "%(title).200B [%(id)s].%(ext)s"),
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    # Optional: cookie jar via base64 env (helps for age/region-restricted videos)
    b64 = os.environ.get("YTDLP_COOKIES_B64", "").strip()
    if b64:
        import base64
        cj_path = os.path.join(tmpdir, "cookies.txt")
        with open(cj_path, "wb") as f:
            f.write(base64.b64decode(b64))
        opts["cookiefile"] = cj_path

    return opts


@app.get("/download")
def download():
    """
    Supports:
      - Single video: /download?url=...
      - Multi (playlist/topic/channel) as ZIP: /download?url=...&mode=zip[&limit=50]
      - Behavior in 'single' mode with multi-URL: download the first playable video.
    """
    url = (request.args.get("url") or "").strip()
    mode = request.args.get("mode", "single").lower()
    if mode == "zip":   # checkbox in HTML sends mode=zip; keep consistent
        mode = "zip"
    else:
        mode = "single"
    try:
        limit = int(request.args.get("limit", "50"))
        limit = max(1, min(limit, 500))  # safety bounds
    except ValueError:
        limit = 50

    if not url:
        abort(400, "Missing 'url' parameter")

    tmpdir = tempfile.mkdtemp(prefix="ytmp3_")
    try:
        # Base options
        ydl_opts = build_common_ydl_opts(tmpdir)

        # First, probe the URL to see if it's a playlist/topic/etc.
        info_probe_opts = dict(ydl_opts)
        info_probe_opts["skip_download"] = True
        info_probe_opts["extract_flat"] = "in_playlist"  # quick listing for big containers

        with yt_dlp.YoutubeDL(info_probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Normalize entries
        def iter_entries(obj):
            if not obj:
                return []
            if isinstance(obj, dict) and obj.get("entries"):
                return obj["entries"]
            return [obj]

        entries = iter_entries(info)

        if mode == "single":
            # Grab first playable item
            first = entries[0] if entries else None
            if not first:
                abort(404, "No videos found at this URL")

            # If we got a flattened entry, use its URL; else use original
            first_url = first.get("url") if isinstance(first, dict) else url

            single_opts = dict(ydl_opts)
            single_opts["noplaylist"] = True
            single_opts.pop("extract_flat", None)
            single_opts.pop("skip_download", None)

            with yt_dlp.YoutubeDL(single_opts) as ydl:
                ydl.extract_info(first_url, download=True)

            # Find produced MP3 and stream from memory (so we can clean temp dir)
            mp3_path = None
            for root, _, files in os.walk(tmpdir):
                for fn in files:
                    if fn.lower().endswith(".mp3"):
                        mp3_path = os.path.join(root, fn)
                        break
                if mp3_path:
                    break

            if not mp3_path:
                abort(500, "Failed to produce MP3")

            with open(mp3_path, "rb") as f:
                data = io.BytesIO(f.read())
            data.seek(0)

            return send_file(
                data,
                as_attachment=True,
                download_name=os.path.basename(mp3_path),
                mimetype="audio/mpeg",
            )

        # ZIP mode
        # Select up to 'limit' entries and download each as MP3
        selected_urls = []
        for e in entries:
            if len(selected_urls) >= limit:
                break
            u = e.get("url") if isinstance(e, dict) else None
            selected_urls.append(u or url)

        if not selected_urls:
            abort(404, "No videos found to zip")

        multi_opts = dict(ydl_opts)
        multi_opts["noplaylist"] = True  # download entries one-by-one as videos
        multi_opts.pop("extract_flat", None)
        multi_opts.pop("skip_download", None)

        with yt_dlp.YoutubeDL(multi_opts) as ydl:
            for u in selected_urls:
                try:
                    ydl.extract_info(u, download=True)
                except Exception:
                    # Keep going even if one item fails
                    continue

        # Pack MP3s into a ZIP (in memory)
        zip_buf = io.BytesIO()
        added = 0
        with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(tmpdir):
                for fn in files:
                    if fn.lower().endswith(".mp3"):
                        full = os.path.join(root, fn)
                        z.write(full, arcname=fn)
                        added += 1

        if added == 0:
            abort(500, "No MP3s were created")

        zip_buf.seek(0)
        stamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        title = sanitize((info or {}).get("title") or "playlist")
        zip_name = f"{title}-{added}tracks-{stamp}.zip"

        return send_file(
            zip_buf,
            as_attachment=True,
            download_name=zip_name,
            mimetype="application/zip",
        )

    finally:
        # Best-effort cleanup
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
