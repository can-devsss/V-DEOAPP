from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import re
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
import zipfile
import socket
import urllib.request
import urllib.parse
import json
from werkzeug.utils import secure_filename

app = Flask(__name__, template_folder='templates')
CORS(app)

DOWNLOAD_FOLDER = os.path.abspath('downloads')
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# Aynı anda 50 kişiye kadar eşzamanlı indirme/işleme kapasitesi (ThreadPoolExecutor)
executor = ThreadPoolExecutor(max_workers=50)

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def format_duration(seconds_val, fallback_str=None):
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

def cleanup_old_downloads(max_age_seconds=1800):
    """30 dakikadan eski geçici dosyaları sunucuyu yormamak için otomatik temizler."""
    try:
        now = time.time()
        for filename in os.listdir(DOWNLOAD_FOLDER):
            filepath = os.path.join(DOWNLOAD_FOLDER, filename)
            if os.path.isfile(filepath):
                if now - os.path.getmtime(filepath) > max_age_seconds:
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
    except Exception:
        pass

def get_platform_headers(url):
    lower = url.lower()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7',
    }
    if 'tiktok.com' in lower:
        headers['Referer'] = 'https://www.tiktok.com/'
    elif 'instagram.com' in lower:
        headers['Referer'] = 'https://www.instagram.com/'
    elif 'twitter.com' in lower or 'x.com' in lower:
        headers['Referer'] = 'https://x.com/'
        headers['Origin'] = 'https://x.com'
    elif 'pinterest.com' in lower or 'pin.it' in lower:
        headers['Referer'] = 'https://www.pinterest.com/'
    elif 'youtube.com' in lower or 'youtu.be' in lower:
        headers['Referer'] = 'https://www.youtube.com/'
    return headers

def download_direct_stream(direct_url, prefix, unique_id, quality, referer='https://google.com/'):
    ext = 'mp3' if quality == 'audio' else 'mp4'
    out_template = os.path.join(DOWNLOAD_FOLDER, f"{prefix}_{unique_id}.%(ext)s")
    dl_opts = {
        'quiet': True,
        'outtmpl': out_template,
        'no_warnings': True,
        'retries': 10,
        'socket_timeout': 120,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Referer': referer,
        }
    }
    if quality == 'audio':
        dl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        })

    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        ydl.download([direct_url])

    for f in os.listdir(DOWNLOAD_FOLDER):
        if f.startswith(f"{prefix}_{unique_id}."):
            return f, f.rsplit('.', 1)[-1]
    return f"{prefix}_{unique_id}.{ext}", ext

def fallback_instagram_extractor(url, unique_id, quality):
    match = re.search(r'instagram\.com/(?:p|reel|reels)/([^/?]+)', url)
    shortcode = match.group(1) if match else 'insta'

    try:
        api_url = f"https://api.cobalt.tools/api/json"
        payload = json.dumps({"url": url, "vQuality": "1080"}).encode('utf-8')
        req = urllib.request.Request(api_url, data=payload, headers={
            'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json', 'Content-Type': 'application/json'
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            res_json = json.loads(resp.read().decode('utf-8'))
        direct_url = res_json.get('url') or res_json.get('picker', [{}])[0].get('url')
        if direct_url:
            fname, ext = download_direct_stream(direct_url, f"ig_{shortcode}", unique_id, quality, 'https://www.instagram.com/')
            return {'id': shortcode, 'title': f"Instagram Video #{shortcode}", 'duration': None, 'thumbnail': '', 'uploader': 'Instagram', 'filename': fname, 'ext': ext}
    except Exception:
        pass
    return None

def fallback_x_extractor(url, unique_id, quality):
    match = re.search(r'(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)', url)
    if not match:
        return None
    user, status_id = match.group(1), match.group(2)

    for api_domain in ['api.fxtwitter.com', 'api.vxtwitter.com']:
        try:
            api_url = f"https://{api_domain}/{user}/status/{status_id}"
            req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode('utf-8'))

            if api_domain == 'api.fxtwitter.com':
                tweet = data.get('tweet') or {}
                media = tweet.get('media') or {}
                videos = media.get('videos') or media.get('all') or []
                if not videos:
                    continue
                v = videos[0]
                direct_url = v.get('url')
                if not direct_url:
                    continue
                fname, ext = download_direct_stream(direct_url, f"x_{status_id}", unique_id, quality, 'https://x.com/')
                author = tweet.get('author') or {}
                return {
                    'id': status_id,
                    'title': (tweet.get('text') or f"X Video #{status_id}").split('\n')[0][:100],
                    'duration': v.get('duration'),
                    'thumbnail': v.get('thumbnail_url') or '',
                    'uploader': author.get('name') or author.get('screen_name') or user,
                    'filename': fname,
                    'ext': ext
                }
            else:
                media_extended = data.get('media_extended') or []
                videos = [m for m in media_extended if m.get('type') in ('video', 'gif')]
                if not videos:
                    continue
                v = videos[0]
                direct_url = v.get('url')
                if not direct_url:
                    continue
                fname, ext = download_direct_stream(direct_url, f"x_{status_id}", unique_id, quality, 'https://x.com/')
                return {
                    'id': status_id,
                    'title': (data.get('text') or f"X Video #{status_id}").split('\n')[0][:100],
                    'duration': None,
                    'thumbnail': v.get('thumbnail_url') or '',
                    'uploader': data.get('user_name') or user,
                    'filename': fname,
                    'ext': ext
                }
        except Exception:
            continue
    return None

def fallback_tiktok_extractor(url, unique_id, quality):
    try:
        api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(url)}&hd=1"
        req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            res_json = json.loads(resp.read().decode('utf-8'))
        data = res_json.get('data') or {}
        direct_url = data.get('hdplay') or data.get('play')
        if not direct_url:
            return None
        vid_id = str(data.get('id') or 'tiktok')
        fname, ext = download_direct_stream(direct_url, f"tt_{vid_id}", unique_id, quality, 'https://www.tiktok.com/')
        author = data.get('author') or {}
        return {
            'id': vid_id,
            'title': (data.get('title') or f"TikTok Video #{vid_id}").split('\n')[0][:100],
            'duration': data.get('duration'),
            'thumbnail': data.get('cover') or '',
            'uploader': author.get('nickname') or 'TikTok',
            'filename': fname,
            'ext': ext
        }
    except Exception:
        return None

def process_single_extraction(url, quality):
    """Uzun ve kalabalık istekleri arka planda eşzamanlı işleyen motor."""
    cleanup_old_downloads()
    unique_id = uuid.uuid4().hex[:8]
    lower_url = url.lower()
    is_twitter = any(d in lower_url for d in ['twitter.com', 'x.com'])
    is_tiktok = 'tiktok.com' in lower_url
    is_instagram = 'instagram.com' in lower_url

    cookies_path = os.path.join(os.path.dirname(__file__), 'cookies.txt')
    headers = get_platform_headers(url)

    ydl_opts = {
        'quiet': True,
        'outtmpl': os.path.join(DOWNLOAD_FOLDER, f'%(id)s_{unique_id}.%(ext)s'),
        'no_warnings': True,
        'ignoreerrors': False,
        'socket_timeout': 120,
        'noplaylist': True,
        'retries': 15,
        'fragment_retries': 15,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'format_sort': ['vcodec:h264', 'acodec:m4a', 'ext:mp4:m4a'],
        'extractor_args': {
            'youtube': {'player_client': ['ios', 'android', 'tv_embedded', 'web']},
            'twitter': {'api': ['syndication', 'graphql', 'legacy']},
            'tiktok': {'app_name': ['trill', 'musical_ly']},
            'instagram': {'geo_bypass': ['1']}
        },
        'http_headers': headers
    }

    if os.path.exists(cookies_path):
        ydl_opts['cookiefile'] = cookies_path

    if quality == 'audio':
        ydl_opts.update({
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        })
    elif quality == '1080':
        ydl_opts.update({
            'format': 'bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
            'merge_output_format': 'mp4'
        })
    elif quality == '720':
        ydl_opts.update({
            'format': 'bestvideo[height<=720][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best',
            'merge_output_format': 'mp4'
        })
    else:
        ydl_opts.update({
            'format': 'bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
            'merge_output_format': 'mp4'
        })

    info = None
    filename = None
    ext = None

    if is_instagram:
        info = fallback_instagram_extractor(url, unique_id, quality)
    elif is_twitter:
        info = fallback_x_extractor(url, unique_id, quality)
    elif is_tiktok:
        info = fallback_tiktok_extractor(url, unique_id, quality)

    if not info:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as primary_err:
            try:
                fallback_opts = dict(ydl_opts)
                fallback_opts.pop('format_sort', None)
                if quality != 'audio':
                    fallback_opts['format'] = 'best[protocol^=m3u8]/bestvideo+bestaudio/best'
                with yt_dlp.YoutubeDL(fallback_opts) as ydl_fb:
                    info = ydl_fb.extract_info(url, download=True)
            except Exception:
                if is_twitter and not info:
                    info = fallback_x_extractor(url, unique_id, quality)
                elif is_tiktok and not info:
                    info = fallback_tiktok_extractor(url, unique_id, quality)
                elif is_instagram and not info:
                    info = fallback_instagram_extractor(url, unique_id, quality)
                
                if not info:
                    raise primary_err

    if not info:
        return {'success': False, 'error': 'Medya bilgisi çözümlenemedi.'}

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

    dimensions = f"{width} × {height}" if (width and height and ext != 'mp3') else ("Ses Akışı" if ext == 'mp3' else "HD Akış")
    fps_text = f"{int(round(fps))} FPS" if fps else ""
    clean_duration = format_duration(info.get('duration'), info.get('duration_string'))

    raw_title = info.get('title') or info.get('description') or 'Medya Dosyası'
    clean_display_title = str(raw_title).strip().split('\n')[0][:110]

    return {
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
    }

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/extract', methods=['POST'])
def extract_media():
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'success': False, 'error': 'Geçersiz veri biçimi.'}), 400

        url = data.get('url')
        quality = data.get('quality', 'best')
        
        if not url or not str(url).strip():
            return jsonify({'success': False, 'error': 'Bağlantı adresi boş olamaz.'}), 400

        url = str(url).strip()
        base_url = request.host_url.rstrip('/')
        lan_base_url = base_url
        if "localhost" in base_url or "127.0.0.1" in base_url:
            port_part = request.host.split(':')[1] if ':' in request.host else '5000'
            lan_base_url = f"http://{get_local_ip()}:{port_part}"

        # ThreadPoolExecutor üzerinden eşzamanlı kuyrukta çalıştır
        future = executor.submit(process_single_extraction, url, quality)
        result = future.result(timeout=180) # 3 dakikalık uzun video zaman aşımı koruması

        if not result.get('success'):
            return jsonify(result), 400

        result['streamUrl'] = f"{base_url}/api/stream/{result['filename']}"
        result['lanBaseUrl'] = lan_base_url

        return jsonify(result)

    except Exception as e:
        return jsonify({'success': False, 'error': f'İşlem hatası (Zaman aşımı veya yoğunluk): {str(e)}'}), 500

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
