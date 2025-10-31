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
  <meta charset="utf-8" />
  <title>YouTube → MP3</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />

  <style>
    :root{
      --bg:#0b0c10; --panel:#0f1115; --panel-2:#0b0d12;
      --text:#e6e8eb; --muted:#9aa3ad; --border:#1b1e26;
      --brand:#6aa6ff; --brand-strong:#4285f4; --ok:#13b884; --err:#ef4444;
      --radius:18px; --shadow:0 18px 40px rgba(0,0,0,.35);
      --focus:0 0 0 2px rgba(106,166,255,.45);
    }
    @media (prefers-color-scheme: light){
      :root{
        --bg:#f6f7fb; --panel:#ffffff; --panel-2:#f3f6fb;
        --text:#0f1115; --muted:#5f6b76; --border:#e8ecf2;
        --brand:#3b7cff; --brand-strong:#1f6bff; --shadow:0 18px 40px rgba(10,22,37,.08);
        --focus:0 0 0 2px rgba(31,107,255,.28);
      }
    }

    *{box-sizing:border-box}
    html,body{margin:0;background:var(--bg);color:var(--text);
      font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}

    .container{max-width:900px;margin:56px auto;padding:0 18px}

    .hero{
      display:flex;align-items:center;gap:14px;margin-bottom:16px
    }
    .logo{
      width:42px;height:42px;border-radius:14px;
      background:linear-gradient(135deg,var(--brand),var(--brand-strong));
      display:grid;place-items:center;color:#fff;font-weight:800;letter-spacing:.4px
    }
    h1{margin:0;font-size:28px}
    p.subtitle{margin:4px 0 22px;color:var(--muted)}

    .card{
      background:var(--panel);border:1px solid var(--border);
      border-radius:var(--radius);box-shadow:var(--shadow);
      padding:20px
    }

    .input-row{display:flex;gap:10px;flex-wrap:wrap}
    input[type="url"]{
      flex:1;min-width:280px;padding:14px 16px;border:1px solid var(--border);
      background:transparent;color:var(--text);border-radius:14px;outline:none;
      transition:box-shadow .2s,border .2s
    }
    input::placeholder{color:var(--muted)}
    input:focus{box-shadow:var(--focus);border-color:transparent}

    .btn{
      padding:14px 18px;border:none;border-radius:14px;cursor:pointer;
      font-weight:700
    }
    .btn.primary{background:var(--brand);color:#fff}
    .btn.primary:hover{background:var(--brand-strong)}
    .btn.ghost{background:transparent;color:var(--text);border:1px solid var(--border)}
    .btn.ghost:hover{background:rgba(127,127,127,.06)}
    .btn:disabled{opacity:.6;cursor:not-allowed}

    .row{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}

    .status{
      display:flex;align-items:center;gap:10px;margin-top:14px;color:var(--muted)
    }
    .dot{width:10px;height:10px;border-radius:50%;background:var(--muted)}
    .dot.ok{background:var(--ok)}
    .dot.err{background:var(--err)}
    .spinner{
      width:16px;height:16px;border-radius:50%;
      border:2px solid var(--muted);border-top-color:transparent;
      animation:spin .8s linear infinite
    }
    @keyframes spin{to{transform:rotate(360deg)}}

    .panel{
      margin-top:18px;background:var(--panel-2);
      border:1px dashed var(--border);border-radius:14px;padding:14px
    }
    .tiny{font-size:13px;color:var(--muted)}
    code.k{background:rgba(127,127,127,.15);padding:2px 6px;border-radius:6px}
    .history{display:flex;flex-direction:column;gap:8px;margin-top:10px}
    .item{
      display:flex;align-items:center;gap:10px;justify-content:space-between;
      background:transparent;border:1px solid var(--border);border-radius:12px;padding:10px 12px
    }
    .item-title{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:70%}
    .badge{font-size:12px;padding:3px 8px;border-radius:999px;border:1px solid var(--border);color:var(--muted)}
    .badge.ok{color:var(--ok);border-color:var(--ok)}
    .badge.err{color:var(--err);border-color:var(--err)}
  </style>
</head>
<body>
  <div class="container">
    <div class="hero">
      <div class="logo">MP3</div>
      <div>
        <h1>YouTube → MP3 (Heroku)</h1>
        <p class="subtitle">Fast MP3 downloads with long-video support. Paste a link, queue, and we’ll ping you when it’s ready.</p>
      </div>
    </div>

    <div class="card">
      <form id="form">
        <div class="input-row">
          <input id="url" type="url" required placeholder="https://www.youtube.com/watch?v=..." />
          <button id="convert" class="btn primary" type="submit">Convert</button>
        </div>

        <div class="row">
          <button id="paste" class="btn ghost" type="button">Paste</button>
          <button id="sample" class="btn ghost" type="button">Try sample</button>
        </div>

        <div id="status" class="status">
          <div id="statusIcon" class="dot"></div>
          <div id="statusText">Ready</div>
        </div>
      </form>

      <div class="panel tiny">
        <b>How it works</b> — We enqueue your job (<code class="k">/enqueue</code>), the server fetches & converts,
        then we offer a download when it’s done. Great for 1-hour videos.
      </div>

      <div id="historyWrap" class="panel" style="display:none">
        <div class="tiny"><b>Recent jobs</b></div>
        <div id="history" class="history"></div>
      </div>
    </div>
  </div>

  <script>
    // ---------- DOM helpers ----------
    const $ = (id) => document.getElementById(id);
    const statusIcon = $("statusIcon");
    const statusText = $("statusText");
    const historyWrap = $("historyWrap");
    const historyList = $("history");

    function setStatus(kind, text){
      statusText.textContent = text;
      statusIcon.className = kind === "spin" ? "spinner" : "dot" + (kind ? " " + kind : "");
    }

    function addHistory(entry){
      const div = document.createElement("div");
      div.className = "item";
      div.innerHTML = \`
        <div class="item-title">\${entry.title || entry.url}</div>
        <div class="badge \${entry.status === "done" ? "ok" : entry.status === "error" ? "err" : ""}">\${entry.status}</div>
      \`;
      if (entry.status === "done" && entry.job_id){
        div.style.cursor = "pointer";
        div.title = "Download";
        div.onclick = () => window.open("/download_job/" + entry.job_id, "_blank");
      }
      historyList.prepend(div);
      historyWrap.style.display = "block";
    }

    // ---------- Actions ----------
    $("paste").onclick = async () => {
      try{
        const t = await navigator.clipboard.readText();
        if (t) $("url").value = t.trim();
        setStatus("ok", t ? "Pasted from clipboard" : "warn", t ? "" : "Clipboard empty");
      }catch{ setStatus("err","Clipboard access denied"); }
    };

    $("sample").onclick = () => {
      $("url").value = "https://www.youtube.com/watch?v=Dx5qFachd3A"; // long, safe sample
      setStatus("ok","Sample URL filled");
    };

    $("form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const url = $("url").value.trim();
      if(!url){ setStatus("err","Please enter a valid YouTube URL"); return; }

      // Queue
      setStatus("spin","Queuing…");
      $("convert").disabled = true;

      try{
        const r = await fetch("/enqueue", {
          method: "POST",
          headers: {"Content-Type":"application/x-www-form-urlencoded"},
          body: new URLSearchParams({ url })
        });
        if(!r.ok){ setStatus("err","Failed to queue"); $("convert").disabled = false; return; }
        const { job_id } = await r.json();
        if(!job_id){ setStatus("err","No job id"); $("convert").disabled = false; return; }

        // Poll
        setStatus("spin","Working… fetching & converting");
        const start = Date.now();
        const iv = setInterval(async () => {
          try{
            const s = await fetch("/status/" + job_id);
            const data = await s.json();

            if (data.status === "done") {
              clearInterval(iv);
              setStatus("ok","Ready! Download starting…");
              addHistory({ title: data.title, status: "done", job_id, url });
              window.open("/download_job/" + job_id, "_blank");
              $("convert").disabled = false;
            } else if (data.status === "error") {
              clearInterval(iv);
              setStatus("err", data.error || "Conversion failed");
              addHistory({ title: data.title || url, status: "error" });
              $("convert").disabled = false;
            } else {
              // still working
              if (Date.now() - start > 30*60*1000) {
                clearInterval(iv);
                setStatus("err","Timed out (30m)");
                addHistory({ title: url, status: "error" });
                $("convert").disabled = false;
              }
            }
          }catch{
            clearInterval(iv);
            setStatus("err","Status check failed");
            addHistory({ title: url, status: "error" });
            $("convert").disabled = false;
          }
        }, 3000);
      }catch{
        setStatus("err","Network error");
        $("convert").disabled = false;
      }
    });

    // ---------- Init ----------
    setStatus("", "Ready");
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