/* mkset-compatible wrapper around reimplement/newset.c.
 * The stdin/argv contract matches rpm-build/tools/mkset.c. */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>

struct set;
struct set *set_new(void);
void set_add(struct set *set, const char *symbol);
const char *set_fini(struct set *set, int bpp);
struct set *set_free(struct set *set);

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s BPP\n", argv[0]);
        return 2;
    }

    char *end = NULL;
    errno = 0;
    long parsed_bpp = strtol(argv[1], &end, 10);
    if (errno || !end || *end || parsed_bpp < 10 || parsed_bpp > 32) {
        fprintf(stderr, "invalid BPP: %s (expected 10..32)\n", argv[1]);
        return 2;
    }
    int bpp = (int)parsed_bpp;

    struct set *set = set_new();
    char *line = NULL;
    size_t allocated = 0;
    ssize_t length;
    int added = 0;

    while ((length = getline(&line, &allocated, stdin)) >= 0) {
        if (length > 0 && line[length - 1] == '\n')
            line[--length] = '\0';
        if (length == 0)
            continue;
        set_add(set, line);
        ++added;
    }

    if (!added) {
        fputs("mkset: no symbols on standard input\n", stderr);
        free(line);
        set_free(set);
        return 2;
    }
    const char *encoded = set_fini(set, bpp);
    if (!encoded) {
        fputs("mkset: unable to encode set\n", stderr);
        free(line);
        set_free(set);
        return 1;
    }
    printf("set:%s\n", encoded);

    free((void *)encoded);
    free(line);
    set_free(set);
    return 0;
}
