#include "stdint.h"
#include "stdio.h"
#include "system.h"

struct set {
  size_t cnt;
  struct symbols {
    const char* str;
    uint64_t hash;
  }* symbols_v;
};

struct set* set_new() {
  // should we use x___ funcs?
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

uint64_t hash(const char* str) {
  uint64_t h = 0xcbf29ce484222325ULL;

  while (*str) {
    h ^= (uint64_t)(unsigned char)(*str++);
    h *= 0x100000001b3ULL;
  }

  return h;
}

int cmp(const void* arg1, const void* arg2) {
  const struct symbols* s1 = arg1;
  const struct symbols* s2 = arg2;

  if (s1->hash > s2->hash) return 1;
  if (s2->hash > s1->hash) return -1;

  return 0;
}

const char* set_fini(struct set* set, int bpp) {
  // Implementation for finalizing the set

  assert(set != NULL);
  assert(set->cnt > 0);
  assert(bpp >= 10 && bpp <= 63);

  uint64_t mask = (1ULL << bpp) - 1;

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

  uint64_t unique_hash[set->cnt];
  size_t unique_cnt = 0;

  // delete duplicates
  for (size_t i = 0; i < set->cnt; ++i) {
    while (i + 1 < set->cnt && set->symbols_v[i].hash == set->symbols_v[i + 1].hash) {
      ++i;
    }
    unique_hash[unique_cnt++] = set->symbols_v[i].hash;
  }

  return NULL;
}
