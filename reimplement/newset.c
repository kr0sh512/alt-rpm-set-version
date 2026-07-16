#include <assert.h>
#include <stdlib.h>
#include <string.h>

#include "rpmlib.h"
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
  while (n /= 2) m++;

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

static void encode_delta(int cnt, unsigned* hash_pt) {
  assert(cnt > 0);

  unsigned* end_pt = hash_pt + cnt;
  unsigned prev_hash = *hash_pt++;

  while (hash_pt < end_pt) {
    *hash_pt -= prev_hash;
    prev_hash += *hash_pt++;
  }

  return;
}

// Main golomb encoding routine: package integers into bits.
// http://algo2.iti.uni-karlsruhe.de/singler/publications/cacheefficientbloomfilters-wea2007.pdf
// The first integer is then stored in unary coding (which is a variable-length sequence of '0'
// followed by a terminating '1'); the second part is stored in normal binary coding (using Mshift
// bits).
static int encode_golomb(int cnt, const unsigned* delta_pt, int Mshift, char* bit_pt) {
  char* start_pt = bit_pt;
  const unsigned mask = (1 << Mshift) - 1;

  for (int i = 0; i < cnt; ++i) {
    unsigned elem = *delta_pt++;

    // first part: variable-length sequence
    unsigned q = elem >> Mshift;
    for (int j = 0; j < (int)q; ++j) {
      *bit_pt++ = 0;
    }

    *bit_pt++ = 1;

    // second part: lower Mshift bits
    unsigned r = elem & mask;
    for (int j = 0; j < Mshift; ++j) {
      *bit_pt++ = r & 1;
      r >>= 1;
    }
  }

  return bit_pt - start_pt;
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

static char* bits_to_char(int c, char* base62) {
  assert(c >= 0 && c <= 61);

  if (c < 10) {
    *base62++ = c + '0';
  } else if (c < 36) {
    *base62++ = c - 10 + 'a';
  } else if (c < 62) {
    *base62++ = c - 36 + 'A';
  }

  return base62;
}

// filling from the least significant bits, in case of Z - put in the most significant bits
static int encode_base62(int bit_cnt, const char* bit_pt, char* base62_str_pt) {
  char* base62_start = base62_str_pt;

  int bits2 = 0;  // number of high bits set
  int bits6 = 0;  // number of regular bits set
  int num6b = 0;  // pending 6-bit number

  while (bit_cnt-- > 0) {
    num6b |= (*bit_pt++ << bits6++);

    if (bits6 + bits2 < 6) continue;

    if (num6b >= 61) {  // 61 62 63 cases
      base62_str_pt = bits_to_char(61, base62_str_pt);
      bits2 = 2;
      num6b = (num6b - 61) << 4;  // (0|16|32) in high bits
    } else {
      assert(num6b < 61);
      base62_str_pt = bits_to_char(num6b, base62_str_pt);
      bits2 = 0;
      num6b = 0;
    }

    bits6 = 0;
  }

  if (bits6 + bits2) {
    assert(num6b < 61);
    base62_str_pt = bits_to_char(num6b, base62_str_pt);
  }

  *base62_str_pt = '\0';

  return base62_str_pt - base62_start;
}

// ---

static inline char encode_bpp(int bpp) { return bpp - 7 + 'a'; }

static int encode_set(int cnt, unsigned* hash_arr, int bpp, char* base62_str) {
  int Mshift = encode_golomb_Mshift(cnt, bpp);
  int bit_cnt = encode_golomb_size(cnt, Mshift);

  char bit_arr[bit_cnt];

  *base62_str++ = encode_bpp(bpp);
  *base62_str++ = encode_bpp(Mshift);

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

// ---

// decode bpp or Mshift value
static inline int decode_bpp(const char* str) { return *str++ + 7 - 'a'; }

static int decode_set_check(const char* str) {
  // 7..32 values encoded with 'a'..'z'
  int bpp = decode_bpp(str);
  if (bpp < 10 || bpp > 32) return -1;

  // golomb parameter
  int Mshift = decode_bpp(str + 1);
  if (Mshift < 7 || Mshift > 31) return -2;
  if (Mshift >= bpp) return -3;

  // no empty sets for now
  if (*str == '\0') return -4;

  return 0;
}

static int decode_set_size(const char* str) {
  int bit_cnt = 6 * (strlen(str) - 2);  // each base62 char can encode up to 6 bits

  return bit_cnt / (decode_bpp(str + 1) + 1);  // estimate number of values based on Mshift
}

static int char_to_num(char c) {
  if (c == '\0') return 0xff;  // end of string

  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'z') return c - 'a' + 10;
  if (c >= 'A' && c <= 'Z') return c - 'A' + 36;

  return 0xee;  // invalid character
}

// надо посмотреть, насколько в действительности это делает хуже
static char* putnbits(int n, int c, char* bit_pt) {
  for (int i = 0; i < n; ++i) {
    *bit_pt++ = (c >> i) & 1;
  }

  return bit_pt;
}

// Main base62 decoding routine: unpack base62 string into bitv[].
static int decode_base62(const char* base62_str, char* bit_pt) {
  char* bit_start = bit_pt;

  unsigned num6b = char_to_num(*base62_str++);  // pending 6-bit number
  while (num6b != 0xff) {
    if (num6b == 0xee) return -1;

    if (num6b < 61) {
      bit_pt = putnbits(6, num6b, bit_pt);
    } else {
      assert(num6b == 61);
      // 61 62 63 cases

      unsigned mask = (1 << 4) | (1 << 5);  // high bits mask
      int num4b = char_to_num(*base62_str++);
      if (num4b == 0xff) return -2;
      if (num4b == 0xee) return -3;

      int num2b = num4b & mask;  // high bits
      num4b &= ~mask;            // low bits
      assert(num2b != mask);     // not both bits set

      bit_pt = putnbits(6, 61 + (num2b >> 4), bit_pt);  // 61 + (0|1|2) in high bits
      bit_pt = putnbits(4, num4b, bit_pt);
    }

    num6b = char_to_num(*base62_str++);
  }

  return bit_pt - bit_start;
}

// Main golomb decoding routine: unpackage bits into values.
static int decode_golomb(int bit_cnt, const char* bit_pt, int Mshift, unsigned* golomb_pt) {
  unsigned* golomb_start = golomb_pt;

  // next value
  while (bit_cnt > 0) {
    // first part
    unsigned q = 0;
    char bit = 0;
    while (bit_cnt > 0) {
      bit_cnt--;
      bit = *bit_pt++;

      if (bit == 0) {
        q++;
      } else {
        break;
      }
    }

    // trailing zero bits in the input are okay
    if (bit_cnt == 0 && bit == 0) {
      // up to 5 bits can be used to complete last character
      if (q > 5) {
        return -10;
      }

      break;
    }

    // otherwise, incomplete value is not okay
    if (bit_cnt < Mshift) {
      return -11;
    }

    // second part
    unsigned r = 0;
    int i;
    for (i = 0; i < Mshift; i++) {
      bit_cnt--;
      if (*bit_pt++) {
        r |= (1 << i);
      }
    }

    // the value
    *golomb_pt++ = (q << Mshift) | r;
  }

  return golomb_pt - golomb_start;
}

static void decode_delta(int cnt, unsigned* delta_pt) {
  assert(cnt > 0);
  unsigned* delta_end = delta_pt + cnt;
  unsigned prev = *delta_pt++;

  while (delta_pt < delta_end) {
    *delta_pt += prev;
    prev = *delta_pt++;
  }

  return;
}

static int decode_set(const char* str, unsigned* hash_arr) {
  int Mshift = decode_bpp(str + 1);
  const char* base62_str = str + 2;

  // base62
  char bit_arr[6 * strlen(base62_str)];  // each base62 char can encode up to 6 bits
  int bit_cnt = decode_base62(base62_str, bit_arr);
  if (bit_cnt < 0) return bit_cnt;

  // golomb
  int cnt = decode_golomb(bit_cnt, bit_arr, Mshift, hash_arr);
  if (cnt < 0) return cnt;

  // delta
  decode_delta(cnt, hash_arr);

  return cnt;
}

// Reduce a set of (bpp + 1) values to a set of bpp values.
static int downsample_set(int cnt, const unsigned* hash_pt, unsigned* ds_pt, int bpp) {
  unsigned mask = (1 << bpp) - 1;

  // find the first element with high bit set
  int l = 0;
  int u = cnt;
  while (l < u) {
    int i = (l + u) / 2;

    if (hash_pt[i] <= mask) {
      l = i + 1;
    } else {
      u = i;
    }
  }

  // initialize parts
  const unsigned* ds_start = ds_pt;
  const unsigned *v1 = hash_pt + 0, *v1_end = hash_pt + u;
  const unsigned *v2 = hash_pt + u, *v2_end = hash_pt + cnt;

  // merge v1 and v2 into w
  if (v1 < v1_end && v2 < v2_end) {
    unsigned v1_val = *v1;
    unsigned v2_val = *v2 & mask;

    while (1) {
      if (v1_val < v2_val) {
        *ds_pt++ = v1_val;
        v1++;

        if (v1 == v1_end) break;

        v1_val = *v1;
      } else if (v2_val < v1_val) {
        *ds_pt++ = v2_val;
        v2++;

        if (v2 == v2_end) break;

        v2_val = *v2 & mask;
      } else {
        *ds_pt++ = v1_val;
        v1++;
        v2++;

        if (v1 == v1_end) break;
        if (v2 == v2_end) break;

        v1_val = *v1;
        v2_val = *v2 & mask;
      }
    }
  }

  // append what's left
  while (v1 < v1_end) *ds_pt++ = *v1++;
  while (v2 < v2_end) *ds_pt++ = *v2++ & mask;

  return ds_pt - ds_start;
}

// main API routine
int rpmsetcmp(const char* str1, const char* str2) {
  if (strncmp(str1, "set:", 4) == 0) str1 += 4;
  if (strncmp(str2, "set:", 4) == 0) str2 += 4;

  if (decode_set_check(str1) < 0) return -3;
  if (decode_set_check(str2) < 0) return -4;

  // decode set1
  int cnt1 = decode_set_size(str1);
  unsigned bufA1[cnt1];
  unsigned bufB1[cnt1];
  unsigned* hash_arr1 = bufA1;
  cnt1 = decode_set(str1, hash_arr1);
  if (cnt1 < 0) return -3;

  // decode set2
  int cnt2 = decode_set_size(str2);
  unsigned bufA2[cnt2];
  unsigned bufB2[cnt2];
  unsigned* hash_arr2 = bufA2;
  cnt2 = decode_set(str2, hash_arr2);
  if (cnt2 < 0) return -4;

  int bpp1 = decode_bpp(str1);
  int bpp2 = decode_bpp(str2);
  int min_bpp = (bpp1 < bpp2) ? bpp1 : bpp2;

  while (bpp1 > min_bpp) {
    unsigned* pt1 = bufA1;
    if (hash_arr1 == pt1) {
      pt1 = bufB1;
    }

    bpp1--;
    cnt1 = downsample_set(cnt1, hash_arr1, pt1, bpp1);
    hash_arr1 = pt1;
  }

  while (bpp2 > min_bpp) {
    unsigned* pt2 = bufA2;
    if (hash_arr2 == pt2) {
      pt2 = bufB2;
    }

    bpp2--;
    cnt2 = downsample_set(cnt2, hash_arr2, pt2, bpp2);
    hash_arr2 = pt2;
  }

  // compare
  int ge = 1;
  int le = 1;
  const unsigned* end1 = hash_arr1 + cnt1;
  const unsigned* end2 = hash_arr2 + cnt2;

  while (hash_arr1 < end1 && hash_arr2 < end2) {
    if (*hash_arr1 < *hash_arr2) {
      le = 0;
      hash_arr1++;
    } else if (*hash_arr2 < *hash_arr1) {
      ge = 0;
      hash_arr2++;
    } else {
      hash_arr1++;
      hash_arr2++;
    }
  }

  if (hash_arr1 < end1) {
    le = 0;
  }
  if (hash_arr2 < end2) {
    ge = 0;
  }

  if (ge && le) {
    return 0;
  } else if (ge) {
    return 1;
  } else if (le) {
    return -1;
  }

  return -2;
}

// ---

#ifdef SELF_TEST
int main(void) {
  char* str1 =
      "set:"
      "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"
      "QpZ2pqtTARCuKaUBu0";
  char* str2 =
      "set:"
      "phJHRl3EzNHzMh24nFH1HG34VOiwtYmZDp9loYdzk0Me6ZAoGEcRZ0JmtuArQAYloeFVP3zyunTSKwV7QyS3NAsFZLwL"
      "G9tZdg69kIXEFrwPmo4utjk5BGFMraU7L16OPHcsCujvjk4cwZFwW8MgXuJNYZiJZm9eKIeZ5raZ6PVbCxFh90gE3l23"
      "ICpQPp3unbKn6uGWKben4WybxAecE9eQT5kbFTmc7PjPIrjiZCKyILUAU72EQSJE0kixTzN4FgGp3OnZGQFB2I5sfudD"
      "SZxefsQFMd6KTpRKPhgFThKYIUbAHNvIFUjIBRK6QjVGkZubCDU0SL2nMbzPZeGkTZev3Zld0SnnRsyGrDlBgw2XfrJU"
      "n4toAVapu9n2Y7wgbZtMQEWxoxpBZ0aDSwKn916ZusjCQZm46pruOAzEmwUHoKiiW8QQeZ9A065jEH5QsxgiWBuGKRq5"
      "ZahkJSCxBS42GZ9o15vfSC5aiD9AsjZKSpvcbsskcQsJAXZyMwdc76wto8RAX2LRa9aZi4kmbtIORg0j2EhefPZ7ZCAp"
      "oCuW21EJxPkrx7fsfMOvyYUQrDfwipSpMk";

  printf("cmp! %d\n", rpmsetcmp(str1, str2));

  return 0;

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
  str10 = _free(str10);
  str11 = _free(str11);
  str20 = _free(str20);
  str21 = _free(str21);
  str22 = _free(str22);

  fprintf(stderr, "%s: api test OK\n", __FILE__);

  return 0;
}
#endif
