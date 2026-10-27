from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import time
import uuid
import threading

app = Flask(__name__, template_folder='templates')
CORS(app)

DOWNLOAD_FOLDER = os.path.abspath('downloads')
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

download_lock = threading.Lock()

def cleanup_old_downloads(max_age_seconds=3600):
    """Downloads klasöründe 1 saatten eski geçici dosyaları temizler."""
    try:
        now = time.time()
        for filename in os.listdir(DOWNLOAD_FOLDER):
            filepath = os.path.join(DOWNLOAD_FOLDER, filename)
            if os.path.isfile(filepath):
                if now - os.path.getmtime(filepath) > max_age_seconds:
                    os.remove(filepath)
    except Exception as e:
        print(f"Temizlik hatası: {e}")

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/extract', methods=['POST'])
def extract_media():
    try:
        # Eski dosyaları arka planda temizle
        threading.Thread(target=cleanup_old_downloads).start()

        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Geçersiz veri biçimi.'}), 400

        url = data.get('url')
        quality = data.get('quality', 'best')
        
        if not url:
            return jsonify({'success': False, 'error': 'Bağlantı adresi boş olamaz.'}), 400

        url = url.strip()
        cookies_path = os.path.join(os.path.dirname(__file__), 'cookies.txt')

        # Çakışmaları önlemek için benzersiz bir önek (unique prefix) üretiyoruz
        unique_id = uuid.uuid4().hex[:8]

        ydl_opts = {
            'quiet': True,
            'outtmpl': os.path.join(DOWNLOAD_FOLDER, f'%(id)s_{unique_id}.%(ext)s'),
            'no_warnings': True,
            'ignoreerrors': True,
            'socket_timeout': 30,
            'noplaylist': True,
            'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            }
        }

        if os.path.exists(cookies_path):
            ydl_opts['cookiefile'] = cookies_path

        if quality == 'audio':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        elif quality == '1080':
            ydl_opts.update({'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best'})
        elif quality == '720':
            ydl_opts.update({'format': 'bestvideo[height<=720]+bestaudio/best[height<=720]/best'})
        else:
            ydl_opts.update({'format': 'best/best'})
        
        # Thread kilidi ile aynı anda yazma çakışmalarını tamamen engelliyoruz
        with download_lock:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    return jsonify({'success': False, 'error': 'Medya bilgisi çözümlenemedi.'}), 400

                video_id = info.get('id', 'media')
                ext = 'mp3' if quality == 'audio' else info.get('ext', 'mp4')
                filename = f"{video_id}_{unique_id}.{ext}"
                filepath = os.path.join(DOWNLOAD_FOLDER, filename)

        file_size_mb = "Bilinmiyor"
        if os.path.exists(filepath):
            size_bytes = os.path.getsize(filepath)
            file_size_mb = f"{size_bytes / (1024 * 1024):.1f} MB"

        base_url = request.host_url.rstrip('/')
        stream_url = f"{base_url}/api/stream/{filename}"

        return jsonify({
            'success': True,
            'title': info.get('title', 'Medya Dosyası'),
            'duration': info.get('duration_string', 'Medya'),
            'thumbnail': info.get('thumbnail', ''),
            'filename': filename,
            'ext': ext,
            'uploader': info.get('uploader', 'CAN STUDIO'),
            'filesize': file_size_mb,
            'streamUrl': stream_url
        })

    except Exception as e:
        return jsonify({'success': False, 'error': f'İşlem hatası: {str(e)}'}), 500

@app.route('/api/download/<filename>', methods=['GET'])
def download_file(filename):
    try:
        filepath = os.path.join(DOWNLOAD_FOLDER, filename)
        return send_file(filepath, as_attachment=True)
    except Exception as e:
        return jsonify({'error': 'Dosya bulunamadı.'}), 404

@app.route('/api/stream/<filename>', methods=['GET'])
def stream_file(filename):
    try:
        filepath = os.path.join(DOWNLOAD_FOLDER, filename)
        return send_file(filepath)
    except Exception as e:
        return jsonify({'error': 'Dosya akışı sağlanamadı.'}), 404

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
