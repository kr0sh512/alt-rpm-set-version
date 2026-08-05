#include <assert.h>
#include <limits.h>
#include <stdatomic.h>
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

_Static_assert(CHAR_BIT == 8, "direct-hash format requires 8-bit bytes");

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

/* A bounded set-string cache owns each key. Values start as a sparse hash map
 * from logical element index to decoded hash and become dense only when an
 * operation necessarily consumes the complete set. */
struct value_slot {
  size_t index_plus_one;
  unsigned value;
};

enum { INDEXED_COMPARISON_CACHE_SIZE = 4 };

struct indexed_set {
  struct indexed_set* bucket_next;
  struct indexed_set* newer;
  struct indexed_set* older;
  char* str;
  struct value_slot* values;
  unsigned* dense_values;
  unsigned* projected_values;
  struct indexed_set* compared_with[INDEXED_COMPARISON_CACHE_SIZE];
  int comparison_results[INDEXED_COMPARISON_CACHE_SIZE];
  size_t str_len;
  size_t input_len;
  size_t byte_count;
  size_t count;
  size_t value_capacity;
  size_t value_count;
  size_t projected_count;
  size_t retained_bytes;
  size_t value_bytes;
  size_t dense_bytes;
  size_t projected_bytes;
  uint32_t fingerprint;
  unsigned bucket;
  unsigned bpp;
  unsigned projected_bpp;
  unsigned cache_id;
  unsigned comparison_next;
  int cache_limited;
  int invalid;
};

enum {
  INDEXED_CACHE_SIZE = 512,
  INDEXED_CACHE_BUCKETS = 1024,
  VALUE_CACHE_INITIAL_CAPACITY = 16,
  RPMSETCMP_FALLBACK = -5,
};

#ifndef INDEXED_CACHE_BYTE_LIMIT
/* Retained key/value storage per argument role; temporary decoder buffers are not cached. */
#define INDEXED_CACHE_BYTE_LIMIT ((size_t)64 * 1024 * 1024)
#endif

static unsigned indexed_cache_count[2];
static size_t indexed_cache_bytes[2];
static struct indexed_set* indexed_cache_buckets[2][INDEXED_CACHE_BUCKETS];
static struct indexed_set* indexed_cache_newest[2];
static struct indexed_set* indexed_cache_oldest[2];
/* Entries may be evicted while a comparison is using them, so rpmsetcmp() holds this lock
 * across the complete lookup/search lifetime. */
static atomic_flag indexed_cache_lock = ATOMIC_FLAG_INIT;

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

  return;
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

static uint32_t indexed_fingerprint(const char* str) {
  const unsigned char* input = (const unsigned char*)str + FORMAT_HEADER_LEN;
  uint32_t fingerprint = (uint32_t)input[0] | ((uint32_t)input[1] << 8) |
                         ((uint32_t)input[2] << 16) | ((uint32_t)input[3] << 24);
  fingerprint ^= (uint32_t)(unsigned char)str[2] << 7;
  fingerprint ^= fingerprint >> 11;
  fingerprint *= UINT32_C(0x9e3779b1);
  fingerprint ^= fingerprint >> 16;
  return fingerprint;
}

static int indexed_meta_init(const char* str, struct indexed_set* set) {
  size_t str_len = strlen(str);
  if (str_len <= FORMAT_HEADER_LEN) return -1;
  if (strncmp(str, FORMAT_PREFIX, sizeof(FORMAT_PREFIX) - 1) != 0) return -1;
  if (str[2] < '0' || str[2] > '9' || str[3] < '0' || str[3] > '9') return -1;

  unsigned bpp = (unsigned)(str[2] - '0') * 10 + (unsigned)(str[3] - '0');
  if (bpp < 10 || bpp > 32) return -1;

  const unsigned char* input = (const unsigned char*)str + FORMAT_HEADER_LEN;
  size_t input_len = str_len - FORMAT_HEADER_LEN;
  if (input_len == 0 || input_len % 4 != 0 || input_len / 4 > SIZE_MAX / 3) return -1;

  /* Sparse same-bpp lookup trusts canonical payloads produced by set_fini(). Validate the final
   * quartet needed to derive the exact element count; indexed probes validate every sextet they
   * touch, while dense and projected paths validate the complete payload during decoding. */
  size_t last = input_len - 4;
  int v0 = base64_value(input[last]);
  int v1 = base64_value(input[last + 1]);
  if (v0 < 0 || v1 < 0) return -1;

  uint32_t final_bytes = (uint32_t)((v0 << 2) | (v1 >> 4));
  unsigned final_byte_count = 1;
  if (input[last + 2] == '=') {
    if (input[last + 3] != '=' || (v1 & 0x0f) != 0) return -1;
  } else {
    int v2 = base64_value(input[last + 2]);
    if (v2 < 0) return -1;
    final_bytes |= (uint32_t)(((v1 & 0x0f) << 4) | (v2 >> 2)) << 8;
    final_byte_count = 2;
    if (input[last + 3] == '=') {
      if ((v2 & 0x03) != 0) return -1;
    } else {
      int v3 = base64_value(input[last + 3]);
      if (v3 < 0) return -1;
      final_bytes |= (uint32_t)(((v2 & 0x03) << 6) | v3) << 16;
      final_byte_count = 3;
    }
  }

  size_t byte_count = input_len / 4 * 3 - (3 - final_byte_count);
  if (byte_count > SIZE_MAX / 8) return -1;
  size_t count = byte_count * 8 / bpp;
  if (count == 0 || count > (SIZE_MAX - 7) / bpp || count > SIZE_MAX / sizeof(unsigned) ||
      (count * bpp + 7) / 8 != byte_count)
    return -1;

  unsigned padding_bits = (unsigned)(byte_count * 8 - count * bpp);
  unsigned final_byte = (unsigned)(final_bytes >> ((final_byte_count - 1) * 8)) & UINT8_MAX;
  if (padding_bits != 0 && final_byte >> (8 - padding_bits) != 0) return -1;

  set->str_len = str_len;
  set->input_len = input_len;
  set->byte_count = byte_count;
  set->count = count;
  set->bpp = bpp;
  return 0;
}

static void indexed_cache_touch(struct indexed_set* set, unsigned cache_id) {
  if (set == indexed_cache_newest[cache_id]) return;

  if (set->newer) set->newer->older = set->older;
  if (set->older) set->older->newer = set->newer;
  if (set == indexed_cache_oldest[cache_id]) indexed_cache_oldest[cache_id] = set->newer;

  set->newer = NULL;
  set->older = indexed_cache_newest[cache_id];
  indexed_cache_newest[cache_id]->newer = set;
  indexed_cache_newest[cache_id] = set;
}

static void indexed_cache_remove(struct indexed_set* victim, unsigned cache_id) {
  if (victim->newer)
    victim->newer->older = victim->older;
  else
    indexed_cache_newest[cache_id] = victim->older;
  if (victim->older)
    victim->older->newer = victim->newer;
  else
    indexed_cache_oldest[cache_id] = victim->newer;

  struct indexed_set** link = &indexed_cache_buckets[cache_id][victim->bucket];
  while (*link && *link != victim) link = &(*link)->bucket_next;
  assert(*link == victim);
  *link = victim->bucket_next;
  assert(indexed_cache_count[cache_id] > 0);
  assert(indexed_cache_bytes[cache_id] >= victim->retained_bytes);
  --indexed_cache_count[cache_id];
  indexed_cache_bytes[cache_id] -= victim->retained_bytes;
  if (cache_id == 1) {
    for (size_t bucket = 0; bucket < INDEXED_CACHE_BUCKETS; ++bucket) {
      for (struct indexed_set* set = indexed_cache_buckets[0][bucket]; set;
           set = set->bucket_next) {
        for (size_t i = 0; i < INDEXED_COMPARISON_CACHE_SIZE; ++i) {
          if (set->compared_with[i] == victim) set->compared_with[i] = NULL;
        }
      }
    }
  }
  _free(victim->values);
  _free(victim->dense_values);
  _free(victim->projected_values);
  _free(victim);
}

static int indexed_cache_make_room(unsigned cache_id, struct indexed_set* protected,
                                   size_t additional_bytes, int adding_entry) {
  if (additional_bytes > INDEXED_CACHE_BYTE_LIMIT) return -1;
  while ((adding_entry && indexed_cache_count[cache_id] >= INDEXED_CACHE_SIZE) ||
         indexed_cache_bytes[cache_id] > INDEXED_CACHE_BYTE_LIMIT - additional_bytes) {
    struct indexed_set* victim = indexed_cache_oldest[cache_id];
    if (victim == protected) victim = victim->newer;
    if (!victim) return -1;
    indexed_cache_remove(victim, cache_id);
  }
  return 0;
}

static void indexed_cache_account_replace(struct indexed_set* set, size_t old_bytes,
                                          size_t new_bytes) {
  if (new_bytes >= old_bytes) {
    size_t added = new_bytes - old_bytes;
    set->retained_bytes += added;
    indexed_cache_bytes[set->cache_id] += added;
    assert(indexed_cache_bytes[set->cache_id] <= INDEXED_CACHE_BYTE_LIMIT);
  } else {
    size_t removed = old_bytes - new_bytes;
    assert(set->retained_bytes >= removed);
    assert(indexed_cache_bytes[set->cache_id] >= removed);
    set->retained_bytes -= removed;
    indexed_cache_bytes[set->cache_id] -= removed;
  }
}

static int indexed_cache_get(const char* source, unsigned cache_id, struct indexed_set** result) {
  assert(cache_id < 2);
  const char* str = strncmp(source, "set:", 4) == 0 ? source + 4 : source;
  if (strncmp(str, "set:", 4) == 0) return -1;
  for (size_t i = 0; i < FORMAT_HEADER_LEN + 4; ++i) {
    if (str[i] == '\0') return -1;
  }

  uint32_t fingerprint = indexed_fingerprint(str);
  unsigned bucket = fingerprint & (INDEXED_CACHE_BUCKETS - 1);
  for (struct indexed_set* set = indexed_cache_buckets[cache_id][bucket]; set;
       set = set->bucket_next) {
    if (set->fingerprint != fingerprint || strcmp(set->str, str) != 0) continue;
    indexed_cache_touch(set, cache_id);
    *result = set;
    return set->invalid ? -1 : 0;
  }

  size_t str_len = strlen(str);
  struct indexed_set meta = {0};
  if (indexed_meta_init(str, &meta) < 0) return -1;
  if (str_len > SIZE_MAX - sizeof(struct indexed_set) - 1) return -1;
  size_t allocation_size = sizeof(struct indexed_set) + str_len + 1;
  if (indexed_cache_make_room(cache_id, NULL, allocation_size, 1) < 0) return 1;

  struct indexed_set* set = xmalloc(allocation_size);
  memset(set, 0, sizeof(*set));
  set->str = (char*)(set + 1);
  memcpy(set->str, str, str_len + 1);
  set->str_len = meta.str_len;
  set->input_len = meta.input_len;
  set->byte_count = meta.byte_count;
  set->count = meta.count;
  set->bpp = meta.bpp;
  set->fingerprint = fingerprint;
  set->bucket = bucket;
  set->cache_id = cache_id;
  set->retained_bytes = allocation_size;

  ++indexed_cache_count[cache_id];
  indexed_cache_bytes[cache_id] += allocation_size;
  assert(indexed_cache_bytes[cache_id] <= INDEXED_CACHE_BYTE_LIMIT);

  set->bucket_next = indexed_cache_buckets[cache_id][bucket];
  indexed_cache_buckets[cache_id][bucket] = set;
  set->older = indexed_cache_newest[cache_id];
  if (indexed_cache_newest[cache_id]) {
    indexed_cache_newest[cache_id]->newer = set;
  } else {
    indexed_cache_oldest[cache_id] = set;
  }
  indexed_cache_newest[cache_id] = set;
  *result = set;
  return 0;
}

static int indexed_comparison_lookup(struct indexed_set* set1, struct indexed_set* set2,
                                     int* result) {
  for (size_t i = 0; i < INDEXED_COMPARISON_CACHE_SIZE; ++i) {
    if (set1->compared_with[i] != set2) continue;
    *result = set1->comparison_results[i];
    return 1;
  }
  return 0;
}

static int indexed_comparison_store(struct indexed_set* set1, struct indexed_set* set2,
                                    int result) {
  unsigned slot = set1->comparison_next++ & (INDEXED_COMPARISON_CACHE_SIZE - 1);
  set1->compared_with[slot] = set2;
  set1->comparison_results[slot] = result;
  return result;
}

static size_t value_slot_position(size_t index, size_t capacity) {
  uint64_t mixed = (uint64_t)index + UINT64_C(0x9e3779b97f4a7c15);
  mixed = (mixed ^ (mixed >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
  mixed = (mixed ^ (mixed >> 27)) * UINT64_C(0x94d049bb133111eb);
  mixed ^= mixed >> 31;
  return (size_t)mixed & (capacity - 1);
}

static int value_cache_lookup(const struct indexed_set* set, size_t index, unsigned* value) {
  if (set->value_capacity == 0) return 0;
  size_t position = value_slot_position(index, set->value_capacity);
  while (set->values[position].index_plus_one != 0) {
    if (set->values[position].index_plus_one == index + 1) {
      *value = set->values[position].value;
      return 1;
    }
    position = (position + 1) & (set->value_capacity - 1);
  }
  return 0;
}

static int value_cache_resize(struct indexed_set* set, size_t capacity) {
  if (capacity == 0 || (capacity & (capacity - 1)) != 0 ||
      capacity > SIZE_MAX / sizeof(*set->values))
    return -1;
  size_t bytes = capacity * sizeof(*set->values);
  if (bytes > set->value_bytes &&
      indexed_cache_make_room(set->cache_id, set, bytes - set->value_bytes, 0) < 0)
    return -1;

  struct value_slot* values = xmalloc(bytes);
  memset(values, 0, bytes);

  for (size_t i = 0; i < set->value_capacity; ++i) {
    if (set->values[i].index_plus_one == 0) continue;
    size_t index = set->values[i].index_plus_one - 1;
    size_t position = value_slot_position(index, capacity);
    while (values[position].index_plus_one != 0) position = (position + 1) & (capacity - 1);
    values[position] = set->values[i];
  }

  _free(set->values);
  set->values = values;
  set->value_capacity = capacity;
  indexed_cache_account_replace(set, set->value_bytes, bytes);
  set->value_bytes = bytes;
  return 0;
}

static int value_cache_grow(struct indexed_set* set) {
  if (set->value_capacity > SIZE_MAX / 2) return -1;
  size_t capacity =
      set->value_capacity == 0 ? VALUE_CACHE_INITIAL_CAPACITY : set->value_capacity * 2;
  return value_cache_resize(set, capacity);
}

static int value_cache_reserve(struct indexed_set* set, size_t expected_values) {
  size_t capacity = set->value_capacity == 0 ? VALUE_CACHE_INITIAL_CAPACITY : set->value_capacity;
  while (capacity - capacity / 4 < expected_values) {
    if (capacity > SIZE_MAX / 2) return -1;
    capacity *= 2;
  }
  return capacity == set->value_capacity ? 0 : value_cache_resize(set, capacity);
}

static int value_cache_insert(struct indexed_set* set, size_t index, unsigned value) {
  if (set->value_count == SIZE_MAX) return -1;
  if (set->value_capacity == 0 ||
      set->value_count + 1 > set->value_capacity - set->value_capacity / 4) {
    if (value_cache_grow(set) < 0) return -1;
  }

  size_t position = value_slot_position(index, set->value_capacity);
  while (set->values[position].index_plus_one != 0)
    position = (position + 1) & (set->value_capacity - 1);
  set->values[position].index_plus_one = index + 1;
  set->values[position].value = value;
  ++set->value_count;
  return 0;
}

static int indexed_byte_at(const struct indexed_set* set, size_t index, unsigned* byte) {
  if (index >= set->byte_count) return -1;
  const unsigned char* input = (const unsigned char*)set->str + FORMAT_HEADER_LEN;
  size_t offset = index / 3 * 4;
  unsigned within = (unsigned)(index % 3);
  int v0 = base64_value(input[offset]);
  int v1 = base64_value(input[offset + 1]);
  if (v0 < 0 || v1 < 0) return -1;
  if (within == 0) {
    *byte = (unsigned)((v0 << 2) | (v1 >> 4));
    return 0;
  }

  int v2 = base64_value(input[offset + 2]);
  if (v2 < 0) return -1;
  if (within == 1) {
    *byte = (unsigned)(((v1 & 0x0f) << 4) | (v2 >> 2));
    return 0;
  }

  int v3 = base64_value(input[offset + 3]);
  if (v3 < 0) return -1;
  *byte = (unsigned)(((v2 & 0x03) << 6) | v3);
  return 0;
}

static int indexed_value(struct indexed_set* set, size_t index, unsigned* value) {
  if (set->invalid || index >= set->count) return -1;
  if (set->dense_values) {
    *value = set->dense_values[index];
    return 0;
  }
  if (value_cache_lookup(set, index, value)) return 0;

  /* Fixed-width packing makes an element independently addressable: at most
   * five decoded bytes cover any 10..32-bit value. */
  size_t bit_offset = index * set->bpp;
  size_t byte_offset = bit_offset / 8;
  unsigned shift = (unsigned)(bit_offset % 8);
  unsigned byte_count = (shift + set->bpp + 7) / 8;
  uint64_t bits = 0;
  for (unsigned i = 0; i < byte_count; ++i) {
    unsigned byte;
    if (indexed_byte_at(set, byte_offset + i, &byte) < 0) {
      set->invalid = 1;
      return -1;
    }
    bits |= (uint64_t)byte << (i * 8);
  }

  uint64_t mask = set->bpp < 32 ? (UINT64_C(1) << set->bpp) - 1 : UINT32_MAX;
  unsigned current = (unsigned)(bits >> shift & mask);
  unsigned neighbor;
  if ((index > 0 && value_cache_lookup(set, index - 1, &neighbor) && neighbor >= current) ||
      (index + 1 < set->count && value_cache_lookup(set, index + 1, &neighbor) &&
       current >= neighbor)) {
    set->invalid = 1;
    return -1;
  }

  if (value_cache_insert(set, index, current) < 0) {
    set->cache_limited = 1;
    return -1;
  }
  *value = current;
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
  for (size_t offset = 0; offset < input_len; offset += 4) {
    int v0 = base64_value((unsigned char)input[offset]);
    int v1 = base64_value((unsigned char)input[offset + 1]);
    int last = offset + 4 == input_len;
    if (v0 < 0 || v1 < 0 || output == output_end) return -1;

    *output++ = (unsigned char)((v0 << 2) | (v1 >> 4));
    if (input[offset + 2] == '=') {
      if (!last || input[offset + 3] != '=' || (v1 & 0x0f) != 0) return -1;
      continue;
    }

    int v2 = base64_value((unsigned char)input[offset + 2]);
    if (v2 < 0 || output == output_end) return -1;
    *output++ = (unsigned char)(((v1 & 0x0f) << 4) | (v2 >> 2));
    if (input[offset + 3] == '=') {
      if (!last || (v2 & 0x03) != 0) return -1;
      continue;
    }

    int v3 = base64_value((unsigned char)input[offset + 3]);
    if (v3 < 0 || output == output_end) return -1;
    *output++ = (unsigned char)(((v2 & 0x03) << 6) | v3);
  }
  return output == output_end ? 0 : -1;
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
#if UINT_MAX == UINT32_MAX
  if (bpp == 32 && sizeof(unsigned) == 4) {
    if (decode_base64_bytes(input, input_len, (unsigned char*)hashes, byte_count) < 0) goto invalid;
#if !defined(__BYTE_ORDER__) || !defined(__ORDER_LITTLE_ENDIAN__) || \
    __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
    for (size_t i = 0; i < count; ++i) {
      const unsigned char* bytes = (const unsigned char*)hashes + i * 4;
      hashes[i] = (unsigned)bytes[0] | ((unsigned)bytes[1] << 8) | ((unsigned)bytes[2] << 16) |
                  ((unsigned)bytes[3] << 24);
    }
#endif
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

static int indexed_decode_all(struct indexed_set* set) {
  if (set->invalid) return -1;
  if (set->dense_values) return 0;

  size_t dense_bytes = set->count * sizeof(*set->dense_values);
  if (dense_bytes > set->value_bytes &&
      indexed_cache_make_room(set->cache_id, set, dense_bytes - set->value_bytes, 0) < 0) {
    set->cache_limited = 1;
    return -1;
  }

  struct decoded_set decoded;
  if (decode_set(set->str, &decoded) < 0) {
    set->invalid = 1;
    return -1;
  }
  if (decoded.count != set->count || decoded.bpp != set->bpp) {
    _free(decoded.hashes);
    set->invalid = 1;
    return -1;
  }

  _free(set->values);
  set->values = NULL;
  set->value_capacity = 0;
  set->value_count = 0;
  indexed_cache_account_replace(set, set->value_bytes, dense_bytes);
  set->value_bytes = 0;
  set->dense_bytes = dense_bytes;
  set->dense_values = decoded.hashes;
  return 0;
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

  return;
}

static int sorted_subset(const unsigned* small, size_t small_count, const unsigned* large,
                         size_t large_count);

/* Preserve comparison semantics when an otherwise valid representation is too large to retain
 * within the cache budget. This path owns only transient decoded arrays. */
static int full_rpmsetcmp(const char* str1, const char* str2) {
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
  if (set1.count == set2.count)
    result = memcmp(set1.hashes, set2.hashes, set1.count * sizeof(*set1.hashes)) == 0 ? 0 : -2;
  else if (set1.count > set2.count)
    result = sorted_subset(set2.hashes, set2.count, set1.hashes, set1.count) ? 1 : -2;
  else
    result = sorted_subset(set1.hashes, set1.count, set2.hashes, set2.count) ? -1 : -2;

  _free(set1.hashes);
  _free(set2.hashes);
  return result;
}

static int indexed_project(struct indexed_set* set, unsigned target_bpp, const unsigned** values,
                           size_t* count) {
  if (indexed_decode_all(set) < 0) return -1;
  if (target_bpp == set->bpp) {
    *values = set->dense_values;
    *count = set->count;
    return 0;
  }
  if (set->projected_values && set->projected_bpp == target_bpp) {
    *values = set->projected_values;
    *count = set->projected_count;
    return 0;
  }

  size_t projected_bytes = set->count * sizeof(*set->projected_values);
  if (projected_bytes > set->projected_bytes &&
      indexed_cache_make_room(set->cache_id, set, projected_bytes - set->projected_bytes, 0) < 0) {
    set->cache_limited = 1;
    return -1;
  }
  _free(set->projected_values);
  set->projected_values = xmalloc(projected_bytes);
  memcpy(set->projected_values, set->dense_values, projected_bytes);
  struct decoded_set projected = {
      .hashes = set->projected_values,
      .count = set->count,
      .bpp = set->bpp,
  };
  downsample_to(&projected, target_bpp);
  indexed_cache_account_replace(set, set->projected_bytes, projected_bytes);
  set->projected_bytes = projected_bytes;
  set->projected_values = projected.hashes;
  set->projected_count = projected.count;
  set->projected_bpp = target_bpp;
  *values = projected.hashes;
  *count = projected.count;
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

static int indexed_step_lower_bound(struct indexed_set* set, size_t first, size_t last,
                                    unsigned value, size_t jump, size_t* result) {
  if (first == last) {
    *result = first;
    return 0;
  }

  unsigned current;
  if (indexed_value(set, first, &current) < 0) return -1;
  if (current >= value) {
    *result = first;
    return 0;
  }
  if (jump == 0) jump = 1;

  size_t position = first;
  size_t step = jump;
  while (step != 0) {
    if (step > last - position - 1) {
      step /= 2;
      continue;
    }

    size_t next = position + step;
    if (indexed_value(set, next, &current) < 0) return -1;
    if (current < value)
      position = next;
    else
      step /= 2;
  }

  *result = position + 1;
  return 0;
}

static int indexed_sorted_subset(struct indexed_set* small, struct indexed_set* large) {
  size_t large_index = 0;
  size_t jump = large->count / small->count;

  /* Every value of the candidate subset is needed for a successful comparison,
   * so decode that side with the faster streaming decoder and retain it. */
  if (indexed_decode_all(small) < 0) return -1;

  if (jump < 4) {
    if (indexed_decode_all(large) < 0) return -1;
    return sorted_subset(small->dense_values, small->count, large->dense_values, large->count);
  }

  size_t probes_per_value = 2;
  for (size_t span = jump; span > 1; span = (span + 1) / 2) ++probes_per_value;
  size_t expected_values =
      small->count > SIZE_MAX / probes_per_value ? large->count : small->count * probes_per_value;
  if (expected_values > large->count) expected_values = large->count;
  if (!large->dense_values && value_cache_reserve(large, expected_values) < 0) {
    large->cache_limited = 1;
    return -1;
  }

  for (size_t small_index = 0; small_index < small->count; ++small_index) {
    unsigned small_value;
    if (indexed_value(small, small_index, &small_value) < 0) return -1;
    if (indexed_step_lower_bound(large, large_index, large->count, small_value, jump,
                                 &large_index) < 0)
      return -1;
    if (large_index == large->count) return 0;

    unsigned large_value;
    if (indexed_value(large, large_index, &large_value) < 0) return -1;
    if (large_value != small_value) return 0;
    ++large_index;
  }

  return 1;
}

static int projected_rpmsetcmp(struct indexed_set* set1, struct indexed_set* set2) {
  unsigned target_bpp = set1->bpp < set2->bpp ? set1->bpp : set2->bpp;
  const unsigned* values1;
  const unsigned* values2;
  size_t count1;
  size_t count2;
  if (indexed_project(set1, target_bpp, &values1, &count1) < 0)
    return set1->cache_limited ? RPMSETCMP_FALLBACK : -3;
  if (indexed_project(set2, target_bpp, &values2, &count2) < 0)
    return set2->cache_limited ? RPMSETCMP_FALLBACK : -4;

  if (count1 == count2) return memcmp(values1, values2, count1 * sizeof(*values1)) == 0 ? 0 : -2;
  if (count1 > count2) return sorted_subset(values2, count2, values1, count1) ? 1 : -2;
  return sorted_subset(values1, count1, values2, count2) ? -1 : -2;
}

static int rpmsetcmp_locked(const char* str1, const char* str2) {
  struct indexed_set* set1;
  struct indexed_set* set2;
  int status = indexed_cache_get(str1, 0, &set1);
  if (status < 0) return -3;
  if (status > 0) return full_rpmsetcmp(str1, str2);
  if (set1->cache_limited) return full_rpmsetcmp(str1, str2);

  status = indexed_cache_get(str2, 1, &set2);
  if (status < 0) return -4;
  if (status > 0) return full_rpmsetcmp(str1, str2);
  if (set2->cache_limited) return full_rpmsetcmp(str1, str2);

  int cached_result;
  if (indexed_comparison_lookup(set1, set2, &cached_result)) return cached_result;

  if (set1->bpp != set2->bpp) {
    int result = projected_rpmsetcmp(set1, set2);
    if (result == RPMSETCMP_FALLBACK) return full_rpmsetcmp(str1, str2);
    return result >= -2 ? indexed_comparison_store(set1, set2, result) : result;
  }
  if (strcmp(set1->str, set2->str) == 0) {
    if (indexed_decode_all(set1) == 0) return indexed_comparison_store(set1, set2, 0);
    return set1->cache_limited ? full_rpmsetcmp(str1, str2) : -3;
  }
  if (set1->count == set2->count) {
    if (indexed_decode_all(set1) < 0) return set1->cache_limited ? full_rpmsetcmp(str1, str2) : -3;
    if (indexed_decode_all(set2) < 0) return set2->cache_limited ? full_rpmsetcmp(str1, str2) : -4;
    int result = memcmp(set1->dense_values, set2->dense_values,
                        set1->count * sizeof(*set1->dense_values)) == 0
                     ? 0
                     : -2;
    return indexed_comparison_store(set1, set2, result);
  }

  int subset;
  if (set1->count > set2->count) {
    subset = indexed_sorted_subset(set2, set1);
    if (subset > 0) return indexed_comparison_store(set1, set2, 1);
  } else {
    subset = indexed_sorted_subset(set1, set2);
    if (subset > 0) return indexed_comparison_store(set1, set2, -1);
  }

  if (subset < 0) {
    if (set1->cache_limited || set2->cache_limited) return full_rpmsetcmp(str1, str2);
    if (set1->invalid) return -3;
    if (set2->invalid) return -4;
  }
  return indexed_comparison_store(set1, set2, -2);
}

int rpmsetcmp(const char* str1, const char* str2) {
  while (atomic_flag_test_and_set_explicit(&indexed_cache_lock, memory_order_acquire)) {
  }
  int result = rpmsetcmp_locked(str1, str2);
  atomic_flag_clear_explicit(&indexed_cache_lock, memory_order_release);
  return result;
}
