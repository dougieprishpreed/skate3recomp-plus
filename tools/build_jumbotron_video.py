#!/usr/bin/env python3
"""Convert a Skate 2 movie into the New San Van jumbotron clip.

Skate 2's Stadium screens (material incandescent.videoscreen, effect
videoscreen.fx: the baseincandescent body over a diffuse the game streamed
video into) ship with only a "JUMBO TRON TEMP" placeholder diffuse. The
native renderer (src/skate3_jumbotron.cpp, UpdateJumbotron) loops a clip on
them from <root>/mods/skate2/jumbotron/<name>.s2jv, the clip named by the
skate3_skate2_jumbotron_clip setting (default "attract").

ffmpeg decodes the EA VP6 movie (video only) and scales it to the clip size;
each frame is stored as BC1 blocks with a gamma-correct mip chain, so the
runtime copies frames straight into the screen texture with no decoding.
Leading and trailing black frames are trimmed so the loop has no dark gap.

File layout (little-endian): 64-byte header
  'S2JV', version 1, width, height, mip count, frame count, fps numerator,
  fps denominator, bytes per frame, format 1 (BC1), 6 reserved words
then the frames; each frame is its mips in order, rows of 8-byte BC1 blocks.

Usage (repository root):
  python tools/build_jumbotron_video.py [--movie Attract] [--name attract]
      [--movies "<Skate 2>/data/movies"] [--size 320x160]
      [--root out/build/relwithdebinfo] [--ffmpeg C:/ffmpeg/ffmpeg.exe]
"""
from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
MAGIC = b'S2JV'
VERSION = 1
FORMAT_BC1 = 1
BLACK_MEAN = 6.0          # mean 8-bit level below which a frame counts as black


def find_tool(explicit, name):
    if explicit:
        return str(explicit)
    found = shutil.which(name)
    if found:
        return found
    fallback = Path('C:/ffmpeg') / f'{name}.exe'
    if fallback.exists():
        return str(fallback)
    raise SystemExit(f'{name} not found (pass --ffmpeg)')


def probe_rate(ffprobe, movie):
    out = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'v:0',
                          '-show_entries', 'stream=r_frame_rate', '-of', 'csv=p=0',
                          str(movie)], capture_output=True, text=True).stdout.strip()
    try:
        num, den = (int(x) for x in out.split('/'))
        if num > 0 and den > 0:
            return num, den
    except ValueError:
        pass
    return 30000, 1001


# ---- BC1 -----------------------------------------------------------------------
_W0 = np.array([1.0, 0.0, 2.0 / 3.0, 1.0 / 3.0], np.float32)
_W1 = 1.0 - _W0


def _quantize(c):
    """float RGB (n,3) 0..255 -> (565 value, expanded 8-bit color)."""
    r = np.clip(np.rint(c[:, 0] * (31.0 / 255.0)), 0, 31).astype(np.uint32)
    g = np.clip(np.rint(c[:, 1] * (63.0 / 255.0)), 0, 63).astype(np.uint32)
    b = np.clip(np.rint(c[:, 2] * (31.0 / 255.0)), 0, 31).astype(np.uint32)
    value = (r << 11) | (g << 5) | b
    expanded = np.stack([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)],
                        axis=1).astype(np.float32)
    return value, expanded


def _indices(blocks, e0, e1):
    palette = _W0[None, :, None] * e0[:, None, :] + _W1[None, :, None] * e1[:, None, :]
    dist = ((blocks[:, :, None, :] - palette[:, None, :, :]) ** 2).sum(-1)   # (n,16,4)
    return dist.argmin(-1)


def encode_bc1(img):
    """img: (H, W, 3) float32 0..255, H and W multiples of 4 -> BC1 bytes."""
    h, w, _ = img.shape
    blocks = img.reshape(h // 4, 4, w // 4, 4, 3).transpose(0, 2, 1, 3, 4).reshape(-1, 16, 3)
    n = blocks.shape[0]
    mean = blocks.mean(1)
    centered = blocks - mean[:, None, :]
    cov = np.einsum('npi,npj->nij', centered, centered)
    axis = np.tile(np.array([0.577, 0.577, 0.577], np.float32), (n, 1))
    for _ in range(8):
        axis = np.einsum('nij,nj->ni', cov, axis)
        norm = np.linalg.norm(axis, axis=1, keepdims=True)
        axis = np.where(norm > 1e-6, axis / np.maximum(norm, 1e-6), 0.577)
    proj = np.einsum('npi,ni->np', centered, axis)
    lo, hi = proj.min(1), proj.max(1)
    inset = (hi - lo) / 16.0
    e0 = mean + axis * (hi - inset)[:, None]
    e1 = mean + axis * (lo + inset)[:, None]
    for _ in range(2):
        v0, q0 = _quantize(e0)
        v1, q1 = _quantize(e1)
        idx = _indices(blocks, q0, q1)
        # Least-squares endpoints for the chosen indices.
        a, b = _W0[idx], _W1[idx]                       # (n,16)
        aa, ab, bb = (a * a).sum(1), (a * b).sum(1), (b * b).sum(1)
        ax = (a[:, :, None] * blocks).sum(1)
        bx = (b[:, :, None] * blocks).sum(1)
        det = aa * bb - ab * ab
        ok = np.abs(det) > 1e-3
        safe = np.where(ok, det, 1.0)[:, None]
        e0 = np.where(ok[:, None], (bb[:, None] * ax - ab[:, None] * bx) / safe, e0)
        e1 = np.where(ok[:, None], (aa[:, None] * bx - ab[:, None] * ax) / safe, e1)
        e0, e1 = np.clip(e0, 0, 255), np.clip(e1, 0, 255)
    v0, q0 = _quantize(e0)
    v1, q1 = _quantize(e1)
    idx = _indices(blocks, q0, q1).astype(np.uint32)
    # 4-color mode needs color0 > color1: swap endpoints (and indices 0<->1,
    # 2<->3) where they came out reversed; equal endpoints use index 0 only.
    swap = v0 < v1
    v0, v1 = np.where(swap, v1, v0), np.where(swap, v0, v1)
    idx = np.where(swap[:, None], idx ^ 1, idx)
    idx = np.where((v0 == v1)[:, None], 0, idx)
    bits = (idx << (2 * np.arange(16, dtype=np.uint32))[None, :]).sum(1, dtype=np.uint64)
    out = np.zeros((n, 2), np.uint32)
    out[:, 0] = v0 | (v1 << 16)
    out[:, 1] = bits.astype(np.uint32)
    return out.astype('<u4').tobytes()


def mip_chain(frame):
    """Gamma-2.0-correct 2x2 box mips while both dimensions stay multiples of 4."""
    levels = [frame]
    lin = (frame / 255.0) ** 2
    while True:
        h, w = lin.shape[:2]
        if h % 8 or w % 8:
            break
        lin = lin.reshape(h // 2, 2, w // 2, 2, 3).mean(axis=(1, 3))
        levels.append(np.sqrt(lin) * 255.0)
    return levels


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--movie', default='Attract',
                    help='Skate 2 movie name (data/movies/<movie>_english_ntsc.vp6) or a path')
    ap.add_argument('--name', help='clip name (default: the movie name, lowercase)')
    ap.add_argument('--movies', default=str(REPO.parent / 'Skate 2' / 'data' / 'movies'))
    ap.add_argument('--size', default='320x160', help='WxH, multiples of 4')
    ap.add_argument('--root', default=str(REPO / 'out' / 'build' / 'relwithdebinfo'))
    ap.add_argument('--ffmpeg')
    args = ap.parse_args()

    movie = Path(args.movie)
    if not movie.exists():
        movie = Path(args.movies) / f'{args.movie}_english_ntsc.vp6'
    if not movie.exists():
        raise SystemExit(f'movie not found: {movie}')
    name = (args.name or movie.stem.replace('_english_ntsc', '')).lower()
    width, height = (int(v) for v in args.size.lower().split('x'))
    if width % 4 or height % 4 or width < 4 or height < 4:
        raise SystemExit('--size needs multiples of 4')
    ffmpeg = find_tool(args.ffmpeg, 'ffmpeg')
    ffprobe = str(Path(ffmpeg).with_name(Path(ffmpeg).name.replace('ffmpeg', 'ffprobe')))
    fps_num, fps_den = probe_rate(ffprobe if Path(ffprobe).exists() else find_tool(None, 'ffprobe'),
                                  movie)

    out_dir = Path(args.root) / 'mods' / 'skate2' / 'jumbotron'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f'{name}.s2jv'
    tmp_path = out_path.with_suffix('.tmp')

    proc = subprocess.Popen([ffmpeg, '-v', 'error', '-i', str(movie), '-an', '-sn',
                             '-vf', f'scale={width}:{height}:flags=area',
                             '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
                            stdout=subprocess.PIPE)
    frame_size = width * height * 3
    mips = len(mip_chain(np.zeros((height, width, 3), np.float32)))
    frame_bytes = sum((width >> m) // 4 * ((height >> m) // 4) * 8 for m in range(mips))
    written = decoded = 0
    pending = []          # black frames that are only kept if footage follows
    started = time.time()
    with open(tmp_path, 'wb') as f:
        f.write(bytes(64))
        while True:
            raw = proc.stdout.read(frame_size)
            if len(raw) < frame_size:
                break
            decoded += 1
            frame = np.frombuffer(raw, np.uint8).reshape(height, width, 3).astype(np.float32)
            data = b''.join(encode_bc1(level) for level in mip_chain(frame))
            assert len(data) == frame_bytes
            if frame.mean() < BLACK_MEAN:
                if written:
                    pending.append(data)
                continue
            for held in pending:
                f.write(held)
            written += len(pending) + 1
            pending.clear()
            f.write(data)
            if decoded % 300 == 0:
                print(f'  {decoded} frames ({time.time() - started:.0f} s)', flush=True)
        proc.stdout.close()
        if proc.wait() != 0 or written == 0:
            tmp_path.unlink(missing_ok=True)
            raise SystemExit(f'ffmpeg failed or produced no footage ({decoded} frames)')
        f.seek(0)
        f.write(struct.pack('<4s9I6I', MAGIC, VERSION, width, height, mips, written,
                            fps_num, fps_den, frame_bytes, FORMAT_BC1, *([0] * 6)))
    tmp_path.replace(out_path)
    print(f'wrote {out_path} ({out_path.stat().st_size / 1e6:.1f} MB): {written} of {decoded} '
          f'frames, {width}x{height} x{mips} mips BC1, {fps_num / fps_den:.3f} fps, '
          f'{written * fps_den / fps_num:.1f} s loop')


if __name__ == '__main__':
    main()
