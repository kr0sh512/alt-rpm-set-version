#include <assert.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

#include "rpmlib.h"
#include "set.h"
#include "system.h"

/*
 * This is intentionally a new set-string format. It is not compatible with
 * the Golomb-Rice/base62 strings produced by the original lib/set.c.
 *
 *   D1<two decimal bpp digits><unpadded RFC 4648 base64 of packed hashes>
 *
 * Sorted unique hashes are packed least-significant bit first, using exactly
 * bpp bits per hash. Base64 is only a textual representation of those bytes;
 * there is no delta or Golomb-Rice coding.
 */
#define FORMAT_PREFIX "D1"
#define FORMAT_HEADER_LEN 4

_Static_assert(CHAR_BIT == 8, "direct-hash format requires 8-bit bytes");

struct set {
  size_t cnt;
  size_t symbols_cap;
  size_t strings_len;
  size_t strings_cap;
  char* strings;
  struct symbols {
    size_t offset;
    unsigned full_hash;
    unsigned hash;
  }* symbols_v;
};

struct decoded_set {
  unsigned* hashes;
  size_t count;
  unsigned bpp;
};

enum {
  DECODED_CACHE_SIZE = 512,
  DECODED_CACHE_BUCKETS = 1024,
};

struct decoded_cache_entry {
  struct decoded_cache_entry* bucket_next;
  struct decoded_cache_entry* newer;
  struct decoded_cache_entry* older;
  char* str;
  unsigned* hashes;
  size_t len;
  size_t count;
  uint32_t fingerprint;
  unsigned bucket;
  unsigned target_bpp;
};

struct set_meta {
  const char* str;
  size_t len;
  unsigned bpp;
};

static unsigned decoded_cache_count[2];
static struct decoded_cache_entry* decoded_cache_buckets[2][DECODED_CACHE_BUCKETS];
static struct decoded_cache_entry* decoded_cache_newest[2];
static struct decoded_cache_entry* decoded_cache_oldest[2];

static unsigned hash(const char* str);

struct set* set_new(void) {
  struct set* set = xmalloc(sizeof(*set));
  set->cnt = 0;
  set->symbols_cap = 0;
  set->strings_len = 0;
  set->strings_cap = 0;
  set->strings = NULL;
  set->symbols_v = NULL;

  return set;
}

void set_add(struct set* set, const char* sym) {
  if (set->cnt == set->symbols_cap) {
    set->symbols_cap += 1024;
    set->symbols_v = xrealloc(set->symbols_v, sizeof(*set->symbols_v) * set->symbols_cap);
  }

  size_t length = strlen(sym) + 1;
  size_t required = set->strings_len + length;
  if (required > set->strings_cap) {
    size_t capacity = set->strings_cap ? set->strings_cap : 4096;
    while (capacity < required) capacity *= 2;
    set->strings = xrealloc(set->strings, capacity);
    set->strings_cap = capacity;
  }

  set->symbols_v[set->cnt].offset = set->strings_len;
  set->symbols_v[set->cnt].full_hash = hash(sym);
  set->symbols_v[set->cnt].hash = set->symbols_v[set->cnt].full_hash;
  memcpy(set->strings + set->strings_len, sym, length);
  set->strings_len = required;
  ++set->cnt;

  return;
}

struct set* set_free(struct set* set) {
  if (set) {
    _free(set->strings);
    _free(set->symbols_v);
    set = _free(set);
  }

  return NULL;
}

static unsigned hash(const char* str) {
  unsigned hash = UINT32_C(0x9e3779b9);
  const unsigned char* p = (const unsigned char*)str;

  while (*p) {
    hash += *p++;
    hash += hash << 10;
    hash ^= hash >> 6;
  }

  hash += hash << 3;
  hash ^= hash >> 11;
  hash += hash << 15;

  return hash;
}

static int compare_symbols(const void* arg1, const void* arg2) {
  const struct symbols* s1 = arg1;
  const struct symbols* s2 = arg2;

  if (s1->hash > s2->hash) return 1;
  if (s1->hash < s2->hash) return -1;

  return 0;
}

static void sort_symbols(struct symbols* values, size_t count, unsigned bpp) {
  if (count < 128) {
    qsort(values, count, sizeof(*values), compare_symbols);

    return;
  }

  struct symbols* temporary = xmalloc(count * sizeof(*temporary));
  struct symbols* source = values;
  struct symbols* destination = temporary;
  unsigned passes = (bpp + 7) / 8;

  for (unsigned pass = 0; pass < passes; ++pass) {
    size_t offsets[256] = {0};
    unsigned shift = pass * 8;
    for (size_t i = 0; i < count; ++i) ++offsets[(source[i].hash >> shift) & 0xffu];

    size_t position = 0;
    for (size_t i = 0; i < 256; ++i) {
      size_t bucket_count = offsets[i];
      offsets[i] = position;
      position += bucket_count;
    }

    for (size_t i = 0; i < count; ++i) {
      unsigned bucket = (source[i].hash >> shift) & 0xffu;
      destination[offsets[bucket]++] = source[i];
    }

    struct symbols* swap = source;
    source = destination;
    destination = swap;
  }

  if (source != values) memcpy(values, source, count * sizeof(*values));
  _free(temporary);

  return;
}

static const char base64_alphabet[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static size_t base64_encoded_size(size_t byte_count) {
  if (byte_count > SIZE_MAX - 2) abort();
  size_t groups = (byte_count + 2) / 3;
  if (groups > (SIZE_MAX - FORMAT_HEADER_LEN - 1) / 4) abort();

  size_t size = groups * 4;
  size_t remainder = byte_count % 3;
  if (remainder != 0) size -= 3 - remainder;
  return size;
}

static void base64_encode(const unsigned char* input, size_t input_len, char* output) {
  while (input_len >= 3) {
    uint32_t value = ((uint32_t)input[0] << 16) | ((uint32_t)input[1] << 8) | input[2];
    output[0] = base64_alphabet[(value >> 18) & 0x3f];
    output[1] = base64_alphabet[(value >> 12) & 0x3f];
    output[2] = base64_alphabet[(value >> 6) & 0x3f];
    output[3] = base64_alphabet[value & 0x3f];
    input += 3;
    input_len -= 3;
    output += 4;
  }

  if (input_len == 1) {
    uint32_t value = (uint32_t)input[0] << 16;
    output[0] = base64_alphabet[(value >> 18) & 0x3f];
    output[1] = base64_alphabet[(value >> 12) & 0x3f];
  } else if (input_len == 2) {
    uint32_t value = ((uint32_t)input[0] << 16) | ((uint32_t)input[1] << 8);
    output[0] = base64_alphabet[(value >> 18) & 0x3f];
    output[1] = base64_alphabet[(value >> 12) & 0x3f];
    output[2] = base64_alphabet[(value >> 6) & 0x3f];
  }

  return;
}

static size_t compact_unique_hashes(struct symbols* symbols, size_t count) {
  size_t unique_count = 0;
  for (size_t i = 0; i < count; ++i) {
    while (i + 1 < count && symbols[i].hash == symbols[i + 1].hash) ++i;
    symbols[unique_count++].hash = symbols[i].hash;
  }

  return unique_count;
}

static unsigned char* pack_symbol_hashes(const struct symbols* symbols, size_t count,
                                         unsigned bpp, size_t* byte_count) {
  if (count > (SIZE_MAX - 7) / bpp) abort();
  size_t bit_count = count * bpp;
  *byte_count = (bit_count + 7) / 8;
  unsigned char* bytes = xmalloc(*byte_count);
  unsigned char* output = bytes;

#if UINT_MAX == UINT32_MAX && defined(__BYTE_ORDER__) && defined(__ORDER_LITTLE_ENDIAN__) && \
    __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
  if (bpp == 32 && sizeof(unsigned) == 4) {
    for (size_t i = 0; i < count; ++i) {
      unsigned hash = symbols[i].hash;
      memcpy(output, &hash, sizeof(hash));
      output += sizeof(hash);
    }
    assert((size_t)(output - bytes) == *byte_count);
    return bytes;
  }
#endif

  uint64_t bits = 0;
  unsigned filled = 0;

  for (size_t i = 0; i < count; ++i) {
    bits |= (uint64_t)symbols[i].hash << filled;
    filled += bpp;

    while (filled >= 8) {
      *output++ = (unsigned char)bits;
      bits >>= 8;
      filled -= 8;
    }
  }

  if (filled) *output++ = (unsigned char)bits;
  assert((size_t)(output - bytes) == *byte_count);

  return bytes;
}

const char* set_fini(struct set* set, int bpp) {
  assert(set != NULL);
  assert(set->cnt > 0);
  assert(bpp >= 10 && bpp <= 32);

  unsigned mask = bpp < 32 ? (UINT32_C(1) << bpp) - 1 : UINT32_MAX;
  for (size_t i = 0; i < set->cnt; ++i) {
    set->symbols_v[i].hash = set->symbols_v[i].full_hash & mask;
  }
  sort_symbols(set->symbols_v, set->cnt, (unsigned)bpp);

  for (size_t i = 0; i + 1 < set->cnt; ++i) {
    if (set->symbols_v[i].hash != set->symbols_v[i + 1].hash) continue;
    const char* left = set->strings + set->symbols_v[i].offset;
    const char* right = set->strings + set->symbols_v[i + 1].offset;
    if (strcmp(left, right) != 0) fprintf(stderr, "warning: hash collision: %s %s\n", left, right);
  }

  size_t byte_count;
  size_t unique_count = compact_unique_hashes(set->symbols_v, set->cnt);
  unsigned char* bytes = pack_symbol_hashes(set->symbols_v, unique_count, (unsigned)bpp, &byte_count);
  size_t payload_len = base64_encoded_size(byte_count);
  char* output = xmalloc(FORMAT_HEADER_LEN + payload_len + 1);
  memcpy(output, FORMAT_PREFIX, sizeof(FORMAT_PREFIX) - 1);
  output[2] = (char)('0' + bpp / 10);
  output[3] = (char)('0' + bpp % 10);
  base64_encode(bytes, byte_count, output + FORMAT_HEADER_LEN);
  output[FORMAT_HEADER_LEN + payload_len] = '\0';

  _free(bytes);

  return output;
}

static const unsigned char base64_values[256] = {
    ['A'] = 1,  ['B'] = 2,  ['C'] = 3,  ['D'] = 4,  ['E'] = 5,  ['F'] = 6,  ['G'] = 7,  ['H'] = 8,
    ['I'] = 9,  ['J'] = 10, ['K'] = 11, ['L'] = 12, ['M'] = 13, ['N'] = 14, ['O'] = 15, ['P'] = 16,
    ['Q'] = 17, ['R'] = 18, ['S'] = 19, ['T'] = 20, ['U'] = 21, ['V'] = 22, ['W'] = 23, ['X'] = 24,
    ['Y'] = 25, ['Z'] = 26, ['a'] = 27, ['b'] = 28, ['c'] = 29, ['d'] = 30, ['e'] = 31, ['f'] = 32,
    ['g'] = 33, ['h'] = 34, ['i'] = 35, ['j'] = 36, ['k'] = 37, ['l'] = 38, ['m'] = 39, ['n'] = 40,
    ['o'] = 41, ['p'] = 42, ['q'] = 43, ['r'] = 44, ['s'] = 45, ['t'] = 46, ['u'] = 47, ['v'] = 48,
    ['w'] = 49, ['x'] = 50, ['y'] = 51, ['z'] = 52, ['0'] = 53, ['1'] = 54, ['2'] = 55, ['3'] = 56,
    ['4'] = 57, ['5'] = 58, ['6'] = 59, ['7'] = 60, ['8'] = 61, ['9'] = 62, ['+'] = 63, ['/'] = 64,
};

static inline int base64_value(unsigned char c) { return (int)base64_values[c] - 1; }

static int base64_decoded_size(size_t input_len, size_t* byte_count) {
  size_t remainder = input_len % 4;
  if (input_len == 0 || remainder == 1) return -1;

  size_t groups = input_len / 4;
  if (groups > SIZE_MAX / 3) return -1;
  size_t size = groups * 3;
  size_t tail_size = remainder == 0 ? 0 : remainder - 1;
  if (size > SIZE_MAX - tail_size) return -1;

  *byte_count = size + tail_size;
  return 0;
}

static inline int has_set_prefix(const char* str) {
  return str[0] == 's' && str[1] == 'e' && str[2] == 't' && str[3] == ':';
}

static int set_meta_init(const char* source, struct set_meta* meta) {
  const char* str = source;
  if (has_set_prefix(str)) str += 4;
  if (has_set_prefix(str)) return -1;

  /* With bpp >= 10, a valid direct set has at least three Base64 characters. */
  if (str[0] != FORMAT_PREFIX[0] || str[1] != FORMAT_PREFIX[1]) return -1;
  if (str[2] < '0' || str[2] > '9' || str[3] < '0' || str[3] > '9') return -1;
  if (str[4] == '\0' || str[5] == '\0' || str[6] == '\0') return -1;

  unsigned bpp = (unsigned)(str[2] - '0') * 10 + (unsigned)(str[3] - '0');
  if (bpp < 10 || bpp > 32) return -1;

  meta->str = str;
  meta->len = strlen(str);
  meta->bpp = bpp;
  return 0;
}

struct decode_writer {
  unsigned* hashes;
  size_t capacity;
  size_t written;
  uint64_t bits;
  uint64_t mask;
  unsigned previous;
  unsigned filled;
  unsigned bpp;
  int has_previous;
};

static inline int decode_writer_put(struct decode_writer* writer, uint32_t bytes,
                                    unsigned byte_count) {
  writer->bits |= (uint64_t)bytes << writer->filled;
  writer->filled += byte_count * 8;

  while (writer->filled >= writer->bpp) {
    if (writer->written == writer->capacity) return -1;

    unsigned current = (unsigned)(writer->bits & writer->mask);
    writer->bits >>= writer->bpp;
    writer->filled -= writer->bpp;

    if (writer->has_previous && writer->previous >= current) return -1;
    if (writer->hashes) writer->hashes[writer->written] = current;
    writer->previous = current;
    writer->has_previous = 1;
    ++writer->written;
  }

  return 0;
}

static int decode_base64_bytes(const char* input, size_t input_len, unsigned char* output,
                               size_t output_len) {
  unsigned char* const output_end = output + output_len;
  size_t offset = 0;
  while (input_len - offset >= 4) {
    int v0 = base64_value((unsigned char)input[offset]);
    int v1 = base64_value((unsigned char)input[offset + 1]);
    int v2 = base64_value((unsigned char)input[offset + 2]);
    int v3 = base64_value((unsigned char)input[offset + 3]);
    if ((v0 | v1 | v2 | v3) < 0 || output_end - output < 3) return -1;
    output[0] = (unsigned char)((v0 << 2) | (v1 >> 4));
    output[1] = (unsigned char)(((v1 & 0x0f) << 4) | (v2 >> 2));
    output[2] = (unsigned char)(((v2 & 0x03) << 6) | v3);
    output += 3;
    offset += 4;
  }

  size_t remainder = input_len - offset;
  if (remainder == 0) return output == output_end ? 0 : -1;
  if (remainder == 1) return -1;

  int v0 = base64_value((unsigned char)input[offset]);
  int v1 = base64_value((unsigned char)input[offset + 1]);
  if ((v0 | v1) < 0 || output == output_end) return -1;
  *output++ = (unsigned char)((v0 << 2) | (v1 >> 4));

  if (remainder == 2) return (v1 & 0x0f) == 0 && output == output_end ? 0 : -1;

  int v2 = base64_value((unsigned char)input[offset + 2]);
  if (v2 < 0 || output == output_end) return -1;
  *output++ = (unsigned char)(((v1 & 0x0f) << 4) | (v2 >> 2));
  return (v2 & 0x03) == 0 && output == output_end ? 0 : -1;
}

static inline int decode_base64_triplet(const char* input, uint32_t* triplet) {
  int v0 = base64_value((unsigned char)input[0]);
  int v1 = base64_value((unsigned char)input[1]);
  int v2 = base64_value((unsigned char)input[2]);
  int v3 = base64_value((unsigned char)input[3]);
  if ((v0 | v1 | v2 | v3) < 0) return -1;

  *triplet = (uint32_t)((v0 << 2) | (v1 >> 4)) |
             (uint32_t)(((v1 & 0x0f) << 4) | (v2 >> 2)) << 8 |
             (uint32_t)(((v2 & 0x03) << 6) | v3) << 16;
  return 0;
}

static int decode_base64_u32(const char* input, size_t input_len, unsigned* hashes,
                             size_t count) {
  size_t blocks = count / 3;
  size_t written = 0;
  unsigned previous = 0;
  int has_previous = 0;

  for (size_t block = 0; block < blocks; ++block) {
    uint32_t t0, t1, t2, t3;
    if (decode_base64_triplet(input, &t0) < 0 ||
        decode_base64_triplet(input + 4, &t1) < 0 ||
        decode_base64_triplet(input + 8, &t2) < 0 ||
        decode_base64_triplet(input + 12, &t3) < 0)
      return -1;

    unsigned value0 = (unsigned)(t0 | ((t1 & UINT32_C(0xff)) << 24));
    unsigned value1 = (unsigned)((t1 >> 8) | ((t2 & UINT32_C(0xffff)) << 16));
    unsigned value2 = (unsigned)((t2 >> 16) | (t3 << 8));
    if ((has_previous && previous >= value0) || value0 >= value1 || value1 >= value2) return -1;

    hashes[written++] = value0;
    hashes[written++] = value1;
    hashes[written++] = value2;
    previous = value2;
    has_previous = 1;
    input += 16;
    input_len -= 16;
  }

  size_t remaining = count - written;
  if (remaining != 0) {
    size_t byte_count = remaining * sizeof(*hashes);
    unsigned char tail[2 * sizeof(*hashes)];
    if (decode_base64_bytes(input, input_len, tail, byte_count) < 0) return -1;
    for (size_t i = 0; i < remaining; ++i, ++written) {
      const unsigned char* bytes = tail + i * 4;
      unsigned current = (unsigned)bytes[0] | ((unsigned)bytes[1] << 8) |
                         ((unsigned)bytes[2] << 16) | ((unsigned)bytes[3] << 24);
      if (has_previous && previous >= current) return -1;
      hashes[written] = current;
      previous = current;
      has_previous = 1;
    }
  } else if (input_len != 0) {
    return -1;
  }

  return 0;
}

static int decode_set_sized(const char* str, size_t str_len, struct decoded_set* decoded) {
  if (str_len == 0) str_len = strlen(str);
  if (str_len <= FORMAT_HEADER_LEN) return -1;
  if (strncmp(str, FORMAT_PREFIX, sizeof(FORMAT_PREFIX) - 1) != 0) return -1;
  if (str[2] < '0' || str[2] > '9' || str[3] < '0' || str[3] > '9') return -1;

  unsigned bpp = (unsigned)(str[2] - '0') * 10 + (unsigned)(str[3] - '0');
  if (bpp < 10 || bpp > 32) return -1;

  const char* input = str + FORMAT_HEADER_LEN;
  size_t input_len = str_len - FORMAT_HEADER_LEN;
  size_t byte_count;
  if (base64_decoded_size(input_len, &byte_count) < 0) return -1;
  if (byte_count > SIZE_MAX / 8) return -1;

  size_t count = byte_count * 8 / bpp;
  if (count == 0 || count > (SIZE_MAX - 7) / bpp || count > SIZE_MAX / sizeof(unsigned) ||
      (count * bpp + 7) / 8 != byte_count) {
    return -1;
  }

  unsigned* hashes = xmalloc(count * sizeof(*hashes));
#if UINT_MAX == UINT32_MAX
  if (bpp == 32 && sizeof(unsigned) == 4) {
    if (decode_base64_u32(input, input_len, hashes, count) < 0) goto invalid;
    if (decoded) {
      decoded->hashes = hashes;
      decoded->count = count;
      decoded->bpp = bpp;
    }
    return 0;
  }

  /* Byte-aligned widths can bypass the generic bit reservoir. Decode the
   * Base64 payload into the front of the final allocation, then expand 16-
   * and 24-bit values backwards so unread packed bytes are never overwritten. */
  if ((bpp == 16 || bpp == 24) && sizeof(unsigned) == 4) {
    if (decode_base64_bytes(input, input_len, (unsigned char*)hashes, byte_count) < 0) goto invalid;
    if (bpp == 16) {
      for (size_t i = count; i > 0; --i) {
        const unsigned char* bytes = (const unsigned char*)hashes + (i - 1) * 2;
        hashes[i - 1] = (unsigned)bytes[0] | ((unsigned)bytes[1] << 8);
      }
    } else if (bpp == 24) {
      for (size_t i = count; i > 0; --i) {
        const unsigned char* bytes = (const unsigned char*)hashes + (i - 1) * 3;
        hashes[i - 1] = (unsigned)bytes[0] | ((unsigned)bytes[1] << 8) |
                        ((unsigned)bytes[2] << 16);
      }
    }
    for (size_t i = 1; i < count; ++i) {
      if (hashes[i - 1] >= hashes[i]) goto invalid;
    }
    if (decoded) {
      decoded->hashes = hashes;
      decoded->count = count;
      decoded->bpp = bpp;
    }
    return 0;
  }
#endif

  struct decode_writer writer = {
      .hashes = hashes,
      .capacity = count,
      .mask = bpp < 32 ? (UINT64_C(1) << bpp) - 1 : UINT32_MAX,
      .bpp = bpp,
  };

  size_t full_len = input_len - input_len % 4;
  for (size_t offset = 0; offset < full_len; offset += 4) {
    int v0 = base64_value((unsigned char)input[offset]);
    int v1 = base64_value((unsigned char)input[offset + 1]);
    int v2 = base64_value((unsigned char)input[offset + 2]);
    int v3 = base64_value((unsigned char)input[offset + 3]);
    if ((v0 | v1 | v2 | v3) < 0) goto invalid;

    uint32_t bytes = (uint32_t)((v0 << 2) | (v1 >> 4)) |
                     (uint32_t)(((v1 & 0x0f) << 4) | (v2 >> 2)) << 8 |
                     (uint32_t)(((v2 & 0x03) << 6) | v3) << 16;
    if (decode_writer_put(&writer, bytes, 3) < 0) goto invalid;
  }

  size_t remainder = input_len - full_len;
  if (remainder != 0) {
    int v0 = base64_value((unsigned char)input[full_len]);
    int v1 = base64_value((unsigned char)input[full_len + 1]);
    if ((v0 | v1) < 0) goto invalid;

    uint32_t bytes = (uint32_t)((v0 << 2) | (v1 >> 4));
    if (remainder == 2) {
      if ((v1 & 0x0f) != 0 || decode_writer_put(&writer, bytes, 1) < 0) goto invalid;
    } else {
      int v2 = base64_value((unsigned char)input[full_len + 2]);
      if (v2 < 0 || (v2 & 0x03) != 0) goto invalid;
      bytes |= (uint32_t)(((v1 & 0x0f) << 4) | (v2 >> 2)) << 8;
      if (decode_writer_put(&writer, bytes, 2) < 0) goto invalid;
    }
  }

  if (writer.written != count || writer.bits != 0) goto invalid;

  if (decoded) {
    decoded->hashes = hashes;
    decoded->count = count;
    decoded->bpp = bpp;
  }

  return 0;

invalid:
  _free(hashes);

  return -1;
}

/* Reduce a sorted set of (bpp + 1)-bit values to a sorted set of bpp-bit values. */
static size_t downsample_set(size_t count, const unsigned* hashes, unsigned* result, unsigned bpp) {
  unsigned mask = (UINT32_C(1) << bpp) - 1;
  size_t lower = 0;
  size_t upper = count;

  while (lower < upper) {
    size_t middle = lower + (upper - lower) / 2;
    if (hashes[middle] <= mask)
      lower = middle + 1;
    else
      upper = middle;
  }

  unsigned* output = result;
  const unsigned* low = hashes;
  const unsigned* low_end = hashes + lower;
  const unsigned* high = hashes + lower;
  const unsigned* high_end = hashes + count;

  while (low < low_end && high < high_end) {
    unsigned low_value = *low;
    unsigned high_value = *high & mask;
    if (low_value < high_value) {
      *output++ = low_value;
      ++low;
    } else if (high_value < low_value) {
      *output++ = high_value;
      ++high;
    } else {
      *output++ = low_value;
      ++low;
      ++high;
    }
  }
  while (low < low_end) *output++ = *low++;
  while (high < high_end) *output++ = *high++ & mask;

  return (size_t)(output - result);
}

static void downsample_radix_to(struct decoded_set* set, unsigned target_bpp) {
  unsigned* original = set->hashes;
  unsigned* scratch = xmalloc(set->count * sizeof(*scratch));
  unsigned mask = (UINT32_C(1) << target_bpp) - 1;
  for (size_t i = 0; i < set->count; ++i) original[i] &= mask;

  unsigned* source = original;
  unsigned* destination = scratch;
  unsigned passes = (target_bpp + 7) / 8;
  for (unsigned pass = 0; pass < passes; ++pass) {
    size_t offsets[256] = {0};
    unsigned shift = pass * 8;
    for (size_t i = 0; i < set->count; ++i) ++offsets[(source[i] >> shift) & 0xffu];

    size_t position = 0;
    for (size_t i = 0; i < 256; ++i) {
      size_t bucket_count = offsets[i];
      offsets[i] = position;
      position += bucket_count;
    }
    for (size_t i = 0; i < set->count; ++i) {
      unsigned value = source[i];
      destination[offsets[(value >> shift) & 0xffu]++] = value;
    }

    unsigned* swap = source;
    source = destination;
    destination = swap;
  }

  size_t unique_count = 1;
  for (size_t i = 1; i < set->count; ++i) {
    if (source[i] != source[unique_count - 1]) source[unique_count++] = source[i];
  }

  if (source == original) {
    _free(scratch);
  } else {
    _free(original);
    set->hashes = scratch;
  }
  set->count = unique_count;
  set->bpp = target_bpp;
}

static void downsample_to(struct decoded_set* set, unsigned target_bpp) {
  if (set->bpp == target_bpp) return;

  unsigned passes = (target_bpp + 7) / 8;
  if (set->bpp - target_bpp > passes) {
    downsample_radix_to(set, target_bpp);
    return;
  }

  unsigned* original = set->hashes;
  unsigned* scratch = xmalloc(set->count * sizeof(*scratch));
  unsigned* source = original;
  unsigned* destination = scratch;

  while (set->bpp > target_bpp) {
    --set->bpp;
    set->count = downsample_set(set->count, source, destination, set->bpp);
    unsigned* swap = source;
    source = destination;
    destination = swap;
  }

  if (source == original) {
    _free(scratch);
  } else {
    _free(original);
    set->hashes = scratch;
  }

  return;
}

static uint32_t decoded_cache_fingerprint(const struct set_meta* meta, unsigned target_bpp) {
  const unsigned char* str = (const unsigned char*)meta->str;
  uint32_t fingerprint = UINT32_C(2166136261);
  fingerprint = (fingerprint ^ (uint32_t)meta->len) * UINT32_C(16777619);
  fingerprint = (fingerprint ^ (meta->bpp * UINT32_C(0x27d4eb2d))) * UINT32_C(16777619);
  fingerprint = (fingerprint ^ (target_bpp * UINT32_C(0x85ebca6b))) * UINT32_C(16777619);

  size_t prefix_len = meta->len < 8 ? meta->len : 8;
  for (size_t i = 0; i < prefix_len; ++i) {
    fingerprint = (fingerprint ^ str[i]) * UINT32_C(16777619);
  }
  size_t suffix_start = meta->len > 8 ? meta->len - 8 : prefix_len;
  for (size_t i = suffix_start; i < meta->len; ++i) {
    fingerprint = (fingerprint ^ str[i]) * UINT32_C(16777619);
  }

  fingerprint ^= fingerprint >> 16;
  fingerprint *= UINT32_C(0x7feb352d);
  fingerprint ^= fingerprint >> 15;
  fingerprint *= UINT32_C(0x846ca68b);
  fingerprint ^= fingerprint >> 16;
  return fingerprint;
}

static void decoded_cache_touch(struct decoded_cache_entry* entry, unsigned cache_id) {
  if (entry == decoded_cache_newest[cache_id]) return;

  if (entry->newer) entry->newer->older = entry->older;
  if (entry->older) entry->older->newer = entry->newer;
  if (entry == decoded_cache_oldest[cache_id]) decoded_cache_oldest[cache_id] = entry->newer;

  entry->newer = NULL;
  entry->older = decoded_cache_newest[cache_id];
  decoded_cache_newest[cache_id]->newer = entry;
  decoded_cache_newest[cache_id] = entry;
}

static void decoded_cache_remove(struct decoded_cache_entry* victim, unsigned cache_id) {
  if (victim->newer)
    victim->newer->older = victim->older;
  else
    decoded_cache_newest[cache_id] = victim->older;
  if (victim->older)
    victim->older->newer = victim->newer;
  else
    decoded_cache_oldest[cache_id] = victim->newer;

  struct decoded_cache_entry** link = &decoded_cache_buckets[cache_id][victim->bucket];
  while (*link && *link != victim) link = &(*link)->bucket_next;
  assert(*link == victim);
  *link = victim->bucket_next;
  assert(decoded_cache_count[cache_id] > 0);
  --decoded_cache_count[cache_id];

  _free(victim->hashes);
  _free(victim);
}

static int cache_decode_set(const struct set_meta* meta, unsigned target_bpp, unsigned cache_id,
                            const unsigned** hashes, size_t* count) {
  assert(cache_id < 2);
  assert(target_bpp <= meta->bpp);

  uint32_t fingerprint = decoded_cache_fingerprint(meta, target_bpp);
  unsigned bucket = fingerprint & (DECODED_CACHE_BUCKETS - 1);
  for (struct decoded_cache_entry* entry = decoded_cache_buckets[cache_id][bucket]; entry;
       entry = entry->bucket_next) {
    if (entry->fingerprint != fingerprint || entry->target_bpp != target_bpp ||
        entry->len != meta->len || memcmp(entry->str, meta->str, meta->len + 1) != 0)
      continue;

    decoded_cache_touch(entry, cache_id);
    *hashes = entry->hashes;
    *count = entry->count;
    return 0;
  }

  struct decoded_set decoded;
  if (decode_set_sized(meta->str, meta->len, &decoded) < 0) return -1;
  if (decoded.bpp != meta->bpp) {
    _free(decoded.hashes);
    return -1;
  }
  downsample_to(&decoded, target_bpp);

  if (meta->len > SIZE_MAX - sizeof(struct decoded_cache_entry) - 1) {
    _free(decoded.hashes);
    return -1;
  }
  struct decoded_cache_entry* entry = xmalloc(sizeof(*entry) + meta->len + 1);
  memset(entry, 0, sizeof(*entry));
  entry->str = (char*)(entry + 1);
  memcpy(entry->str, meta->str, meta->len + 1);
  entry->hashes = decoded.hashes;
  entry->len = meta->len;
  entry->count = decoded.count;
  entry->fingerprint = fingerprint;
  entry->bucket = bucket;
  entry->target_bpp = target_bpp;

  if (decoded_cache_count[cache_id] == DECODED_CACHE_SIZE) {
    decoded_cache_remove(decoded_cache_oldest[cache_id], cache_id);
  }

  entry->bucket_next = decoded_cache_buckets[cache_id][bucket];
  decoded_cache_buckets[cache_id][bucket] = entry;
  entry->older = decoded_cache_newest[cache_id];
  if (decoded_cache_newest[cache_id]) {
    decoded_cache_newest[cache_id]->newer = entry;
  } else {
    decoded_cache_oldest[cache_id] = entry;
  }
  decoded_cache_newest[cache_id] = entry;
  ++decoded_cache_count[cache_id];

  *hashes = entry->hashes;
  *count = entry->count;
  return 0;
}

static const unsigned* step_lower_bound(const unsigned* first, const unsigned* last, unsigned value,
                                        size_t jump) {
  size_t count = (size_t)(last - first);
  if (count == 0 || first[0] >= value) return first;
  if (jump == 0) jump = 1;

  size_t position = 0;
  size_t step = jump;
  while (step != 0) {
    if (step > count - position - 1) {
      step /= 2;
      continue;
    }
    size_t next = position + step;
    if (first[next] < value)
      position = next;
    else
      step /= 2;
  }

  return first + position + 1;
}

static int sorted_subset(const unsigned* small, size_t small_count, const unsigned* large,
                         size_t large_count) {
  const unsigned* small_end = small + small_count;
  const unsigned* large_end = large + large_count;
  size_t jump = large_count / small_count;

  if (jump < 4) {
    while (small < small_end) {
      unsigned value = *small++;
      while (large < large_end && *large < value) ++large;
      if (large == large_end || *large != value) return 0;
      ++large;
    }

    return 1;
  }

  while (small < small_end) {
    unsigned value = *small++;
    large = step_lower_bound(large, large_end, value, jump);
    if (large == large_end || *large != value) return 0;
    ++large;
  }

  return 1;
}

int rpmsetcmp(const char* str1, const char* str2) {
  struct set_meta meta1;
  if (set_meta_init(str1, &meta1) < 0) return -3;

  struct set_meta meta2;
  if (set_meta_init(str2, &meta2) < 0) return -4;
  unsigned target_bpp = meta2.bpp < meta1.bpp ? meta2.bpp : meta1.bpp;

  const unsigned* hashes1;
  size_t count1;
  if (cache_decode_set(&meta1, target_bpp, 0, &hashes1, &count1) < 0) return -3;

  if (meta1.len == meta2.len && memcmp(meta1.str, meta2.str, meta1.len + 1) == 0) return 0;

  const unsigned* hashes2;
  size_t count2;
  if (cache_decode_set(&meta2, target_bpp, 1, &hashes2, &count2) < 0) return -4;

  if (count1 == count2)
    return memcmp(hashes1, hashes2, count1 * sizeof(*hashes1)) == 0 ? 0 : -2;
  else if (count1 > count2)
    return sorted_subset(hashes2, count2, hashes1, count1) ? 1 : -2;
  else
    return sorted_subset(hashes1, count1, hashes2, count2) ? -1 : -2;
}
