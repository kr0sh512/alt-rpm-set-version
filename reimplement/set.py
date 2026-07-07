# set.py - base64, golomb and set-string routines. analog set.c used in alt-rpm

import hashlib
from typing import List
from typing import Tuple
from dataclasses import dataclass


@dataclass
class Set:
    """just a bag of strings and their hash values.

    Attributes:
        cnt (int): The number of elements in the set.
        labels (List[Tuple[str, int]]): A list of tuples, where each tuple contains a string and its corresponding hash value.
    """

    cnt: int
    labels: List[Tuple[str, int]]  # List of tuples (string, hash value)


def check_bpp(bpp: int) -> bool:
    """Check if the bits per pixel (bpp) value is within the valid range."""
    return 2 <= bpp <= 64


def check_Mshift(Mshift: int) -> bool:
    """Check if the Mshift value is within the valid range."""
    return 1 <= Mshift <= 63


def int_to_char(value: int) -> str:
    """Convert an integer value to a base64 character."""
    if 0 <= value <= 9:
        return chr(value + ord("0"))
    elif 10 <= value <= 35:
        return chr(value - 10 + ord("a"))
    elif 36 <= value <= 61:
        return chr(value - 36 + ord("A"))
    elif value == 62:
        return "+"
    elif value == 63:
        return "/"
    else:
        raise ValueError("Value must be in the range [0, 63] for base64 encoding.")


def char_to_int(char: str) -> int:
    """Convert a base64 character to its corresponding integer value."""
    if "0" <= char <= "9":
        return ord(char) - ord("0")
    elif "a" <= char <= "z":
        return ord(char) - ord("a") + 10
    elif "A" <= char <= "Z":
        return ord(char) - ord("A") + 36
    elif char == "+":
        return 62
    elif char == "/":
        return 63
    else:
        raise ValueError("Character must be a valid base64 character.")


def encode_golomb_Mshift(cnt: int, bpp: int) -> int:
    """Calculate Mshift paramter for encoding."""
    Mshift = bpp - (cnt.bit_length() - 1) - 1

    Mshift = max(Mshift, 1)
    Mshift = min(Mshift, 63)
    assert Mshift < bpp, "Mshift must be less than bpp"

    return Mshift


def encode_delta(hash_values: List[int]) -> List[int]:
    """Encode the hash values into delta values."""
    if not hash_values:
        return []

    delta_values = [hash_values[0]]
    for i in range(1, len(hash_values)):
        delta = hash_values[i] - hash_values[i - 1]
        delta_values.append(delta)

    return delta_values


def encode_golomb(delta_values: List[int], Mshift: int) -> List[int]:
    """Main golomb encoding routine: package integers into bits."""
    mask = (1 << Mshift) - 1
    golomb_encoded = []

    for val in delta_values:
        q = val >> Mshift  # quotient
        r = val & mask  # remainder

        for _ in range(q):
            golomb_encoded.append(0)  # "unary" part
        golomb_encoded.append(1)  # separator

        for i in range(Mshift):
            golomb_encoded.append((r >> i) & 1)  # binary part

    return golomb_encoded


def encode_base64(bits: List[int]) -> str:
    """Encode a list of bits into a base64 string."""
    base64_chars = []

    for i in range(0, len(bits), 6):
        chunk = bits[i : i + 6]

        while len(chunk) < 6:
            chunk.append(0)  # pad with zeros if necessary

        value = sum(bit << (5 - j) for j, bit in enumerate(chunk))
        base64_chars.append(int_to_char(value))

    return "".join(base64_chars)


def encode_set(hash_values: List[int], bpp: int) -> str:
    """Encode the set of hash values into a set-string representation using base64 and Golomb coding."""

    cnt = len(hash_values)
    Mshift = encode_golomb_Mshift(cnt, bpp)

    assert check_bpp(bpp), "bpp must be between 2 and 64"
    assert check_Mshift(Mshift), "Mshift must be between 1 and 63"

    delta_values = encode_delta(hash_values)

    bits = encode_golomb(delta_values, Mshift)

    base64_chars = encode_base64(bits)

    return int_to_char(bpp) + int_to_char(Mshift) + base64_chars


def decode_base64(encoded_str: str) -> List[int]:
    """Decode a base64 string into a list of bits."""
    bits = []

    for char in encoded_str:
        value = char_to_int(char)
        for i in range(6):
            bits.append((value >> (5 - i)) & 1)

    return bits


def decode_golomb(bits: List[int], Mshift: int) -> List[int]:
    """Decode a list of bits encoded with Golomb coding into delta values."""
    delta_values = []
    i = 0

    while i < len(bits):
        q = 0

        while i < len(bits) and bits[i] == 0:
            q += 1
            i += 1

        if i >= len(bits):
            if q > 5:
                raise ValueError("Invalid Golomb encoding: too many leading zeros.")
            break

        i += 1  # skip the separator bit

        r = 0
        for j in range(Mshift):
            if i < len(bits):
                if bits[i] == 1:
                    r |= 1 << j
            else:
                i += 1

        delta_value = (q << Mshift) | r
        delta_values.append(delta_value)

    return delta_values


def decode_delta(delta_values: List[int]) -> List[int]:
    """Decode a list of delta values into hash values."""
    last_hash = delta_values[0]
    hash_values = [last_hash]

    for i in range(1, len(delta_values)):
        hash_values.append(hash_values[i - 1] + delta_values[i])

    return hash_values


def downsample_set(hash_values: List[int], bpp: int) -> List[int]:
    """Reduce a set of (bpp + 1) values to a set of bpp values, keeping it sorted."""
    # find the first element with high bit set
    mask = (1 << bpp) - 1
    l = 0
    u = len(hash_values)
    while l < u:
        i = (l + u) // 2
        if hash_values[i] <= mask:
            l = i + 1
        else:
            u = i

    # merge v1 and v2 into merged, keeping it sorted
    merged = []
    i = 0
    j = l
    while i < l and j < len(hash_values):
        high_value = hash_values[j] & mask
        if hash_values[i] < high_value:
            merged.append(hash_values[i])
            i += 1
        elif high_value < hash_values[i]:
            merged.append(high_value)
            j += 1
        else:
            merged.append(hash_values[i])
            i += 1
            j += 1

    while i < l:
        merged.append(hash_values[i])
        i += 1

    while j < len(hash_values):
        merged.append(hash_values[j] & mask)
        j += 1

    return merged


def decode_set_init(encoded_str: str) -> Tuple[int, int]:
    """Decode the initial part of the set-string to extract bpp and Mshift."""
    if len(encoded_str) < 2:
        raise ValueError("Encoded string is too short to contain bpp and Mshift.")

    bpp = char_to_int(encoded_str[0])
    Mshift = char_to_int(encoded_str[1])

    assert check_bpp(bpp), "bpp must be between 2 and 64"
    assert check_Mshift(Mshift), "Mshift must be between 1 and 63"

    return bpp, Mshift


def decode_set(encoded_str: str, Mshift: int) -> List[int]:
    """Decode the set-string representation into a list of hash values."""
    data_str = encoded_str[2:]

    str_bits = decode_base64(data_str)
    str_delta = decode_golomb(str_bits, Mshift)
    hash_values = decode_delta(str_delta)

    return hash_values


def rpmsetcmp(str1: str, str2: str) -> int:
    if str1[:3] == "set:":
        str1 = str1[3:]
    if str2[:3] == "set:":
        str2 = str2[3:]

    bpp1, Mshift1 = decode_set_init(str1)
    bpp2, Mshift2 = decode_set_init(str2)
    hash_values1 = decode_set(str1, Mshift1)
    hash_values2 = decode_set(str2, Mshift2)

    while bpp1 > bpp2:
        hash_values1 = downsample_set(hash_values1, bpp1)
        bpp1 -= 1

    while bpp2 > bpp1:
        hash_values2 = downsample_set(hash_values2, bpp2)
        bpp2 -= 1

    # WIP

    ge = True
    le = True

    i = 0
    j = 0

    while i < len(hash_values1) and j < len(hash_values2):
        if hash_values1[i] < hash_values2[j]:
            ge = False
            i += 1
        elif hash_values1[i] > hash_values2[j]:
            le = False
            j += 1
        else:
            i += 1
            j += 1

    if ge and le:
        return 0
    elif ge:
        return 1
    else:
        return -1


def set_new() -> Set:
    """Create a new Set instance with cnt initialized to 0 and an empty list of labels."""
    return Set(cnt=0, labels=[])


def set_add(set: Set, label: str) -> None:
    """Add a label to the set."""
    hash_value = 0
    set.labels.append((label, hash_value))
    set.cnt += 1


def set_free(set: Set) -> None:
    """Free the resources associated with the set."""
    set.labels.clear()
    set.cnt = 0


def hash(label: str) -> int:
    """Compute a stable 64-bit hash value for an ASCII label."""
    digest = hashlib.blake2b(label.encode("ascii"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="little", signed=False)


def set_fini(set: Set, bpp: int) -> str | None:
    """Finalize the set and return a set-string representation."""

    if set.cnt < 1:
        return None

    assert check_bpp(bpp), "bpp must be between 2 and 64"

    mask = (1 << bpp) - 1

    # calculate hash values for each label and store them in the set
    for i in range(set.cnt):
        label, _ = set.labels[i]
        computed_hash = hash(label) & mask
        set.labels[i] = (label, computed_hash)

    set.labels.sort(key=lambda x: x[1])  # Sort by hash value

    # warn on hash collisions
    for i in range(1, set.cnt):
        if set.labels[i][1] == set.labels[i - 1][1]:
            print(
                f"Warning: Hash collision detected for labels '{set.labels[i][0]}' and '{set.labels[i - 1][0]}'"
            )

    hash_values = [label_hash for _, label_hash in set.labels]

    encoded_string = encode_set(hash_values, bpp)

    return encoded_string


if __name__ == "__main__":
    # Example usage
    my_set = set_new()
    set_add(my_set, "example_label_1")
    set_add(my_set, "example_label_2")
    result = set_fini(my_set, 8)
    print(result)
    set_free(my_set)

    # example with comparison
    set1 = set_new()
    set_add(set1, "label1")
    set_add(set1, "label2")
    str1 = set_fini(set1, 8)
    set2 = set_new()
    set_add(set2, "label1")
    set_add(set2, "label2")
    set_add(set2, "label3")
    str2 = set_fini(set2, 8)
    assert str1 is not None and str2 is not None, "Set strings should not be None"
    comparison_result = rpmsetcmp(str1, str2)
    assert comparison_result == -1, "Expected str1 to be less than str2"
    print(comparison_result)
