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
    /* ========= THEME ========= */
    :root{
      --bg:#0b0c10;
      --bg-2:#0e1118;
      --glass:#10131a90;        /* translucent */
      --panel:#0f1115;          /* solid */
      --text:#e8edf3;
      --muted:#9aa3ad;
      --border:#1b1e26;
      --brand:#6aa6ff;
      --brand-2:#8c7dff;
      --brand-3:#29d3c8;
      --ok:#13b884;
      --err:#ef4444;
      --warn:#ffb020;

      --radius-xl:22px;
      --radius-lg:18px;
      --radius:14px;

      --shadow-xl:0 30px 80px rgba(0,0,0,.55), 0 6px 20px rgba(0,0,0,.35);
      --shadow:0 16px 40px rgba(0,0,0,.35), 0 4px 14px rgba(0,0,0,.25);
      --focus:0 0 0 2px rgba(106,166,255,.45);
      --glass-blur:14px;
    }
    @media (prefers-color-scheme: light){
      :root{
        --bg:#edf2f8; --bg-2:#e9eef6;
        --glass:#ffffffc7; --panel:#ffffff;
        --text:#0f1115; --muted:#5f6b76; --border:#e6ebf2;
        --shadow-xl:0 30px 80px rgba(10,22,37,.14), 0 6px 20px rgba(10,22,37,.08);
        --shadow:0 16px 40px rgba(10,22,37,.12), 0 4px 14px rgba(10,22,37,.08);
        --focus:0 0 0 2px rgba(31,107,255,.28);
      }
    }
    *{box-sizing:border-box}

    /* ========= BACKGROUND LAYERS ========= */
    html,body{height:100%}
    body{
      margin:0; color:var(--text);
      font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;
      background:
        radial-gradient(1200px 800px at 10% -10%, rgba(108,141,255,.18), transparent 55%),
        radial-gradient(1200px 800px at 110% 10%, rgba(41,211,200,.18), transparent 55%),
        linear-gradient(180deg, var(--bg), var(--bg-2));
      overflow-x:hidden;
    }
    .fx{
      position:fixed; inset:-20vmax -20vmax auto auto; pointer-events:none; z-index:0;
      width:60vmax; height:60vmax; filter:blur(40px); opacity:.35;
      background: radial-gradient(closest-side, #5f7cff 0%, transparent 62%),
                  radial-gradient(closest-side, #33d1c0 0%, transparent 62%),
                  radial-gradient(closest-side, #8b7bff 0%, transparent 62%);
      mix-blend-mode:screen; animation:float 18s ease-in-out infinite alternate;
    }
    @keyframes float{
      0%{ transform:translate3d(0,0,0) rotate(0deg); }
      100%{ transform:translate3d(-6vmax,4vmax,0) rotate(12deg); }
    }

    /* ========= LAYOUT ========= */
    .wrap{position:relative; z-index:1; max-width:1100px; margin:64px auto 100px; padding:0 22px}

    .hero{
      display:grid; grid-template-columns: 74px 1fr auto; gap:18px; align-items:center; margin-bottom:18px;
    }
    .logo{
      width:74px;height:74px;border-radius:20px;
      background:conic-gradient(from 180deg, var(--brand), var(--brand-2), var(--brand-3), var(--brand));
      box-shadow: inset 0 0 0 2px rgba(255,255,255,.08), var(--shadow);
      display:grid; place-items:center; color:#fff; font-weight:900; letter-spacing:.6px; font-size:18px;
    }
    .title{margin:0; font-size:36px; line-height:1.1; letter-spacing:.2px}
    .subtitle{margin:6px 0 0; color:var(--muted)}

    .glass{
      backdrop-filter: blur(var(--glass-blur));
      -webkit-backdrop-filter: blur(var(--glass-blur));
      background:var(--glass);
      border:1px solid var(--border);
      border-radius:var(--radius-xl);
      box-shadow:var(--shadow-xl);
    }

    .panel{ padding:22px; margin-top:18px }
    .grid{ display:grid; gap:18px }
    @media (min-width:900px){ .grid{ grid-template-columns: 1.4fr .8fr } }

    /* ========= CONVERTER CARD ========= */
    .card{
      background:linear-gradient(180deg, rgba(255,255,255,.02), transparent), var(--panel);
      border:1px solid var(--border);
      border-radius:var(--radius-xl);
      box-shadow:var(--shadow);
      padding:22px;
    }

    .row{ display:flex; gap:12px; flex-wrap:wrap; align-items:center }
    .inp{
      flex:1; min-width:320px;
      border:1px solid var(--border); background:transparent; color:var(--text);
      border-radius:16px; padding:14px 16px; outline:none; transition:box-shadow .2s,border .2s;
    }
    .inp::placeholder{ color:var(--muted) }
    .inp:focus{ box-shadow:var(--focus); border-color:transparent }

    .btn{
      padding:14px 18px; border:none; border-radius:16px; cursor:pointer; font-weight:800;
      letter-spacing:.2px; transition:transform .05s ease, filter .2s, background .2s, opacity .2s;
    }
    .btn:active{ transform:translateY(1px) }
    .btn[disabled]{ opacity:.6; cursor:not-allowed }
    .btn.primary{
      color:#fff;
      background:linear-gradient(135deg, var(--brand), var(--brand-2) 60%, var(--brand-3));
      box-shadow:0 12px 28px rgba(92,122,255,.35);
    }
    .btn.primary:hover{ filter:saturate(1.1) brightness(1.05) }
    .btn.ghost{
      background:transparent; color:var(--text); border:1px solid var(--border);
    }
    .btn.ghost:hover{ background:rgba(127,127,127,.07) }

    .tools{ display:flex; gap:10px; flex-wrap:wrap; margin-top:12px }
    .hint{ color:var(--muted); font-size:13px }

    /* status + progress */
    .status{ display:flex; align-items:center; gap:10px; margin-top:14px; color:var(--muted) }
    .dot{ width:10px; height:10px; border-radius:50%; background:var(--muted) }
    .dot.ok{ background:var(--ok) } .dot.err{ background:var(--err) } .dot.warn{ background:var(--warn) }
    .spin{ width:16px; height:16px; border-radius:50%; border:2px solid var(--muted); border-top-color:transparent; animation:spin .8s linear infinite }
    @keyframes spin{ to{ transform:rotate(360deg) } }

    .bar{ position:relative; height:8px; border-radius:999px; background:rgba(127,127,127,.18); overflow:hidden; margin-top:8px }
    .bar > i{
      position:absolute; inset:0; width:35%;
      background:linear-gradient(90deg, var(--brand), var(--brand-2));
      border-radius:999px; animation:flow 1.4s ease-in-out infinite;
    }
    @keyframes flow{
      0% { left:-40% } 50% { left:45% } 100% { left:105% }
    }

    /* ========= SIDE: FEATURES ========= */
    .features{ display:grid; gap:14px }
    .feat{
      display:grid; grid-template-columns: 42px 1fr; gap:12px; align-items:center;
      background:linear-gradient(180deg, rgba(255,255,255,.02), transparent), var(--panel);
      border:1px solid var(--border); border-radius:16px; padding:14px;
      box-shadow: 0 6px 16px rgba(0,0,0,.18);
    }
    .ico{
      width:42px;height:42px;border-radius:12px; display:grid; place-items:center; color:#fff;
      background:linear-gradient(135deg, var(--brand), var(--brand-2)); font-weight:900;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.18);
    }
    .feat h4{ margin:0 0 4px; font-size:15px }
    .feat p{ margin:0; color:var(--muted); font-size:13px }

    /* ========= HISTORY ========= */
    .history{ margin-top:18px; }
    .history h3{ margin:0 0 10px }
    .items{ display:flex; flex-direction:column; gap:10px }
    .item{
      display:flex; align-items:center; justify-content:space-between; gap:12px;
      background:linear-gradient(180deg, rgba(255,255,255,.02), transparent), var(--panel);
      border:1px solid var(--border); border-radius:14px; padding:12px 14px;
    }
    .item .name{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:66% }
    .badge{ font-size:12px; padding:4px 10px; border-radius:999px; border:1px solid var(--border); color:var(--muted) }
    .badge.ok{ color:var(--ok); border-color:var(--ok) }
    .badge.err{ color:var(--err); border-color:var(--err) }

    /* ========= FOOTER ========= */
    footer{opacity:.75; text-align:center; margin:28px 0 8px; font-size:13px; color:var(--muted)}

  </style>
</head>
<body>
  <div class="fx"></div>
  <div class="wrap">
    <!-- HERO -->
    <div class="hero">
      <div class="logo">MP3</div>
      <div>
        <h1 class="title">YouTube → MP3 (Heroku)</h1>
        <div class="subtitle">Layered depth, glass panels, and a resilient queue for long videos.</div>
      </div>
      <div class="glass" style="padding:10px 14px;border-radius:999px; font-weight:700">192 kbps • MP3</div>
    </div>

    <!-- GRID -->
    <div class="grid">
      <!-- LEFT: CONVERTER -->
      <div class="card">
        <form id="form">
          <div class="row">
            <input id="url" class="inp" type="url" required placeholder="https://www.youtube.com/watch?v=..." />
            <button id="convert" class="btn primary" type="submit">Convert</button>
          </div>

          <div class="tools">
            <button id="paste" type="button" class="btn ghost">Paste</button>
            <button id="sample" type="button" class="btn ghost">Try sample</button>
            <span class="hint">We queue, convert, and notify when ready.</span>
          </div>

          <div class="status">
            <div id="statusIcon" class="dot"></div>
            <div id="statusText">Ready</div>
          </div>
          <div id="progress" class="bar" style="display:none"><i></i></div>
        </form>

        <!-- History -->
        <div class="history">
          <h3>Recent jobs</h3>
          <div id="history" class="items"></div>
        </div>
      </div>

      <!-- RIGHT: FEATURES -->
      <aside class="features">
        <div class="feat">
          <div class="ico">⏱</div>
          <div>
            <h4>Survives timeouts</h4>
            <p>Async queue avoids router limits. One-hour videos? No sweat.</p>
          </div>
        </div>
        <div class="feat">
          <div class="ico">🎧</div>
          <div>
            <h4>Clear 192 kbps MP3</h4>
            <p>FFmpeg post-processing for consistent output quality.</p>
          </div>
        </div>
        <div class="feat">
          <div class="ico">🛡</div>
          <div>
            <h4>Smart fallback clients</h4>
            <p>Multiple YouTube clients to dodge throttling and SABR checks.</p>
          </div>
        </div>
        <div class="feat">
          <div class="ico">⚡</div>
          <div>
            <h4>Fast fragments</h4>
            <p>Parallel fragment fetching for long videos (3× concurrency).</p>
          </div>
        </div>
      </aside>
    </div>

    <footer>Built with Flask + yt-dlp • Glassmorphism UI • Dark/Light aware</footer>
  </div>

  <script>
    // ---------- Helpers ----------
    const $ = (id) => document.getElementById(id);
    const statusIcon = $("statusIcon");
    const statusText = $("statusText");
    const progress = $("progress");
    const historyList = $("history");

    function setStatus(kind, text){
      statusText.textContent = text || "";
      statusIcon.className = kind === "spin" ? "spin" : "dot" + (kind ? " " + kind : "");
      if (kind === "spin") progress.style.display = "block"; else progress.style.display = "none";
    }
    function addHistory({title, status, job_id, url}){
      const el = document.createElement("div");
      el.className = "item";
      el.innerHTML = \`
        <div class="name" title="\${title || url}">\${(title || url || "").replace(/</g,"&lt;")}</div>
        <div class="badge \${status==="done" ? "ok" : status==="error" ? "err" : ""}">\${status}</div>
      \`;
      if (status === "done" && job_id){
        el.style.cursor = "pointer";
        el.title = "Download";
        el.onclick = () => window.open("/download_job/" + job_id, "_blank");
      }
      historyList.prepend(el);
    }

    // ---------- UI Buttons ----------
    $("paste").onclick = async () => {
      try {
        const t = await navigator.clipboard.readText();
        if (t) { $("url").value = t.trim(); setStatus("ok","URL pasted from clipboard"); }
        else   { setStatus("warn","Clipboard is empty"); }
      } catch {
        setStatus("err","Clipboard access denied");
      }
    };
    $("sample").onclick = () => {
      $("url").value = "https://www.youtube.com/watch?v=Dx5qFachd3A";
      setStatus("ok","Sample URL filled");
    };

    // ---------- Submit flow: enqueue → poll → download ----------
    $("form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const url = $("url").value.trim();
      if (!/^https?:\\/\\/(www\\.)?(youtube\\.com|youtu\\.be)\\//i.test(url)) {
        setStatus("err","Please enter a valid YouTube URL");
        return;
      }
      $("convert").disabled = true;
      setStatus("spin","Queuing…");

      try{
        const r = await fetch("/enqueue", {
          method:"POST",
          headers:{"Content-Type":"application/x-www-form-urlencoded"},
          body:new URLSearchParams({ url })
        });
        if (!r.ok) { setStatus("err","Failed to queue"); $("convert").disabled = false; return; }
        const { job_id } = await r.json();
        if (!job_id) { setStatus("err","No job id returned"); $("convert").disabled = false; return; }

        // Poll status every 3s
        setStatus("spin","Working… fetching & converting");
        const start = Date.now(), TIMEOUT = 30*60*1000;
        const iv = setInterval(async () => {
          try{
            const s = await fetch("/status/" + job_id);
            const data = await s.json();

            if (data.status === "done") {
              clearInterval(iv);
              setStatus("ok","Ready! Starting download…");
              addHistory({ title: data.title || url, status:"done", job_id, url });
              window.open("/download_job/" + job_id, "_blank");
              $("convert").disabled = false;
            } else if (data.status === "error") {
              clearInterval(iv);
              setStatus("err", data.error || "Conversion failed");
              addHistory({ title: data.title || url, status:"error" });
              $("convert").disabled = false;
            } else {
              // still queued/working — leave spinner & bar running
              if (Date.now() - start > TIMEOUT) {
                clearInterval(iv);
                setStatus("err","Timed out (30m)");
                addHistory({ title: url, status:"error" });
                $("convert").disabled = false;
              }
            }
          }catch{
            clearInterval(iv);
            setStatus("err","Status check failed");
            addHistory({ title: url, status:"error" });
            $("convert").disabled = false;
          }
        }, 3000);

      }catch{
        setStatus("err","Network error");
        $("convert").disabled = false;
      }
    });

    // initial
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