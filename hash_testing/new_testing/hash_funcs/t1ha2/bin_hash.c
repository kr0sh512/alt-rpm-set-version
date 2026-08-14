/*
 * Standalone t1ha2_atonce command-line wrapper for avalanche testing.
 *
 * Upstream: https://gitflic.ru/project/erthink/t1ha
 * Commit: 00eb779b6c042ccd831ec2f1ae757409c73f39f6
 * Algorithm: t1ha2_atonce(data, length, seed=0), stable portable 64-bit mode.
 *
 * The vendored upstream implementation is licensed under the zlib License;
 * see upstream/LICENSE. This wrapper is an altered integration file and is not
 * represented as an original upstream source file.
 */

#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define T1HA0_DISABLED
#define T1HA1_DISABLED
#define T1HA_SYS_UNALIGNED_ACCESS 0
#define T1HA_USE_FAST_ONESHOT_READ 0
#include "upstream/src/t1ha2.c"

#define T1HA2_SEED UINT64_C(0)

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

    printf("%016" PRIx64 "\n", t1ha2_atonce(word, length, T1HA2_SEED));
    free(stdin_buffer);
    return EXIT_SUCCESS;
}
