#!/usr/bin/env bash
# Build LGPL media tools from the already pinned FFmpeg submodule. No network.
set -euo pipefail
source_root="$(cd "$1" && pwd)"
mkdir -p "$2"
output_root="$(cd "$2" && pwd)"
mkdir -p "$output_root/obj" "$output_root/bin"
# The SDK tracks configuration headers and codec lists for its runtime build.
# Export a separate source tree without them so configure creates native media
# settings and component lists. Never mutate the SDK checkout.
if [[ -e "$output_root/source" ]]; then
  echo 'Choose a fresh media output directory.' >&2
  exit 1
fi
mkdir "$output_root/source"
git -C "$source_root" archive HEAD | tar -xf - -C "$output_root/source" \
  --exclude=config.h --exclude=config.asm --exclude=config_components.h \
  --exclude=ffbuild/config.mak --exclude=ffbuild/config.sh \
  --exclude=libavutil/avconfig.h --exclude='*_list.c'
media_source="$output_root/source"
cd "$output_root/obj"
"$media_source/configure" --prefix="$output_root" --bindir="$output_root/bin" \
  --cc="${CC:-clang}" --disable-doc --disable-debug --disable-autodetect \
  --disable-shared --enable-static --disable-x86asm --disable-network \
  --disable-everything --enable-ffmpeg --enable-ffprobe \
  --enable-protocol=file,pipe --enable-demuxer=ea,avi,mov,matroska,rawvideo \
  --enable-decoder=vp6,vp6f,vp6a,rawvideo --enable-filter=scale,format \
  --enable-muxer=rawvideo --enable-encoder=rawvideo --enable-swscale --enable-avfilter
make -j2
make install
cp "$source_root/COPYING.LGPLv2.1" "$output_root/bin/LICENSE-FFmpeg.txt"
"$output_root/bin/ffmpeg" -version
"$output_root/bin/ffprobe" -version
