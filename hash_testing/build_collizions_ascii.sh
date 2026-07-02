#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

mkdir -p build

cat > build/config.h <<'EOF'
#define HAVE_STDINT_H 1
#define HAVE_STRING_H 1
#define HAVE_STDLIB_H 1
EOF

cc -O3 -std=c99 -Wall -Wextra -Werror -I. \
  -c src/joaat/hash.c -o build/joaat.o

cc -O3 -std=c99 -Wall -Wextra -Werror -I. \
  -c src/xxHash/xxhash.c -o build/xxhash.o

g++ -O3 -std=c++17 -Wall -Wextra -Werror \
  -I. -Ibuild -Isrc/cityhash/src \
  collizions_ascii.cpp src/cityhash/src/city.cc build/joaat.o build/xxhash.o \
  -o build/collizions_ascii_test

# Args are passed through to collizions_ascii_test:
#   ./build_collizions_ascii.sh [runs=5] [samples=50000]
./build/collizions_ascii_test "$@"
