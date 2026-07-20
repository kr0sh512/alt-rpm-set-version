# set:version

What happens inside [set:version](https://git.altlinux.org/gears/r/rpm.git?a=blob;f=lib/set.c)

The current code with the latest developments is available in the [repository](https://github.com/kr0sh512/alt-rpm-set-version.git).

## Motivation

set:version in alt-rpm allows package Provides and Requires to be matched not by ordinary version comparison, but by comparing special dependency set-strings of the form

```text
libfoo.so.X = set:<encoded-set>
```

`encoded-set` is generated from the symbols required/provided by a package. This mechanism guarantees (up to hash collisions, discussed below) that all required symbols are present in the library. It prevents situations where `>= version` matching breaks when a symbol is removed from a library, as well as situations where libraries with different symbol sets have the same SONAME.

Set-strings (which are re-encoded list of symbols) are generated in a way that allows them to be compared to determine whether one symbol set is contained in another.

## `set.c` implementation

`set.c` provides five "public" APIs for working with set:version:

```c
int rpmsetcmp(const char *set1, const char *set2);

struct set *set_new(void);
void set_add(struct set *set, const char *sym);
const char *set_fini(struct set *set, int bpp);
struct set *set_free(struct set *set);
```

### `rpmsetcmp()`

The main function, which [compares strings](#comparing-set-strings) and returns a result based on set inclusion:

- 1: set1 > set2
- 0: set1 == set2
- -1: set1 < set2
- -2: set1 != set2
- -3: set1 decoder error
- -4: set2 decoder error

Based on [this](https://github.com/svpv/rpmss/blob/4256d86cc9ba1aa4ceb8c0f03f7d48675d9d27bb/set.h#L12) note, it is better to use `set1` as `Provides` for better performance.

### `set_new()`

Creates an empty `struct set` object - a container for symbol strings and their future hash values.
From the implementation:

> internally struct set is just a bag of strings and their hash values.

### `set_add()`

Adds a symbol string to the set.

Usage:

```c
set_add(s, "printf@@GLIBC_2.2.5");
set_add(s, "malloc@@GLIBC_2.2.5");
```

### `set_fini()`

Finalizes the set and returns the resulting set-version string.

It accepts `struct set *set` and `int bpp` as parameters.
If `bpp` < 10 or `bpp` > 32, the function returns `NULL`.

The function's operation is [described below](#operation-inside-set_fini).

### `set_free()`

Frees the struct set and its internal strings.

!!! NOTE The `struct set` structure itself, whose memory is allocated with `xmalloc` in `set_new()`, is currently not freed.

## Operation inside `set_fini()`

The main process of converting an array of strings into a set-string is as follows:

```
array of strings
  | (Jenkins OAAT)
  v
array of hashes
  | (qsort)
  v
sorted array of hashes
  | (calculate differences between elements)
  v
delta array
  | (Rice-Golomb transformation)
  v
bit array
  | (base62 transformation)
  v
set-string
```

### hash

For each element (string), a 32-bit hash is calculated and truncated to the lowest `bpp` bits:

```c
unsigned mask = (1u << bpp) - 1;
set->sv[i].v = hash(set->sv[i].s) & mask;
```

`Jenkins OAAT` is used as the hash function.

The resulting array is sorted by hash value in ascending order. On a collision, `warning: hash collision` is printed to `stderr`.

The `uniqv()` function leaves only unique values in the array; its return value is the size of the array after duplicates have been removed.

All subsequent transformations take place inside the `encode_set()` function. Its return value is the length of the resulting set-string, including bpp and Mshift as the first two characters.

A negative return value from `encode_set()` may be used as an error code.

### delta

Inside the `encode_delta()` function, an array of numbers sorted in ascending order is converted into an array of deltas between the numbers.
Example input array:

```c
unsigned *v = {1, 4, 16, 22};
```

Example resulting array:

```c
unsigned *v = {1, 3, 12, 6};
```

### Rice-Golomb

> `encode_golomb()` - main golomb encoding routine: package integers into bits.

Rice-Golomb coding is used to reduce the length of the resulting string. With the `Mshift` parameter, it represents a number `n` as `n >> Mshift`, encoded as a sequence of zeroes, and `n & (2^Mshift - 1)`, encoded in ordinary binary. The quotient and remainder are separated by a one bit.

`Mshift` is chosen as `bpp - log2(c) - 1`, because with `c` hashes uniformly distributed over the `2^bpp` range, the average `delta` is `2^bpp / c`.

Example:

```
Mshift = 8
n = 553
q = 553 >> 8 = 2
r = 553 & 255 = 42

bits = 00 1 00101010
```

The `encode_golomb()` function returns the length of the resulting bit sequence. The sequence itself is stored in `char *bitv` (`char = [1,0]`).

### base62

> `encode_base62()` - pack bitv into base62 string

The final step is to convert the bit sequence into a `base62` string.

The encoding alphabet is `0-9,a-z,A-Z`, but the `Z` character encodes 61, 62, and 63 as follows:

1. Two bits are added to the bit sequence after encoding `Z`:
   - `00` for 61
   - `01` for 62
   - `10` for 63
2. Encoding of the bit sequence then continues normally.

Note that this encoding cannot produce a `ZZ` sequence, because the `Z` character requires the two high bits to be set to `11`.

!!! Example is incorrect.

Transformation example:

```
Mshift = 5
bits = 1 01111 00 1 00010
```

Or, equivalently:

```
bits = 101111 001000 10
```

Bits are read least-significant bit first. For the first six bits:

```
101111 = 1*1 + 0*2 + 1*4 + 1*8 + 1*16 + 1*32 = 61 = Z
```

Two bits, `00`, are then inserted into the sequence immediately after the bits encoding Z.

```
bits = 101111 000010 0010
000010 = 16 = g
0010(00) = 4 = 4
```

The resulting `base62` string is:

```
10111100100010 = Zg4
```

## Comparing set-strings

When set-strings are compared, the transformation process is reversed until the hash values are obtained:

```
set-string
  | (inverse base62 transformation)
  v
bit array
  | (inverse Rice-Golomb transformation)
  v
delta array
  | (restore the original values)
  v
array of hashes
```

The hash arrays are then compared to determine whether the elements of one array are present in the other.

## Magic inside `rpmsetcmp()`

1. The `set:` prefix is removed from a string if present, and the `bpp` and `Mshift` values are decoded using `decode_set_init()`.
2. `set1` is decoded using `cache_decode_set()`.
3. `set2` is decoded using `decode_set()`.
4. For each hash array (`v1` and `v2`), two buffer arrays, `v(1|2)buf(A|B)`, are created for the `downsample_set()` function.
5. The `downsample_set()` function equalizes the `bpp` of both hash arrays to the smaller of `bpp1` and `bpp2`.
6. Two flags, `ge` and `le`, are created to determine set inclusion.
7. Set inclusion is checked using three macros: `IFGE`, `IFLT4`, and `IFLT8`.
8. A value is returned based on set inclusion:
   - 1: set1 > set2
   - 0: set1 == set2
   - -1: set1 < set2
   - -2: set1 != set2

### `decode_set()`

The intuitive transformation using the `decode_base62()`, `decode_golomb()`, and `decode_delta()` functions is shown under `if(0)` in the `decode_set()` function.

In practice, an optimized implementation using `decode_base62_golomb()` and `decode_delta()` is used. These functions convert the set-string directly into an array of `delta` values, from which the hash array is then restored.

### `decode_base62_golomb()`

`decode_base62_golomb()` is an optimized implementation of the `decode_base62` and `decode_golomb` stages. The function reads two bytes at a time, converts them to a bit sequence using the `word_to_num` table, and then decodes them with Rice-Golomb after accumulating up to 24 bits.

#### `enum`

> `enum` - word types (when two bytes from base62 string cast to unsigned short).

The code uses an `enum` to identify special cases when reading characters:

```c
enum {
    W_AA = 0x0000, // two ordinary characters (not used explicitly)
    W_AZ = 0x1000, // ordinary character + Z
    W_ZA = 0x2000, // Z + ordinary character
    W_A0 = 0x3000, // ordinary character + end of string
    W_0X = 0x4000, // end of string
    W_EE = 0xeeee, // impossible case
};
```

#### `CCI` macros

`CCI` is a macro that combines two characters into an index in the `word_to_num` table, taking byte order into account.

#### `word_to_num[]`

```c
static const unsigned short word_to_num[65536];
```

This is a precompiled lookup table that maps every combination of two bytes to a 12-bit representation plus an optional flag.

The table is built as follows:

1. All entries are filled with `W_EE` to mark them as invalid.
2. The `AA1` macro is used to build the `AA1x2`, `AA1x25`, and subsequent macros, up to `AA10x10` and its counterparts.
   - The final macros make it possible to fill the table quickly with values of the form `[CCI(c1, c2)] = (c1 - b1) | ((c2 - b2) << 6)`.
3. A similar filling process is used for `AZ`, with the `W_AZ` flag added to the table values.
4. The table is filled for `ZA` values with the `W_ZA` flag. Invalid high bits of `11` in the second character (that is, characters with values starting at 48) are checked only inside the functions; `W_EE` is not returned for such values.
5. The table is filled for `A0` and `0X` values with the `W_A0` and `W_0X` flags, respectively.

#### Reading

Using the `GetXX` macros, the function reads several bits (normally in blocks of up to 24 bits) and passes them to the decoder.

For 24 bits:

- Exactly four base62 characters (two pairs) are taken.
- The value is placed in a 32-bit `unsigned`.
- Given the `Mshift >= 7` constraint, at most three encoded Golomb numbers can fit into one sequence.

The "12-bit" reader is used when the first pair is ordinary and the second contains a special case.

The "10-bit" reader is used in Z-escape cases.

The "6-bit" reader is used for a single character.

#### Rice-Golomb decoder

The decoder is always in one of two states:

- `q-state`: looks for the unary prefix and the separating `1`
- `r-state`: collects the remaining Mshift bits of the remainder `r`

The decoded number is restored as

```c
value = (q << Mshift) | r;
```

#### Handling Z-escape

The `Esc1` and `Esc2` macros are used to handle Z-escape.

`Esc1` is invoked when a `Z` character is detected and reads the two high bits of the next character. As a result, `Esc1` obtains 10 bits for the Golomb decoder.

`Esc2` is invoked after an escape pair has been completed and determines what to do next:

- ordinary character - process six bits
- end of string - finish decoding
- invalid character - return an error
- another `Z` - switch to `Esc1` again

#### `QMake` and `RMake` macros

`QMake` looks for the separating one bit in the current block of bits. If the block contains only zeroes, all of them are added to `q`, after which the function reads the next block.

If the block contains a one, `__builtin_ffs(bits)` is used to determine the position of the first bit, which is the separator for the Golomb code. The remaining bits fill `r`.

`RMake` checks whether `Mshift` remainder bits have been collected and writes the completed value:

```c
*v++ = (q << Mshift) | r;
```

#### End of string

The string may end in `q-state`, but only with no more than five zeroes. It cannot end in `r-state`, because the `Mshift`-bit remainder has not been fully collected.

### `cache_decode_set()`

> `cache_decode_set()` - special decode_set version with LRU caching.

Returns the number of hash elements in the array and sets the `*pv` pointer to the hash array.

The following static arrays are created inside `cache_decode_set()`:

- `static unsigned hv[CACHE_SIZE]` - stores set-string fingerprints
- `static struct cache_ent *ev[CACHE_SIZE]` - stores complete information about decoded strings

The fingerprint is a simple value that can be calculated quickly:

```c
unsigned hash = str[0] | (str[2] << 8) | (str[3] << 16);
```

The `hv` and `ev` arrays form an LRU-like cache of size `CACHE_SIZE=256`.

If the fingerprint of the set-string being decoded matches an existing entry in the `hv` array, the full string is compared against `ev[i]->str`. On a hit, the entry is moved to position zero and the other entries are shifted.
If the full set-string comparison fails, the cache search continues.

If the entry is not found in the cache, the string is decoded by `decode_set()`, `SENTINELS=8` values are added, and it is placed in the cache as follows:

- If the cache is not full, the decoded string is written to the first free position.
- If the cache is full, the entry is placed at position `PIVOT_SIZE=243`, and entries starting at this position are shifted.

#### `SENTINELS` values

These `~0u` values are needed during the subsequent traversal of the array inside `rpmsetcmp()`, because that code jumps by four and eight elements.

### `downsample_set()`

> `downsample_set()` - reduce a set of (bpp + 1) values to a set of bpp values

The input array is `v`.
The resulting array is made available through the `w` pointer.
The return value is the number of elements in the new `w` array.

Because the original input array is sorted, after truncation to `bpp` bits the array is divided into two parts, each of which remains sorted. The array is split where the high bit at position `bpp+1` becomes `1`.

The two halves are then merged into the `w` buffer, and duplicate values are removed.

Example:

```text
v = [1, 3, 6, 8, 10, 14]
bpp = 3
mask = 7
v_mask = [1, 3, 6, 0, 2, 6]
w = [0, 1, 2, 3, 6]
return value = 5
```

### `IFGE`, `IFLT4`, and `IFLT8` macros

#### `IFLT*` macros

`IFLT8` advances `v1` quickly while `*v1 < v2val`. First, the macro makes jumps of eight elements, then refines the position with steps of four, two, and one.

```text
+8 +8 +8 ...  // coarse search
-4            // step back
±2            // refine
±1            // refine
+1 possibly   // final correction
```

As a result, `v1` points to the first element that is not less than `v2val`.

`IFLT4` performs similar work, but with a step of four. `IFLT8` is selected when the `v1` array is more than 16 times larger than the `v2` array.

#### `IFGE` macro

After `IFLT*`, the `*v1 < v2val` case is no longer possible.

Therefore, the remaining case is:

```c
*v1 > v2val
```

That is, the current `v2val` element is present in the second set but absent from the first.

Consequently, `v1` can no longer be a superset of `v2`:

```c
ge = 0;
v2++;
```

## `SELF_TEST` flag

When the `SELF_TEST` flag is set, the following happens:

1. `NDEBUG` is explicitly disabled so that `assert()` works.
2. The `test_*` functions are compiled.
3. A `main()` function that runs all `test_*` functions is compiled.

### `test_base62()`

Tests the operation of the following functions:

```c
encode_base62()
decode_base62()
```

### `test_golomb()`

Tests the operation of the following functions:

```c
encode_golomb()
decode_golomb()
```

### `test_word_table()`

In the `word_to_num[65536]` table used by `decode_base62_golomb()`, this test verifies that an `AA` sequence (two non-escape characters) is equal to `(char_to_num[i] | (char_to_num[j] << 6)`. For all other cases, it only checks that the value of one of the characters is greater than 61: `char_to_num[i] >= 61 || char_to_num[j] >= 61`.

### `test_base62_golomb()`

Tests the optimized combined `decode_base62_golomb()` decoder by comparing it with the reference implementations:

```c
decode_base62()
decode_golomb()
```

### `test_delta()`

Tests the operation of the following functions:

```c
encode_delta()
decode_delta()
```

### `test_set()`

Tests the complete encode/decode pipeline for a set of numbers. The test uses `bpp = 16`.

#### Note on `encode_set()`

The following lines appear inside `encode_set()`:

```c
#ifdef SELF_TEST
  decode_delta(c, v);
#endif
```

They are necessary because `test_set()` subsequently compares the original array with the array obtained after the pipeline.

### `test_api()`

Tests the public API:

```c
set_new()
set_add()
set_fini()
rpmsetcmp()
set_free()
```
