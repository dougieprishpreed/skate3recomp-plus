"""Verify the installer's media pipeline with generated pixels, without game data."""
import argparse
from pathlib import Path
import subprocess


def invoke(program, arguments, data=None):
    result = subprocess.run([str(program), *arguments], input=data, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace'))
    return result.stdout


def verify_media(ffmpeg, ffprobe):
    # Check the exact EA VP6 readers used by jumbotron preparation, then exercise
    # its scale/rgb24/rawvideo pipe with a synthetic, deterministic input.
    for flag, required in (('-decoders', {'vp6', 'vp6f', 'vp6a', 'rawvideo'}),
                           ('-demuxers', {'ea', 'rawvideo'}),
                           ('-filters', {'scale', 'format'})):
        output = invoke(ffmpeg, ['-hide_banner', flag]).decode(errors='replace')
        available = {line.split()[1] for line in output.splitlines() if len(line.split()) >= 2}
        if not required <= available:
            raise RuntimeError(f'Missing installer media support: {sorted(required - available)}')
    pixel = bytes((31, 127, 223))
    frames = pixel * (8 * 8 * 2)
    input_args = ['-v', 'error', '-f', 'rawvideo', '-pixel_format', 'rgb24',
                  '-video_size', '8x8', '-framerate', '1', '-i', 'pipe:0']
    result = invoke(ffmpeg, [*input_args, '-an', '-sn', '-vf', 'scale=4:4:flags=area',
                            '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'], frames)
    if result != pixel * (4 * 4 * 2):
        raise RuntimeError('Installer media scaling/pipe did not preserve the fixture pixels')
    rate = invoke(ffprobe, [*input_args[:-2], '-show_entries', 'stream=r_frame_rate',
                           '-of', 'csv=p=0', 'pipe:0'], frames).decode().strip()
    if rate != '1/1':
        raise RuntimeError('Installer frame-rate probe failed: ' + rate)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--ffmpeg', type=Path, required=True)
    ap.add_argument('--ffprobe', type=Path, required=True)
    a = ap.parse_args()
    verify_media(a.ffmpeg, a.ffprobe)
    print('EA VP6 components, synthetic RGB scaling/pipe and frame-rate probe passed.')


if __name__ == '__main__':
    main()
