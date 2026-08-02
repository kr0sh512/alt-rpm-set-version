#!/usr/bin/env bash
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
BUILD="$HERE/build"
mkdir -p "$BUILD"
touch "$BUILD/rpmlib.h" "$BUILD/system.h"

if [[ -z ${ROARING_CFLAGS+x} || -z ${ROARING_LIBS+x} ]]; then
  if pkg-config --exists roaring; then
    ROARING_CFLAGS=$(pkg-config --cflags roaring)
    ROARING_LIBS=$(pkg-config --libs roaring)
  else
    CROARING_SRC="$BUILD/CRoaring"
    CROARING_BUILD="$BUILD/CRoaring-build"
    if [[ ! -d $CROARING_SRC/.git ]]; then
      rm -rf "$CROARING_SRC"
      git clone --depth 1 https://github.com/RoaringBitmap/CRoaring.git "$CROARING_SRC"
    fi
    if [[ ! -f $CROARING_BUILD/src/libroaring.a ]]; then
      cmake -S "$CROARING_SRC" -B "$CROARING_BUILD" \
        -DROARING_BUILD_STATIC=ON -DENABLE_ROARING_TESTS=OFF -DCMAKE_BUILD_TYPE=Release
      cmake --build "$CROARING_BUILD" --parallel
    fi
    ROARING_CFLAGS="-I$CROARING_SRC/include"
    ROARING_LIBS="$CROARING_BUILD/src/libroaring.a"
  fi
fi
ZSTD_CFLAGS=${ZSTD_CFLAGS-$(pkg-config --cflags libzstd)}
ZSTD_LIBS=${ZSTD_LIBS-$(pkg-config --libs libzstd)}
read -r -a ROARING_CFLAGS_A <<<"$ROARING_CFLAGS"
read -r -a ROARING_LIBS_A <<<"$ROARING_LIBS"
read -r -a ZSTD_CFLAGS_A <<<"$ZSTD_CFLAGS"
read -r -a ZSTD_LIBS_A <<<"$ZSTD_LIBS"
CFLAGS=(-O2 -std=gnu11 -D_GNU_SOURCE -Wall -Wextra -I"$HERE" -I"$BUILD")

for tool in mkset setcmp; do
  cc "${CFLAGS[@]}" -include "$ROOT/scripts/rpmsetcmp/newset_compat.h" \
    "$ROOT/reimplement/set9.c" "$ROOT/scripts/rpmsetcmp/$tool.c" \
    -o "$BUILD/$tool-set9"
  cc "${CFLAGS[@]}" "${ROARING_CFLAGS_A[@]}" "${ZSTD_CFLAGS_A[@]}" \
    "$HERE/bitmap_set.c" "$ROOT/scripts/rpmsetcmp/$tool.c" \
    "${ROARING_LIBS_A[@]}" "${ZSTD_LIBS_A[@]}" -o "$BUILD/$tool-bitmap"
done

printf 'Built in %s: mkset-{set9,bitmap} setcmp-{set9,bitmap}\n' "$BUILD"
