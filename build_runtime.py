import shutil
import subprocess
import static_ffmpeg
static_ffmpeg.add_paths(weak=True)
for binary in ('ffmpeg', 'ffprobe', 'node', 'yt-dlp'):
    if not shutil.which(binary):
        raise SystemExit(f'Required runtime unavailable: {binary}')
    result = subprocess.run([binary, '--version' if binary in ('node','yt-dlp') else '-version'], capture_output=True, text=True, check=True)
    print(result.stdout.splitlines()[0])
