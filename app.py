import os
import base64
import re
import time
import json
import urllib.parse
import urllib.request
import threading
from pathlib import Path
from uuid import uuid4
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
CLIENTS_TO_TRY = ["web", "web_safari", "web_embedded", "tv", "ios", "android"]

def safe_filename(name: str, ext: str = "mp3") -> str:
    name = SAFE_CHARS.sub("", name).strip() or "audio"
    return f"{name}.{ext}"

def _base_ydl_opts(out_default: str, cookiefile: str | None, dsid: str | None, client: str):
    opts = {
        "format": "bestaudio/best",
        "paths": {"home": str(DOWNLOAD_DIR), "temp": str(DOWNLOAD_DIR)},
        "outtmpl": {"default": out_default},
        "noprogress": True,
        "quiet": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 1,
        "geo_bypass": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "extractor_args": {
            "youtube": {
                "player_client": [client],
                "player_skip": ["webpage"],
                **({"data_sync_id": [dsid]} if (dsid and client.startswith("web")) else {}),
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.youtube.com/",
        },
        # "force_ip": "0.0.0.0",
        # "throttledratelimit": 102400,
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts

def _resolve_mp3_path(ydl: yt_dlp.YoutubeDL, info) -> Path:
    try:
        pre = Path(ydl.prepare_filename(info))
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

# ===================== Minimal UI =====================
HOME_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>YouTube → MP3</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />

  <!-- ====== THEME ====== -->
  <style>
    :root{
      --bg:#0b0c10;
      --bg-accent:#0a0c13;
      --glass:rgba(255,255,255,.04);
      --glass-2:rgba(255,255,255,.06);
      --text:#e7eaee;
      --muted:#9aa3ad;
      --border:#1b1e26;
      --brand:#6aa6ff;
      --brand-2:#7bc6ff;
      --brand-strong:#4285f4;
      --ok:#16c292;
      --warn:#ffb020;
      --err:#ef4444;
      --shadow-1:0 12px 40px rgba(0,0,0,.35);
      --shadow-2:0 20px 60px rgba(0,0,0,.45);
      --radius-xl:22px;
      --radius-lg:16px;
      --radius:14px;
      --focus:0 0 0 2px rgba(106,166,255,.45);
      --grid-gap:18px;
    }
    @media (prefers-color-scheme: light){
      :root{
        --bg:#f6f7fb;
        --bg-accent:#eef3fb;
        --glass:rgba(255,255,255,.75);
        --glass-2:rgba(255,255,255,.9);
        --text:#0f1115;
        --muted:#5f6b76;
        --border:#e8ecf2;
        --brand:#3b7cff;
        --brand-2:#5aa8ff;
        --brand-strong:#1f6bff;
        --shadow-1:0 12px 40px rgba(10,22,37,.09);
        --shadow-2:0 20px 60px rgba(10,22,37,.12);
        --focus:0 0 0 2px rgba(31,107,255,.30);
      }
    }

    *{box-sizing:border-box}
    html,body{
      margin:0; background:radial-gradient(1200px 800px at 10% -10%, #142039 0%, transparent 60%),
                 radial-gradient(1000px 700px at 110% 0%, #1c1f2b 0%, transparent 55%),
                 var(--bg);
      color:var(--text);
      font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;
      min-height:100%;
    }

    /* Floating gradient orbs for depth */
    .orb{position:fixed;filter:blur(60px);opacity:.35;pointer-events:none;z-index:0}
    .orb.a{width:420px;height:420px;left:-120px;top:-80px;background:radial-gradient(closest-side, #3576ff, transparent)}
    .orb.b{width:360px;height:360px;right:-100px;top:-40px;background:radial-gradient(closest-side, #7b3bff, transparent)}
    .orb.c{width:520px;height:520px;left:20%;bottom:-180px;background:radial-gradient(closest-side, #00d4ff, transparent)}

    .container{position:relative;z-index:1;max-width:1050px;margin:56px auto 90px; padding:0 22px}

    /* ====== HERO ====== */
    .hero{
      display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center;margin-bottom:18px
    }
    .title{
      display:flex;gap:14px;align-items:center
    }
    .logo{
      width:54px;height:54px;border-radius:18px;box-shadow:var(--shadow-2);
      background:conic-gradient(from 140deg, var(--brand) 0 35%, var(--brand-2) 35% 70%, var(--brand-strong) 70% 100%);
      display:grid;place-items:center;color:#fff;font-weight:900;letter-spacing:.4px
    }
    .logo span{font-size:14px}
    h1{margin:0;font-size:32px;letter-spacing:.2px}
    .sub{margin:4px 0 0;color:var(--muted)}

    .pill{
      border:1px solid var(--border);border-radius:999px;padding:10px 14px;background:var(--glass);
      backdrop-filter:blur(8px); box-shadow:var(--shadow-1);font-weight:700
    }

    /* ====== GRID ====== */
    .grid{
      display:grid;
      grid-template-columns: 2.2fr 1.2fr;
      gap:var(--grid-gap);
    }
    @media (max-width: 940px){
      .grid{grid-template-columns:1fr}
    }

    /* ====== PANELS ====== */
    .panel{
      background:linear-gradient(180deg, var(--glass-2), var(--glass));
      border:1px solid var(--border);
      border-radius:var(--radius-xl);
      box-shadow:var(--shadow-2);
      overflow:hidden;
    }
    .panel .hd{
      padding:18px 20px;border-bottom:1px solid var(--border);
      background:linear-gradient(180deg, rgba(255,255,255,.03), transparent);
      display:flex;align-items:center;justify-content:space-between
    }
    .panel .bd{padding:20px}
    .panel .ft{padding:14px 20px;border-top:1px solid var(--border);background:linear-gradient(180deg, transparent, rgba(255,255,255,.02))}

    /* ====== FORM ====== */
    .row{display:flex;gap:12px;flex-wrap:wrap}
    input[type="url"]{
      flex:1;min-width:280px;padding:15px 16px;border:1px solid var(--border);
      background:rgba(0,0,0,.15);color:var(--text);border-radius:16px;
      outline:none;transition:box-shadow .2s, border .2s;
    }
    input::placeholder{color:var(--muted)}
    input:focus{box-shadow:var(--focus);border-color:transparent}

    .btn{
      padding:14px 18px;border:none;border-radius:16px;cursor:pointer;font-weight:800;
      transition:transform .06s ease, background .2s, opacity .2s;
    }
    .btn.primary{
      background:linear-gradient(135deg, var(--brand), var(--brand-strong));
      color:white; box-shadow:0 10px 30px rgba(66,133,244,.35);
    }
    .btn.primary:hover{transform:translateY(-1px)}
    .btn.ghost{
      background:transparent;color:var(--text);border:1px solid var(--border)
    }
    .btn.ghost:hover{background:rgba(255,255,255,.06)}
    .btn:disabled{opacity:.6;cursor:not-allowed}

    /* ====== STATUS / PROGRESS ====== */
    .status{display:flex;align-items:center;gap:10px;color:var(--muted)}
    .dot{width:10px;height:10px;border-radius:50%;background:var(--muted)}
    .dot.ok{background:var(--ok)} .dot.err{background:var(--err)} .spin{width:16px;height:16px;border-radius:50%;border:2px solid var(--muted);border-top-color:transparent;animation:spin .8s linear infinite}
    @keyframes spin{to{transform:rotate(360deg)}}

    .timeline{display:flex;gap:10px;margin-top:14px}
    .step{flex:1;height:6px;background:rgba(255,255,255,.08);border-radius:999px;overflow:hidden}
    .bar{height:100%;width:0%;background:linear-gradient(90deg, var(--brand), var(--brand-2));box-shadow:0 4px 16px rgba(66,133,244,.35)}
    .bar.fill{width:100%;transition:width 1.2s ease}

    /* ====== HISTORY ====== */
    .list{display:flex;flex-direction:column;gap:10px}
    .item{
      display:grid;grid-template-columns:1fr auto auto;gap:12px;align-items:center;
      border:1px solid var(--border);border-radius:14px;padding:12px;background:rgba(0,0,0,.12)
    }
    .title{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .badge{font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid var(--border);color:var(--muted)}
    .badge.ok{color:var(--ok);border-color:var(--ok)} .badge.err{color:var(--err);border-color:var(--err)}
    .dl{padding:8px 10px;border-radius:10px;border:1px solid var(--border);background:transparent;color:var(--text);cursor:pointer}
    .dl:hover{background:rgba(255,255,255,.06)}

    /* ====== SIDECARD ====== */
    .callout{
      background:linear-gradient(180deg, var(--glass-2), var(--glass));
      border:1px solid var(--border);
      border-radius:var(--radius-lg);
      padding:16px;
      box-shadow:var(--shadow-1);
    }
    .callout h3{margin:0 0 6px 0}
    .min{font-size:13.5px;color:var(--muted)}

    /* Footer */
    .foot{margin-top:22px;color:var(--muted);font-size:13px;text-align:center}
    .kbd{background:rgba(127,127,127,.18);padding:2px 6px;border-radius:6px}
  </style>
</head>

<body>
  <!-- background depth -->
  <div class="orb a"></div>
  <div class="orb b"></div>
  <div class="orb c"></div>

  <div class="container">
    <!-- ====== HERO ====== -->
    <div class="hero">
      <div class="title">
        <div class="logo"><span>MP3</span></div>
        <div>
          <h1>YouTube → MP3 (Heroku)</h1>
          <div class="sub">Layered UI, long-video queue, and one-click download.</div>
        </div>
      </div>
      <div class="pill">192kbps • MP3 • Throttle-smart</div>
    </div>

    <!-- ====== GRID ====== -->
    <div class="grid">
      <!-- Left: main panel -->
      <div class="panel">
        <div class="hd">
          <div>Convert a video</div>
          <div style="display:flex;gap:10px">
            <button id="paste" class="btn ghost" type="button" title="Paste from clipboard">Paste</button>
            <button id="sample" class="btn ghost" type="button" title="Fill with sample">Try sample</button>
          </div>
        </div>
        <div class="bd">
          <form id="form" class="row" autocomplete="off">
            <input id="url" type="url" required placeholder="https://www.youtube.com/watch?v=..." />
            <button id="convert" class="btn primary" type="submit">Convert</button>
          </form>

          <div style="margin-top:14px;display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap">
            <div class="status">
              <div id="statusIcon" class="dot"></div>
              <div id="statusText">Ready</div>
            </div>
            <div class="timeline" style="min-width:220px;flex:1">
              <div class="step"><div id="bar1" class="bar"></div></div>
              <div class="step"><div id="bar2" class="bar"></div></div>
              <div class="step"><div id="bar3" class="bar"></div></div>
            </div>
          </div>
        </div>
        <div class="ft">
          <div class="min">Tip: press <span class="kbd">Ctrl</span> + <span class="kbd">V</span> to paste a link instantly.</div>
        </div>
      </div>

      <!-- Right: features + recent -->
      <div style="display:flex;flex-direction:column;gap:var(--grid-gap)">
        <div class="callout">
          <h3>Why this is fast</h3>
          <div class="min">Multiple YouTube client fallbacks, m4a-first fetch, and queued conversion keep long videos stable—even on Heroku.</div>
        </div>

        <div class="panel">
          <div class="hd">Recent jobs</div>
          <div class="bd">
            <div id="history" class="list"></div>
            <div id="empty" class="min">No jobs yet—convert something!</div>
          </div>
        </div>
      </div>
    </div>

    <div class="foot">Built with yt-dlp • Flask • FFmpeg • Async jobs</div>
  </div>

  <!-- ====== APP SCRIPT ====== -->
  <script>
    const $ = (id) => document.getElementById(id);
    const statusIcon = $("statusIcon");
    const statusText = $("statusText");
    const bars = [ $("bar1"), $("bar2"), $("bar3") ];
    const history = $("history"), empty = $("empty");

    function setStatus(kind, text){
      statusText.textContent = text || "";
      statusIcon.className = kind === "spin" ? "spin" : ("dot" + (kind ? " " + kind : ""));
    }
    function prog(step){  // 0..3
      bars.forEach((b,i)=> b.classList.toggle("fill", i < step));
    }
    function addHistory(item){
      empty.style.display = "none";
      const row = document.createElement("div");
      row.className = "item";
      row.innerHTML = \`
        <div class="title" title="\${item.title || item.url}">\${(item.title || item.url || "").replace(/</g,"&lt;")}</div>
        <div class="badge \${item.status==="done"?"ok":item.status==="error"?"err":""}">\${item.status}</div>
        <button class="dl" \${item.status==="done"?"":"disabled"}>Download</button>
      \`;
      const btn = row.querySelector(".dl");
      if(item.status==="done" && item.job_id){
        btn.addEventListener("click", ()=> window.open("/download_job/"+item.job_id,"_blank"));
      }
      history.prepend(row);
    }

    $("paste").onclick = async () => {
      try{
        const t = await navigator.clipboard.readText();
        if(t){
          $("url").value = t.trim();
          setStatus("ok", "Pasted from clipboard");
        } else setStatus("", "Clipboard is empty");
      }catch{ setStatus("err","Clipboard access denied"); }
    };
    $("sample").onclick = () => {
      $("url").value = "https://www.youtube.com/watch?v=Dx5qFachd3A";
      setStatus("ok","Sample URL filled");
    };

    $("form").addEventListener("submit", async (e)=>{
      e.preventDefault();
      const url = $("url").value.trim();
      if(!/^https?:\\/\\/(www\\.)?(youtube\\.com|youtu\\.be)\\//i.test(url)){
        setStatus("err","Please enter a valid YouTube URL");
        return;
      }

      // queue
      setStatus("spin","Queuing…"); prog(1); $("convert").disabled = true;
      try{
        const r = await fetch("/enqueue", {
          method:"POST",
          headers:{"Content-Type":"application/x-www-form-urlencoded"},
          body:new URLSearchParams({ url })
        });
        if(!r.ok){ setStatus("err","Failed to queue"); $("convert").disabled=false; return; }
        const { job_id } = await r.json();
        if(!job_id){ setStatus("err","No job id"); $("convert").disabled=false; return; }

        // poll
        setStatus("spin","Working… Fetching & converting"); prog(2);
        const start = Date.now(), POLL=3000, LIMIT=30*60*1000;
        const iv = setInterval(async ()=>{
          try{
            const s = await fetch("/status/"+job_id);
            const data = await s.json();
            if(data.status==="done"){
              clearInterval(iv);
              setStatus("ok","Ready! Download starting…"); prog(3);
              addHistory({ title:data.title||url, status:"done", job_id, url });
              window.open("/download_job/"+job_id,"_blank");
              $("convert").disabled = false;
            }else if(data.status==="error"){
              clearInterval(iv);
              setStatus("err", data.error || "Conversion failed"); prog(0);
              addHistory({ title:url, status:"error" });
              $("convert").disabled = false;
            }else{
              if(Date.now()-start > LIMIT){
                clearInterval(iv);
                setStatus("err","Timed out (30m)"); prog(0);
                addHistory({ title:url, status:"error" });
                $("convert").disabled = false;
              }
            }
          }catch{
            clearInterval(iv);
            setStatus("err","Status check failed"); prog(0);
            addHistory({ title:url, status:"error" });
            $("convert").disabled = false;
          }
        }, POLL);
      }catch{
        setStatus("err","Network error"); prog(0); $("convert").disabled=false;
      }
    });

    // initial
    setStatus("", "Ready"); prog(0);
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

# ===================== Async Job System =====================
JOBS = {}  # job_id -> {"status": "queued|working|done|error", "title": str|None, "path": str|None, "error": str|None, "ts": float}
JOBS_LOCK = threading.Lock()

def _set_job(job_id, **kwargs):
    with JOBS_LOCK:
        job = JOBS.get(job_id, {})
        job.update(kwargs)
        JOBS[job_id] = job

def _cleanup_file_later(path: str, delay: int = 30):
    def _runner(p=path, d=delay):
        try:
            time.sleep(d)
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass
    threading.Thread(target=_runner, daemon=True).start()

def _purge_old_jobs(max_age_seconds: int = 3600):
    now = time.time()
    with JOBS_LOCK:
        to_del = [jid for jid, j in JOBS.items() if now - j.get("ts", now) > max_age_seconds]
        for jid in to_del:
            JOBS.pop(jid, None)

def _worker(job_id: str, url: str):
    _set_job(job_id, status="working", ts=time.time())
    cookiefile = str(COOKIE_PATH) if COOKIE_PATH and COOKIE_PATH.exists() else None
    try:
        # 1) download
        title, mp3_path = download_audio_with_fallback(url, OUT_DEFAULT, cookiefile, YTDLP_DATA_SYNC_ID)

        # 2) improve title if needed
        if not title or title.strip().lower() == "audio":
            t2 = fetch_title_with_ytdlp(url, cookiefile, YTDLP_DATA_SYNC_ID)
            if t2: title = t2
        if not title or title.strip().lower() == "audio":
            t3 = fetch_title_oembed(url)
            if t3: title = t3
        title = title or "audio"

        _set_job(job_id, status="done", title=title, path=mp3_path)
        _purge_old_jobs()
    except Exception as e:
        _set_job(job_id, status="error", error=str(e))
        _purge_old_jobs()

@app.post("/enqueue")
def enqueue():
    url = request.form.get("url") or (request.json or {}).get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    job_id = uuid4().hex
    _set_job(job_id, status="queued", title=None, path=None, error=None, ts=time.time())
    threading.Thread(target=_worker, args=(job_id, url), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.get("/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"status": "error", "error": "job not found"}), 404
    return jsonify({
        "status": job.get("status"),
        "title": job.get("title"),
        "error": job.get("error"),
    })

@app.get("/download_job/<job_id>")
def download_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    if job.get("status") != "done" or not job.get("path"):
        return jsonify({"error": "job not ready"}), 409

    mp3_path = job["path"]
    title = job.get("title") or "audio"
    safe_name = safe_filename(title, "mp3")
    try:
        resp = send_file(mp3_path, mimetype="audio/mpeg", as_attachment=True, download_name=safe_name)
        resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
        _cleanup_file_later(mp3_path, delay=30)
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ===================== (Optional) direct /download kept for bare calls =====================
@app.route("/download", methods=["GET", "POST"])
def download():
    url = request.args.get("url") or request.form.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    try:
        cookiefile = str(COOKIE_PATH) if COOKIE_PATH and COOKIE_PATH.exists() else None
        title, mp3_path = download_audio_with_fallback(url, OUT_DEFAULT, cookiefile=cookiefile, dsid=YTDLP_DATA_SYNC_ID)
        if not title or title.strip().lower() == "audio":
            t2 = fetch_title_with_ytdlp(url, cookiefile, YTDLP_DATA_SYNC_ID)
            if t2: title = t2
        if not title or title.strip().lower() == "audio":
            t3 = fetch_title_oembed(url)
            if t3: title = t3
        safe_name = safe_filename(title or "audio", "mp3")
        resp = send_file(mp3_path, mimetype="audio/mpeg", as_attachment=True, download_name=safe_name)
        resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
        _cleanup_file_later(mp3_path, delay=30)
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
