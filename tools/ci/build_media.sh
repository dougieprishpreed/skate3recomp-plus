#!/usr/bin/env bash
# Build LGPL media tools from the already pinned FFmpeg submodule. No network.
set -euo pipefail
source_root="$(cd "$1" && pwd)"
mkdir -p "$2"
output_root="$(cd "$2" && pwd)"
mkdir -p "$output_root/obj" "$output_root/bin"
cd "$output_root/obj"
"$source_root/configure" --prefix="$output_root" --bindir="$output_root/bin" \
  --cc="${CC:-clang}" --disable-doc --disable-debug --disable-autodetect \
  --disable-shared --enable-static --disable-x86asm --disable-network \
  --disable-everything --enable-ffmpeg --enable-ffprobe \
  --enable-protocol=file,pipe --enable-demuxer=ea,avi,mov,matroska \
  --enable-decoder=vp6,vp6f,vp6a --enable-filter=scale,format \
  --enable-muxer=rawvideo --enable-encoder=rawvideo --enable-swscale --enable-avfilter
make -j2
make install
cp "$source_root/COPYING.LGPLv2.1" "$output_root/bin/LICENSE-FFmpeg.txt"
"$output_root/bin/ffmpeg" -version
"$output_root/bin/ffprobe" -version
