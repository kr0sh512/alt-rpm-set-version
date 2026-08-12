#ifdef SELF_TEST
#undef NDEBUG
#endif
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

#include "rpmlib.h"
#ifdef SELF_TEST
#include <stdio.h>
#endif
#include "set.h"
#include "system.h"

enum {
  CACHE_SIZE = 512,
  CACHE_BUCKETS = 1024,
};

enum {
  SET_HEADER_SIZE = 2,
  SET_PARAM_CHAR_OFFSET = 7,
  SET_BPP_MIN = 10,
  SET_BPP_MAX = 32,
  SET_MSHIFT_MIN = 7,
  SET_MSHIFT_MAX = 31,
};

enum {
  BASE62_ESCAPE_BITS = 4,
  BASE62_VALUE_BITS = 6,
  BASE62_ESCAPE_CHUNK_BITS = BASE62_ESCAPE_BITS + BASE62_VALUE_BITS,
  BASE62_MIN_BITS_PER_CHAR = BASE62_ESCAPE_CHUNK_BITS / 2,
  BASE62_MAX_PADDING_BITS = BASE62_VALUE_BITS - 1,
  BASE62_LOWERCASE_OFFSET = 10,
  BASE62_UPPERCASE_OFFSET = 36,
  BASE62_ESCAPE_VALUE = 61,
  BASE62_VALUE_COUNT = 62,
  BASE62_ESCAPE_LOW_MASK = (1u << BASE62_ESCAPE_BITS) - 1,
  BASE62_ESCAPE_HIGH_MASK = 3u << BASE62_ESCAPE_BITS,
};

enum {
  BASE62_INVALID = 0xee,
  BASE62_END = UCHAR_MAX,
};

_Static_assert(CHAR_BIT == 8, "set:version relies on 8-bit");

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

struct set* set_new(void) {
  struct set* set = xmalloc(sizeof *set);
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
    size_t capacity = set->strings_cap ?: 4096;
    while (capacity < required) capacity *= 2;

    set->strings = xrealloc(set->strings, capacity);
    set->strings_cap = capacity;
  }

  set->symbols_v[set->cnt].offset = set->strings_len;
  set->symbols_v[set->cnt].hash = 0;
  memcpy(set->strings + set->strings_len, sym, length);
  set->strings_len = required;
  set->cnt++;
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
    hash += (hash << 10);
    hash ^= (hash >> 6);
  }

  hash += (hash << 3);
  hash ^= (hash >> 11);
  hash += (hash << 15);

  return hash;
}

int cmp(const void* arg1, const void* arg2) {
  const struct symbols* s1 = arg1;
  const struct symbols* s2 = arg2;

  if (s1->hash > s2->hash) return 1;
  if (s2->hash > s1->hash) return -1;

  return 0;
}

static void sort_symbols(struct symbols* values, size_t count, int bpp) {
  if (count < 128) {
    qsort(values, count, sizeof(*values), cmp);
    return;
  }

  struct symbols temporary[count];
  struct symbols* source = values;
  struct symbols* destination = temporary;
  unsigned passes = ((unsigned)bpp + 7) / 8;

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
}

static int log2i(int n) {
  int m = 0;
  while (n /= 2) m++;

  return m;
}

/* Calculate Mshift paramter for encoding. */
static int encode_golomb_Mshift(int cnt, int bpp) {
  /*
   * XXX Slightly better Mshift estimations are probably possible.
   * Recheck "Compression and coding algorithms" by Moffat & Turpin.
   */
  int Mshift = bpp - log2i(cnt) - 1;

  /* Adjust out-of-range values. */
  Mshift = (Mshift < SET_MSHIFT_MIN) ? SET_MSHIFT_MIN : Mshift;
  Mshift = (Mshift > SET_MSHIFT_MAX) ? SET_MSHIFT_MAX : Mshift;
  assert(Mshift < bpp);

  return Mshift;
}

/* Estimate how many bits can be filled up. */
static inline int encode_golomb_size(int cnt, int Mshift) {
  /*
   * XXX No precise estimation.  However, we do not expect unary-encoded bits
   * to take more than binary-encoded Mshift bits.
   */
  return 2 * Mshift * cnt + 16;
}

/* Estimate base62 buffer size required to encode a given number of bits. */
static inline int encode_base62_size(int bit_cnt) {
  /*
   * In the worst case, which is ZxZxZx..., five bits can make a character;
   * the remaining bits can make a character, too.  And the string must be
   * null-terminated.
   */
  return bit_cnt / BASE62_MIN_BITS_PER_CHAR + 2;
}

static int encode_set_size(int cnt, int bpp) {
  int Mshift = encode_golomb_Mshift(cnt, bpp);
  int bit_cnt = encode_golomb_size(cnt, Mshift);
  /* The leading characters encode bpp and Mshift. */
  return SET_HEADER_SIZE + encode_base62_size(bit_cnt);
}

/* Main base62 encoding routine: pack bit_arr into base62 string. */
/*
 * Base62 routines - encode bits with alnum characters.
 *
 * This is a base64-based base62 implementation.  Values 0..61 are encoded
 * with '0'..'9', 'a'..'z', and 'A'..'Z'.  However, 'Z' is special: it will
 * also encode 62 and 63.  To achieve this, 'Z' will occupy two high bits in
 * the next character.  Thus 'Z' can be interpreted as an escape character
 * (which indicates that the next character must be handled specially).
 * Note that setting high bits to "00", "01" or "10" cannot contribute
 * to another 'Z' (which would require high bits set to "11").  This is
 * how multiple escapes are avoided.
 */

static inline char encode_bpp(int bpp) { return (char)(bpp - SET_PARAM_CHAR_OFFSET + 'a'); }

struct encode_writer {
  uint64_t bits;
  unsigned filled;
  unsigned escaped;
  unsigned pending_high;
  char* output;
};

static inline void encode_writer_digit(struct encode_writer* writer, unsigned value) {
  assert(value < BASE62_VALUE_COUNT);

  if (value < BASE62_LOWERCASE_OFFSET) {
    *writer->output++ = (char)('0' + value);
  } else if (value < BASE62_UPPERCASE_OFFSET) {
    *writer->output++ = (char)('a' + value - BASE62_LOWERCASE_OFFSET);
  } else {
    *writer->output++ = (char)('A' + value - BASE62_UPPERCASE_OFFSET);
  }
}

static inline void encode_writer_flush(struct encode_writer* writer) {
  for (;;) {
    unsigned width = writer->escaped ? BASE62_ESCAPE_BITS : BASE62_VALUE_BITS;
    if (writer->filled < width) return;

    unsigned value = (unsigned)writer->bits & ((1u << width) - 1);
    writer->bits >>= width;
    writer->filled -= width;

    if (writer->escaped) {
      encode_writer_digit(writer, writer->pending_high | value);
      writer->escaped = 0;
    } else if (value >= BASE62_ESCAPE_VALUE) {
      encode_writer_digit(writer, BASE62_ESCAPE_VALUE);
      writer->pending_high = (value - BASE62_ESCAPE_VALUE) << BASE62_ESCAPE_BITS;
      writer->escaped = 1;
    } else {
      encode_writer_digit(writer, value);
    }
  }
}

static inline void encode_writer_zeros(struct encode_writer* writer, unsigned count) {
  /*
   * encode_writer_flush() leaves fewer than BASE62_VALUE_BITS bits buffered.
   * Adding at most 56 bits therefore cannot overflow uint64_t.
   */
  while (count) {
    unsigned take = count > 56 ? 56 : count;
    writer->filled += take;
    count -= take;

    encode_writer_flush(writer);
  }
}

static inline void encode_writer_put(struct encode_writer* writer, uint64_t value, unsigned width) {
  writer->bits |= value << writer->filled;
  writer->filled += width;
  encode_writer_flush(writer);
}

static int encode_set(int cnt, const unsigned* hash_arr, int bpp, char* base62_str) {
  const unsigned Mshift = (unsigned)encode_golomb_Mshift(cnt, bpp);
  const unsigned mask = (1u << Mshift) - 1;
  char* const start = base62_str;
  unsigned previous = 0;

  *base62_str++ = encode_bpp(bpp);
  *base62_str++ = encode_bpp((int)Mshift);
  struct encode_writer writer = {.output = base62_str};

  for (int i = 0; i < cnt; ++i) {
    unsigned current = hash_arr[i];
    unsigned delta = current - previous;
    previous = current;

    encode_writer_zeros(&writer, delta >> Mshift);
    encode_writer_put(&writer, 1, 1);
    encode_writer_put(&writer, delta & mask, Mshift);
  }

  encode_writer_flush(&writer);
  if (writer.filled || writer.escaped) {
    unsigned value = (unsigned)writer.bits;
    if (writer.escaped) value |= writer.pending_high;
    encode_writer_digit(&writer, value);
  }

  *writer.output = '\0';

  return (int)(writer.output - start);
}

const char* set_fini(struct set* set, int bpp) {
  /* Implementation for finalizing the set */

  assert(set != NULL);
  assert(set->cnt > 0);
  assert(bpp >= SET_BPP_MIN && bpp <= SET_BPP_MAX);

  unsigned mask = (bpp < SET_BPP_MAX) ? (1u << bpp) - 1 : ~0u;

  for (size_t i = 0; i < set->cnt; ++i) {
    set->symbols_v[i].hash = hash(set->strings + set->symbols_v[i].offset) & mask;
  }

  sort_symbols(set->symbols_v, set->cnt, bpp);

  /* warn on hash collizions */
  for (size_t i = 0; i < set->cnt - 1; ++i) {
    if (set->symbols_v[i].hash != set->symbols_v[i + 1].hash) continue;
    const char* left = set->strings + set->symbols_v[i].offset;
    const char* right = set->strings + set->symbols_v[i + 1].offset;
    if (!strcmp(left, right)) continue;

    fprintf(stderr, "warning: set-version hash collision: %s %s\n", left, right);
  }

  unsigned unique_hash[set->cnt];
  int unique_cnt = 0;

  /* delete duplicates */
  for (size_t i = 0; i < set->cnt; ++i) {
    while (i + 1 < set->cnt && set->symbols_v[i].hash == set->symbols_v[i + 1].hash) {
      ++i;
    }
    unique_hash[unique_cnt++] = set->symbols_v[i].hash;
  }

  char base62_str[encode_set_size(unique_cnt, bpp)];
  encode_set(unique_cnt, unique_hash, bpp, base62_str);

  return xstrdup(base62_str);
}

struct set_meta {
  const char* str;
  const char* payload;
  size_t len;
  size_t payload_len;
  int bpp;
  int Mshift;
  int bit_capacity;
  int value_capacity;
};

static int set_meta_init(const char* str, struct set_meta* meta) {
  /* The header must be followed by at least one payload character. */
  if (!str[0] || !str[1] || !str[SET_HEADER_SIZE]) return -EPIPE;

  int bpp = str[0] + SET_PARAM_CHAR_OFFSET - 'a';
  if (bpp < SET_BPP_MIN || bpp > SET_BPP_MAX) return -ERANGE;

  int Mshift = str[1] + SET_PARAM_CHAR_OFFSET - 'a';
  if (Mshift < SET_MSHIFT_MIN || Mshift > SET_MSHIFT_MAX) return -ERANGE;
  if (Mshift >= bpp) return -EINVAL;

  *meta = (struct set_meta){
    .str = str,
    .payload = str + SET_HEADER_SIZE,
    .bpp = bpp,
    .Mshift = Mshift,
  };

  return 0;
}

static int set_meta_fini(struct set_meta* meta) {
  size_t len = strlen(meta->str);

  size_t payload_len = len - SET_HEADER_SIZE;

  int bit_capacity = (int)payload_len * BASE62_VALUE_BITS;
  int value_capacity = bit_capacity / (meta->Mshift + 1);

  if (value_capacity < 1) return -EINVAL;

  meta->len = len;
  meta->payload_len = payload_len;
  meta->bit_capacity = bit_capacity;
  meta->value_capacity = value_capacity;

  return 0;
}
/* clang-format off */
__extension__ static const unsigned char char_to_num[UCHAR_MAX + 1] = {
  [0] = BASE62_END,  /* end of string */
  [1 ... ('0' - 1)] = BASE62_INVALID,

  ['0'] = 0, ['1'] = 1, ['2'] = 2, ['3'] = 3, ['4'] = 4,
  ['5'] = 5, ['6'] = 6, ['7'] = 7, ['8'] = 8, ['9'] = 9,

  [('9' + 1) ... ('A' - 1)] = BASE62_INVALID,

  ['A'] = 36, ['B'] = 37, ['C'] = 38, ['D'] = 39, ['E'] = 40,
  ['F'] = 41, ['G'] = 42, ['H'] = 43, ['I'] = 44, ['J'] = 45,
  ['K'] = 46, ['L'] = 47, ['M'] = 48, ['N'] = 49, ['O'] = 50,
  ['P'] = 51, ['Q'] = 52, ['R'] = 53, ['S'] = 54, ['T'] = 55,
  ['U'] = 56, ['V'] = 57, ['W'] = 58, ['X'] = 59, ['Y'] = 60,
  ['Z'] = BASE62_ESCAPE_VALUE,

  [('Z' + 1) ... ('a' - 1)] = BASE62_INVALID,

  ['a'] = 10, ['b'] = 11, ['c'] = 12, ['d'] = 13, ['e'] = 14,
  ['f'] = 15, ['g'] = 16, ['h'] = 17, ['i'] = 18, ['j'] = 19,
  ['k'] = 20, ['l'] = 21, ['m'] = 22, ['n'] = 23, ['o'] = 24,
  ['p'] = 25, ['q'] = 26, ['r'] = 27, ['s'] = 28, ['t'] = 29,
  ['u'] = 30, ['v'] = 31, ['w'] = 32, ['x'] = 33, ['y'] = 34,
  ['z'] = 35,

  [('z' + 1) ... UCHAR_MAX] = BASE62_INVALID,
};
/* clang-format on */

/*
 * Decode base62 and Golomb-Rice in one pass. Base62 is LSB-first; a Z escape contributes
 * BASE62_ESCAPE_CHUNK_BITS stream bits.
 */
static inline int decode_chunk(const unsigned char** input, uint64_t* chunk, unsigned* width) {
  unsigned value = char_to_num[*(*input)++];

  if (value < BASE62_ESCAPE_VALUE) {
    *chunk = value;
    *width = BASE62_VALUE_BITS;
    return 1;
  }
  if (value == BASE62_END) return 0;
  if (value == BASE62_INVALID) return -EINVAL;

  unsigned escaped = char_to_num[*(*input)++];
  if (escaped == BASE62_END) return -EPIPE;
  if (escaped == BASE62_INVALID) return -EINVAL;

  unsigned high = escaped & BASE62_ESCAPE_HIGH_MASK;
  if (high == BASE62_ESCAPE_HIGH_MASK) return -EILSEQ;

  *chunk = (BASE62_ESCAPE_VALUE + (high >> BASE62_ESCAPE_BITS)) |
           ((uint64_t)(escaped & BASE62_ESCAPE_LOW_MASK) << BASE62_VALUE_BITS);
  *width = BASE62_ESCAPE_CHUNK_BITS;

  return 1;
}

static int decode_set(const struct set_meta* meta, unsigned* hash_arr) {
  const unsigned char* input = (const unsigned char*)meta->payload;
  const unsigned Mshift = (unsigned)meta->Mshift;
  const uint64_t mask = (UINT64_C(1) << Mshift) - 1;
  uint64_t bits = 0;
  unsigned filled = 0;
  unsigned q = 0;
  unsigned previous = 0;
  int count = 0;

  for (;;) {
    /* Unary quotient: zero bits terminated by one. */
    for (;;) {
      if (filled == 0) {
        uint64_t chunk;
        unsigned width;
        int rc = decode_chunk(&input, &chunk, &width);

        if (rc < 0) return rc;
        if (rc == 0) return q <= BASE62_MAX_PADDING_BITS ? count : -EINVAL;

        bits = chunk;
        filled = width;
      }

      if (bits == 0) {
        q += filled;
        filled = 0;
        continue;
      }

      unsigned zeros = (unsigned)__builtin_ctzll(bits);
      if (zeros >= filled) {
        q += filled;
        bits = 0;
        filled = 0;
        continue;
      }

      q += zeros;
      bits >>= zeros + 1;
      filled -= zeros + 1;
      break;
    }

    /* Fixed-width remainder.  At most 31+10 bits are held at once. */
    while (filled < Mshift) {
      uint64_t chunk;
      unsigned width;
      int rc = decode_chunk(&input, &chunk, &width);
      if (rc < 0) return rc;
      if (rc == 0) return -EINVAL;
      bits |= chunk << filled;
      filled += width;
    }

    unsigned delta = (q << Mshift) | (unsigned)(bits & mask);
    bits >>= Mshift;
    filled -= Mshift;
    q = 0;

    previous += delta;
    hash_arr[count++] = previous;
  }
}

/* Bounded decoded-set cache: bucketed lookup plus O(1) LRU updates. */
static int downsample_set(const unsigned* hash_pt, size_t hash_cnt, unsigned* dest_pt,
                          int target_bpp);

static inline unsigned cache_bucket(uint32_t fingerprint, int target_bpp) {
  uint32_t mixed = fingerprint ^ ((uint32_t)target_bpp * UINT32_C(0x85ebca6b));
  mixed ^= mixed >> 11;
  mixed *= UINT32_C(0x9e3779b1);
  mixed ^= mixed >> 16;

  return mixed & (CACHE_BUCKETS - 1);
}

static int cache_decode_set(struct set_meta* meta, int target_bpp, const unsigned** hash_pt,
                            unsigned cache_id) {
  struct cache_ent {
    struct cache_ent* bucket_next;
    struct cache_ent* newer;
    struct cache_ent* older;
    char* str;
    unsigned* hash_arr;
    uint32_t fingerprint;
    int len;
    int cnt;
    int target_bpp;
  };

  static unsigned cache_count[2];
  static struct cache_ent* buckets[2][CACHE_BUCKETS];
  static struct cache_ent* newest[2];
  static struct cache_ent* oldest[2];

  assert(cache_id < 2);

  const unsigned char* str = (const unsigned char*)meta->str;
  uint32_t fp = (uint32_t)str[0] | ((uint32_t)str[2] << 8) | ((uint32_t)str[3] << 16);
  unsigned bucket = cache_bucket(fp, target_bpp);

  for (struct cache_ent* ent = buckets[cache_id][bucket]; ent; ent = ent->bucket_next) {
    if (ent->fingerprint != fp || ent->target_bpp != target_bpp || strcmp(meta->str, ent->str) != 0)
      continue;

    if (ent != newest[cache_id]) {
      if (ent->newer) ent->newer->older = ent->older;
      if (ent->older) ent->older->newer = ent->newer;
      if (ent == oldest[cache_id]) oldest[cache_id] = ent->newer;

      ent->newer = NULL;
      ent->older = newest[cache_id];
      newest[cache_id]->newer = ent;
      newest[cache_id] = ent;
    }

    *hash_pt = ent->hash_arr;

    return ent->cnt;
  }

  int meta_status = set_meta_fini(meta);
  if (meta_status < 0) return meta_status;

  int len = (int)meta->len;
  int capacity = meta->value_capacity;
  struct cache_ent* ent =
      xmalloc(sizeof(*ent) + (size_t)capacity * sizeof(unsigned) + (size_t)len + 1);
  ent->hash_arr = (unsigned*)(ent + 1);
  ent->str = (char*)(ent->hash_arr + capacity);

  int cnt = decode_set(meta, ent->hash_arr);
  if (cnt <= 0) {
    _free(ent);
    return cnt;
  }

  if (target_bpp < meta->bpp) {
    unsigned temporary[capacity];
    unsigned* current = ent->hash_arr;
    unsigned* destination = temporary;

    for (int bpp = meta->bpp - 1; bpp >= target_bpp; --bpp) {
      cnt = downsample_set(current, (size_t)cnt, destination, bpp);
      unsigned* swap = current;
      current = destination;
      destination = swap;
    }

    if (current != ent->hash_arr) {
      memcpy(ent->hash_arr, current, (size_t)cnt * sizeof(*current));
    }
  }

  memcpy(ent->str, meta->str, (size_t)len + 1);
  ent->fingerprint = fp;
  ent->len = len;
  ent->cnt = cnt;
  ent->target_bpp = target_bpp;

  if (cache_count[cache_id] == CACHE_SIZE) {
    struct cache_ent* victim = oldest[cache_id];
    oldest[cache_id] = victim->newer;
    if (oldest[cache_id]) oldest[cache_id]->older = NULL;
    if (victim == newest[cache_id]) newest[cache_id] = NULL;

    unsigned victim_bucket = cache_bucket(victim->fingerprint, victim->target_bpp);
    struct cache_ent** link = &buckets[cache_id][victim_bucket];
    while (*link != victim) link = &(*link)->bucket_next;
    *link = victim->bucket_next;
    _free(victim);
  } else {
    ++cache_count[cache_id];
  }

  ent->bucket_next = buckets[cache_id][bucket];
  buckets[cache_id][bucket] = ent;
  ent->newer = NULL;
  ent->older = newest[cache_id];
  if (newest[cache_id]) {
    newest[cache_id]->newer = ent;
  } else {
    oldest[cache_id] = ent;
  }
  newest[cache_id] = ent;

  *hash_pt = ent->hash_arr;

  return cnt;
}

/* Reduce a set of (bpp + 1) values to a set of bpp values. */
static int downsample_set(const unsigned* hash_pt, size_t hash_cnt, unsigned* dest_pt,
                          int target_bpp) {
  unsigned mask = (1u << target_bpp) - 1;

  /* find the first element with high bit set */
  size_t l = 0;
  size_t u = hash_cnt;
  while (l < u) {
    size_t i = (l + u) / 2;

    if (hash_pt[i] <= mask) {
      l = i + 1;
    } else {
      u = i;
    }
  }

  /* initialize parts */
  const unsigned* ds_start = dest_pt;
  const unsigned *v1 = hash_pt + 0, *v1_end = hash_pt + u;
  const unsigned *v2 = hash_pt + u, *v2_end = hash_pt + hash_cnt;

  /* merge v1 and v2 into w */
  if (v1 < v1_end && v2 < v2_end) {
    unsigned v1_val = *v1;
    unsigned v2_val = *v2 & mask;

    while (1) {
      if (v1_val < v2_val) {
        *dest_pt++ = v1_val;
        v1++;

        if (v1 == v1_end) break;

        v1_val = *v1;
      } else if (v2_val < v1_val) {
        *dest_pt++ = v2_val;
        v2++;

        if (v2 == v2_end) break;

        v2_val = *v2 & mask;
      } else {
        *dest_pt++ = v1_val;
        v1++;
        v2++;

        if (v1 == v1_end) break;
        if (v2 == v2_end) break;

        v1_val = *v1;
        v2_val = *v2 & mask;
      }
    }
  }

  /* append what's left */
  while (v1 < v1_end) *dest_pt++ = *v1++;
  while (v2 < v2_end) *dest_pt++ = *v2++ & mask;

  return (int)(dest_pt - ds_start);
}

static const unsigned* step_lower_bound(const unsigned* first, const unsigned* last, unsigned value,
                                        size_t jump) {
  const size_t count = (size_t)(last - first);

  if (count == 0 || first[0] >= value) {
    return first;
  }

  if (jump == 0) {
    jump = 1;
  }

  size_t position = 0;
  size_t step = jump;

  while (step != 0) {
    if (step > count - position - 1) {
      step /= 2;
      continue;
    }

    const size_t next = position + step;

    if (first[next] < value) {
      position = next;
    } else {
      step /= 2;
    }
  }

  return first + position + 1;
}

static int sorted_subset(const unsigned* small, size_t small_count, const unsigned* large,
                         size_t large_count) {
  const unsigned* const small_end = small + small_count;
  const unsigned* const large_end = large + large_count;
  size_t jump = large_count / small_count;

  /*
   * Dense sets favor a conventional merge; sparse sets skip by approximately
   * the mean distance between required values and then refine the last block.
   */
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

/* main API routine */
int rpmsetcmp(const char* str1, const char* str2) {
  if (strncmp(str1, "set:", 4) == 0) str1 += 4;
  if (strncmp(str2, "set:", 4) == 0) str2 += 4;

  struct set_meta meta1;
  struct set_meta meta2;

  if (set_meta_init(str1, &meta1) < 0) return -3;
  if (set_meta_init(str2, &meta2) < 0) return -4;

  int target_bpp = meta1.bpp < meta2.bpp ? meta1.bpp : meta2.bpp;

  /* Decode and cache the first operand at the comparison precision. */
  const unsigned* hash_arr1 = NULL;
  int cnt1 = cache_decode_set(&meta1, target_bpp, &hash_arr1, 0);
  if (cnt1 < 0) return -3;

  /*
   * Metadata for both operands has already been validated, and set1 has been
   * decoded, so this preserves set8's malformed-input error precedence.
   */
  if (str1 == str2 || strcmp(str1, str2) == 0) return 0;

  /* Requirement sets are frequently reused by dependency solvers too. */
  const unsigned* hash_arr2 = NULL;
  int cnt2 = cache_decode_set(&meta2, target_bpp, &hash_arr2, 1);
  if (cnt2 < 0) return -4;

  /*
   * Cardinality determines which strict-inclusion result is even possible.
   * For equal cardinalities, sorted unique sets are equal iff their bytes match.
   */
  if (cnt1 == cnt2) {
    return memcmp(hash_arr1, hash_arr2, (size_t)cnt1 * sizeof(*hash_arr1)) == 0 ? 0 : -2;
  }
  if (cnt1 > cnt2) {
    return sorted_subset(hash_arr2, (size_t)cnt2, hash_arr1, (size_t)cnt1) ? 1 : -2;
  }
  return sorted_subset(hash_arr1, (size_t)cnt1, hash_arr2, (size_t)cnt2) ? -1 : -2;
}

#ifdef SELF_TEST
static void test_hash(void) {
  assert(hash("") == UINT32_C(0xecd739e9));
  assert(hash("mama") == UINT32_C(0xd6707329));
  assert(hash("myla") == UINT32_C(0x29171f6c));
  assert(hash("ramu") == UINT32_C(0x41196985));

  fprintf(stderr, "%s: hash test OK\n", __FILE__);
}

static void test_sort(void) {
  struct symbols small[] = {
    {.offset = 0, .hash = 9}, {.offset = 1, .hash = 1}, {.offset = 2, .hash = 7},
    {.offset = 3, .hash = 3}, {.offset = 4, .hash = 5},
  };
  const unsigned small_expected[] = {1, 3, 5, 7, 9};
  const size_t offset_expected[] = {1, 3, 4, 2, 0};

  sort_symbols(small, sizeof(small) / sizeof(*small), 16);

  for (size_t i = 0; i < sizeof(small) / sizeof(*small); ++i) {
    assert(small[i].hash == small_expected[i]);
    assert(small[i].offset == offset_expected[i]);
  }

  enum { LARGE_COUNT = 257 };
  const int bpps[] = {10, 16, 24, 32};
  for (size_t bpp_i = 0; bpp_i < sizeof(bpps) / sizeof(*bpps); ++bpp_i) {
    int bpp = bpps[bpp_i];
    unsigned mask = bpp < 32 ? (1u << bpp) - 1 : ~0u;
    struct symbols values[LARGE_COUNT];
    struct symbols expected[LARGE_COUNT];

    for (size_t i = 0; i < LARGE_COUNT; ++i) {
      values[i].offset = i;
      values[i].hash = ((unsigned)i * UINT32_C(0x9e3779b1) ^ UINT32_C(0x85ebca6b)) & mask;
    }
    memcpy(expected, values, sizeof(values));
    qsort(expected, LARGE_COUNT, sizeof(*expected), cmp);

    sort_symbols(values, LARGE_COUNT, bpp);
    for (size_t i = 0; i < LARGE_COUNT; ++i) {
      assert(values[i].offset == expected[i].offset);
      assert(values[i].hash == expected[i].hash);
    }
  }

  fprintf(stderr, "%s: sort test OK\n", __FILE__);
}

static void test_encode_decode(void) {
  const unsigned original_values[] = {
    0x020a, 0x07e5, 0x3305, 0x35f5, 0x4980, 0x4c4f, 0x74ef, 0x7739,
    0x82ae, 0x8415, 0xa3e7, 0xb07e, 0xb584, 0xb89f, 0xbb40, 0xf39e,
  };

  const int original_count = (int)(sizeof(original_values) / sizeof(*original_values));
  char encoded[encode_set_size(original_count, 16)];
  int len = encode_set(original_count, original_values, 16, encoded);

  assert(len == (int)strlen(encoded));
  assert(strcmp(encoded, "jelgTKwwIMbKUZs24kk9ptXp1BZuBI1Z6Ixa0Z20") == 0);

  struct set_meta meta;
  assert(set_meta_init(encoded, &meta) == 0);
  assert(meta.bpp == 16);
  assert(meta.Mshift == 11);
  assert(set_meta_fini(&meta) == 0);

  unsigned decoded[meta.value_capacity];
  int count = decode_set(&meta, decoded);
  assert(count == original_count);
  assert(memcmp(decoded, original_values, sizeof(original_values)) == 0);

  const int bpps[] = {10, 16, 24, 32};
  enum { VALUE_COUNT = 32 };
  for (size_t bpp_i = 0; bpp_i < sizeof(bpps) / sizeof(*bpps); ++bpp_i) {
    int bpp = bpps[bpp_i];
    uint64_t mask = bpp < 32 ? (UINT64_C(1) << bpp) - 1 : UINT32_MAX;
    unsigned values[VALUE_COUNT];

    /* uniform distribution of values */
    for (int i = 0; i < VALUE_COUNT; ++i) {
      values[i] = (unsigned)(((uint64_t)(i + 1) * mask) / (VALUE_COUNT + 1));
    }

    char buf[encode_set_size(VALUE_COUNT, bpp)];
    assert(encode_set(VALUE_COUNT, values, bpp, buf) > 0);
    assert(set_meta_init(buf, &meta) == 0);
    assert(set_meta_fini(&meta) == 0);
    unsigned result[meta.value_capacity];
    count = decode_set(&meta, result);
    assert(count == VALUE_COUNT);
    assert(memcmp(result, values, sizeof(values)) == 0);
  }

  fprintf(stderr, "%s: encode/decode test OK\n", __FILE__);
}

static void test_metadata_and_chunks(void) {
  for (int c = 0; c <= UCHAR_MAX; ++c) {
    unsigned char expected = BASE62_INVALID;
    if (c == 0) {
      expected = BASE62_END;
    } else if (c >= '0' && c <= '9') {
      expected = (unsigned char)(c - '0');
    } else if (c >= 'a' && c <= 'z') {
      expected = (unsigned char)(c - 'a' + 10);
    } else if (c >= 'A' && c <= 'Z') {
      expected = (unsigned char)(c - 'A' + 36);
    }
    assert(char_to_num[c] == expected);
  }

  struct chunk_case {
    const char* input;
    int rc;
    uint64_t chunk;
    unsigned width;
  };

  const struct chunk_case cases[] = {
    {.input = "0", .rc = 1, .chunk = 0, .width = 6},
    {.input = "Y", .rc = 1, .chunk = 60, .width = 6},
    {.input = "Z0", .rc = 1, .chunk = 61, .width = 10},
    {.input = "Zg", .rc = 1, .chunk = 62, .width = 10},
    {.input = "Zw", .rc = 1, .chunk = 63, .width = 10},
    {.input = "", .rc = 0, .chunk = 0, .width = 0},
    {.input = "!", .rc = -EINVAL, .chunk = 0, .width = 0},
    {.input = "Z", .rc = -EPIPE, .chunk = 0, .width = 0},
    {.input = "Z!", .rc = -EINVAL, .chunk = 0, .width = 0},
    {.input = "ZM", .rc = -EILSEQ, .chunk = 0, .width = 0},
  };

  for (size_t i = 0; i < sizeof(cases) / sizeof(*cases); ++i) {
    const unsigned char* input = (const unsigned char*)cases[i].input;
    uint64_t chunk = 0;
    unsigned width = 0;
    int rc = decode_chunk(&input, &chunk, &width);
    assert(rc == cases[i].rc);

    if (rc > 0) {
      assert(chunk == cases[i].chunk);
      assert(width == cases[i].width);
    }
  }

  struct set_meta meta;
  assert(set_meta_init("", &meta) == -EPIPE); /* too short */
  assert(set_meta_init("da", &meta) == -EPIPE);
  assert(set_meta_init("ca0", &meta) == -ERANGE); /* incorrect bpp */
  assert(set_meta_init("{a0", &meta) == -ERANGE);
  assert(set_meta_init("d`0", &meta) == -ERANGE); /* incorrect Mshift */
  assert(set_meta_init("dz0", &meta) == -ERANGE);
  assert(set_meta_init("dd0", &meta) == -EINVAL); /* Mshift == bpp */
  assert(set_meta_init("da0", &meta) == 0);
  assert(set_meta_fini(&meta) == -EINVAL); /* not enough data */
  assert(set_meta_init("da00", &meta) == 0);
  assert(set_meta_fini(&meta) == 0); /* ok */
  assert(meta.len == 4);
  assert(meta.payload_len == 2);
  assert(meta.bit_capacity == 12);
  assert(meta.value_capacity == 1);

  fprintf(stderr, "%s: metadata/chunk test OK\n", __FILE__);
}

static void test_downsample(void) {
  const unsigned mixed[] = {0, 2, 5, 8, 10, 13, 15};
  const unsigned mixed_expected[] = {0, 2, 5, 7};

  unsigned result[sizeof(mixed) / sizeof(*mixed)];
  int count = downsample_set(mixed, sizeof(mixed) / sizeof(*mixed), result, 3);
  assert(count == (int)(sizeof(mixed_expected) / sizeof(*mixed_expected)));
  assert(memcmp(result, mixed_expected, sizeof(mixed_expected)) == 0);

  const unsigned low[] = {1, 2, 3};
  count = downsample_set(low, sizeof(low) / sizeof(*low), result,
                         3); /* sizeof(result) >= sizeof(low) */
  assert(count == (int)(sizeof(low) / sizeof(*low)));
  assert(memcmp(result, low, sizeof(low)) == 0);

  const unsigned high[] = {8, 9};
  const unsigned high_expected[] = {0, 1};
  count = downsample_set(high, sizeof(high) / sizeof(*high), result, 3);
  assert(count == (int)(sizeof(high_expected) / sizeof(*high_expected)));
  assert(memcmp(result, high_expected, sizeof(high_expected)) == 0);

  fprintf(stderr, "%s: downsample test OK\n", __FILE__);
}

static void test_subset(void) {
  const unsigned dense_large[] = {1, 2, 3, 4, 5, 6, 7};
  const unsigned dense_small[] = {2, 4, 6};
  const unsigned dense_missing[] = {2, 4, 8};
  assert(sorted_subset(dense_small, 3, dense_large, 7) == 1);
  assert(sorted_subset(dense_missing, 3, dense_large, 7) == 0); /* 0 - incompatible */

  unsigned sparse_large[64];
  for (size_t i = 0; i < sizeof(sparse_large) / sizeof(*sparse_large); ++i) {
    sparse_large[i] = (unsigned)i;
  }
  const unsigned sparse_small[] = {0, 17, 63};
  const unsigned sparse_missing[] = {0, 17, 64};
  assert(sorted_subset(sparse_small, 3, sparse_large, 64) == 1);
  assert(sorted_subset(sparse_missing, 3, sparse_large, 64) == 0);

  assert(step_lower_bound(sparse_large, sparse_large, 1, 8) == sparse_large);
  assert(step_lower_bound(sparse_large, sparse_large + 64, 0, 8) == sparse_large);
  assert(step_lower_bound(sparse_large, sparse_large + 64, 18, 8) == sparse_large + 18);
  assert(step_lower_bound(sparse_large, sparse_large + 64, 64, 8) == sparse_large + 64);

  fprintf(stderr, "%s: subset test OK\n", __FILE__);
}

static void test_cache(void) {
  const unsigned values[] = {0x020a, 0x3305, 0x4980, 0x82ae, 0xb584, 0xf39e};
  const int value_count = (int)(sizeof(values) / sizeof(*values));
  char encoded[encode_set_size(value_count, 16)];
  assert(encode_set(value_count, values, 16, encoded) > 0);

  struct set_meta meta;
  const unsigned* first = NULL;
  const unsigned* second = NULL;
  assert(set_meta_init(encoded, &meta) == 0);
  int count = cache_decode_set(&meta, 16, &first, 0);
  assert(count == value_count);
  assert(memcmp(first, values, sizeof(values)) == 0);
  assert(set_meta_init(encoded, &meta) == 0);
  assert(cache_decode_set(&meta, 16, &second, 0) == value_count);
  assert(second == first);

  unsigned downsampled[value_count];
  int downsampled_count = downsample_set(values, (size_t)value_count, downsampled, 15);
  assert(set_meta_init(encoded, &meta) == 0);
  assert(cache_decode_set(&meta, 15, &second, 0) == downsampled_count);
  assert(memcmp(second, downsampled, (size_t)downsampled_count * sizeof(*downsampled)) == 0);

  char last_encoded[encode_set_size(1, 16)];
  for (unsigned i = 1; i <= CACHE_SIZE + 8; ++i) {
    char item_encoded[encode_set_size(1, 16)];
    assert(encode_set(1, &i, 16, item_encoded) > 0);
    assert(set_meta_init(item_encoded, &meta) == 0);
    assert(cache_decode_set(&meta, 16, &second, 0) == 1);
    assert(second[0] == i);
    if (i == CACHE_SIZE + 8) memcpy(last_encoded, item_encoded, sizeof(last_encoded));
  }

  assert(set_meta_init(last_encoded, &meta) == 0);
  assert(cache_decode_set(&meta, 16, &first, 0) == 1);
  assert(set_meta_init(last_encoded, &meta) == 0);
  assert(cache_decode_set(&meta, 16, &second, 0) == 1);
  assert(first == second);

  fprintf(stderr, "%s: cache test OK\n", __FILE__);
}

static void test_builder(void) {
  enum { SYMBOL_COUNT = 1100 };
  struct set* set = set_new();
  struct symbols expected_symbols[SYMBOL_COUNT];

  for (int i = 0; i < SYMBOL_COUNT; ++i) {
    char symbol[32];
    int written = snprintf(symbol, sizeof(symbol), "symbol-%04d", i);
    assert(written > 0 && (size_t)written < sizeof(symbol));
    set_add(set, symbol);
    expected_symbols[i].offset = (size_t)i;
    expected_symbols[i].hash = hash(symbol);
  }

  assert(set->cnt == SYMBOL_COUNT);
  assert(set->symbols_cap >= SYMBOL_COUNT);
  assert(set->strings_len > 4096);
  assert(set->strings_cap >= set->strings_len);

  qsort(expected_symbols, SYMBOL_COUNT, sizeof(*expected_symbols), cmp);
  unsigned expected_hashes[SYMBOL_COUNT];
  int expected_count = 0;
  for (int i = 0; i < SYMBOL_COUNT; ++i) {
    if (i == 0 || expected_symbols[i].hash != expected_symbols[i - 1].hash) {
      expected_hashes[expected_count++] = expected_symbols[i].hash;
    }
  }

  const char* encoded = set_fini(set, 32);
  struct set_meta meta;
  assert(set_meta_init(encoded, &meta) == 0);
  assert(set_meta_fini(&meta) == 0);
  unsigned decoded[meta.value_capacity];
  int count = decode_set(&meta, decoded);
  assert(count == expected_count);
  assert(memcmp(decoded, expected_hashes, (size_t)expected_count * sizeof(*decoded)) == 0);
  for (int i = 1; i < count; ++i) assert(decoded[i - 1] < decoded[i]);

  set = set_free(set);
  encoded = _free((void*)encoded);
  assert(set == NULL);
  assert(encoded == NULL);
  assert(set_free(NULL) == NULL);

  fprintf(stderr, "%s: builder test OK\n", __FILE__);
}

static void test_api(void) {
  struct set* set1 = set_new();
  set_add(set1, "mama");
  set_add(set1, "myla");
  set_add(set1, "ramu");
  const char* str10 = set_fini(set1, 16);
  fprintf(stderr, "set10=%s\n", str10);

  int cmp;
  struct set* set2 = set_new();
  set_add(set2, "myla");
  set_add(set2, "mama");
  const char* str20 = set_fini(set2, 16);
  fprintf(stderr, "set20=%s\n", str20);
  cmp = rpmsetcmp(str10, str20);
  assert(cmp == 1);

  set_add(set2, "ramu");
  const char* str21 = set_fini(set2, 16);
  fprintf(stderr, "set21=%s\n", str21);
  cmp = rpmsetcmp(str10, str21);
  assert(cmp == 0);

  set_add(set2, "baba");
  const char* str22 = set_fini(set2, 16);
  cmp = rpmsetcmp(str10, str22);
  assert(cmp == -1);

  set_add(set1, "deda");
  const char* str11 = set_fini(set1, 16);
  cmp = rpmsetcmp(str11, str22);
  assert(cmp == -2);

  set1 = set_free(set1);
  set2 = set_free(set2);
  str10 = _free((void*)str10);
  str11 = _free((void*)str11);
  str20 = _free((void*)str20);
  str21 = _free((void*)str21);
  str22 = _free((void*)str22);

  assert(rpmsetcmp("bad", "bad") == -3);
  assert(rpmsetcmp("da00", "bad") == -4);

  fprintf(stderr, "%s: api test OK\n", __FILE__);
}

int main(void) {
  test_hash();
  test_sort();
  test_encode_decode();
  test_metadata_and_chunks();
  test_downsample();
  test_subset();
  test_cache();
  test_builder();
  test_api();

  return 0;
}
#endif
