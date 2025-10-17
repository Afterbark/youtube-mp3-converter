from flask import Flask, render_template, request, send_file
import yt_dlp
import os
import uuid

app = Flask(__name__)

DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

@app.route('/')
def home():
    return '''
        <h2>YouTube to MP3 Converter</h2>
        <form action="/download" method="post">
            <input type="text" name="url" placeholder="Enter YouTube URL" required>
            <button type="submit">Convert</button>
        </form>
    '''

@app.route('/download', methods=['POST'])
def download():
    url = request.form['url']
    file_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_FOLDER, f"{file_id}.%(ext)s")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_path,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    mp3_file = os.path.join(DOWNLOAD_FOLDER, f"{file_id}.mp3")
    return send_file(mp3_file, as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True)
