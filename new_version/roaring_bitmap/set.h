#ifndef ARSV_SET_H
#define ARSV_SET_H

struct set;

int rpmsetcmp(const char *set1, const char *set2);
struct set *set_new(void);
void set_add(struct set *set, const char *sym);
const char *set_fini(struct set *set, int bpp);
struct set *set_free(struct set *set);

#endif
