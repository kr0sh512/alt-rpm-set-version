#include <assert.h>
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
 *   D1<two decimal bpp digits><RFC 4648 base64 of packed hashes>
 *
 * Sorted unique hashes are packed least-significant bit first, using exactly
 * bpp bits per hash. Base64 is only a textual representation of those bytes;
 * there is no delta or Golomb-Rice coding.
 */
#define FORMAT_PREFIX "D1"
#define FORMAT_HEADER_LEN 4

struct set {
  size_t cnt;
  size_t symbols_cap;
  size_t strings_len;
  size_t strings_cap;
  char* strings;
  struct symbols {
    size_t offset;
    unsigned hash;
  }* symbols_v;
};

struct decoded_set {
  unsigned* hashes;
  size_t count;
  unsigned bpp;
};

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
  set->symbols_v[set->cnt].hash = 0;
  memcpy(set->strings + set->strings_len, sym, length);
  set->strings_len = required;
  ++set->cnt;
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
}

static const char base64_alphabet[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static size_t base64_encoded_size(size_t byte_count) {
  if (byte_count > SIZE_MAX - 2) abort();
  size_t groups = (byte_count + 2) / 3;
  if (groups > (SIZE_MAX - FORMAT_HEADER_LEN - 1) / 4) abort();
  return groups * 4;
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
    output[2] = '=';
    output[3] = '=';
  } else if (input_len == 2) {
    uint32_t value = ((uint32_t)input[0] << 16) | ((uint32_t)input[1] << 8);
    output[0] = base64_alphabet[(value >> 18) & 0x3f];
    output[1] = base64_alphabet[(value >> 12) & 0x3f];
    output[2] = base64_alphabet[(value >> 6) & 0x3f];
    output[3] = '=';
  }
}

static unsigned char* pack_hashes(const unsigned* hashes, size_t count, unsigned bpp,
                                  size_t* byte_count) {
  if (count > (SIZE_MAX - 7) / bpp) abort();
  size_t bit_count = count * bpp;
  *byte_count = (bit_count + 7) / 8;
  unsigned char* bytes = xmalloc(*byte_count);
  unsigned char* output = bytes;
  uint64_t bits = 0;
  unsigned filled = 0;

  for (size_t i = 0; i < count; ++i) {
    bits |= (uint64_t)hashes[i] << filled;
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
    set->symbols_v[i].hash = hash(set->strings + set->symbols_v[i].offset) & mask;
  }
  sort_symbols(set->symbols_v, set->cnt, (unsigned)bpp);

  for (size_t i = 0; i + 1 < set->cnt; ++i) {
    if (set->symbols_v[i].hash != set->symbols_v[i + 1].hash) continue;
    const char* left = set->strings + set->symbols_v[i].offset;
    const char* right = set->strings + set->symbols_v[i + 1].offset;
    if (strcmp(left, right) != 0) fprintf(stderr, "warning: hash collision: %s %s\n", left, right);
  }

  unsigned* unique_hashes = xmalloc(set->cnt * sizeof(*unique_hashes));
  size_t unique_count = 0;
  for (size_t i = 0; i < set->cnt; ++i) {
    while (i + 1 < set->cnt && set->symbols_v[i].hash == set->symbols_v[i + 1].hash) ++i;
    unique_hashes[unique_count++] = set->symbols_v[i].hash;
  }

  size_t byte_count;
  unsigned char* bytes = pack_hashes(unique_hashes, unique_count, (unsigned)bpp, &byte_count);
  size_t payload_len = base64_encoded_size(byte_count);
  char* output = xmalloc(FORMAT_HEADER_LEN + payload_len + 1);
  memcpy(output, FORMAT_PREFIX, sizeof(FORMAT_PREFIX) - 1);
  output[2] = (char)('0' + bpp / 10);
  output[3] = (char)('0' + bpp % 10);
  base64_encode(bytes, byte_count, output + FORMAT_HEADER_LEN);
  output[FORMAT_HEADER_LEN + payload_len] = '\0';

  _free(bytes);
  _free(unique_hashes);
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

struct decode_writer {
  unsigned* hashes;
  size_t capacity;
  size_t written;
  uint64_t bits;
  uint64_t mask;
  unsigned filled;
  unsigned bpp;
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
    if (writer->written > 0 && writer->hashes[writer->written - 1] >= current) return -1;
    writer->hashes[writer->written++] = current;
  }

  return 0;
}

static int decode_set(const char* str, struct decoded_set* decoded) {
  if (strncmp(str, "set:", 4) == 0) str += 4;
  size_t str_len = strlen(str);
  if (str_len <= FORMAT_HEADER_LEN) return -1;
  if (strncmp(str, FORMAT_PREFIX, sizeof(FORMAT_PREFIX) - 1) != 0) return -1;
  if (str[2] < '0' || str[2] > '9' || str[3] < '0' || str[3] > '9') return -1;

  unsigned bpp = (unsigned)(str[2] - '0') * 10 + (unsigned)(str[3] - '0');
  if (bpp < 10 || bpp > 32) return -1;

  const char* input = str + FORMAT_HEADER_LEN;
  size_t input_len = str_len - FORMAT_HEADER_LEN;
  if (input_len == 0 || input_len % 4 != 0 || input_len / 4 > SIZE_MAX / 3) return -1;

  size_t byte_count = input_len / 4 * 3;
  if (input[input_len - 1] == '=') --byte_count;
  if (input[input_len - 2] == '=') --byte_count;
  if (byte_count > SIZE_MAX / 8) return -1;

  size_t count = byte_count * 8 / bpp;
  if (count == 0 || count > (SIZE_MAX - 7) / bpp || count > SIZE_MAX / sizeof(unsigned) ||
      (count * bpp + 7) / 8 != byte_count) {
    return -1;
  }

  unsigned* hashes = xmalloc(count * sizeof(*hashes));
  struct decode_writer writer = {
      .hashes = hashes,
      .capacity = count,
      .mask = bpp < 32 ? (UINT64_C(1) << bpp) - 1 : UINT32_MAX,
      .bpp = bpp,
  };

  for (size_t offset = 0; offset < input_len; offset += 4) {
    int v0 = base64_value((unsigned char)input[offset]);
    int v1 = base64_value((unsigned char)input[offset + 1]);
    int last = offset + 4 == input_len;
    if (v0 < 0 || v1 < 0) goto invalid;

    uint32_t bytes = (uint32_t)((v0 << 2) | (v1 >> 4));
    if (input[offset + 2] == '=') {
      if (!last || input[offset + 3] != '=' || (v1 & 0x0f) != 0 ||
          decode_writer_put(&writer, bytes, 1) < 0)
        goto invalid;
      continue;
    }

    int v2 = base64_value((unsigned char)input[offset + 2]);
    if (v2 < 0) goto invalid;
    bytes |= (uint32_t)(((v1 & 0x0f) << 4) | (v2 >> 2)) << 8;
    if (input[offset + 3] == '=') {
      if (!last || (v2 & 0x03) != 0 || decode_writer_put(&writer, bytes, 2) < 0) goto invalid;
      continue;
    }

    int v3 = base64_value((unsigned char)input[offset + 3]);
    if (v3 < 0) goto invalid;
    bytes |= (uint32_t)(((v2 & 0x03) << 6) | v3) << 16;
    if (decode_writer_put(&writer, bytes, 3) < 0) goto invalid;
  }

  if (writer.written != count || writer.bits != 0) goto invalid;

  decoded->hashes = hashes;
  decoded->count = count;
  decoded->bpp = bpp;
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

static void downsample_to(struct decoded_set* set, unsigned target_bpp) {
  if (set->bpp == target_bpp) return;

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
  struct decoded_set set1;
  struct decoded_set set2;
  if (decode_set(str1, &set1) < 0) return -3;
  if (decode_set(str2, &set2) < 0) {
    _free(set1.hashes);
    return -4;
  }

  unsigned target_bpp = set1.bpp < set2.bpp ? set1.bpp : set2.bpp;
  downsample_to(&set1, target_bpp);
  downsample_to(&set2, target_bpp);

  int result;
  if (set1.count == set2.count) {
    result = memcmp(set1.hashes, set2.hashes, set1.count * sizeof(*set1.hashes)) == 0 ? 0 : -2;
  } else if (set1.count > set2.count) {
    result = sorted_subset(set2.hashes, set2.count, set1.hashes, set1.count) ? 1 : -2;
  } else {
    result = sorted_subset(set1.hashes, set1.count, set2.hashes, set2.count) ? -1 : -2;
  }

  _free(set1.hashes);
  _free(set2.hashes);
  return result;
}
