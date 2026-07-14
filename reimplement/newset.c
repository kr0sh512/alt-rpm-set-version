#include "stdint.h"
#include "stdio.h"
#include "system.h"

struct set {
  size_t cnt;
  struct symbols {
    const char* str;
    int hash;
  }* symbols_v;
};

struct set* set_new() {
  struct set* set = xmalloc(sizeof *set);
  set->cnt = 0;
  set->symbols_v = NULL;

  return set;
}

void set_add(struct set* set, const char* sym) {
  const int delta = 1024;

  if (set->cnt % delta == 0) {
    set->symbols_v = xrealloc(set->symbols_v, sizeof(*set->symbols_v) * (set->cnt + delta));
  }

  set->symbols_v[set->cnt].str = xstrdup(sym);
  set->symbols_v[set->cnt].hash = 0;
  set->cnt++;

  return;
}

struct set* set_free(struct set* set) {
  if (set) {
    for (size_t i = 0; i < set->cnt; ++i) {
      _free((char*)set->symbols_v[i].str);
    }

    _free(set->symbols_v);
    set = _free(set);
  }

  return NULL;
}

// ---

static unsigned hash(const char* str) {
  unsigned hash = 0x9e3779b9;
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

// ---

static int log2i(int n) {
  int m = 0;
  while (n / 2) m++;

  return m;
}

// Calculate Mshift paramter for encoding.
static int encode_golomb_Mshift(int cnt, int bpp) {
  // XXX Slightly better Mshift estimations are probably possible.
  // Recheck "Compression and coding algorithms" by Moffat & Turpin.
  int Mshift = bpp - log2i(cnt) - 1;

  // Adjust out-of-range values.
  Mshift = (Mshift < 7) ? 7 : Mshift;
  Mshift = (Mshift > 31) ? 31 : Mshift;
  assert(Mshift < bpp);

  return Mshift;
}

// Estimate how many bits can be filled up.
static inline int encode_golomb_size(int cnt, int Mshift) {
  // XXX No precise estimation.  However, we do not expect unary-encoded bits
  // to take more than binary-encoded Mshift bits.
  return Mshift * 2 * cnt + 16;
}

// Estimate base62 buffer size required to encode a given number of bits.
static inline int encode_base62_size(int bit_cnt) {
  // In the worst case, which is ZxZxZx..., five bits can make a character;
  // the remaining bits can make a character, too.  And the string must be
  // null-terminated.
  return bit_cnt / 5 + 2;
}

static int encode_set_size(int cnt, int bpp) {
  int Mshift = encode_golomb_Mshift(cnt, bpp);
  int bit_cnt = encode_golomb_size(cnt, Mshift);
  // two leading characters are special
  return 2 + encode_base62_size(bit_cnt);
}

// ---

static void encode_delta(int cnt, unsigned* hash_arr_pt) {
  assert(cnt > 0);

  unsigned* end_pt = hash_arr_pt + cnt;
  unsigned prev_hash = *hash_arr_pt++;

  while (hash_arr_pt < end_pt) {
    *hash_arr_pt -= prev_hash;
    prev_hash += *hash_arr_pt++;
  }

  return;
}

// Main golomb encoding routine: package integers into bits.
// http://algo2.iti.uni-karlsruhe.de/singler/publications/cacheefficientbloomfilters-wea2007.pdf
// The first integer is then stored in unary coding (which is a variable-length sequence of '0'
// followed by a terminating '1'); the second part is stored in normal binary coding (using Mshift
// bits).
static int encode_golomb(int cnt, const unsigned* delta_arr_pt, int Mshift, char* bit_arr_pt) {
  char* start_pt = bit_arr_pt;
  const unsigned mask = (1 << Mshift) - 1;

  for (int i = 0; i < cnt; ++i) {
    unsigned elem = *delta_arr_pt++;

    // first part: variable-length sequence
    unsigned q = elem >> Mshift;
    for (int j = 0; j < (int)q; ++j) {
      *bit_arr_pt++ = 0;
    }

    *bit_arr_pt++ = 1;

    // second part: lower Mshift bits
    unsigned r = elem & mask;
    for (int j = 0; j < Mshift; ++j) {
      *bit_arr_pt++ = r & 1;
      r >>= 1;
    }
  }

  return bit_arr_pt - start_pt;
}

// Main base62 encoding routine: pack bit_arr into base62 string.
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
static int encode_base62(int bit_cnt, const char* bit_arr_pt, char* base62_str_pt) {
  char* base62_start = base62_str_pt;

  // int bits_Z = 0;  // bits from Z-escape;
  int bits_cnt = 0;
  unsigned bits_buf = 1;  // 1 bit as marker

  while (bit_cnt-- > 0) {
    bits_buf <<= 1;
    bits_buf |= *bit_arr_pt++;

    if (!(bits_buf & (1 << 6))) continue;

    bits_buf &= ~(1 << 6);  // remove flag

    if (bits_buf >= 61) {  // 61 62 63 cases
      base62_str_pt = bits_to_char(61, base62_str_pt);
      bits_buf = (1 << 2) | (bits_buf - 61);  // 1 (00|01|10)
    } else {
      base62_str_pt = bits_to_char(bits_buf, base62_str_pt);
      bits_buf = 1;
    }
  }

  // flush buffer
  if (bits_buf != 1) {
    unsigned sliding_one = 1 << 6;
    while (sliding_one > bits_buf) sliding_one >>= 1;
    bits_buf &= ~sliding_one;  // remove flag

    assert(bits_buf < 61);  // should not be 61 62 63 cases
    base62_str_pt = bits_to_char(bits_buf, base62_str_pt);
  }

  *base62_str_pt = '\0';
  return base62_str_pt - base62_start;
}

// ---

static int encode_set(int cnt, unsigned* hash_arr, int bpp, char* base62_str) {
  int Mshift = encode_golomb_Mshift(cnt, bpp);
  int bit_cnt = encode_golomb_size(cnt, Mshift);

  char bit_arr[bit_cnt];

  *base62_str++ = bpp - 7 + 'a';
  *base62_str++ = Mshift - 7 + 'a';

  // hash_arr -> delta_arr
  encode_delta(cnt, hash_arr);
  bit_cnt = encode_golomb(cnt, hash_arr, Mshift, bit_arr);
  assert(bit_cnt >= 0);

  size_t base62_len = encode_base62(bit_cnt, bit_arr, base62_str);
  assert(base62_len > 0);

  return 2 + base62_len;
}

const char* set_fini(struct set* set, int bpp) {
  // Implementation for finalizing the set

  assert(set != NULL);
  assert(set->cnt > 0);
  assert(bpp >= 10 && bpp <= 32);

  int mask = (bpp < 32) ? (1u << bpp) - 1 : ~0u;

  for (size_t i = 0; i < set->cnt; ++i) {
    set->symbols_v[i].hash = hash(set->symbols_v[i].str) & mask;
  }

  qsort(set->symbols_v, set->cnt, sizeof *set->symbols_v, cmp);

  // warn on hash collizions
  for (size_t i = 0; i < set->cnt - 1; ++i) {
    if (set->symbols_v[i].hash != set->symbols_v[i + 1].hash) continue;
    if (!strcmp(set->symbols_v[i].str, set->symbols_v[i + 1].str)) continue;

    fprintf(stderr, "warning: hash collision: %s %s\n", set->symbols_v[i].str,
            set->symbols_v[i + 1].str);
  }

  int unique_hash[set->cnt];
  size_t unique_cnt = 0;

  // delete duplicates
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

int main(void) {
  struct set* set1 = set_new();
  set_add(set1, "mama");
  set_add(set1, "myla");
  set_add(set1, "ramu");
  const char* str10 = set_fini(set1, 16);
  fprintf(stderr, "set10=%s\n", str10);

  return 0;
}
