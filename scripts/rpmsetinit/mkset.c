#include <stdio.h>
#include <stdlib.h>
#include <assert.h>
#include "set.h"

int main(int argc, const char **argv)
{
    assert(argc == 2);
    int bpp = atoi(argv[1]);
    assert(bpp >= 10);
    assert(bpp <= 32);
    struct set *set = set_new();
    char *line = NULL;
    size_t alloc_size = 0;
    ssize_t len;
    int added = 0;
    while ((len = getline(&line, &alloc_size, stdin)) >= 0) {
	if (len > 0 && line[len-1] == '\n')
	    line[--len] = '\0';
	if (len == 0)
	    continue;
	set_add(set, line);
	added++;
    }
    assert(added > 0);
    const char *str = set_fini(set, bpp);
    assert(str);
    printf("set:%s\n", str);
    return 0;
}
