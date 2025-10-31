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
  <title>YouTube → MP3</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root{
      --bg:#0b0c10;
      --card:#0f1115;
      --text:#e6e8eb;
      --muted:#9aa3ad;
      --brand1:#6aa6ff;
      --brand2:#4285f4;
      --ok:#13b884;
      --warn:#ffb020;
      --err:#ef4444;
      --border:#1b1e26;
      --radius:18px;
      --shadow:0 24px 60px rgba(0,0,0,.35);
      --ring:0 0 0 2px rgba(106,166,255,.45);
    }
    @media (prefers-color-scheme: light){
      :root{
        --bg:#f7f9fc;
        --card:#ffffff;
        --text:#0f1115;
        --muted:#5f6b76;
        --brand1:#3b7cff;
        --brand2:#1f6bff;
        --border:#e6ebf2;
        --shadow:0 24px 60px rgba(10,22,37,.08);
        --ring:0 0 0 2px rgba(31,107,255,.30);
      }
    }
    *{box-sizing:border-box}
    html,body{margin:0;background:var(--bg);color:var(--text);font:16px/1.6 system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}
    .wrap{max-width:900px;margin:64px auto;padding:0 16px}
    .hero{
      display:flex;align-items:center;gap:14px;margin-bottom:18px
    }
    .logo{
      width:44px;height:44px;border-radius:14px;background:linear-gradient(135deg,var(--brand1),var(--brand2));
      display:grid;place-items:center;color:#fff;font-weight:800;letter-spacing:.3px;box-shadow:0 10px 24px rgba(66,133,244,.35)
    }
    h1{margin:0;font-size:28px}
    p.lead{margin:6px 0 24px;color:var(--muted)}
    .card{
      background:radial-gradient(1200px 400px at 20% -10%, rgba(66,133,244,.08), transparent 40%) , var(--card);
      border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);
      padding:22px;
    }
    .row{display:flex;gap:10px;flex-wrap:wrap}
    input[type="url"]{
      flex:1;min-width:260px;padding:14px 16px;border:1px solid var(--border);background:transparent;color:var(--text);
      border-radius:14px;outline:none;transition:box-shadow .2s,border .2s
    }
    input::placeholder{color:var(--muted)}
    input:focus{box-shadow:var(--ring);border-color:transparent}
    button{
      padding:14px 18px;border:none;border-radius:14px;color:#fff;cursor:pointer;font-weight:700;
      background:linear-gradient(135deg,var(--brand1),var(--brand2));box-shadow:0 10px 20px rgba(66,133,244,.35);
      transition:transform .05s ease, filter .2s
    }
    button:active{transform:translateY(1px)}
    .actions{display:flex;align-items:center;justify-content:space-between;margin-top:10px}
    .link{color:var(--brand2);text-decoration:none;font-weight:600}
    .link:hover{text-decoration:underline}
    .status{margin-top:14px;display:flex;align-items:center;gap:10px;color:var(--muted);min-height:24px}
    .dot{width:10px;height:10px;border-radius:50%;background:var(--muted)}
    .dot.ok{background:var(--ok)} .dot.warn{background:var(--warn)} .dot.err{background:var(--err)}
    .spinner{width:16px;height:16px;border-radius:50%;border:3px solid var(--muted);border-top-color:transparent;animation:spin .8s linear infinite}
    @keyframes spin{to{transform:rotate(360deg)}}
    .kitchen{margin-top:18px;display:flex;gap:10px;flex-wrap:wrap}
    .chip{border:1px solid var(--border);border-radius:9999px;padding:8px 12px;color:var(--muted)}
    .foot{margin-top:18px;color:var(--muted);font-size:13px}
    /* Toast */
    .toast{
      position:fixed;left:50%;bottom:28px;transform:translateX(-50%);padding:12px 16px;border-radius:12px;
      background:var(--card);border:1px solid var(--border);box-shadow:var(--shadow);color:var(--text);opacity:0;pointer-events:none;
      transition:opacity .25s, transform .25s;
    }
    .toast.show{opacity:1;transform:translate(-50%, -6px)}
    /* Progress */
    .progress{height:8px;border-radius:999px;background:rgba(127,127,127,.18);overflow:hidden;margin-top:12px;display:none}
    .bar{height:100%;width:25%;background:linear-gradient(135deg,var(--brand1),var(--brand2));animation:bar 1.1s linear infinite}
    @keyframes bar{0%{transform:translateX(-100%)}100%{transform:translateX(400%)}}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div class="logo">MP3</div>
      <div>
        <h1>YouTube → MP3 (Heroku)</h1>
        <p class="lead">Paste a YouTube link and we’ll fetch a high-quality MP3. Works with long videos (queue mode).</p>
      </div>
    </div>

    <div class="card">
      <form id="form" class="row" autocomplete="off">
        <input id="url" type="url" required placeholder="https://www.youtube.com/watch?v=..." />
        <button id="go" type="submit">Convert</button>
      </form>

      <div class="actions">
        <div>
          <a class="link" id="sample" href="#">Try a sample video</a>
          <span class="chip" id="health">Checking server…</span>
        </div>
        <a class="link" href="https://chrome://extensions" id="ext" target="_blank" rel="noreferrer">Use the Chrome extension</a>
      </div>

      <div class="progress" id="progress"><div class="bar"></div></div>

      <div class="status" id="status">
        <div class="dot" id="dot"></div>
        <span id="statustxt">Ready</span>
      </div>
    </div>

    <div class="kitchen">
      <div class="chip">MP3 • 192 kbps</div>
      <div class="chip">FFmpeg post-processing</div>
      <div class="chip">Client fallbacks (web/ios/tv)</div>
      <div class="chip">Queue & Poll for long videos</div>
    </div>

    <div class="foot">Tip: livestreams and very long videos take a while—queue mode keeps working in the background.</div>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    const $ = (s) => document.querySelector(s);
    const dot = $("#dot");
    const txt = $("#statustxt");
    const progress = $("#progress");
    const toast = $("#toast");
    const form = $("#form");
    const urlInput = $("#url");
    const healthChip = $("#health");
    const sampleBtn = $("#sample");

    // --- helpers ---
    const setStatus = (state, message) => {
      txt.textContent = message || "";
      dot.className = "dot";
      if (state === "ok") dot.classList.add("ok");
      else if (state === "warn") dot.classList.add("warn");
      else if (state === "err") dot.classList.add("err");
    };
    const showToast = (msg) => {
      toast.textContent = msg;
      toast.classList.add("show");
      setTimeout(()=> toast.classList.remove("show"), 2200);
    };
    const validYouTube = (u) => /^(https?:\\/\\/)?(www\\.)?(youtube\\.com|youtu\\.be)\\//i.test(u);

    // Health
    fetch("/health").then(r => r.ok ? r.json(): null).then(data=>{
      healthChip.textContent = (data && data.ok) ? "Server OK" : "Server check failed";
    }).catch(()=> healthChip.textContent = "Server check failed");

    // Sample link (stable chillhop stream ~long)
    sampleBtn.addEventListener("click", (e)=>{
      e.preventDefault();
      urlInput.value = "https://www.youtube.com/watch?v=Dx5qFachd3A";
      showToast("Loaded sample video");
    });

    async function tryEnqueue(url){
      try{
        const resp = await fetch("/enqueue", {
          method:"POST",
          headers:{ "Content-Type":"application/x-www-form-urlencoded" },
          body: new URLSearchParams({ url })
        });
        if (!resp.ok) return null; // not supported, fall back
        const data = await resp.json();
        return data.job_id || null;
      }catch(e){ return null; }
    }

    async function poll(jobId){
      progress.style.display = "block";
      const start = Date.now();
      const iv = setInterval(async ()=>{
        try{
          const r = await fetch("/status/" + jobId);
          if (!r.ok){ clearInterval(iv); setStatus("err","Status error"); progress.style.display="none"; return; }
          const s = await r.json();
          if (s.status === "done"){
            clearInterval(iv);
            setStatus("ok","Ready — starting download…");
            progress.style.display = "none";
            window.open("/download_job/" + jobId, "_blank");
          } else if (s.status === "error"){
            clearInterval(iv);
            setStatus("err", s.error || "Conversion failed");
            progress.style.display = "none";
          } else {
            // still working
            const mins = Math.floor((Date.now() - start)/60000);
            setStatus("warn", "Working…" + (mins ? " ("+mins+"m)" : ""));
          }
        }catch(e){
          clearInterval(iv);
          setStatus("err","Status check failed");
          progress.style.display = "none";
        }
      }, 3000);
    }

    // Submit
    form.addEventListener("submit", async (e)=>{
      e.preventDefault();
      const url = urlInput.value.trim();
      if (!url){ setStatus("warn","Please paste a YouTube URL"); urlInput.focus(); return; }
      if (!validYouTube(url)){ setStatus("warn","That doesn’t look like a YouTube link"); return; }

      // Try async queue first; if not available, fall back to direct download
      setStatus("warn","Queuing…");
      dot.className = "spinner";

      const jobId = await tryEnqueue(url);
      if (jobId){
        showToast("Queued. You can stay on this page.");
        await poll(jobId);
        return;
      }

      // Fallback: direct
      setStatus("warn","Starting direct download…");
      progress.style.display = "block";
      const dl = "/download?url=" + encodeURIComponent(url);
      window.open(dl, "_blank");
      setTimeout(()=> {
        progress.style.display="none";
        setStatus("ok","If your download didn’t start, the queue mode is recommended for long videos.");
      }, 1500);
    });

    // Prefill if URL param exists (?url=...)
    try{
      const qs = new URLSearchParams(location.search);
      const u = qs.get("url");
      if (u){ urlInput.value = u; setStatus("ok","URL prefilled"); }
    }catch{}
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