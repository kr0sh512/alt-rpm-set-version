#include "hash.h"

#include <stddef.h>

#define JOAAT_SEED 0x9e3779b9u

uint32_t joaat_hash_bytes(const void *data, size_t len)
{
    uint32_t hash = JOAAT_SEED;
    const unsigned char *p = (const unsigned char *)data;

    while (len-- > 0) {
        hash += *p++;
        hash += (hash << 10);
        hash ^= (hash >> 6);
    }

    hash += (hash << 3);
    hash ^= (hash >> 11);
    hash += (hash << 15);

    return hash;
}

uint32_t joaat_hash(const char *str)
{
    uint32_t hash = JOAAT_SEED;
    const unsigned char *p = (const unsigned char *)str;

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

unsigned int hash(const char *str)
{
    return (unsigned int)joaat_hash(str);
}
