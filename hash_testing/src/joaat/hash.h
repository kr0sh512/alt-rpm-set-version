#ifndef ARSV_JOAAT_HASH_H
#define ARSV_JOAAT_HASH_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

uint32_t joaat_hash(const char *str);
uint32_t joaat_hash_bytes(const void *data, size_t len);

/* Compatibility wrapper matching the original set.c function name. */
unsigned int hash(const char *str);

#ifdef __cplusplus
}
#endif

#endif /* ARSV_JOAAT_HASH_H */
