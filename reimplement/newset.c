#include "system.h"

struct set {
  int cnt;
  struct symbols {
    const char* str;
    unsigned hash;
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

  if ((set->cnt & (delta - 1)) == 0) {
    set->symbols_v = xrealloc(set->symbols_v, sizeof(*set->symbols_v) * (set->cnt + delta));
  }

  set->symbols_v[set->cnt].str = xstrdup(sym);
  set->symbols_v[set->cnt].hash = 0;
  set->cnt++;

  return;
}

struct set* set_free(struct set* set) {
  if (set) {
    for (int i = 0; i < set->cnt; i++) {
      _free((char*)set->symbols_v[i].str);
    }

    _free(set->symbols_v);
    set = _free(set);
  }

  return NULL;
}

// ---

const char* set_fini(struct set* set, int bpp) {
  // Implementation for finalizing the set
  return NULL;
}
