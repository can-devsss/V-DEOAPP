from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import re
import time
import uuid
import threading
import zipfile
import socket
import urllib.request
import json
from werkzeug.utils import secure_filename

app = Flask(__name__, template_folder='templates')
CORS(app)

DOWNLOAD_FOLDER = os.path.abspath('downloads')
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

def get_local_ip():
    """Telefonla QR okutulduğunda localhost yerine yerel Wi-Fi IP adresini döndürür."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def format_duration(seconds_val, fallback_str=None):
    """Saniye değerini her zaman 00:09 veya 01:24:05 gibi profesyonel formata çevirir."""
    try:
        if seconds_val is not None:
            total_sec = int(round(float(seconds_val)))
            hrs, rem = divmod(total_sec, 3600)
            mins, secs = divmod(rem, 60)
            if hrs > 0:
                return f"{hrs:02d}:{mins:02d}:{secs:02d}"
            return f"{mins:02d}:{secs:02d}"
    except Exception:
        pass
    if fallback_str and ":" in str(fallback_str):
        return str(fallback_str)
    if fallback_str and str(fallback_str).isdigit():
        return f"00:{int(fallback_str):02d}"
    return "00:00"

def format_number(n):
    """İzlenme ve beğeni sayılarını okunaklı formata çevirir."""
    try:
        if not n:
            return ""
        n = int(n)
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}B"
        return str(n)
    except Exception:
        return ""

def cleanup_old_downloads(max_age_seconds=3600):
    """1 saatten eski geçici dosyaları sessizce temizler."""
    try:
        now = time.time()
        for filename in os.listdir(DOWNLOAD_FOLDER):
            filepath = os.path.join(DOWNLOAD_FOLDER, filename)
            if os.path.isfile(filepath):
                if now - os.path.getmtime(filepath) > max_age_seconds:
                    os.remove(filepath)
    except Exception:
        pass

def fallback_x_extractor(url, unique_id, quality):
    """
    Render gibi bulut sunucularda X (Twitter) 403 Forbidden verdiğinde
    FxTwitter / VxTwitter API köprüsü üzerinden videoyu doğrudan kurtarır.
    """
    match = re.search(r'(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)', url)
    if not match:
        return None
    user, status_id = match.group(1), match.group(2)
    api_url = f"https://api.fxtwitter.com/{user}/status/{status_id}"

    req = urllib.request.Request(api_url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36'
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode('utf-8'))

    tweet = data.get('tweet') or {}
    media = tweet.get('media') or {}
    videos = media.get('videos') or media.get('all') or []
    if not videos:
        return None

    video_obj = videos[0]
    direct_video_url = video_obj.get('url')
    if not direct_video_url:
        return None

    ext = 'mp3' if quality == 'audio' else 'mp4'
    filename = f"x_{status_id}_{unique_id}.{ext}"
    filepath = os.path.join(DOWNLOAD_FOLDER, filename)

    dl_opts = {
        'quiet': True,
        'outtmpl': filepath,
        'no_warnings': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Referer': 'https://x.com/',
        }
    }
    if quality == 'audio':
        dl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'outtmpl': os.path.join(DOWNLOAD_FOLDER, f"x_{status_id}_{unique_id}.%(ext)s")
        })

    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        ydl.download([direct_video_url])

    # Oluşan dosyayı doğrula
    for f in os.listdir(DOWNLOAD_FOLDER):
        if f.startswith(f"x_{status_id}_{unique_id}."):
            filename = f
            ext = f.rsplit('.', 1)[-1]
            break

    author = tweet.get('author') or {}
    return {
        'id': status_id,
        'title': (tweet.get('text') or f"X Video #{status_id}").split('\n')[0][:100],
        'duration': video_obj.get('duration'),
        'thumbnail': video_obj.get('thumbnail_url') or '',
        'width': video_obj.get('width'),
        'height': video_obj.get('height'),
        'uploader': author.get('name') or author.get('screen_name') or user,
        'view_count': tweet.get('views'),
        'like_count': tweet.get('likes'),
        'filename': filename,
        'ext': ext
    }

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/extract', methods=['POST'])
def extract_media():
    try:
        threading.Thread(target=cleanup_old_downloads, daemon=True).start()

        data = request.get_json(silent=True)
        if not data:
            return jsonify({'success': False, 'error': 'Geçersiz veri biçimi.'}), 400

        url = data.get('url')
        quality = data.get('quality', 'best')
        
        if not url or not str(url).strip():
            return jsonify({'success': False, 'error': 'Bağlantı adresi boş olamaz.'}), 400

        url = str(url).strip()
        is_twitter = any(d in url.lower() for d in ['twitter.com', 'x.com'])
        cookies_path = os.path.join(os.path.dirname(__file__), 'cookies.txt')
        unique_id = uuid.uuid4().hex[:8]

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept-Language': 'tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7',
        }
        if is_twitter:
            headers['Referer'] = 'https://x.com/'
            headers['Origin'] = 'https://x.com'

        ydl_opts = {
            'quiet': True,
            'outtmpl': os.path.join(DOWNLOAD_FOLDER, f'%(id)s_{unique_id}.%(ext)s'),
            'no_warnings': True,
            'ignoreerrors': False,
            'socket_timeout': 30,
            'noplaylist': True,
            'retries': 3,
            'fragment_retries': 3,
            'format_sort': ['vcodec:h264', 'acodec:m4a', 'ext:mp4:m4a'],
            'extractor_args': {
                'twitter': {'api': ['syndication', 'graphql', 'legacy']}
            },
            'http_headers': headers
        }

        if os.path.exists(cookies_path):
            ydl_opts['cookiefile'] = cookies_path

        if quality == 'audio':
            ydl_opts.update({
                'format': 'bestaudio[ext=m4a]/bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        elif quality == '1080':
            ydl_opts.update({
                'format': 'bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
                'merge_output_format': 'mp4'
            })
        elif quality == '720':
            ydl_opts.update({
                'format': 'bestvideo[height<=720][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best',
                'merge_output_format': 'mp4'
            })
        else:
            ydl_opts.update({
                'format': 'bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
                'merge_output_format': 'mp4'
            })

        info = None
        filename = None
        ext = None

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as primary_err:
            # Eğer X (Twitter) 403 Forbidden verdiyse 2. ve 3. Kurtarma Motorunu devreye sok
            if is_twitter:
                try:
                    # 2. Aşama: HLS (m3u8) protokolünü zorlayarak dene
                    hls_opts = dict(ydl_opts)
                    hls_opts['format'] = 'best[protocol^=m3u8]/bestvideo[protocol^=m3u8]+bestaudio/best'
                    with yt_dlp.YoutubeDL(hls_opts) as ydl_hls:
                        info = ydl_hls.extract_info(url, download=True)
                except Exception:
                    # 3. Aşama: FxTwitter API Köprüsü ile doğrudan çek (%100 403 Bypass)
                    fb_data = fallback_x_extractor(url, unique_id, quality)
                    if fb_data:
                        info = fb_data
                        filename = fb_data['filename']
                        ext = fb_data['ext']
                    else:
                        raise primary_err
            else:
                raise primary_err

        if not info:
            return jsonify({'success': False, 'error': 'Medya bilgisi çözümlenemedi.'}), 400

        if not filename:
            video_id = secure_filename(str(info.get('id', 'media'))) or 'media'
            expected_prefix = f"{video_id}_{unique_id}."
            actual_filename = None
            for f in os.listdir(DOWNLOAD_FOLDER):
                if f.endswith(f"_{unique_id}.mp4") or f.endswith(f"_{unique_id}.mp3") or f.startswith(expected_prefix):
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

        height = info.get('height')
        width = info.get('width')
        fps = info.get('fps')
        view_count = info.get('view_count')
        like_count = info.get('like_count')

        if quality == 'audio' or ext == 'mp3':
            res_badge = "192 kbps Ses"
        elif height:
            short_side = min(width, height) if width else height
            if short_side >= 2160 or height >= 2160:
                res_badge = "4K Ultra HD"
            elif short_side >= 1440 or height >= 1440:
                res_badge = "2K QHD"
            elif short_side >= 1080 or height >= 1080:
                res_badge = "1080p Full HD"
            elif short_side >= 720 or height >= 720:
                res_badge = "720p HD"
            else:
                res_badge = f"{short_side}p SD"
        else:
            res_badge = "Orijinal Kalite"

        dimensions = f"{width} × {height}" if (width and height and ext != 'mp3') else ("Ses Akışı" if ext == 'mp3' else "Standart")
        fps_text = f"{int(round(fps))} FPS" if fps else ""
        clean_duration = format_duration(info.get('duration'), info.get('duration_string'))

        raw_title = info.get('title') or info.get('description') or 'Medya Dosyası'
        clean_display_title = str(raw_title).strip().split('\n')[0][:110]

        base_url = request.host_url.rstrip('/')
        stream_url = f"{base_url}/api/stream/{filename}"

        lan_base_url = base_url
        if "localhost" in base_url or "127.0.0.1" in base_url:
            port_part = request.host.split(':')[1] if ':' in request.host else '5000'
            lan_base_url = f"http://{get_local_ip()}:{port_part}"

        return jsonify({
            'success': True,
            'title': clean_display_title,
            'duration': clean_duration,
            'thumbnail': info.get('thumbnail', ''),
            'filename': filename,
            'ext': ext,
            'uploader': info.get('uploader') or info.get('uploader_id') or info.get('channel') or 'CAN STUDIO',
            'filesize': file_size_mb,
            'resolution': res_badge,
            'dimensions': dimensions,
            'fps': fps_text,
            'views': format_number(view_count),
            'likes': format_number(like_count),
            'streamUrl': stream_url,
            'lanBaseUrl': lan_base_url
        })

    except Exception as e:
        return jsonify({'success': False, 'error': f'İşlem hatası: {str(e)}'}), 500

@app.route('/api/download-zip', methods=['POST'])
def download_zip():
    try:
        data = request.get_json(silent=True) or {}
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
    except Exception:
        return jsonify({'error': 'Dosya bulunamadı.'}), 404

@app.route('/api/stream/<filename>', methods=['GET'])
def stream_file(filename):
    """HTTP 206 Byte-Range desteğiyle videonun kasmadan ve takılmadan akmasını sağlar."""
    try:
        safe_filename = secure_filename(filename)
        filepath = os.path.join(DOWNLOAD_FOLDER, safe_filename)
        if not os.path.exists(filepath):
            return jsonify({'error': 'Dosya bulunamadı.'}), 404
        response = send_file(filepath, conditional=True)
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Cache-Control'] = 'public, max-age=3600'
        return response
    except Exception:
        return jsonify({'error': 'Dosya akışı sağlanamadı.'}), 404

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
