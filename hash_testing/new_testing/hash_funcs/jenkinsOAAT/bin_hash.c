#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define JOAAT_SEED UINT32_C(0x9e3779b9)

static uint32_t jenkins_oaat(const unsigned char *data, size_t length)
{
    uint32_t hash = JOAAT_SEED;

    for (size_t index = 0; index < length; ++index) {
        hash += data[index];
        hash += hash << 10;
        hash ^= hash >> 6;
    }

    hash += hash << 3;
    hash ^= hash >> 11;
    hash += hash << 15;
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

        if (capacity > SIZE_MAX / 2) {
            free(buffer);
            return -1;
        }
        capacity *= 2;

        unsigned char *larger_buffer = realloc(buffer, capacity);
        if (larger_buffer == NULL) {
            free(buffer);
            return -1;
        }
        buffer = larger_buffer;
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

    while (length > 0 && is_ascii_trailing_space(word[length - 1])) {
        --length;
    }

    for (size_t index = 0; index < length; ++index) {
        if (word[index] > 0x7f) {
            fprintf(stderr, "input must contain ASCII characters only\n");
            free(stdin_buffer);
            return EXIT_FAILURE;
        }
    }

    printf("%08" PRIx32 "\n", jenkins_oaat(word, length));
    free(stdin_buffer);
    return EXIT_SUCCESS;
}