"""Private YouTube import service. No account cookies or arbitrary URL fetching."""
import concurrent.futures
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = os.environ.get('IMPORT_TOKEN', '')
ORIGIN = os.environ.get('ALLOWED_ORIGIN', 'https://one-minute-studio.elkysky.chatgpt.site').rstrip('/')
ROOT = Path(os.environ.get('JOB_DIR', '/tmp/one-cut-jobs'))
ROOT.mkdir(parents=True, exist_ok=True)
MAX_CLIP = 300
MAX_SOURCE = 1800
MAX_BYTES = 350 * 1024 * 1024
TTL = 900
jobs = {}
lock = threading.RLock()
worker = concurrent.futures.ThreadPoolExecutor(max_workers=1)

class UserError(Exception):
    pass

def canonical_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise UserError('유튜브 영상 주소를 확인해 주세요.')
    u = urlparse(value.strip())
    if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443):
        raise UserError('https로 시작하는 유튜브 주소만 사용할 수 있습니다.')
    host = (u.hostname or '').lower()
    if host == 'youtu.be':
        vid = u.path.strip('/')
    elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
        if u.path == '/watch':
            vid = parse_qs(u.query).get('v', [''])[0]
        elif u.path.startswith(('/shorts/', '/embed/')):
            vid = u.path.split('/')[2]
        else:
            vid = ''
    else:
        vid = ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
        raise UserError('재생목록이 아닌 유튜브 영상 하나의 주소를 입력해 주세요.')
    return 'https://www.youtube.com/watch?v=' + vid

def clip_range(start, end):
    if isinstance(start, bool) or isinstance(end, bool):
        raise UserError('시작·끝 시간을 확인해 주세요.')
    try:
        start, end = float(start), float(end)
    except (TypeError, ValueError):
        raise UserError('시작·끝 시간을 확인해 주세요.')
    if not all(map(math.isfinite, (start, end))) or start < 0 or end > MAX_SOURCE or not .2 <= end - start <= MAX_CLIP:
        raise UserError('0.2초~5분 구간을 지정해 주세요. 원본은 최대 30분까지 지원합니다.')
    return start, end

def run_command(command, job, timeout=120):
    log = Path(job['dir']) / 'command.log'
    with log.open('wb') as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                size = sum(p.stat().st_size for p in Path(job['dir']).glob('*') if p.is_file())
                if job['cancel'].is_set():
                    raise UserError('가져오기를 취소했습니다.')
                if time.monotonic() > deadline:
                    raise UserError('요청 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.')
                if size > MAX_BYTES:
                    raise UserError('임시 파일 용량 한도를 넘었습니다. 더 짧은 구간을 선택해 주세요.')
                time.sleep(.2)
            if process.returncode:
                raise UserError('유튜브에서 영상을 가져오지 못했습니다. 접근 제한 또는 일시적인 오류일 수 있습니다.')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    return log.read_text(errors='replace')

def base_command():
    return ['yt-dlp', '--ignore-config', '--no-cache-dir', '--no-playlist', '--no-progress', '--quiet',
            '--no-warnings', '--socket-timeout', '15', '--retries', '1', '--extractor-retries', '0',
            '--js-runtimes', 'node', '--extractor-args', 'youtube:skip=hls,dash']

def inspect(url, job):
    result = run_command(base_command() + ['--skip-download', '--dump-single-json', url], job)
    try:
        data = json.loads(result)
    except ValueError:
        raise UserError('영상 정보를 읽지 못했습니다. 잠시 후 다시 시도해 주세요.')
    duration = data.get('duration')
    if data.get('is_live') or data.get('live_status') in ('is_live', 'is_upcoming'):
        raise UserError('실시간 방송은 가져올 수 없습니다.')
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= MAX_SOURCE:
        raise UserError('길이를 확인할 수 없거나 30분을 넘는 영상입니다.')
    if data.get('availability') not in (None, 'public', 'unlisted'):
        raise UserError('공개 재생 가능한 영상만 지원합니다.')
    return {'title': str(data.get('title') or '유튜브 영상')[:300], 'duration': duration, 'url': url}

def process_job(job):
    try:
        if job['cancel'].is_set():
            return
        job.update(status='working', message='영상 정보를 확인하고 있습니다.')
        meta = inspect(job['url'], job)
        if job['kind'] == 'inspect':
            job.update(status='ready', result=meta, message='영상 정보를 확인했습니다.')
            return
        start, end = job['start'], job['end']
        if end > meta['duration'] + .05:
            raise UserError('끝 시간이 원본 영상보다 깁니다.')
        job.update(message='선택한 구간을 가져오고 있습니다. 잠시 기다려 주세요.')
        directory = Path(job['dir'])
        output = directory / 'clip.%(ext)s'
        run_command(base_command() + [
            '-f', 'bv[height<=720][vcodec^=avc1]+ba[ext=m4a]/b[height<=720][ext=mp4]',
            '--max-filesize', '300M', '--download-sections', f'*{start}-{end}',
            '--force-keyframes-at-cuts', '--merge-output-format', 'mp4',
            '-o', str(output), meta['url']], job, timeout=420)
        files = list(directory.glob('clip.mp4'))
        if not files or not 100 <= files[0].stat().st_size <= MAX_BYTES:
            raise UserError('완성된 MP4 파일을 확인하지 못했습니다.')
        job.update(message='영상 파일을 확인하고 있습니다.')
        probe = run_command(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=codec_type',
                             '-of', 'json', str(files[0])], job, timeout=20)
        verified = json.loads(probe)
        duration = float(verified.get('format', {}).get('duration', 0))
        if not max(.1, end - start - .5) <= duration <= end - start + 1 or not any(s.get('codec_type') == 'video' for s in verified.get('streams', [])):
            raise UserError('선택한 구간의 영상 파일 검증에 실패했습니다.')
        job.update(status='ready', result={**meta, 'start': start, 'end': end, 'size': files[0].stat().st_size},
                   file=str(files[0]), message='준비되었습니다. 편집기로 전달합니다.')
    except UserError as exc:
        job.update(status='cancelled' if job['cancel'].is_set() else 'failed', message=str(exc))
    except Exception:
        job.update(status='failed', message='처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')
    finally:
        if job['cancel'].is_set():
            job.update(status='cancelled', message='가져오기를 취소했습니다.')
        if job['kind'] == 'inspect' or job['status'] != 'ready':
            shutil.rmtree(job['dir'], ignore_errors=True)

def submit(payload):
    kind = payload.get('kind')
    if kind not in ('inspect', 'import'):
        raise UserError('잘못된 작업입니다.')
    url = canonical_url(payload.get('url'))
    start, end = clip_range(payload.get('start'), payload.get('end')) if kind == 'import' else (0, 0)
    with lock:
        if sum(j['status'] in ('queued', 'working') for j in jobs.values()) >= 3:
            raise UserError('처리 중인 작업이 많습니다. 잠시 후 다시 시도해 주세요.')
        ident = secrets.token_urlsafe(24)
        job = dict(id=ident, kind=kind, url=url, start=start, end=end, created=time.time(),
                   status='queued', message='순서를 기다리고 있습니다.', cancel=threading.Event(),
                   dir=tempfile.mkdtemp(prefix='job-', dir=ROOT))
        jobs[ident] = job
        worker.submit(process_job, job)
    return job

def cleanup_loop():
    while True:
        time.sleep(30)
        with lock:
            for ident, job in list(jobs.items()):
                if time.time() - job['created'] > TTL:
                    job['cancel'].set()
                    if job['status'] not in ('queued', 'working'):
                        shutil.rmtree(job['dir'], ignore_errors=True)
                        jobs.pop(ident, None)

class Handler(BaseHTTPRequestHandler):
    server_version = 'OneCutImport/1'
    def log_message(self, *args):
        pass
    def common(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if self.headers.get('Origin') == ORIGIN:
            self.send_header('Access-Control-Allow-Origin', ORIGIN)
            self.send_header('Vary', 'Origin')
    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.common()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def authorized(self):
        origin = self.headers.get('Origin')
        if origin and origin != ORIGIN:
            self.send_json(403, {'error': '허용되지 않은 접속입니다.'})
            return False
        given = self.headers.get('Authorization', '').removeprefix('Bearer ')
        if not TOKEN or not hmac.compare_digest(given.encode(), TOKEN.encode()):
            self.send_json(401, {'error': '연결 비밀번호를 확인해 주세요.'})
            return False
        return True
    def do_OPTIONS(self):
        if self.headers.get('Origin') != ORIGIN:
            self.send_json(403, {'error': '허용되지 않은 접속입니다.'})
            return
        self.send_response(204)
        self.common()
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.end_headers()
    def do_GET(self):
        if self.path == '/health':
            self.send_json(200, {'service': 'one-cut-import', 'api': 1})
            return
        if not self.authorized():
            return
        if self.path == '/v1/status':
            self.send_json(200, {'service': 'one-cut-import', 'api': 1, 'max_clip_seconds': MAX_CLIP})
            return
        parts = self.path.strip('/').split('/')
        job = jobs.get(parts[2]) if len(parts) >= 3 and parts[:2] == ['v1', 'jobs'] else None
        if not job:
            self.send_json(404, {'error': '작업이 만료되었습니다. 다시 가져와 주세요.'})
        elif len(parts) == 4 and parts[3] == 'file' and job['status'] == 'ready' and job.get('file'):
            try:
                with open(job['file'], 'rb') as f:
                    self.send_response(200)
                    self.common()
                    self.send_header('Content-Type', 'video/mp4')
                    self.send_header('Content-Length', str(os.fstat(f.fileno()).st_size))
                    self.end_headers()
                    shutil.copyfileobj(f, self.wfile, 64 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except FileNotFoundError:
                self.send_json(404, {'error': '파일이 만료되었습니다.'})
        elif len(parts) == 3:
            self.send_json(200, {k: job[k] for k in ('id', 'status', 'message', 'result') if k in job})
        else:
            self.send_json(409, {'error': '아직 영상이 준비되지 않았습니다.'})
    def do_POST(self):
        if not self.authorized():
            return
        if self.path != '/v1/jobs':
            self.send_json(404, {'error': '요청 주소가 올바르지 않습니다.'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                raise UserError('요청 크기가 올바르지 않습니다.')
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise UserError('요청 형식이 올바르지 않습니다.')
            job = submit(payload)
            self.send_json(202, {'id': job['id'], 'status': job['status']})
        except (UserError, ValueError) as exc:
            self.send_json(400, {'error': str(exc) if isinstance(exc, UserError) else '입력값을 확인해 주세요.'})
    def do_DELETE(self):
        if not self.authorized():
            return
        parts = self.path.strip('/').split('/')
        ident = parts[2] if len(parts) == 3 and parts[:2] == ['v1', 'jobs'] else ''
        with lock:
            job = jobs.get(ident)
            if job:
                job['cancel'].set()
                if job['status'] not in ('queued', 'working'):
                    shutil.rmtree(job['dir'], ignore_errors=True)
                    jobs.pop(ident, None)
        self.send_json(200, {'deleted': True})

def main():
    import static_ffmpeg
    static_ffmpeg.add_paths(weak=True)
    if len(TOKEN) < 24:
        raise SystemExit('IMPORT_TOKEN must contain at least 24 characters')
    # Only this app owns ROOT. Clean leftover temporary jobs on restart.
    for directory in ROOT.glob('job-*'):
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
    threading.Thread(target=cleanup_loop, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '10000'))), Handler).serve_forever()

if __name__ == '__main__':
    main()
