import os
import base64
import re
import time
import json
import uuid
import urllib.parse
import urllib.request
import threading
import zipfile
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

# Spotify support (optional)
try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    SPOTIPY_AVAILABLE = True
except ImportError:
    SPOTIPY_AVAILABLE = False
    print("⚠ spotipy not installed - Spotify features disabled", flush=True)

app = Flask(__name__)
CORS(app)

# ---------- Configuration ----------
DOWNLOAD_DIR = Path("/tmp")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

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
SAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

CLIENTS_TO_TRY = ["web", "mweb", "mediaconnect", "tv_embedded"]

# Spotify configuration
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
spotify_client = None

if SPOTIPY_AVAILABLE and SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
    try:
        auth_manager = SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET
        )
        spotify_client = spotipy.Spotify(auth_manager=auth_manager)
        print("✓ Spotify client initialized", flush=True)
    except Exception as e:
        print(f"✗ Failed to init Spotify: {e}", flush=True)

job_queue = {}
batch_queue = {}

def safe_filename(name: str) -> str:
    name = SAFE_CHARS.sub("_", name).strip() or "media"
    name = " ".join(name.split())
    if len(name) > 200:
        name = name[:200].rsplit(' ', 1)[0]
    return f"{name}.mp3"

def _base_ydl_opts(out_default: str, cookiefile: str | None, dsid: str | None, client: str, quality: str = "192"):
    opts = {
        "format": "ba/b",
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
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
        "extractor_args": {"youtube": {"player_client": [client], "player_skip": ["configs", "webpage"]}},
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if dsid:
        opts["extractor_args"]["youtube"]["data_sync_id"] = [dsid]
    return opts

def fetch_title_with_ytdlp(url: str) -> str:
    cookiefile = str(COOKIE_PATH) if COOKIE_PATH else None
    dsid = YTDLP_DATA_SYNC_ID
    for client in CLIENTS_TO_TRY:
        try:
            opts = _base_ydl_opts(OUT_DEFAULT, cookiefile, dsid, client, "192")
            opts["skip_download"] = True
            opts["quiet"] = True
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info.get("title", "Unknown")
        except Exception as e:
            continue
    return "Unknown"

def format_duration(seconds):
    if not seconds:
        return "Unknown"
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

def fetch_video_info(url: str) -> dict:
    """Fetch video metadata without downloading"""
    cookiefile = str(COOKIE_PATH) if COOKIE_PATH else None
    dsid = YTDLP_DATA_SYNC_ID
    for client in CLIENTS_TO_TRY:
        try:
            opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
                "extract_flat": False,
                "extractor_args": {"youtube": {"player_client": [client]}},
            }
            if cookiefile:
                opts["cookiefile"] = cookiefile
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return {
                    "id": info.get("id"),
                    "title": info.get("title", "Unknown"),
                    "thumbnail": info.get("thumbnail") or f"https://i.ytimg.com/vi/{info.get('id')}/hqdefault.jpg",
                    "duration": info.get("duration"),
                    "duration_formatted": format_duration(info.get("duration")),
                    "channel": info.get("channel") or info.get("uploader", "Unknown"),
                    "view_count": info.get("view_count"),
                    "url": url,
                }
        except Exception as e:
            print(f"Client {client} failed for preview: {e}", flush=True)
            continue
    return None

def fetch_playlist_info(url: str) -> dict:
    """Fetch playlist metadata and all video info"""
    cookiefile = str(COOKIE_PATH) if COOKIE_PATH else None
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "noplaylist": False,
        }
        if cookiefile:
            opts["cookiefile"] = cookiefile
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist" or "entries" in info:
                entries = info.get("entries", [])
                videos = []
                for entry in entries[:50]:  # Limit to 50 videos
                    if entry:
                        vid_id = entry.get("id") or entry.get("url", "").split("=")[-1]
                        videos.append({
                            "id": vid_id,
                            "title": entry.get("title", "Unknown"),
                            "thumbnail": f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
                            "duration": entry.get("duration"),
                            "duration_formatted": format_duration(entry.get("duration")),
                            "url": f"https://www.youtube.com/watch?v={vid_id}",
                        })
                return {
                    "is_playlist": True,
                    "title": info.get("title", "Playlist"),
                    "channel": info.get("channel") or info.get("uploader", "Unknown"),
                    "video_count": len(videos),
                    "videos": videos,
                }
            return None
    except Exception as e:
        print(f"Playlist fetch error: {e}", flush=True)
        return None

def is_playlist_url(url: str) -> bool:
    return "list=" in url and "watch?v=" not in url.split("list=")[0][-20:]

# ---------- Spotify Functions ----------
def parse_spotify_url(url: str) -> tuple:
    """Parse Spotify URL and return (type, id)"""
    patterns = [
        (r'spotify\.com/playlist/([a-zA-Z0-9]+)', 'playlist'),
        (r'spotify\.com/album/([a-zA-Z0-9]+)', 'album'),
        (r'spotify\.com/track/([a-zA-Z0-9]+)', 'track'),
    ]
    for pattern, url_type in patterns:
        match = re.search(pattern, url)
        if match:
            return (url_type, match.group(1))
    return (None, None)

def get_spotify_playlist(playlist_id: str) -> dict:
    """Fetch Spotify playlist tracks"""
    if not spotify_client:
        return None
    try:
        results = spotify_client.playlist(playlist_id)
        tracks = []
        items = results.get('tracks', {}).get('items', [])
        
        # Handle pagination for large playlists
        while len(tracks) < 50 and items:
            for item in items:
                if len(tracks) >= 50:
                    break
                track = item.get('track')
                if track and track.get('name'):
                    artists = ", ".join([a['name'] for a in track.get('artists', [])])
                    tracks.append({
                        'title': track['name'],
                        'artist': artists,
                        'album': track.get('album', {}).get('name', ''),
                        'duration_ms': track.get('duration_ms', 0),
                        'duration_formatted': format_duration(track.get('duration_ms', 0) // 1000),
                        'thumbnail': track.get('album', {}).get('images', [{}])[0].get('url', ''),
                        'search_query': f"{track['name']} {artists}",
                    })
            
            # Get next page if available
            next_url = results.get('tracks', {}).get('next')
            if next_url and len(tracks) < 50:
                results['tracks'] = spotify_client.next(results['tracks'])
                items = results.get('tracks', {}).get('items', [])
            else:
                break
        
        return {
            'is_spotify': True,
            'type': 'playlist',
            'title': results.get('name', 'Playlist'),
            'owner': results.get('owner', {}).get('display_name', 'Unknown'),
            'thumbnail': results.get('images', [{}])[0].get('url', ''),
            'total_tracks': results.get('tracks', {}).get('total', len(tracks)),
            'tracks': tracks,
        }
    except Exception as e:
        print(f"Spotify playlist error: {e}", flush=True)
        return None

def get_spotify_album(album_id: str) -> dict:
    """Fetch Spotify album tracks"""
    if not spotify_client:
        return None
    try:
        results = spotify_client.album(album_id)
        tracks = []
        for item in results.get('tracks', {}).get('items', [])[:50]:
            artists = ", ".join([a['name'] for a in item.get('artists', [])])
            tracks.append({
                'title': item['name'],
                'artist': artists,
                'album': results.get('name', ''),
                'duration_ms': item.get('duration_ms', 0),
                'duration_formatted': format_duration(item.get('duration_ms', 0) // 1000),
                'thumbnail': results.get('images', [{}])[0].get('url', ''),
                'search_query': f"{item['name']} {artists}",
            })
        
        return {
            'is_spotify': True,
            'type': 'album',
            'title': results.get('name', 'Album'),
            'owner': ", ".join([a['name'] for a in results.get('artists', [])]),
            'thumbnail': results.get('images', [{}])[0].get('url', ''),
            'total_tracks': results.get('total_tracks', len(tracks)),
            'tracks': tracks,
        }
    except Exception as e:
        print(f"Spotify album error: {e}", flush=True)
        return None

def get_spotify_track(track_id: str) -> dict:
    """Fetch single Spotify track"""
    if not spotify_client:
        return None
    try:
        track = spotify_client.track(track_id)
        artists = ", ".join([a['name'] for a in track.get('artists', [])])
        return {
            'is_spotify': True,
            'type': 'track',
            'title': track.get('name', 'Track'),
            'owner': artists,
            'thumbnail': track.get('album', {}).get('images', [{}])[0].get('url', ''),
            'total_tracks': 1,
            'tracks': [{
                'title': track['name'],
                'artist': artists,
                'album': track.get('album', {}).get('name', ''),
                'duration_ms': track.get('duration_ms', 0),
                'duration_formatted': format_duration(track.get('duration_ms', 0) // 1000),
                'thumbnail': track.get('album', {}).get('images', [{}])[0].get('url', ''),
                'search_query': f"{track['name']} {artists}",
            }],
        }
    except Exception as e:
        print(f"Spotify track error: {e}", flush=True)
        return None

def search_youtube_for_track(search_query: str) -> str:
    """Search YouTube and return video URL"""
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "default_search": "ytsearch1",
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.extract_info(f"ytsearch1:{search_query}", download=False)
            if result and 'entries' in result and result['entries']:
                video = result['entries'][0]
                return f"https://www.youtube.com/watch?v={video['id']}"
    except Exception as e:
        print(f"YouTube search error for '{search_query}': {e}", flush=True)
    return None

def download_task(job_id: str, url: str, quality: str):
    job_queue[job_id]["status"] = "downloading"
    cookiefile = str(COOKIE_PATH) if COOKIE_PATH else None
    dsid = YTDLP_DATA_SYNC_ID
    last_error = None
    for client in CLIENTS_TO_TRY:
        try:
            print(f"[{job_id}] Trying client: {client}", flush=True)
            opts = _base_ydl_opts(OUT_DEFAULT, cookiefile, dsid, client, quality)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "Unknown")
                video_id = info.get("id", "unknown")
                downloaded_file = DOWNLOAD_DIR / f"yt_{video_id}.mp3"
                if not downloaded_file.exists():
                    matches = list(DOWNLOAD_DIR.glob(f"yt_{video_id}.*"))
                    if matches:
                        downloaded_file = matches[0]
                if downloaded_file.exists():
                    job_queue[job_id].update({"status": "done", "file_path": str(downloaded_file), "title": title})
                    print(f"[{job_id}] ✓ Completed with {client}", flush=True)
                    return
                else:
                    raise FileNotFoundError("File not found")
        except Exception as e:
            last_error = str(e)
            print(f"[{job_id}] ✗ {client} failed: {e}", flush=True)
            continue
    job_queue[job_id].update({"status": "error", "error": f"All clients failed. {last_error}"})

def batch_download_task(batch_id: str, urls: list, quality: str):
    batch = batch_queue[batch_id]
    for i, url in enumerate(urls):
        job_id = batch["jobs"][i]["job_id"]
        batch["current_index"] = i
        batch["jobs"][i]["status"] = "downloading"
        job_queue[job_id] = {"status": "downloading", "url": url, "title": "Fetching...", "quality": quality, "error": None, "file_path": None, "created_at": datetime.now().isoformat()}
        download_task(job_id, url, quality)
        job = job_queue[job_id]
        batch["jobs"][i]["status"] = job["status"]
        batch["jobs"][i]["title"] = job.get("title", "Unknown")
        batch["jobs"][i]["error"] = job.get("error")
        batch["jobs"][i]["file_path"] = job.get("file_path")
        if job["status"] == "done":
            batch["completed"] += 1
        elif job["status"] == "error":
            batch["failed"] += 1
    batch["status"] = "done"

@app.route("/")
def home():
    return render_template_string(HOME_HTML)

@app.route("/health")
def health():
    return jsonify({"ok": True, "status": "online"})

@app.route("/preview", methods=["POST"])
def preview():
    url = request.form.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    
    # Check if it's a playlist URL
    if "list=" in url:
        playlist_info = fetch_playlist_info(url)
        if playlist_info:
            return jsonify(playlist_info)
    
    # Single video
    video_info = fetch_video_info(url)
    if video_info:
        video_info["is_playlist"] = False
        return jsonify(video_info)
    
    return jsonify({"error": "Could not fetch video info"}), 400

@app.route("/playlist_info", methods=["POST"])
def playlist_info():
    url = request.form.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    info = fetch_playlist_info(url)
    if info:
        return jsonify(info)
    return jsonify({"error": "Could not fetch playlist info"}), 400

@app.route("/spotify_status", methods=["GET"])
def spotify_status():
    """Check if Spotify is configured"""
    return jsonify({
        "available": spotify_client is not None,
        "message": "Spotify ready" if spotify_client else "Spotify not configured. Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to environment."
    })

@app.route("/spotify_preview", methods=["POST"])
def spotify_preview():
    """Fetch Spotify playlist/album/track info"""
    url = request.form.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    
    if not spotify_client:
        return jsonify({"error": "Spotify not configured. Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET."}), 400
    
    url_type, spotify_id = parse_spotify_url(url)
    if not url_type:
        return jsonify({"error": "Invalid Spotify URL"}), 400
    
    if url_type == 'playlist':
        info = get_spotify_playlist(spotify_id)
    elif url_type == 'album':
        info = get_spotify_album(spotify_id)
    elif url_type == 'track':
        info = get_spotify_track(spotify_id)
    else:
        return jsonify({"error": "Unsupported Spotify URL type"}), 400
    
    if info:
        return jsonify(info)
    return jsonify({"error": "Could not fetch Spotify info"}), 400

@app.route("/spotify_download", methods=["POST"])
def spotify_download():
    """Convert Spotify tracks to YouTube and start batch download"""
    tracks_json = request.form.get("tracks", "").strip()
    quality = request.form.get("quality", "192").strip()
    
    if not tracks_json:
        return jsonify({"error": "Tracks required"}), 400
    if quality not in ["128", "192", "256", "320"]:
        quality = "192"
    
    try:
        tracks = json.loads(tracks_json)
    except:
        return jsonify({"error": "Invalid tracks data"}), 400
    
    if len(tracks) > 50:
        tracks = tracks[:50]
    
    # Search YouTube for each track
    youtube_urls = []
    track_titles = []
    for track in tracks:
        search_query = track.get('search_query', f"{track.get('title', '')} {track.get('artist', '')}")
        yt_url = search_youtube_for_track(search_query)
        if yt_url:
            youtube_urls.append(yt_url)
            track_titles.append(f"{track.get('title', 'Unknown')} - {track.get('artist', 'Unknown')}")
    
    if not youtube_urls:
        return jsonify({"error": "No YouTube matches found"}), 400
    
    # Create batch job
    batch_id = str(uuid.uuid4())
    batch_queue[batch_id] = {
        "status": "processing", "total": len(youtube_urls), "completed": 0, "failed": 0, 
        "current_index": 0, "quality": quality, "created_at": datetime.now().isoformat(),
        "jobs": [{"job_id": str(uuid.uuid4()), "url": url, "status": "queued", "title": title, "error": None, "file_path": None} 
                 for url, title in zip(youtube_urls, track_titles)]
    }
    
    thread = threading.Thread(target=batch_download_task, args=(batch_id, youtube_urls, quality))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        "batch_id": batch_id, 
        "total": len(youtube_urls), 
        "status": "processing",
        "jobs": [{"job_id": j["job_id"], "url": j["url"], "status": "queued", "title": j["title"]} for j in batch_queue[batch_id]["jobs"]]
    })

@app.route("/enqueue", methods=["POST"])
def enqueue():
    url = request.form.get("url", "").strip()
    quality = request.form.get("quality", "192").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    if quality not in ["128", "192", "256", "320"]:
        quality = "192"
    job_id = str(uuid.uuid4())
    try:
        title = fetch_title_with_ytdlp(url)
    except:
        title = "Unknown"
    job_queue[job_id] = {"status": "queued", "url": url, "title": title, "quality": quality, "error": None, "file_path": None, "created_at": datetime.now().isoformat()}
    thread = threading.Thread(target=download_task, args=(job_id, url, quality))
    thread.daemon = True
    thread.start()
    return jsonify({"job_id": job_id, "status": "queued", "title": title, "quality": quality})

@app.route("/batch_enqueue", methods=["POST"])
def batch_enqueue():
    urls_raw = request.form.get("urls", "").strip()
    quality = request.form.get("quality", "192").strip()
    if not urls_raw:
        return jsonify({"error": "URLs required"}), 400
    if quality not in ["128", "192", "256", "320"]:
        quality = "192"
    urls = [line.strip() for line in urls_raw.replace(",", "\n").split("\n") if line.strip() and ("youtube.com" in line or "youtu.be" in line)]
    if not urls:
        return jsonify({"error": "No valid YouTube URLs found"}), 400
    if len(urls) > 50:
        return jsonify({"error": "Maximum 50 URLs per batch"}), 400
    batch_id = str(uuid.uuid4())
    batch_queue[batch_id] = {
        "status": "processing", "total": len(urls), "completed": 0, "failed": 0, "current_index": 0, "quality": quality, "created_at": datetime.now().isoformat(),
        "jobs": [{"job_id": str(uuid.uuid4()), "url": url, "status": "queued", "title": "Waiting...", "error": None, "file_path": None} for url in urls]
    }
    thread = threading.Thread(target=batch_download_task, args=(batch_id, urls, quality))
    thread.daemon = True
    thread.start()
    return jsonify({"batch_id": batch_id, "total": len(urls), "status": "processing", "jobs": [{"job_id": j["job_id"], "url": j["url"], "status": "queued"} for j in batch_queue[batch_id]["jobs"]]})

@app.route("/batch_status/<batch_id>", methods=["GET"])
def batch_status(batch_id):
    batch = batch_queue.get(batch_id)
    if not batch:
        return jsonify({"error": "Batch not found"}), 404
    return jsonify({"batch_id": batch_id, "status": batch["status"], "total": batch["total"], "completed": batch["completed"], "failed": batch["failed"], "current_index": batch["current_index"], "jobs": [{"job_id": j["job_id"], "url": j["url"], "status": j["status"], "title": j["title"], "error": j.get("error")} for j in batch["jobs"]]})

@app.route("/batch_download/<batch_id>", methods=["GET"])
def batch_download(batch_id):
    batch = batch_queue.get(batch_id)
    if not batch:
        return jsonify({"error": "Batch not found"}), 404
    files_to_zip = [(Path(j["file_path"]), safe_filename(j.get("title", "audio"))) for j in batch["jobs"] if j["status"] == "done" and j.get("file_path") and Path(j["file_path"]).exists()]
    if not files_to_zip:
        return jsonify({"error": "No files"}), 400
    zip_path = DOWNLOAD_DIR / f"batch_{batch_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp, fn in files_to_zip:
            zf.write(fp, fn)
    return send_file(zip_path, mimetype="application/zip", as_attachment=True, download_name=f"youtube_mp3_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")

@app.route("/status/<job_id>", methods=["GET"])
def get_status(job_id):
    job = job_queue.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job_id": job_id, "status": job["status"], "title": job.get("title"), "error": job.get("error"), "quality": job.get("quality")})

@app.route("/download_job/<job_id>", methods=["GET"])
def download_job(job_id):
    job = job_queue.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] != "done":
        return jsonify({"error": f"Job status: {job['status']}"}), 400
    file_path = Path(job["file_path"])
    if not file_path.exists():
        return jsonify({"error": "File not found"}), 404
    return send_file(file_path, mimetype="audio/mpeg", as_attachment=True, download_name=safe_filename(job.get("title", "media")))


HOME_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>YouTube → MP3 | Premium Audio Converter</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #050510;
      --bg-dark: #010104;
      --card: rgba(10, 10, 20, 0.6);
      --card-hover: rgba(15, 15, 30, 0.7);
      --text: #ffffff;
      --text-dim: #b4b8c5;
      --text-muted: #6b7280;
      --primary: #6366f1;
      --primary-light: #818cf8;
      --primary-dark: #4f46e5;
      --accent: #f0abfc;
      --accent-2: #fbbf24;
      --accent-3: #34d399;
      --gradient-1: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      --gradient-2: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
      --gradient-3: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
      --gradient-rainbow: linear-gradient(90deg, #ff6b6b, #feca57, #48dbfb, #ff9ff3, #6c5ce7);
      --success: #10b981;
      --warning: #fbbf24;
      --error: #ef4444;
      --glow-primary: 0 0 60px rgba(99, 102, 241, 0.5);
      --glow-accent: 0 0 60px rgba(240, 171, 252, 0.4);
      --glow-intense: 0 0 120px rgba(99, 102, 241, 0.6), 0 0 200px rgba(99, 102, 241, 0.3);
      --shadow-xl: 0 20px 60px rgba(0, 0, 0, 0.8);
      --shadow-2xl: 0 25px 80px rgba(0, 0, 0, 0.9);
      --shadow-glow: 0 0 100px rgba(99, 102, 241, 0.2);
      --border: rgba(255, 255, 255, 0.08);
      --border-light: rgba(255, 255, 255, 0.15);
      --transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      --transition-slow: all 0.6s cubic-bezier(0.4, 0, 0.2, 1);
      --transition-bounce: all 0.5s cubic-bezier(0.68, -0.55, 0.265, 1.55);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
      background: var(--bg-dark);
      color: var(--text);
      min-height: 100vh;
      overflow-x: hidden;
      -webkit-font-smoothing: antialiased;
    }
    .universe-bg {
      position: fixed; inset: 0; z-index: 0;
      background: 
        radial-gradient(ellipse at top left, rgba(99, 102, 241, 0.15) 0%, transparent 40%),
        radial-gradient(ellipse at bottom right, rgba(240, 171, 252, 0.15) 0%, transparent 40%),
        radial-gradient(ellipse at center, rgba(79, 70, 229, 0.08) 0%, transparent 60%),
        linear-gradient(180deg, var(--bg-dark) 0%, var(--bg) 100%);
    }
    .stars { position: fixed; inset: 0; z-index: 1; }
    .star {
      position: absolute; width: 2px; height: 2px; background: white; border-radius: 50%;
      animation: twinkle 3s ease-in-out infinite; box-shadow: 0 0 6px white;
    }
    @keyframes twinkle { 0%, 100% { opacity: 0; transform: scale(0.5); } 50% { opacity: 1; transform: scale(1); } }
    .particles { position: fixed; inset: 0; z-index: 2; pointer-events: none; }
    .particle {
      position: absolute; width: 4px; height: 4px; background: var(--primary-light);
      border-radius: 50%; filter: blur(1px); animation: floatUp 20s linear infinite;
    }
    @keyframes floatUp {
      0% { transform: translateY(100vh) translateX(0) scale(0); opacity: 0; }
      10% { opacity: 0.8; } 90% { opacity: 0.8; }
      100% { transform: translateY(-100vh) translateX(100px) scale(1.5); opacity: 0; }
    }
    .gradient-orbs { position: fixed; inset: 0; z-index: 1; filter: blur(100px); opacity: 0.5; }
    .orb { position: absolute; border-radius: 50%; mix-blend-mode: screen; }
    .orb1 { width: 600px; height: 600px; background: radial-gradient(circle, var(--primary) 0%, transparent 70%); top: -300px; left: -300px; animation: floatOrb1 25s ease-in-out infinite; }
    .orb2 { width: 500px; height: 500px; background: radial-gradient(circle, var(--accent) 0%, transparent 70%); bottom: -250px; right: -250px; animation: floatOrb2 30s ease-in-out infinite; }
    .orb3 { width: 400px; height: 400px; background: radial-gradient(circle, var(--accent-2) 0%, transparent 70%); top: 50%; left: 50%; transform: translate(-50%, -50%); animation: floatOrb3 35s ease-in-out infinite; }
    @keyframes floatOrb1 { 0%, 100% { transform: translate(0, 0) scale(1) rotate(0deg); } 33% { transform: translate(100px, 50px) scale(1.1) rotate(120deg); } 66% { transform: translate(-50px, 100px) scale(0.9) rotate(240deg); } }
    @keyframes floatOrb2 { 0%, 100% { transform: translate(0, 0) scale(1) rotate(0deg); } 33% { transform: translate(-100px, -50px) scale(1.2) rotate(-120deg); } 66% { transform: translate(50px, -100px) scale(0.8) rotate(-240deg); } }
    @keyframes floatOrb3 { 0%, 100% { transform: translate(-50%, -50%) scale(1); } 50% { transform: translate(-45%, -55%) scale(1.1); } }
    .grid-bg {
      position: fixed; inset: 0; z-index: 1;
      background-image: linear-gradient(rgba(99, 102, 241, 0.03) 1px, transparent 1px), linear-gradient(90deg, rgba(99, 102, 241, 0.03) 1px, transparent 1px);
      background-size: 50px 50px; animation: gridMove 20s linear infinite;
    }
    @keyframes gridMove { 0% { transform: translate(0, 0); } 100% { transform: translate(50px, 50px); } }
    .container { position: relative; z-index: 10; max-width: 900px; margin: 0 auto; padding: 60px 24px; min-height: 100vh; }
    .header { text-align: center; margin-bottom: 50px; animation: fadeInDown 1s ease; }
    @keyframes fadeInDown { from { opacity: 0; transform: translateY(-40px); } to { opacity: 1; transform: translateY(0); } }
    .logo-container { display: inline-block; margin-bottom: 32px; }
    .logo-wave {
      width: 120px; height: 120px; display: flex; align-items: center; justify-content: center;
      background: linear-gradient(135deg, rgba(99, 102, 241, 0.1) 0%, rgba(240, 171, 252, 0.1) 100%);
      border-radius: 30px; backdrop-filter: blur(10px); border: 1px solid rgba(255, 255, 255, 0.1);
      box-shadow: 0 8px 32px rgba(99, 102, 241, 0.3), inset 0 1px 0 rgba(255, 255, 255, 0.2);
      position: relative; overflow: hidden;
    }
    .logo-wave::before { content: ''; position: absolute; inset: 0; background: radial-gradient(circle at center, transparent 30%, rgba(99, 102, 241, 0.1) 100%); animation: pulseGlow 3s ease-in-out infinite; }
    @keyframes pulseGlow { 0%, 100% { opacity: 0.5; transform: scale(1); } 50% { opacity: 1; transform: scale(1.1); } }
    .sound-bars { display: flex; align-items: center; justify-content: center; gap: 4px; height: 50px; position: relative; z-index: 1; }
    .sound-bar { width: 6px; background: var(--gradient-1); border-radius: 3px; animation: soundWave 1.2s ease-in-out infinite; box-shadow: 0 0 10px rgba(99, 102, 241, 0.5); }
    .sound-bar:nth-child(1) { height: 20px; animation-delay: 0s; }
    .sound-bar:nth-child(2) { height: 35px; animation-delay: 0.1s; }
    .sound-bar:nth-child(3) { height: 45px; animation-delay: 0.2s; }
    .sound-bar:nth-child(4) { height: 40px; animation-delay: 0.3s; }
    .sound-bar:nth-child(5) { height: 30px; animation-delay: 0.4s; }
    .sound-bar:nth-child(6) { height: 25px; animation-delay: 0.5s; }
    .sound-bar:nth-child(7) { height: 35px; animation-delay: 0.6s; }
    @keyframes soundWave { 0%, 100% { transform: scaleY(1); opacity: 0.8; } 50% { transform: scaleY(1.5); opacity: 1; } }
    h1 {
      font-size: clamp(40px, 6vw, 72px); font-weight: 900; margin-bottom: 16px;
      background: var(--gradient-rainbow); background-size: 200% auto;
      -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
      animation: shimmer 3s linear infinite; letter-spacing: -2px; line-height: 1;
    }
    @keyframes shimmer { 0% { background-position: 0% center; } 100% { background-position: 200% center; } }
    .subtitle { font-size: 20px; color: var(--text-dim); font-weight: 500; letter-spacing: 0.5px; animation: fadeIn 1s ease 0.3s both; }
    @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
    .card {
      background: linear-gradient(135deg, rgba(255, 255, 255, 0.05) 0%, rgba(255, 255, 255, 0.02) 100%);
      backdrop-filter: blur(20px) saturate(180%); -webkit-backdrop-filter: blur(20px) saturate(180%);
      border: 1px solid var(--border); border-radius: 32px; padding: 48px;
      box-shadow: var(--shadow-2xl), var(--shadow-glow), inset 0 1px 0 rgba(255, 255, 255, 0.1);
      animation: cardEntrance 0.8s ease 0.2s both; position: relative; overflow: hidden; transition: var(--transition);
    }
    @keyframes cardEntrance { from { opacity: 0; transform: translateY(40px) scale(0.95); } to { opacity: 1; transform: translateY(0) scale(1); } }
    .card::before { content: ''; position: absolute; top: 0; left: -100%; width: 100%; height: 2px; background: var(--gradient-rainbow); animation: scanLine 3s linear infinite; }
    @keyframes scanLine { 0% { left: -100%; } 100% { left: 100%; } }
    .card:hover { transform: translateY(-2px); box-shadow: var(--shadow-2xl), var(--glow-intense), inset 0 1px 0 rgba(255, 255, 255, 0.15); border-color: var(--border-light); }
    .mode-toggle { display: flex; background: rgba(0, 0, 0, 0.4); border-radius: 20px; padding: 6px; margin-bottom: 32px; border: 1px solid var(--border); }
    .mode-btn { flex: 1; padding: 16px 24px; background: transparent; border: none; border-radius: 16px; color: var(--text-muted); font-size: 15px; font-weight: 600; cursor: pointer; transition: var(--transition); font-family: inherit; }
    .mode-btn.active { background: var(--gradient-1); color: white; box-shadow: 0 4px 20px rgba(99, 102, 241, 0.4); }
    .mode-btn:hover:not(.active) { color: var(--text); background: rgba(255,255,255,0.05); }
    .input-group { margin-bottom: 28px; position: relative; }
    .input-wrapper { display: flex; gap: 16px; position: relative; flex-wrap: wrap; }
    .input-field { flex: 1; position: relative; min-width: 250px; }
    input[type="url"], input[type="text"] {
      width: 100%; padding: 20px 24px 20px 56px;
      background: rgba(0, 0, 0, 0.4); border: 2px solid var(--border); border-radius: 20px;
      color: var(--text); font-size: 16px; font-weight: 500; outline: none; transition: var(--transition); font-family: inherit;
    }
    input::placeholder { color: var(--text-muted); font-weight: 400; }
    input:focus { border-color: var(--primary); background: rgba(0, 0, 0, 0.6); box-shadow: 0 0 0 4px rgba(99, 102, 241, 0.1), var(--glow-primary); transform: translateY(-1px); }
    textarea {
      width: 100%; padding: 20px 24px; min-height: 140px; resize: vertical;
      background: rgba(0, 0, 0, 0.4); border: 2px solid var(--border); border-radius: 20px;
      color: var(--text); font-size: 15px; font-family: inherit; outline: none; transition: var(--transition); line-height: 1.6;
    }
    textarea:focus { border-color: var(--primary); background: rgba(0, 0, 0, 0.6); box-shadow: 0 0 0 4px rgba(99, 102, 241, 0.1); }
    textarea::placeholder { color: var(--text-muted); }
    .input-icon { position: absolute; left: 20px; top: 50%; transform: translateY(-50%); width: 24px; height: 24px; color: var(--text-muted); transition: var(--transition); pointer-events: none; }
    input:focus ~ .input-icon { color: var(--primary); }
    .quality-selector {
      padding: 20px 16px; background: rgba(0, 0, 0, 0.4); border: 2px solid var(--border); border-radius: 20px;
      color: var(--text); font-size: 15px; font-weight: 600; outline: none; transition: var(--transition); cursor: pointer; min-width: 130px; font-family: inherit;
    }
    .quality-selector:hover { border-color: var(--primary-light); background: rgba(0, 0, 0, 0.5); }
    .quality-selector:focus { border-color: var(--primary); box-shadow: 0 0 0 4px rgba(99, 102, 241, 0.1); }
    .quality-selector option { background: var(--bg-dark); color: var(--text); padding: 10px; }
    .btn-convert {
      padding: 20px 48px; background: var(--gradient-1); border: none; border-radius: 20px;
      color: white; font-size: 16px; font-weight: 700; cursor: pointer; transition: var(--transition-bounce);
      box-shadow: 0 10px 30px rgba(99, 102, 241, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.2);
      position: relative; overflow: hidden; text-transform: uppercase; letter-spacing: 1px; white-space: nowrap; font-family: inherit;
    }
    .btn-convert::before { content: ''; position: absolute; top: 0; left: -100%; width: 100%; height: 100%; background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.4), transparent); transition: left 0.5s; }
    .btn-convert:hover::before { left: 100%; }
    .btn-convert:hover { transform: translateY(-3px) scale(1.02); box-shadow: 0 15px 40px rgba(99, 102, 241, 0.5), inset 0 1px 0 rgba(255, 255, 255, 0.3); }
    .btn-convert:active { transform: translateY(-1px) scale(1); }
    .btn-convert:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
    .quick-actions { display: flex; justify-content: space-between; align-items: center; margin-top: 24px; flex-wrap: wrap; gap: 16px; }
    .action-group { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
    .action-link { color: var(--text-dim); text-decoration: none; font-size: 14px; font-weight: 500; transition: var(--transition); padding: 8px 16px; border-radius: 12px; background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border); font-family: inherit; }
    .action-link:hover { color: var(--primary-light); background: rgba(99, 102, 241, 0.1); border-color: var(--primary); }
    .health-badge { display: inline-flex; align-items: center; gap: 8px; padding: 8px 16px; border-radius: 20px; font-size: 13px; font-weight: 600; background: rgba(16, 185, 129, 0.1); color: var(--success); border: 1px solid rgba(16, 185, 129, 0.2); font-family: inherit; }
    .health-badge.error { background: rgba(239, 68, 68, 0.1); color: var(--error); border-color: rgba(239, 68, 68, 0.2); }
    .health-badge.loading { background: rgba(251, 191, 36, 0.1); color: var(--warning); border-color: rgba(251, 191, 36, 0.2); }
    .health-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; animation: healthPulse 2s ease-in-out infinite; }
    @keyframes healthPulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
    .status-display { display: flex; align-items: center; gap: 16px; padding: 20px 24px; background: linear-gradient(135deg, rgba(0, 0, 0, 0.4) 0%, rgba(0, 0, 0, 0.2) 100%); border-radius: 16px; border: 1px solid var(--border); margin-top: 28px; min-height: 70px; transition: var(--transition); position: relative; overflow: hidden; }
    .status-display::after { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px; background: linear-gradient(90deg, transparent, var(--primary-light), transparent); opacity: 0; transition: opacity 0.3s; }
    .status-display.active::after { opacity: 1; animation: shimmerLine 2s linear infinite; }
    @keyframes shimmerLine { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }
    .status-indicator { width: 12px; height: 12px; border-radius: 50%; background: var(--text-muted); position: relative; flex-shrink: 0; transition: var(--transition); }
    .status-indicator::before { content: ''; position: absolute; inset: -6px; border-radius: 50%; background: inherit; opacity: 0.3; animation: pulse 2s ease-in-out infinite; }
    @keyframes pulse { 0%, 100% { transform: scale(1); opacity: 0.3; } 50% { transform: scale(1.5); opacity: 0; } }
    .status-indicator.ready { background: var(--text-muted); }
    .status-indicator.processing { background: var(--warning); }
    .status-indicator.success { background: var(--success); }
    .status-indicator.error { background: var(--error); }
    .status-text { flex: 1; font-size: 15px; font-weight: 500; color: var(--text-dim); }
    .progress-wrapper { margin-top: 28px; opacity: 0; transform: translateY(20px); transition: var(--transition); }
    .progress-wrapper.active { opacity: 1; transform: translateY(0); }
    .progress-bar { height: 8px; background: rgba(255, 255, 255, 0.05); border-radius: 999px; overflow: hidden; position: relative; border: 1px solid var(--border); }
    .progress-fill { height: 100%; background: var(--gradient-rainbow); background-size: 200% 100%; border-radius: 999px; animation: progressMove 2s linear infinite, shimmer 2s linear infinite; width: 100%; transform-origin: left; }
    @keyframes progressMove { 0% { transform: scaleX(0) translateX(0); } 50% { transform: scaleX(1) translateX(0); } 100% { transform: scaleX(1) translateX(100%); } }
    .single-input { display: block; }
    .batch-input { display: none; }
    .mode-batch .single-input { display: none; }
    .mode-batch .batch-input { display: block; }
    .batch-queue { margin-top: 28px; display: none; }
    .batch-queue.active { display: block; }
    .batch-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; padding: 0 4px; }
    .batch-title { font-size: 16px; font-weight: 700; color: var(--text); }
    .batch-progress { font-size: 14px; color: var(--text-muted); font-weight: 600; }
    .batch-list { display: flex; flex-direction: column; gap: 10px; max-height: 350px; overflow-y: auto; padding-right: 8px; }
    .batch-list::-webkit-scrollbar { width: 6px; }
    .batch-list::-webkit-scrollbar-track { background: rgba(255,255,255,0.05); border-radius: 3px; }
    .batch-list::-webkit-scrollbar-thumb { background: var(--primary); border-radius: 3px; }
    .batch-item { display: flex; align-items: center; gap: 14px; padding: 16px 18px; background: rgba(0, 0, 0, 0.3); border: 1px solid var(--border); border-radius: 16px; transition: var(--transition); }
    .batch-item.downloading { border-color: var(--warning); background: rgba(251, 191, 36, 0.1); }
    .batch-item.done { border-color: var(--success); background: rgba(16, 185, 129, 0.1); }
    .batch-item.error { border-color: var(--error); background: rgba(239, 68, 68, 0.1); }
    .batch-status-icon { width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
    .batch-status-icon.queued { background: var(--text-muted); }
    .batch-status-icon.downloading { background: var(--warning); animation: pulse 1.5s infinite; }
    .batch-status-icon.done { background: var(--success); }
    .batch-status-icon.error { background: var(--error); }
    .batch-status-icon svg { width: 14px; height: 14px; fill: white; }
    .batch-info { flex: 1; min-width: 0; }
    .batch-item-title { font-size: 14px; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .batch-item-url { font-size: 12px; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }
    .batch-item-download { padding: 10px 16px; background: var(--success); border: none; border-radius: 12px; color: white; font-size: 13px; font-weight: 600; cursor: pointer; opacity: 0; pointer-events: none; transition: var(--transition); font-family: inherit; }
    .batch-item.done .batch-item-download { opacity: 1; pointer-events: auto; animation: popIn 0.3s ease; }
    @keyframes popIn { 0% { transform: scale(0.8); opacity: 0; } 100% { transform: scale(1); opacity: 1; } }
    .batch-item-download:hover { transform: scale(1.05); box-shadow: 0 4px 15px rgba(16, 185, 129, 0.4); }
    .download-all-btn { margin-top: 20px; width: 100%; background: linear-gradient(135deg, var(--success), #059669); display: none; }
    .download-all-btn.active { display: flex; align-items: center; justify-content: center; gap: 10px; }
    /* Platform Toggle (YouTube/Spotify) */
    .platform-toggle { display: flex; gap: 12px; margin-bottom: 24px; }
    .platform-btn { flex: 1; padding: 16px 20px; background: rgba(0, 0, 0, 0.3); border: 2px solid var(--border); border-radius: 16px; color: var(--text-muted); font-size: 15px; font-weight: 600; cursor: pointer; transition: var(--transition); display: flex; align-items: center; justify-content: center; gap: 10px; font-family: inherit; }
    .platform-btn:hover { border-color: var(--primary-light); color: var(--text); }
    .platform-btn.active { background: rgba(99, 102, 241, 0.15); border-color: var(--primary); color: var(--primary-light); }
    .platform-btn.spotify.active { background: rgba(30, 215, 96, 0.15); border-color: #1ed760; color: #1ed760; }
    .platform-btn svg { width: 22px; height: 22px; }
    .spotify-section { display: none; }
    .platform-spotify .youtube-section { display: none; }
    .platform-spotify .spotify-section { display: block; }
    .spotify-badge { background: rgba(30, 215, 96, 0.15); border-color: rgba(30, 215, 96, 0.3); color: #1ed760; }
    .spotify-not-configured { padding: 32px; text-align: center; background: rgba(0,0,0,0.2); border-radius: 16px; border: 1px dashed var(--border); }
    .spotify-not-configured h3 { color: var(--text); margin-bottom: 12px; font-size: 18px; }
    .spotify-not-configured p { color: var(--text-muted); font-size: 14px; line-height: 1.6; }
    .spotify-not-configured code { background: rgba(0,0,0,0.3); padding: 2px 8px; border-radius: 4px; font-size: 13px; }
    /* Spotify Track List */
    .spotify-tracks { max-height: 300px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; }
    .spotify-track { display: flex; gap: 12px; padding: 12px; background: rgba(0,0,0,0.2); border-radius: 12px; border: 1px solid var(--border); }
    .spotify-track-thumb { width: 50px; height: 50px; border-radius: 6px; object-fit: cover; }
    .spotify-track-info { flex: 1; min-width: 0; }
    .spotify-track-title { font-size: 14px; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .spotify-track-artist { font-size: 12px; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .spotify-track-duration { font-size: 12px; color: var(--text-muted); margin-left: auto; }
    /* Preview Card Styles */
    .preview-card { display: none; margin-top: 24px; padding: 20px; background: rgba(0, 0, 0, 0.3); border: 1px solid var(--border); border-radius: 20px; animation: fadeIn 0.3s ease; }
    .preview-card.active { display: block; }
    .preview-card.loading { display: flex; align-items: center; justify-content: center; min-height: 100px; }
    .preview-content { display: flex; gap: 20px; align-items: flex-start; }
    .preview-thumbnail { width: 180px; height: 100px; border-radius: 12px; object-fit: cover; flex-shrink: 0; background: rgba(0,0,0,0.3); }
    .preview-info { flex: 1; min-width: 0; }
    .preview-title { font-size: 16px; font-weight: 700; color: var(--text); margin-bottom: 8px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    .preview-meta { display: flex; gap: 16px; flex-wrap: wrap; }
    .preview-meta-item { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text-muted); }
    .preview-meta-item svg { width: 16px; height: 16px; opacity: 0.7; }
    .preview-badge { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; background: rgba(99, 102, 241, 0.15); border: 1px solid rgba(99, 102, 241, 0.3); border-radius: 20px; font-size: 12px; font-weight: 600; color: var(--primary-light); margin-top: 12px; }
    /* Playlist Preview Styles */
    .playlist-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }
    .playlist-title { font-size: 18px; font-weight: 700; color: var(--text); }
    .playlist-count { font-size: 14px; color: var(--text-muted); }
    .playlist-videos { max-height: 300px; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; padding-right: 8px; }
    .playlist-videos::-webkit-scrollbar { width: 6px; }
    .playlist-videos::-webkit-scrollbar-track { background: rgba(255,255,255,0.05); border-radius: 3px; }
    .playlist-videos::-webkit-scrollbar-thumb { background: var(--primary); border-radius: 3px; }
    .playlist-video-item { display: flex; gap: 12px; padding: 12px; background: rgba(0,0,0,0.2); border-radius: 12px; border: 1px solid var(--border); transition: var(--transition); }
    .playlist-video-item:hover { background: rgba(0,0,0,0.3); border-color: var(--primary); }
    .playlist-video-thumb { width: 120px; height: 68px; border-radius: 8px; object-fit: cover; flex-shrink: 0; background: rgba(0,0,0,0.3); }
    .playlist-video-info { flex: 1; min-width: 0; }
    .playlist-video-title { font-size: 14px; font-weight: 600; color: var(--text); margin-bottom: 4px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    .playlist-video-duration { font-size: 12px; color: var(--text-muted); }
    .playlist-actions { margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border); }
    @media (max-width: 768px) {
      .preview-content { flex-direction: column; }
      .preview-thumbnail { width: 100%; height: 180px; }
      .playlist-video-item { flex-direction: column; }
      .playlist-video-thumb { width: 100%; height: 120px; }
    }
    .features-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-top: 40px; }
    .feature-card { padding: 24px; background: linear-gradient(135deg, rgba(255, 255, 255, 0.03) 0%, rgba(255, 255, 255, 0.01) 100%); border: 1px solid var(--border); border-radius: 20px; text-align: center; transition: var(--transition); }
    .feature-card:hover { transform: translateY(-5px); background: linear-gradient(135deg, rgba(255, 255, 255, 0.06) 0%, rgba(255, 255, 255, 0.02) 100%); border-color: var(--primary); box-shadow: var(--glow-primary); }
    .feature-icon { font-size: 32px; margin-bottom: 12px; }
    .feature-title { font-size: 16px; font-weight: 700; color: var(--text); margin-bottom: 8px; }
    .feature-desc { font-size: 13px; color: var(--text-muted); line-height: 1.5; }
    .footer { text-align: center; margin-top: 50px; padding-top: 30px; border-top: 1px solid var(--border); animation: fadeIn 0.8s ease 0.6s both; }
    .footer-text { color: var(--text-muted); font-size: 14px; margin-bottom: 16px; }
    .footer-links { display: flex; justify-content: center; gap: 24px; flex-wrap: wrap; }
    .footer-link { color: var(--text-dim); text-decoration: none; font-size: 13px; transition: var(--transition); }
    .footer-link:hover { color: var(--primary-light); }
    .toast { position: fixed; bottom: 40px; left: 50%; transform: translateX(-50%) translateY(100px) scale(0.9); padding: 20px 32px; background: linear-gradient(135deg, rgba(17, 24, 39, 0.95) 0%, rgba(10, 10, 20, 0.95) 100%); backdrop-filter: blur(20px); border: 1px solid var(--border-light); border-radius: 20px; box-shadow: var(--shadow-2xl), var(--glow-primary); color: var(--text); font-weight: 500; z-index: 1000; opacity: 0; transition: var(--transition-bounce); font-size: 15px; }
    .toast.show { opacity: 1; transform: translateX(-50%) translateY(0) scale(1); }
    .spinner { width: 20px; height: 20px; border: 3px solid rgba(255, 255, 255, 0.1); border-top-color: var(--primary); border-radius: 50%; animation: spin 0.8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    @media (max-width: 768px) {
      .container { padding: 40px 20px; }
      .card { padding: 32px 24px; }
      h1 { font-size: 36px; }
      .input-wrapper { flex-direction: column; }
      .btn-convert { width: 100%; }
      .quick-actions { flex-direction: column; align-items: stretch; }
      .action-group { flex-direction: column; width: 100%; }
      .action-link { width: 100%; text-align: center; }
      .features-grid { grid-template-columns: 1fr; }
      .logo-wave { width: 90px; height: 90px; }
    }
  </style>
</head>
<body>
  <div class="universe-bg"></div>
  <div class="grid-bg"></div>
  <div class="gradient-orbs"><div class="orb orb1"></div><div class="orb orb2"></div><div class="orb orb3"></div></div>
  <div class="stars" id="stars"></div>
  <div class="particles" id="particles"></div>

  <div class="container">
    <div class="header">
      <div class="logo-container">
        <div class="logo-wave">
          <div class="sound-bars">
            <div class="sound-bar"></div><div class="sound-bar"></div><div class="sound-bar"></div>
            <div class="sound-bar"></div><div class="sound-bar"></div><div class="sound-bar"></div><div class="sound-bar"></div>
          </div>
        </div>
      </div>
      <h1>YouTube → MP3 Converter</h1>
      <p class="subtitle">Transform any YouTube or Spotify playlist into premium quality audio</p>
    </div>

    <div class="card" id="mainCard">
      <!-- Platform Toggle -->
      <div class="platform-toggle">
        <button type="button" class="platform-btn youtube active" data-platform="youtube">
          <svg viewBox="0 0 24 24" fill="currentColor"><path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/></svg>
          YouTube
        </button>
        <button type="button" class="platform-btn spotify" data-platform="spotify">
          <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z"/></svg>
          Spotify
        </button>
      </div>

      <!-- YouTube Section -->
      <div class="youtube-section">
        <div class="mode-toggle">
          <button type="button" class="mode-btn active" data-mode="single">Single URL</button>
          <button type="button" class="mode-btn" data-mode="batch">Batch Mode (up to 50)</button>
        </div>

        <form id="form">
          <div class="input-group single-input">
            <div class="input-wrapper">
              <div class="input-field">
                <input id="singleUrl" type="url" placeholder="Paste your YouTube URL here..." autocomplete="off" />
                <svg class="input-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"></path></svg>
              </div>
              <select class="quality-selector" id="qualitySelect">
                <option value="128">128 kbps</option>
                <option value="192" selected>192 kbps</option>
                <option value="256">256 kbps</option>
                <option value="320">320 kbps</option>
              </select>
              <button class="btn-convert" id="convertBtn" type="submit"><span id="btnText">Convert Now</span></button>
            </div>
          </div>

          <div class="input-group batch-input">
            <textarea id="batchUrls" placeholder="Paste YouTube URLs here (one per line, max 50)...

https://www.youtube.com/watch?v=abc123
https://youtu.be/xyz789
https://www.youtube.com/watch?v=..."></textarea>
            <div class="input-wrapper" style="margin-top: 16px;">
              <select class="quality-selector" id="batchQualitySelect">
                <option value="128">128 kbps</option>
                <option value="192" selected>192 kbps</option>
                <option value="256">256 kbps</option>
                <option value="320">320 kbps</option>
              </select>
              <button class="btn-convert" type="submit"><span>Start Batch Download</span></button>
            </div>
          </div>

          <div class="quick-actions">
            <div class="action-group">
              <a href="#" id="sampleLink" class="action-link">✨ Try Sample</a>
              <span class="health-badge loading" id="healthBadge"><span class="spinner" style="width: 12px; height: 12px; border-width: 2px;"></span><span>Checking...</span></span>
            </div>
          </div>

          <!-- Video/Playlist Preview Card -->
          <div class="preview-card" id="previewCard">
            <div id="previewContent"></div>
          </div>

          <div class="progress-wrapper" id="progressWrapper">
          <div class="progress-bar"><div class="progress-fill"></div></div>
        </div>

        <div class="status-display" id="statusDisplay">
          <div class="status-indicator ready" id="statusIndicator"></div>
          <span class="status-text" id="statusText">Ready to convert your audio</span>
        </div>
      </form>
      </div><!-- End YouTube Section -->

      <!-- Spotify Section -->
      <div class="spotify-section">
        <div id="spotifyNotConfigured" class="spotify-not-configured" style="display:none;">
          <h3>🔧 Spotify Setup Required</h3>
          <p>To use Spotify features, add these environment variables:</p>
          <p style="margin-top: 12px;"><code>SPOTIFY_CLIENT_ID</code> and <code>SPOTIFY_CLIENT_SECRET</code></p>
          <p style="margin-top: 12px;">Get them free at <a href="https://developer.spotify.com/dashboard" target="_blank" style="color: #1ed760;">developer.spotify.com</a></p>
        </div>
        
        <div id="spotifyConfigured">
          <div class="input-group">
            <div class="input-wrapper">
              <div class="input-field">
                <input id="spotifyUrl" type="url" placeholder="Paste Spotify playlist, album, or track URL..." autocomplete="off" />
                <svg class="input-icon" fill="currentColor" viewBox="0 0 24 24" style="color: #1ed760;"><path d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z"/></svg>
              </div>
              <select class="quality-selector" id="spotifyQualitySelect">
                <option value="128">128 kbps</option>
                <option value="192" selected>192 kbps</option>
                <option value="256">256 kbps</option>
                <option value="320">320 kbps</option>
              </select>
              <button type="button" class="btn-convert" id="spotifyConvertBtn" style="background: linear-gradient(135deg, #1ed760, #1db954);"><span id="spotifyBtnText">Convert Now</span></button>
            </div>
          </div>

          <div class="quick-actions">
            <div class="action-group">
              <a href="#" id="spotifySampleLink" class="action-link">✨ Try Sample Playlist</a>
              <span class="health-badge spotify-badge" id="spotifyBadge"><span class="health-dot" style="background:#1ed760;"></span><span>Spotify Ready</span></span>
            </div>
          </div>

          <!-- Spotify Preview Card -->
          <div class="preview-card" id="spotifyPreviewCard">
            <div id="spotifyPreviewContent"></div>
          </div>
        </div>
      </div><!-- End Spotify Section -->

      <div class="batch-queue" id="batchQueue">
        <div class="batch-header">
          <span class="batch-title">📥 Download Queue</span>
          <span class="batch-progress" id="batchProgress">0 / 0</span>
        </div>
        <div class="batch-list" id="batchList"></div>
        <button type="button" class="btn-convert download-all-btn" id="downloadAll">
          <svg fill="none" stroke="currentColor" viewBox="0 0 24 24" style="width:20px;height:20px;"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg>
          Download All as ZIP
        </button>
      </div>

      <div class="features-grid">
        <div class="feature-card"><div class="feature-icon">🎵</div><div class="feature-title">Premium Quality</div><div class="feature-desc">Crystal clear up to 320kbps MP3 audio</div></div>
        <div class="feature-card"><div class="feature-icon">⚡</div><div class="feature-title">Lightning Fast</div><div class="feature-desc">Optimized processing with smart caching</div></div>
        <div class="feature-card"><div class="feature-icon">📋</div><div class="feature-title">Playlist Support</div><div class="feature-desc">Download entire playlists at once</div></div>
        <div class="feature-card"><div class="feature-icon">🖼️</div><div class="feature-title">Album Art</div><div class="feature-desc">Thumbnails embedded automatically</div></div>
      </div>
    </div>

    <div class="footer">
      <p class="footer-text">Powered by advanced audio extraction technology</p>
      <div class="footer-links">
        <a href="#" class="footer-link">Privacy Policy</a>
        <a href="#" class="footer-link">Terms of Service</a>
        <a href="#" class="footer-link">Support</a>
      </div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    const $ = s => document.querySelector(s);
    const $$ = s => document.querySelectorAll(s);

    const mainCard = $('#mainCard');
    const form = $('#form');
    const singleUrl = $('#singleUrl');
    const batchUrls = $('#batchUrls');
    const qualitySelect = $('#qualitySelect');
    const batchQualitySelect = $('#batchQualitySelect');
    const convertBtn = $('#convertBtn');
    const btnText = $('#btnText');
    const statusDisplay = $('#statusDisplay');
    const statusIndicator = $('#statusIndicator');
    const statusText = $('#statusText');
    const progressWrapper = $('#progressWrapper');
    const batchQueue = $('#batchQueue');
    const batchList = $('#batchList');
    const batchProgress = $('#batchProgress');
    const downloadAll = $('#downloadAll');
    const toast = $('#toast');
    const healthBadge = $('#healthBadge');
    const sampleLink = $('#sampleLink');
    const previewCard = $('#previewCard');
    const previewContent = $('#previewContent');
    
    // Spotify elements
    const spotifyUrl = $('#spotifyUrl');
    const spotifyQualitySelect = $('#spotifyQualitySelect');
    const spotifyConvertBtn = $('#spotifyConvertBtn');
    const spotifyBtnText = $('#spotifyBtnText');
    const spotifyPreviewCard = $('#spotifyPreviewCard');
    const spotifyPreviewContent = $('#spotifyPreviewContent');
    const spotifyNotConfigured = $('#spotifyNotConfigured');
    const spotifyConfigured = $('#spotifyConfigured');
    const spotifySampleLink = $('#spotifySampleLink');
    const spotifyBadge = $('#spotifyBadge');

    let currentMode = 'single';
    let currentPlatform = 'youtube';
    let currentBatchId = null;
    let hasBatchData = false;
    let previewData = null;
    let previewTimeout = null;
    let spotifyPreviewData = null;

    // Create stars
    (function() {
      const c = $('#stars');
      for (let i = 0; i < 100; i++) {
        const s = document.createElement('div');
        s.className = 'star';
        s.style.left = Math.random() * 100 + '%';
        s.style.top = Math.random() * 100 + '%';
        s.style.animationDelay = Math.random() * 3 + 's';
        s.style.animationDuration = 3 + Math.random() * 2 + 's';
        c.appendChild(s);
      }
    })();

    // Create particles
    (function() {
      const c = $('#particles');
      for (let i = 0; i < 25; i++) {
        const p = document.createElement('div');
        p.className = 'particle';
        p.style.left = Math.random() * 100 + '%';
        p.style.animationDelay = Math.random() * 20 + 's';
        p.style.animationDuration = 20 + Math.random() * 10 + 's';
        c.appendChild(p);
      }
    })();

    // Health check
    fetch('/health').then(r => r.ok ? r.json() : null).then(d => {
      if (d && d.ok) {
        healthBadge.className = 'health-badge';
        healthBadge.innerHTML = '<span class="health-dot"></span><span>Online</span>';
      } else throw new Error();
    }).catch(() => {
      healthBadge.className = 'health-badge error';
      healthBadge.innerHTML = '<span>Offline</span>';
    });

    // Spotify status check
    fetch('/spotify_status').then(r => r.json()).then(d => {
      if (d.available) {
        spotifyNotConfigured.style.display = 'none';
        spotifyConfigured.style.display = 'block';
      } else {
        spotifyNotConfigured.style.display = 'block';
        spotifyConfigured.style.display = 'none';
      }
    }).catch(() => {
      spotifyNotConfigured.style.display = 'block';
      spotifyConfigured.style.display = 'none';
    });

    // Platform toggle (YouTube/Spotify)
    $$('.platform-btn').forEach(btn => {
      btn.addEventListener('click', e => {
        e.preventDefault();
        $$('.platform-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentPlatform = btn.dataset.platform;
        if (currentPlatform === 'spotify') {
          mainCard.classList.add('platform-spotify');
        } else {
          mainCard.classList.remove('platform-spotify');
        }
        // Hide previews and queues when switching
        previewCard.classList.remove('active');
        spotifyPreviewCard.classList.remove('active');
        statusDisplay.classList.remove('active');
        progressWrapper.classList.remove('active');
      });
    });

    // Sample link
    sampleLink.addEventListener('click', e => {
      e.preventDefault();
      singleUrl.value = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';
      showToast('✨ Sample video loaded!');
      singleUrl.focus();
      fetchPreview(singleUrl.value);
    });

    // Spotify sample link
    spotifySampleLink.addEventListener('click', e => {
      e.preventDefault();
      spotifyUrl.value = 'https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M';
      showToast('✨ Sample Spotify playlist loaded!');
      spotifyUrl.focus();
      fetchSpotifyPreview(spotifyUrl.value);
    });

    // Preview functionality
    function formatViews(num) {
      if (!num) return '';
      if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M views';
      if (num >= 1000) return (num / 1000).toFixed(1) + 'K views';
      return num + ' views';
    }

    function renderVideoPreview(data) {
      return `
        <div class="preview-content">
          <img class="preview-thumbnail" src="${data.thumbnail}" alt="Thumbnail" onerror="this.src='https://via.placeholder.com/180x100?text=No+Thumbnail'">
          <div class="preview-info">
            <div class="preview-title">${data.title}</div>
            <div class="preview-meta">
              <span class="preview-meta-item">
                <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                ${data.duration_formatted || 'Unknown'}
              </span>
              <span class="preview-meta-item">
                <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/></svg>
                ${data.channel}
              </span>
              ${data.view_count ? `<span class="preview-meta-item">
                <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg>
                ${formatViews(data.view_count)}
              </span>` : ''}
            </div>
            <div class="preview-badge">✓ Ready to convert</div>
          </div>
        </div>
      `;
    }

    function renderPlaylistPreview(data) {
      const videosHtml = data.videos.slice(0, 50).map((v, i) => `
        <div class="playlist-video-item">
          <img class="playlist-video-thumb" src="${v.thumbnail}" alt="Thumbnail" onerror="this.src='https://via.placeholder.com/120x68?text=No+Thumb'">
          <div class="playlist-video-info">
            <div class="playlist-video-title">${v.title}</div>
            <div class="playlist-video-duration">${v.duration_formatted || 'Unknown duration'}</div>
          </div>
        </div>
      `).join('');
      
      return `
        <div class="playlist-header">
          <div>
            <div class="playlist-title">📋 ${data.title}</div>
            <div style="font-size: 13px; color: var(--text-muted); margin-top: 4px;">${data.channel}</div>
          </div>
          <div class="playlist-count">${data.video_count} videos</div>
        </div>
        <div class="playlist-videos">${videosHtml}</div>
        <div class="playlist-actions">
          <div style="font-size: 13px; color: var(--text-muted); margin-bottom: 12px;">
            ${data.video_count > 50 ? `⚠️ Will download first 50 of ${data.video_count} videos. ` : ''}Click "Convert Now" to download${data.video_count > 50 ? ' the first 50' : ' all'} as a batch.
          </div>
        </div>
      `;
    }

    async function fetchPreview(url) {
      if (!url || !isValidYT(url)) {
        previewCard.classList.remove('active');
        previewData = null;
        return;
      }
      
      // Show loading state
      previewCard.classList.add('active', 'loading');
      previewContent.innerHTML = '<div class="spinner"></div><span style="margin-left: 12px; color: var(--text-muted);">Loading preview...</span>';
      
      try {
        const resp = await fetch('/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ url })
        });
        
        if (!resp.ok) throw new Error('Preview failed');
        const data = await resp.json();
        previewData = data;
        
        previewCard.classList.remove('loading');
        
        if (data.is_playlist) {
          previewContent.innerHTML = renderPlaylistPreview(data);
        } else {
          previewContent.innerHTML = renderVideoPreview(data);
        }
      } catch (e) {
        previewCard.classList.remove('active', 'loading');
        previewData = null;
      }
    }

    // Debounced preview on input
    singleUrl.addEventListener('input', e => {
      clearTimeout(previewTimeout);
      const url = e.target.value.trim();
      if (url && isValidYT(url)) {
        previewTimeout = setTimeout(() => fetchPreview(url), 800);
      } else {
        previewCard.classList.remove('active');
        previewData = null;
      }
    });

    // Also fetch preview on paste
    singleUrl.addEventListener('paste', e => {
      setTimeout(() => {
        const url = singleUrl.value.trim();
        if (url && isValidYT(url)) {
          fetchPreview(url);
        }
      }, 100);
    });

    // Mode toggle - FIXED: preserves batch queue when switching back
    $$('.mode-btn').forEach(btn => {
      btn.addEventListener('click', e => {
        e.preventDefault();
        $$('.mode-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentMode = btn.dataset.mode;
        mainCard.className = 'card' + (currentMode === 'batch' ? ' mode-batch' : '');
        
        // Only show batch queue in batch mode AND if there's batch data
        if (currentMode === 'batch' && hasBatchData) {
          batchQueue.classList.add('active');
        } else if (currentMode === 'single') {
          // Hide batch queue in single mode but don't clear data
          batchQueue.classList.remove('active');
        }
        
        // Hide preview card and status when switching modes
        if (currentMode === 'batch') {
          previewCard.classList.remove('active');
          statusDisplay.classList.remove('active');
          progressWrapper.classList.remove('active');
        }
      });
    });

    function showToast(msg) {
      toast.textContent = msg;
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 4000);
    }

    function setStatus(type, msg) {
      statusDisplay.classList.add('active');
      statusIndicator.className = 'status-indicator ' + type;
      statusText.textContent = msg;
      if (type === 'processing') progressWrapper.classList.add('active');
      else progressWrapper.classList.remove('active');
    }

    function isValidYT(url) {
      return /^(https?:\/\/)?(www\.)?(youtube\.com|youtu\.be)\//i.test(url);
    }

    async function handleSingle(url, quality) {
      setStatus('processing', 'Starting conversion...');
      try {
        const resp = await fetch('/enqueue', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ url, quality })
        });
        if (!resp.ok) throw new Error('Failed');
        const data = await resp.json();
        setStatus('processing', 'Converting: ' + (data.title || 'Loading...'));
        const poll = setInterval(async () => {
          const sr = await fetch('/status/' + data.job_id);
          const st = await sr.json();
          if (st.status === 'done') {
            clearInterval(poll);
            setStatus('success', '✓ Complete! Downloading...');
            showToast('🎉 Your MP3 is ready!');
            window.location.href = '/download_job/' + data.job_id;
            resetBtn();
          } else if (st.status === 'error') {
            clearInterval(poll);
            setStatus('error', st.error || 'Conversion failed');
            showToast('❌ Conversion failed');
            resetBtn();
          } else {
            setStatus('processing', 'Converting: ' + (st.title || 'Processing...'));
          }
        }, 2000);
      } catch (e) {
        setStatus('error', 'Failed to start conversion');
        resetBtn();
      }
    }

    async function handleBatch(urls, quality) {
      hasBatchData = true;
      batchQueue.classList.add('active');
      batchList.innerHTML = '';
      downloadAll.classList.remove('active');
      
      try {
        const resp = await fetch('/batch_enqueue', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ urls, quality })
        });
        if (!resp.ok) {
          const err = await resp.json();
          showToast('❌ ' + (err.error || 'Failed'));
          resetBtn();
          return;
        }
        const data = await resp.json();
        currentBatchId = data.batch_id;
        
        // Track which jobs have been marked done to show notifications
        const completedJobs = new Set();
        
        data.jobs.forEach(job => {
          const item = document.createElement('div');
          item.className = 'batch-item';
          item.id = 'job-' + job.job_id;
          item.innerHTML = '<div class="batch-status-icon queued"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/></svg></div><div class="batch-info"><div class="batch-item-title">Waiting...</div><div class="batch-item-url">' + job.url + '</div></div><button type="button" class="batch-item-download" onclick="window.location.href=\'/download_job/' + job.job_id + '\'">⬇ Download</button>';
          batchList.appendChild(item);
        });
        
        batchProgress.textContent = '0 / ' + data.total;
        
        const poll = setInterval(async () => {
          const sr = await fetch('/batch_status/' + currentBatchId);
          const st = await sr.json();
          batchProgress.textContent = st.completed + ' / ' + st.total;
          
          st.jobs.forEach(job => {
            const item = $('#job-' + job.job_id);
            if (!item) return;
            
            // Check if this job just completed
            if (job.status === 'done' && !completedJobs.has(job.job_id)) {
              completedJobs.add(job.job_id);
              showToast(`✅ Ready: ${job.title || 'Track'} - Click to download!`);
              // Scroll the completed item into view
              item.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
            
            item.className = 'batch-item ' + job.status;
            item.querySelector('.batch-item-title').textContent = job.title || 'Processing...';
            const icon = item.querySelector('.batch-status-icon');
            icon.className = 'batch-status-icon ' + job.status;
            if (job.status === 'done') icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>';
            else if (job.status === 'error') icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>';
            else if (job.status === 'downloading') icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z"/></svg>';
          });
          
          if (st.status === 'done') {
            clearInterval(poll);
            showToast('🎉 All done! ' + st.completed + '/' + st.total + ' successful');
            resetBtn();
            if (st.completed > 0) downloadAll.classList.add('active');
          }
        }, 2000);
      } catch (e) {
        showToast('❌ Error starting batch');
        resetBtn();
      }
    }

    function resetBtn() {
      convertBtn.disabled = false;
      btnText.textContent = 'Convert Now';
      spotifyConvertBtn.disabled = false;
      spotifyBtnText.textContent = 'Convert Now';
    }

    downloadAll.addEventListener('click', e => {
      e.preventDefault();
      if (currentBatchId) window.location.href = '/batch_download/' + currentBatchId;
    });

    // Spotify Preview Functions
    function isValidSpotify(url) {
      return /^(https?:\/\/)?(open\.)?spotify\.com\/(playlist|album|track)\/[a-zA-Z0-9]+/i.test(url);
    }

    function renderSpotifyPreview(data) {
      const tracksHtml = data.tracks.slice(0, 50).map((t, i) => `
        <div class="spotify-track">
          <img class="spotify-track-thumb" src="${t.thumbnail || 'https://via.placeholder.com/50?text=♪'}" alt="Album art" onerror="this.src='https://via.placeholder.com/50?text=♪'">
          <div class="spotify-track-info">
            <div class="spotify-track-title">${t.title}</div>
            <div class="spotify-track-artist">${t.artist}</div>
          </div>
          <span class="spotify-track-duration">${t.duration_formatted || ''}</span>
        </div>
      `).join('');
      
      const typeIcon = data.type === 'playlist' ? '📋' : data.type === 'album' ? '💿' : '🎵';
      
      return `
        <div class="playlist-header">
          <div style="display: flex; align-items: center; gap: 16px;">
            ${data.thumbnail ? `<img src="${data.thumbnail}" style="width: 60px; height: 60px; border-radius: 8px; object-fit: cover;">` : ''}
            <div>
              <div class="playlist-title">${typeIcon} ${data.title}</div>
              <div style="font-size: 13px; color: var(--text-muted); margin-top: 4px;">${data.owner}</div>
            </div>
          </div>
          <div class="playlist-count">${data.total_tracks} tracks</div>
        </div>
        <div class="spotify-tracks">${tracksHtml}</div>
        <div class="playlist-actions">
          <div style="font-size: 13px; color: var(--text-muted); margin-bottom: 12px;">
            ${data.total_tracks > 50 ? `⚠️ Will download first 50 of ${data.total_tracks} tracks. ` : ''}
            Tracks will be searched on YouTube and downloaded as MP3.
          </div>
        </div>
      `;
    }

    async function fetchSpotifyPreview(url) {
      if (!url || !isValidSpotify(url)) {
        spotifyPreviewCard.classList.remove('active');
        spotifyPreviewData = null;
        return;
      }
      
      spotifyPreviewCard.classList.add('active', 'loading');
      spotifyPreviewContent.innerHTML = '<div class="spinner"></div><span style="margin-left: 12px; color: var(--text-muted);">Loading from Spotify...</span>';
      
      try {
        const resp = await fetch('/spotify_preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ url })
        });
        
        if (!resp.ok) {
          const err = await resp.json();
          throw new Error(err.error || 'Failed');
        }
        
        const data = await resp.json();
        spotifyPreviewData = data;
        spotifyPreviewCard.classList.remove('loading');
        spotifyPreviewContent.innerHTML = renderSpotifyPreview(data);
      } catch (e) {
        spotifyPreviewCard.classList.remove('loading');
        spotifyPreviewContent.innerHTML = `<div style="text-align: center; padding: 20px; color: var(--error);">❌ ${e.message || 'Failed to load Spotify data'}</div>`;
        spotifyPreviewData = null;
      }
    }

    // Spotify input handlers
    let spotifyTimeout = null;
    spotifyUrl.addEventListener('input', e => {
      clearTimeout(spotifyTimeout);
      const url = e.target.value.trim();
      if (url && isValidSpotify(url)) {
        spotifyTimeout = setTimeout(() => fetchSpotifyPreview(url), 800);
      } else {
        spotifyPreviewCard.classList.remove('active');
        spotifyPreviewData = null;
      }
    });

    spotifyUrl.addEventListener('paste', e => {
      setTimeout(() => {
        const url = spotifyUrl.value.trim();
        if (url && isValidSpotify(url)) {
          fetchSpotifyPreview(url);
        }
      }, 100);
    });

    // Spotify convert button
    spotifyConvertBtn.addEventListener('click', async e => {
      e.preventDefault();
      
      if (!spotifyPreviewData || !spotifyPreviewData.tracks || spotifyPreviewData.tracks.length === 0) {
        showToast('⚠️ Please enter a valid Spotify URL first');
        return;
      }
      
      spotifyConvertBtn.disabled = true;
      spotifyBtnText.textContent = 'Processing...';
      
      const quality = spotifyQualitySelect.value;
      const tracksToDownload = spotifyPreviewData.tracks.slice(0, 50);
      
      showToast(`🎵 Searching YouTube for ${tracksToDownload.length} tracks...`);
      
      hasBatchData = true;
      batchQueue.classList.add('active');
      batchList.innerHTML = '';
      downloadAll.classList.remove('active');
      
      try {
        const resp = await fetch('/spotify_download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ 
            tracks: JSON.stringify(tracksToDownload), 
            quality 
          })
        });
        
        if (!resp.ok) {
          const err = await resp.json();
          showToast('❌ ' + (err.error || 'Failed'));
          resetBtn();
          return;
        }
        
        const data = await resp.json();
        currentBatchId = data.batch_id;
        
        data.jobs.forEach(job => {
          const item = document.createElement('div');
          item.className = 'batch-item';
          item.id = 'job-' + job.job_id;
          item.innerHTML = '<div class="batch-status-icon queued"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/></svg></div><div class="batch-info"><div class="batch-item-title">' + (job.title || 'Waiting...') + '</div><div class="batch-item-url" style="color: #1ed760;">via YouTube search</div></div><button type="button" class="batch-item-download" onclick="window.location.href=\'/download_job/' + job.job_id + '\'">⬇ Download</button>';
          batchList.appendChild(item);
        });
        
        batchProgress.textContent = '0 / ' + data.total;
        showToast(`✅ Found ${data.total} tracks on YouTube. Starting download...`);
        
        // Track completed jobs for notifications
        const completedJobs = new Set();
        
        const poll = setInterval(async () => {
          const sr = await fetch('/batch_status/' + currentBatchId);
          const st = await sr.json();
          batchProgress.textContent = st.completed + ' / ' + st.total;
          
          st.jobs.forEach(job => {
            const item = $('#job-' + job.job_id);
            if (!item) return;
            
            // Check if this job just completed
            if (job.status === 'done' && !completedJobs.has(job.job_id)) {
              completedJobs.add(job.job_id);
              showToast(`✅ Ready: ${job.title || 'Track'} - Click to download!`);
              item.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
            
            item.className = 'batch-item ' + job.status;
            item.querySelector('.batch-item-title').textContent = job.title || 'Processing...';
            const icon = item.querySelector('.batch-status-icon');
            icon.className = 'batch-status-icon ' + job.status;
            if (job.status === 'done') icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>';
            else if (job.status === 'error') icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>';
            else if (job.status === 'downloading') icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z"/></svg>';
          });
          
          if (st.status === 'done') {
            clearInterval(poll);
            showToast('🎉 Spotify download complete! ' + st.completed + '/' + st.total + ' successful');
            resetBtn();
            if (st.completed > 0) downloadAll.classList.add('active');
          }
        }, 2000);
      } catch (e) {
        showToast('❌ Error starting Spotify download');
        resetBtn();
      }
    });

    form.addEventListener('submit', async e => {
      e.preventDefault();
      convertBtn.disabled = true;
      btnText.textContent = 'Processing...';
      
      if (currentMode === 'single') {
        const url = singleUrl.value.trim();
        const quality = qualitySelect.value;
        if (!url) { showToast('⚠️ Please enter a URL'); resetBtn(); return; }
        if (!isValidYT(url)) { showToast('❌ Invalid YouTube URL'); resetBtn(); return; }
        
        // Check if it's a playlist from preview data
        if (previewData && previewData.is_playlist && previewData.videos) {
          // Convert playlist to batch download (max 50 videos)
          const videosToDownload = previewData.videos.slice(0, 50);
          const playlistUrls = videosToDownload.map(v => v.url).join('\n');
          const totalVideos = previewData.videos.length;
          if (totalVideos > 50) {
            showToast(`📋 Downloading first 50 of ${totalVideos} videos`);
          } else {
            showToast(`📋 Starting playlist download (${totalVideos} videos)`);
          }
          await handleBatch(playlistUrls, quality);
        } else {
          await handleSingle(url, quality);
        }
      } else {
        const urls = batchUrls.value.trim();
        const quality = batchQualitySelect.value;
        if (!urls) { showToast('⚠️ Please enter URLs'); resetBtn(); return; }
        await handleBatch(urls, quality);
      }
    });
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)