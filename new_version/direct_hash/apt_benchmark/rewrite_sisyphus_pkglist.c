#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef ARSV_WITH_RPM
#include <rpm/header.h>
#include <rpm/rpmio.h>
#include <rpm/rpmlib.h>
#include <rpm/rpmtag.h>
#include <rpm/rpmtd.h>
#endif

int arsv_set9_decode(const char* source, unsigned** hashes, size_t* count, unsigned* bpp);

#define D1_PREFIX "set:D1"
#define D1_HEADER_LEN 8

static const char base64_alphabet[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static size_t base64_encoded_size(size_t byte_count) {
  if (byte_count > SIZE_MAX - 2) return SIZE_MAX;
  size_t groups = (byte_count + 2) / 3;
  if (groups > SIZE_MAX / 4) return SIZE_MAX;
  size_t size = groups * 4;
  size_t remainder = byte_count % 3;
  if (remainder) size -= 3 - remainder;
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
}

static int encode_d1(const unsigned* hashes, size_t count, unsigned bpp, char** result) {
  if (!hashes || !count || !result || bpp < 10 || bpp > 32) return -EINVAL;
  if (count > (SIZE_MAX - 7) / bpp) return -EOVERFLOW;

  size_t bit_count = count * bpp;
  size_t byte_count = (bit_count + 7) / 8;
  unsigned char* bytes = calloc(byte_count, 1);
  if (!bytes) return -ENOMEM;

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
  if ((size_t)(output - bytes) != byte_count) {
    free(bytes);
    return -EIO;
  }

  size_t payload_len = base64_encoded_size(byte_count);
  if (payload_len == SIZE_MAX || payload_len > SIZE_MAX - D1_HEADER_LEN - 1) {
    free(bytes);
    return -EOVERFLOW;
  }
  char* encoded = malloc(D1_HEADER_LEN + payload_len + 1);
  if (!encoded) {
    free(bytes);
    return -ENOMEM;
  }
  memcpy(encoded, D1_PREFIX, sizeof(D1_PREFIX) - 1);
  encoded[6] = (char)('0' + bpp / 10);
  encoded[7] = (char)('0' + bpp % 10);
  base64_encode(bytes, byte_count, encoded + D1_HEADER_LEN);
  encoded[D1_HEADER_LEN + payload_len] = '\0';
  free(bytes);
  *result = encoded;
  return 0;
}

static int convert_set(const char* source, char** result, size_t* hash_count, unsigned* bpp) {
  if (!source || strncmp(source, "set:", 4) != 0 || strncmp(source, D1_PREFIX, 6) == 0)
    return -EINVAL;

  unsigned* hashes = NULL;
  size_t count = 0;
  unsigned precision = 0;
  int rc = arsv_set9_decode(source, &hashes, &count, &precision);
  if (rc == 0) rc = encode_d1(hashes, count, precision, result);
  free(hashes);
  if (rc < 0) return rc;
  if (hash_count) *hash_count = count;
  if (bpp) *bpp = precision;
  return 0;
}

#ifdef ARSV_WITH_RPM
struct statistics {
  unsigned long headers;
  unsigned long set_occurrences;
  unsigned long set_bytes_old;
  unsigned long set_bytes_new;
  unsigned long hashes;
};

static int rewrite_version_tag(Header header, rpmTagVal tag, const char* package,
                               struct statistics* stats) {
  struct rpmtd_s values;
  memset(&values, 0, sizeof(values));
  if (headerGet(header, tag, &values, HEADERGET_MINMEM) != 1) return 0;
  if (rpmtdType(&values) != RPM_STRING_ARRAY_TYPE) {
    rpmtdFreeData(&values);
    fprintf(stderr, "%s: tag %d is not a string array\n", package, (int)tag);
    return -EINVAL;
  }

  rpm_count_t count = rpmtdCount(&values);
  const char** rewritten = calloc((size_t)count, sizeof(*rewritten));
  if (!rewritten) {
    rpmtdFreeData(&values);
    return -ENOMEM;
  }

  int rc = 0;
  int changed = 0;
  rpmtdInit(&values);
  for (rpm_count_t i = 0; i < count; ++i) {
    const char* value = rpmtdNextString(&values);
    if (!value) {
      rc = -EINVAL;
      break;
    }
    if (strncmp(value, "set:", 4) == 0) {
      char* converted = NULL;
      size_t hashes = 0;
      unsigned bpp = 0;
      rc = convert_set(value, &converted, &hashes, &bpp);
      if (rc < 0) {
        fprintf(stderr, "%s: cannot convert tag %d index %u (rc=%d)\n", package,
                (int)tag, (unsigned)i, rc);
        break;
      }
      rewritten[i] = converted;
      changed = 1;
      ++stats->set_occurrences;
      stats->set_bytes_old += strlen(value);
      stats->set_bytes_new += strlen(converted);
      stats->hashes += hashes;
    } else {
      rewritten[i] = strdup(value);
      if (!rewritten[i]) {
        rc = -ENOMEM;
        break;
      }
    }
  }

  if (rc == 0 && changed) {
    headerDel(header, tag);
    if (!headerPutStringArray(header, tag, rewritten, count)) rc = -EIO;
  }
  for (rpm_count_t i = 0; i < count; ++i) free((void*)rewritten[i]);
  free(rewritten);
  rpmtdFreeData(&values);
  return rc;
}

static int rewrite_pkglist(const char* input_path, const char* output_path, unsigned long limit) {
  FD_t input = Fopen(input_path, "r.ufdio");
  if (!input || Ferror(input)) {
    fprintf(stderr, "%s: %s\n", input_path, input ? Fstrerror(input) : "cannot open");
    return 1;
  }
  FD_t output = Fopen(output_path, "w.ufdio");
  if (!output || Ferror(output)) {
    fprintf(stderr, "%s: %s\n", output_path, output ? Fstrerror(output) : "cannot open");
    Fclose(input);
    return 1;
  }

  struct statistics stats = {0};
  Header header;
  int failed = 0;
  const rpmTagVal tags[] = {
      RPMTAG_REQUIREVERSION,
      RPMTAG_PROVIDEVERSION,
      RPMTAG_CONFLICTVERSION,
      RPMTAG_OBSOLETEVERSION,
      RPMTAG_RECOMMENDVERSION,
      RPMTAG_SUGGESTVERSION,
      RPMTAG_SUPPLEMENTVERSION,
      RPMTAG_ENHANCEVERSION,
  };
  while ((!limit || stats.headers < limit) &&
         (header = headerRead(input, HEADER_MAGIC_YES)) != NULL) {
    const char* package = headerGetString(header, RPMTAG_NAME);
    if (!package) package = "<unknown>";
    for (size_t i = 0; i < sizeof(tags) / sizeof(tags[0]); ++i) {
      int rc = rewrite_version_tag(header, tags[i], package, &stats);
      if (rc < 0) {
        failed = 1;
        break;
      }
    }
    if (!failed && headerWrite(output, header, HEADER_MAGIC_YES) != 0) {
      fprintf(stderr, "%s: failed to write header for %s\n", output_path, package);
      failed = 1;
    }
    headerFree(header);
    if (failed) break;
    ++stats.headers;
  }
  if (!failed && Ferror(input)) {
    fprintf(stderr, "%s: read error: %s\n", input_path, Fstrerror(input));
    failed = 1;
  }
  if (Fclose(output) != 0) failed = 1;
  Fclose(input);

  if (failed) {
    remove(output_path);
    return 1;
  }
  fprintf(stderr,
          "headers=%lu set_occurrences=%lu hashes=%lu old_set_bytes=%lu "
          "new_set_bytes=%lu\n",
          stats.headers, stats.set_occurrences, stats.hashes, stats.set_bytes_old,
          stats.set_bytes_new);
  return 0;
}
#endif

static void usage(const char* program) {
  fprintf(stderr, "usage: %s --convert-set set:VALUE\n", program);
#ifdef ARSV_WITH_RPM
  fprintf(stderr, "       %s --rewrite INPUT OUTPUT [--max-headers N]\n", program);
#endif
}

int main(int argc, char** argv) {
  if (argc == 3 && strcmp(argv[1], "--convert-set") == 0) {
    char* converted = NULL;
    int rc = convert_set(argv[2], &converted, NULL, NULL);
    if (rc < 0) {
      fprintf(stderr, "cannot convert set value (rc=%d)\n", rc);
      return 1;
    }
    puts(converted);
    free(converted);
    return 0;
  }
#ifdef ARSV_WITH_RPM
  if ((argc == 4 || argc == 6) && strcmp(argv[1], "--rewrite") == 0) {
    unsigned long limit = 0;
    if (argc == 6) {
      if (strcmp(argv[4], "--max-headers") != 0) {
        usage(argv[0]);
        return 2;
      }
      char* end = NULL;
      errno = 0;
      limit = strtoul(argv[5], &end, 10);
      if (errno || !end || *end || !limit) {
        fprintf(stderr, "invalid --max-headers value: %s\n", argv[5]);
        return 2;
      }
    }
    return rewrite_pkglist(argv[2], argv[3], limit);
  }
#endif
  usage(argv[0]);
  return 2;
}