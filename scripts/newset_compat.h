#ifndef ARSV_NEWSET_COMPAT_H
#define ARSV_NEWSET_COMPAT_H

#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static inline void *arsv_xmalloc(size_t size)
{
    void *value = malloc(size);
    if (!value) {
        fputs("newset: out of memory\n", stderr);
        abort();
    }
    return value;
}

static inline void *arsv_xrealloc(void *pointer, size_t size)
{
    void *value = realloc(pointer, size);
    if (!value) {
        fputs("newset: out of memory\n", stderr);
        abort();
    }
    return value;
}

static inline char *arsv_xstrdup(const char *source)
{
    size_t size = strlen(source) + 1;
    char *copy = (char *)arsv_xmalloc(size);
    memcpy(copy, source, size);
    return copy;
}

static inline void *arsv_free(void *pointer)
{
    free(pointer);
    return NULL;
}

#define xmalloc arsv_xmalloc
#define xrealloc arsv_xrealloc
#define xstrdup arsv_xstrdup
#define _free arsv_free

#endif
