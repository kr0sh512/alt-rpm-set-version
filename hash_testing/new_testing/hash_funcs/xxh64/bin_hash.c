/*
 * Standalone XXH64 command-line wrapper for avalanche testing.
 *
 * XXH64 algorithm derived from xxHash by Yann Collet:
 * https://github.com/Cyan4973/xxHash
 *
 * Copyright (C) 2012-2023 Yann Collet
 *
 * BSD 2-Clause License
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 *
 * 1. Redistributions of source code must retain the above copyright notice,
 *    this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright notice,
 *    this list of conditions and the following disclaimer in the documentation
 *    and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
 * AND ANY EXPRESS OR IMPLIED WARRANTIES ARE DISCLAIMED. IN NO EVENT SHALL THE
 * COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DAMAGES ARISING IN ANY WAY
 * OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH
 * DAMAGE.
 */

#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define XXH64_SEED UINT64_C(0)
#define XXH_PRIME64_1 UINT64_C(11400714785074694791)
#define XXH_PRIME64_2 UINT64_C(14029467366897019727)
#define XXH_PRIME64_3 UINT64_C(1609587929392839161)
#define XXH_PRIME64_4 UINT64_C(9650029242287828579)
#define XXH_PRIME64_5 UINT64_C(2870177450012600261)

static uint64_t rotate_left64(uint64_t value, unsigned int count)
{
    return (value << count) | (value >> (64U - count));
}

static uint32_t read_little_endian32(const unsigned char *data)
{
    return (uint32_t)data[0] | ((uint32_t)data[1] << 8U) |
           ((uint32_t)data[2] << 16U) | ((uint32_t)data[3] << 24U);
}

static uint64_t read_little_endian64(const unsigned char *data)
{
    return (uint64_t)read_little_endian32(data) |
           ((uint64_t)read_little_endian32(data + 4) << 32U);
}

static uint64_t xxh64_round(uint64_t accumulator, uint64_t input)
{
    accumulator += input * XXH_PRIME64_2;
    accumulator = rotate_left64(accumulator, 31U);
    accumulator *= XXH_PRIME64_1;
    return accumulator;
}

static uint64_t xxh64_merge_round(uint64_t accumulator, uint64_t value)
{
    value = xxh64_round(UINT64_C(0), value);
    accumulator ^= value;
    accumulator = accumulator * XXH_PRIME64_1 + XXH_PRIME64_4;
    return accumulator;
}

static uint64_t xxh64(const unsigned char *data, size_t length, uint64_t seed)
{
    const unsigned char *position = data;
    const unsigned char *const end = data + length;
    uint64_t hash;

    if (length >= 32U) {
        const unsigned char *const block_end = end - 32U;
        uint64_t accumulator1 = seed + XXH_PRIME64_1 + XXH_PRIME64_2;
        uint64_t accumulator2 = seed + XXH_PRIME64_2;
        uint64_t accumulator3 = seed;
        uint64_t accumulator4 = seed - XXH_PRIME64_1;

        do {
            accumulator1 = xxh64_round(accumulator1, read_little_endian64(position));
            position += 8;
            accumulator2 = xxh64_round(accumulator2, read_little_endian64(position));
            position += 8;
            accumulator3 = xxh64_round(accumulator3, read_little_endian64(position));
            position += 8;
            accumulator4 = xxh64_round(accumulator4, read_little_endian64(position));
            position += 8;
        } while (position <= block_end);

        hash = rotate_left64(accumulator1, 1U) +
               rotate_left64(accumulator2, 7U) +
               rotate_left64(accumulator3, 12U) +
               rotate_left64(accumulator4, 18U);
        hash = xxh64_merge_round(hash, accumulator1);
        hash = xxh64_merge_round(hash, accumulator2);
        hash = xxh64_merge_round(hash, accumulator3);
        hash = xxh64_merge_round(hash, accumulator4);
    } else {
        hash = seed + XXH_PRIME64_5;
    }

    hash += (uint64_t)length;

    while ((size_t)(end - position) >= 8U) {
        uint64_t value = xxh64_round(UINT64_C(0), read_little_endian64(position));
        hash ^= value;
        hash = rotate_left64(hash, 27U) * XXH_PRIME64_1 + XXH_PRIME64_4;
        position += 8;
    }

    if ((size_t)(end - position) >= 4U) {
        hash ^= (uint64_t)read_little_endian32(position) * XXH_PRIME64_1;
        hash = rotate_left64(hash, 23U) * XXH_PRIME64_2 + XXH_PRIME64_3;
        position += 4;
    }

    while (position < end) {
        hash ^= (uint64_t)(*position) * XXH_PRIME64_5;
        hash = rotate_left64(hash, 11U) * XXH_PRIME64_1;
        ++position;
    }

    hash ^= hash >> 33U;
    hash *= XXH_PRIME64_2;
    hash ^= hash >> 29U;
    hash *= XXH_PRIME64_3;
    hash ^= hash >> 32U;
    return hash;
}

static int is_ascii_trailing_space(unsigned char character)
{
    return character == ' ' || character == '\t' || character == '\n' ||
           character == '\r' || character == '\v' || character == '\f';
}

static int read_stdin(unsigned char **data, size_t *length)
{
    size_t capacity = 256;
    unsigned char *buffer = malloc(capacity);

    if (buffer == NULL) {
        return -1;
    }

    *length = 0;
    for (;;) {
        size_t available = capacity - *length;
        size_t bytes_read = fread(buffer + *length, 1, available, stdin);
        *length += bytes_read;

        if (bytes_read < available) {
            if (ferror(stdin)) {
                free(buffer);
                return -1;
            }
            break;
        }

        if (capacity > SIZE_MAX / 2U) {
            free(buffer);
            return -1;
        }
        capacity *= 2U;

        {
            unsigned char *larger_buffer = realloc(buffer, capacity);
            if (larger_buffer == NULL) {
                free(buffer);
                return -1;
            }
            buffer = larger_buffer;
        }
    }

    *data = buffer;
    return 0;
}

int main(int argc, char **argv)
{
    const unsigned char *word;
    unsigned char *stdin_buffer = NULL;
    size_t length;

    if (argc > 2) {
        fprintf(stderr, "usage: %s [ASCII_WORD]\n", argv[0]);
        return EXIT_FAILURE;
    }

    if (argc == 2) {
        word = (const unsigned char *)argv[1];
        length = strlen(argv[1]);
    } else {
        if (read_stdin(&stdin_buffer, &length) != 0) {
            fprintf(stderr, "failed to read input\n");
            return EXIT_FAILURE;
        }
        word = stdin_buffer;
    }

    while (length > 0U && is_ascii_trailing_space(word[length - 1U])) {
        --length;
    }

    for (size_t index = 0; index < length; ++index) {
        if (word[index] > 0x7fU) {
            fprintf(stderr, "input must contain ASCII characters only\n");
            free(stdin_buffer);
            return EXIT_FAILURE;
        }
    }

    printf("%016" PRIx64 "\n", xxh64(word, length, XXH64_SEED));
    free(stdin_buffer);
    return EXIT_SUCCESS;
}
