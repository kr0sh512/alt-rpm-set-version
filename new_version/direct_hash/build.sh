#!/usr/bin/env bash
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
BUILD="$HERE/build"
mkdir -p "$BUILD"
touch "$BUILD/rpmlib.h" "$BUILD/system.h"
cp "$HERE/../roaring_bitmap/set.h" "$BUILD/set.h"

CFLAGS=(-O2 -std=gnu11 -D_GNU_SOURCE -Wall -Wextra -Werror -I"$BUILD")
COMPAT=(-include "$ROOT/scripts/rpmsetcmp/newset_compat.h")

for tool in mkset setcmp; do
  cc "${CFLAGS[@]}" "${COMPAT[@]}" \
    "$ROOT/reimplement/set9.c" "$ROOT/scripts/rpmsetcmp/$tool.c" \
    -o "$BUILD/$tool-set9"
  cc "${CFLAGS[@]}" "${COMPAT[@]}" \
    "$HERE/hash_set.c" "$ROOT/scripts/rpmsetcmp/$tool.c" \
    -o "$BUILD/$tool-direct"
done

cc "${CFLAGS[@]}" -fPIC -shared "${COMPAT[@]}" \
  "$ROOT/reimplement/set9.c" -o "$BUILD/libset9.so"
cc "${CFLAGS[@]}" -fPIC -shared "${COMPAT[@]}" \
  "$HERE/hash_set.c" -o "$BUILD/libdirect-hash.so"

printf 'Built tools and benchmark libraries in %s\n' "$BUILD"
