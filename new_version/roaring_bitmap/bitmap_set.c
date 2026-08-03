#include <roaring/roaring.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/*
 * This is intentionally a new set-string format.  It is not compatible with
 * the Rice-Golomb/base62 strings produced by the original lib/set.c.
 *
 *     R1<two decimal bpp digits><hex CRoaring portable serialization>
 */
#define FORMAT_PREFIX "R1"
#define FORMAT_HEADER_LEN 4
#define MAX_SERIALIZED_SIZE (64u * 1024u * 1024u)

struct set {
  roaring_bitmap_t* hashes;
  size_t added;
  char* encoded;
};

static void* xmalloc(size_t size) {
  void* ptr = malloc(size);
  if (!ptr) abort();
  return ptr;
}

static uint32_t hash_symbol(const char* str) {
  uint32_t hash = UINT32_C(0x9e3779b9);
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

struct set* set_new(void) {
  struct set* set = xmalloc(sizeof(*set));
  set->hashes = roaring_bitmap_create();
  if (!set->hashes) abort();
  set->added = 0;
  set->encoded = NULL;
  return set;
}

void set_add(struct set* set, const char* sym) {
  if (!set || !sym) return;
  roaring_bitmap_add(set->hashes, hash_symbol(sym));
  set->added++;
}

static roaring_bitmap_t* truncate_bitmap(const roaring_bitmap_t* source, unsigned bpp) {
  roaring_bitmap_t* result;
  roaring_uint32_iterator_t iterator;
  uint32_t mask;

  if (bpp == 32) {
    result = roaring_bitmap_copy(source);
    if (!result) abort();
    return result;
  }

  mask = (UINT32_C(1) << bpp) - 1;
  result = roaring_bitmap_create();
  if (!result) abort();

  roaring_iterator_init(source, &iterator);
  while (iterator.has_value) {
    roaring_bitmap_add(result, iterator.current_value & mask);
    roaring_uint32_iterator_advance(&iterator);
  }

  return result;
}

static char hex_digit(unsigned value) {
  return (char)(value < 10 ? '0' + value : 'a' + value - 10);
}

const char* set_fini(struct set* set, int bpp) {
  roaring_bitmap_t* truncated;
  size_t portable_size;
  size_t written;
  unsigned char* portable;
  char* output;

  if (!set || set->added == 0 || bpp < 10 || bpp > 32) return NULL;

  truncated = truncate_bitmap(set->hashes, (unsigned)bpp);
  if (roaring_bitmap_get_cardinality(truncated) < set->added)
    fprintf(stderr, "warning: hash collision\n");

  roaring_bitmap_run_optimize(truncated);
  portable_size = roaring_bitmap_portable_size_in_bytes(truncated);
  if (portable_size > MAX_SERIALIZED_SIZE) {
    roaring_bitmap_free(truncated);
    return NULL;
  }

  portable = xmalloc(portable_size);
  written = roaring_bitmap_portable_serialize(truncated, (char*)portable);
  if (written != portable_size) abort();
  if (portable_size > (SIZE_MAX - FORMAT_HEADER_LEN - 1) / 2) abort();

  output = xmalloc(FORMAT_HEADER_LEN + portable_size * 2 + 1);
  memcpy(output, FORMAT_PREFIX, sizeof(FORMAT_PREFIX) - 1);
  output[2] = (char)('0' + bpp / 10);
  output[3] = (char)('0' + bpp % 10);

  for (size_t i = 0; i < portable_size; ++i) {
    output[FORMAT_HEADER_LEN + i * 2] = hex_digit(portable[i] >> 4);
    output[FORMAT_HEADER_LEN + i * 2 + 1] = hex_digit(portable[i] & 0x0f);
  }
  output[FORMAT_HEADER_LEN + portable_size * 2] = '\0';

  free(portable);
  roaring_bitmap_free(truncated);
  free(set->encoded);
  set->encoded = output;
  return set->encoded;
}

struct set* set_free(struct set* set) {
  if (set) {
    roaring_bitmap_free(set->hashes);
    free(set->encoded);
    free(set);
  }
  return NULL;
}

static int hex_value(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

static roaring_bitmap_t* decode_bitmap(const char* str, unsigned* bpp) {
  roaring_bitmap_t* bitmap;
  unsigned char* bytes;
  const char* hex;
  const char* reason = NULL;
  size_t hex_len;
  size_t str_len;
  size_t byte_count;
  size_t expected;

  if (!str || !bpp) return NULL;
  if (strncmp(str, "set:", 4) == 0) str += 4;
  str_len = strlen(str);
  if (str_len < FORMAT_HEADER_LEN) return NULL;
  if (strncmp(str, FORMAT_PREFIX, sizeof(FORMAT_PREFIX) - 1) != 0) return NULL;
  if (str[2] < '0' || str[2] > '9' || str[3] < '0' || str[3] > '9') return NULL;

  *bpp = (unsigned)(str[2] - '0') * 10u + (unsigned)(str[3] - '0');
  if (*bpp < 10 || *bpp > 32) return NULL;

  hex = str + FORMAT_HEADER_LEN;
  hex_len = str_len - FORMAT_HEADER_LEN;
  if (hex_len == 0 || (hex_len & 1u) != 0) return NULL;
  byte_count = hex_len / 2;
  if (byte_count > MAX_SERIALIZED_SIZE) return NULL;
  bytes = xmalloc(byte_count);

  for (size_t i = 0; i < byte_count; ++i) {
    int high = hex_value(hex[i * 2]);
    int low = hex_value(hex[i * 2 + 1]);
    if (high < 0 || low < 0) {
      free(bytes);
      return NULL;
    }
    bytes[i] = (unsigned char)((high << 4) | low);
  }

  expected = roaring_bitmap_portable_deserialize_size((const char*)bytes, byte_count);
  if (expected != byte_count) {
    free(bytes);
    return NULL;
  }

  bitmap = roaring_bitmap_portable_deserialize_safe((const char*)bytes, byte_count);
  free(bytes);
  if (!bitmap) return NULL;
  if (!roaring_bitmap_internal_validate(bitmap, &reason) || roaring_bitmap_is_empty(bitmap)) {
    roaring_bitmap_free(bitmap);
    return NULL;
  }

  return bitmap;
}

int rpmsetcmp(const char* set1, const char* set2) {
  roaring_bitmap_t* bitmap1;
  roaring_bitmap_t* bitmap2;
  unsigned bpp1;
  unsigned bpp2;
  unsigned common_bpp;
  bool one_in_two;
  bool two_in_one;

  bitmap1 = decode_bitmap(set1, &bpp1);
  if (!bitmap1) return -3;
  bitmap2 = decode_bitmap(set2, &bpp2);
  if (!bitmap2) {
    roaring_bitmap_free(bitmap1);
    return -4;
  }

  common_bpp = bpp1 < bpp2 ? bpp1 : bpp2;
  if (bpp1 != common_bpp) {
    roaring_bitmap_t* truncated = truncate_bitmap(bitmap1, common_bpp);
    roaring_bitmap_free(bitmap1);
    bitmap1 = truncated;
  }
  if (bpp2 != common_bpp) {
    roaring_bitmap_t* truncated = truncate_bitmap(bitmap2, common_bpp);
    roaring_bitmap_free(bitmap2);
    bitmap2 = truncated;
  }

  one_in_two = roaring_bitmap_is_subset(bitmap1, bitmap2);
  two_in_one = roaring_bitmap_is_subset(bitmap2, bitmap1);
  roaring_bitmap_free(bitmap1);
  roaring_bitmap_free(bitmap2);

  if (one_in_two && two_in_one) return 0;
  if (two_in_one) return 1;
  if (one_in_two) return -1;
  return -2;
}

#ifdef SELF_TEST
#include <assert.h>

static char* make_set(const char* const* symbols, size_t count, int bpp) {
  struct set* set = set_new();
  const char* encoded;
  char* copy;

  for (size_t i = 0; i < count; ++i) set_add(set, symbols[i]);
  encoded = set_fini(set, bpp);
  assert(encoded);
  copy = xmalloc(strlen(encoded) + 1);
  strcpy(copy, encoded);
  set = set_free(set);
  assert(!set);
  return copy;
}

int main(void) {
  static const char* const small[] = {"malloc", "printf"};
  static const char* const large[] = {"free", "malloc", "printf"};
  static const char* const other[] = {"calloc", "malloc"};
  char* small16 = make_set(small, 2, 16);
  char* small32 = make_set(small, 2, 32);
  char* large16 = make_set(large, 3, 16);
  char* other16 = make_set(other, 2, 16);
  char* prefixed = xmalloc(strlen(small16) + 5);

  sprintf(prefixed, "set:%s", small16);
  assert(rpmsetcmp(small16, small16) == 0);
  assert(rpmsetcmp(prefixed, small16) == 0);
  assert(rpmsetcmp(small32, small16) == 0);
  assert(rpmsetcmp(large16, small16) == 1);
  assert(rpmsetcmp(small16, large16) == -1);
  assert(rpmsetcmp(small16, other16) == -2);
  assert(rpmsetcmp("bad", small16) == -3);
  assert(rpmsetcmp("R21600", small16) == -3);
  assert(rpmsetcmp(small16, "R116xyz") == -4);
  assert(rpmsetcmp("R", small16) == -3);

  free(prefixed);
  free(other16);
  free(large16);
  free(small32);
  free(small16);
  puts("bitmap_set self-test: OK");
  return 0;
}
#endif
