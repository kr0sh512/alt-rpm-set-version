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


def rpmsetcmp():
    pass


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
