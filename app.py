from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import time
import uuid
import threading
import zipfile
from werkzeug.utils import secure_filename

app = Flask(__name__, template_folder='templates')
CORS(app)

DOWNLOAD_FOLDER = os.path.abspath('downloads')
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

def cleanup_old_downloads(max_age_seconds=3600):
    """1 saatten eski geçici dosyaları temizler."""
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
        threading.Thread(target=cleanup_old_downloads, daemon=True).start()

        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Geçersiz veri biçimi.'}), 400

        url = data.get('url')
        quality = data.get('quality', 'best')
        
        if not url:
            return jsonify({'success': False, 'error': 'Bağlantı adresi boş olamaz.'}), 400

        url = url.strip()
        cookies_path = os.path.join(os.path.dirname(__file__), 'cookies.txt')
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
            ydl_opts.update({
                'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
                'merge_output_format': 'mp4'
            })
        elif quality == '720':
            ydl_opts.update({
                'format': 'bestvideo[height<=720]+bestaudio/best[height<=720]/best',
                'merge_output_format': 'mp4'
            })
        else:
            ydl_opts.update({
                'format': 'bestvideo+bestaudio/best',
                'merge_output_format': 'mp4'
            })
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return jsonify({'success': False, 'error': 'Medya bilgisi çözümlenemedi.'}), 400

            video_id = info.get('id', 'media')
            
            # Gerçekte oluşan dosyayı klasörde bularak uzantı uyuşmazlığını %100 önle
            expected_prefix = f"{video_id}_{unique_id}."
            actual_filename = None
            for f in os.listdir(DOWNLOAD_FOLDER):
                if f.startswith(expected_prefix):
                    actual_filename = f
                    break

            if actual_filename:
                filename = actual_filename
                ext = filename.rsplit('.', 1)[-1]
            else:
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

@app.route('/api/download-zip', methods=['POST'])
def download_zip():
    try:
        data = request.get_json()
        filenames = data.get('filenames', [])
        if not filenames:
            return jsonify({'success': False, 'error': 'Dosya listesi boş.'}), 400

        zip_filename = f"can_studio_batch_{uuid.uuid4().hex[:6]}.zip"
        zip_path = os.path.join(DOWNLOAD_FOLDER, zip_filename)

        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for fname in filenames:
                safe_name = secure_filename(fname)
                fpath = os.path.join(DOWNLOAD_FOLDER, safe_name)
                if os.path.exists(fpath):
                    zipf.write(fpath, arcname=safe_name)

        return jsonify({'success': True, 'zipUrl': f"/api/download/{zip_filename}"})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/download/<filename>', methods=['GET'])
def download_file(filename):
    try:
        safe_filename = secure_filename(filename)
        filepath = os.path.join(DOWNLOAD_FOLDER, safe_filename)
        if not os.path.exists(filepath):
            return jsonify({'error': 'Dosya bulunamadı.'}), 404

        custom_title = request.args.get('title', '').strip()
        if custom_title:
            ext = safe_filename.rsplit('.', 1)[-1]
            clean_title = "".join(c for c in custom_title if c.isalnum() or c in (' ', '-', '_')).strip()
            download_name = f"{clean_title or 'can_studio_medya'}.{ext}"
            return send_file(filepath, as_attachment=True, download_name=download_name)

        return send_file(filepath, as_attachment=True)
    except Exception as e:
        return jsonify({'error': 'Dosya bulunamadı.'}), 404

@app.route('/api/stream/<filename>', methods=['GET'])
def stream_file(filename):
    try:
        safe_filename = secure_filename(filename)
        filepath = os.path.join(DOWNLOAD_FOLDER, safe_filename)
        return send_file(filepath)
    except Exception as e:
        return jsonify({'error': 'Dosya akışı sağlanamadı.'}), 404

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
