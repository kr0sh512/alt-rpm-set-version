#!/usr/bin/env python3
"""Continuously compare rpmsetcmp results from set.c and newset.c."""

from __future__ import annotations

import random
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MAX_N = 1000  # max words in file
MAX_M = 100  # max len for word
MIN_BPP = 10
MAX_BPP = 32
ALPHABET = ".0123456789@ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"
LARGE_ALPHABET = "".join(chr(code) for code in range(33, 127))
CASES = (1, 0, -2, -3)
CC = "cc"
CFLAGS = ("-O2", "-std=gnu11", "-D_GNU_SOURCE")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
OLD_SET_SOURCE = REPO_ROOT / "set.c"
NEW_SET_SOURCE = REPO_ROOT / "reimplement" / "newset.c"
MKSET_SOURCE = SCRIPT_DIR / "mkset.c"
SETCMP_SOURCE = SCRIPT_DIR / "setcmp.c"
COMPAT_HEADER = SCRIPT_DIR / "newset_compat.h"
ERROR_DIR = SCRIPT_DIR / "error"

SET_HEADER = """\
#ifndef ARSV_SET_H
#define ARSV_SET_H

struct set;
struct set *set_new(void);
void set_add(struct set *set, const char *symbol);
const char *set_fini(struct set *set, int bpp);
int rpmsetcmp(const char *set1, const char *set2);

#endif
"""


@dataclass(frozen=True)
class Outcome:
    result: int | None
    returncode: int
    stdout: str
    stderr: str

    def comparison_key(self) -> tuple[object, ...]:
        if self.result is not None:
            return ("result", self.result)
        return ("process", self.returncode, self.stdout, self.stderr)

    def describe(self) -> str:
        if self.result is not None:
            return str(self.result)
        return (
            f"exit={self.returncode}, stdout={self.stdout!r}, "
            f"stderr={self.stderr!r}"
        )


def build_binary(
    source: Path, wrapper: Path, output: Path, build_dir: Path
) -> None:
    command = [
        CC,
        *CFLAGS,
        f"-I{build_dir}",
        "-include",
        str(COMPAT_HEADER),
        str(source),
        str(wrapper),
        "-o",
        str(output),
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"Compilation failed: {shlex.join(command)}\n{completed.stderr}"
        )


def run_mkset(binary: Path, input_path: Path, bpp: int) -> str:
    with input_path.open("r", encoding="ascii") as input_file:
        completed = subprocess.run(
            [str(binary), str(bpp)],
            stdin=input_file,
            text=True,
            capture_output=True,
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{binary.name} failed with code {completed.returncode}:\n"
            f"{completed.stderr}"
        )
    return completed.stdout.strip()


def run_setcmp(binary: Path, set1: str, set2: str) -> Outcome:
    completed = subprocess.run(
        [str(binary), set1, set2],
        text=True,
        capture_output=True,
    )
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    result: int | None = None

    if completed.returncode == 0:
        try:
            result = int(stdout)
        except ValueError:
            pass
    elif completed.returncode == 1:
        if "set1 error" in stderr:
            result = -3
        elif "set2 error" in stderr:
            result = -4

    return Outcome(result, completed.returncode, stdout, stderr)


def random_word(alphabet: str = ALPHABET) -> str:
    length = random.randint(1, MAX_M)
    return "".join(random.choices(alphabet, k=length))


def generate_words(n: int) -> list[str]:
    words: list[str] = []
    used: set[str] = set()
    while len(words) < n:
        word = random_word()
        if word not in used:
            used.add(word)
            words.append(word)
    return words


def write_words(path: Path, words: list[str]) -> None:
    path.write_text("".join(f"{word}\n" for word in words), encoding="ascii")


def generate_second_words(case: int, words: list[str]) -> list[str]:
    if case == 0:
        return words.copy()

    remove_count = random.randint(1, len(words) - 1)
    removed_indices = set(random.sample(range(len(words)), remove_count))
    second_words = [
        word for index, word in enumerate(words) if index not in removed_indices
    ]

    if case == -2:
        used = set(words)
        add_count = random.randint(1, remove_count)
        while add_count > 0:
            word = random_word()
            if word not in used:
                used.add(word)
                second_words.append(word)
                add_count -= 1
        random.shuffle(second_words)

    return second_words


def generate_invalid_set(alphabet: str) -> str:
    invalid_first_chars = [character for character in alphabet if character not in "cdefghijklmnopqrstuvwxyz"]
    first = random.choice(invalid_first_chars)
    length = random.randint(1, MAX_M)
    tail = "".join(random.choices(alphabet, k=length - 1))
    return f"set:{first}{tail}"


def compare_pair(
    label: str,
    setcmp: Path,
    setcmp_new: Path,
    old_set1: str,
    old_set2: str,
    new_set1: str,
    new_set2: str,
) -> tuple[str, Outcome, Outcome]:
    old_outcome = run_setcmp(setcmp, old_set1, old_set2)
    new_outcome = run_setcmp(setcmp_new, new_set1, new_set2)
    return label, old_outcome, new_outcome


def save_error(
    test_number: int,
    case: int,
    bpp: int,
    artifacts: dict[str, bytes],
    comparisons: list[tuple[str, Outcome, Outcome]],
) -> None:
    timestamp = time.time_ns()
    case_name = str(case).replace("-", "minus")
    stem = f"test_{test_number}_case_{case_name}_bpp{bpp}_{timestamp}"

    for suffix, content in artifacts.items():
        (ERROR_DIR / f"{stem}_{suffix}").write_bytes(content)

    details = []
    for label, old_outcome, new_outcome in comparisons:
        details.append(
            f"{label}: setcmp={old_outcome.describe()} "
            f"setcmp_new={new_outcome.describe()}"
        )
    (ERROR_DIR / f"{stem}_results.txt").write_text(
        "\n".join(details) + "\n", encoding="utf-8"
    )
    print(f"mismatch: saved inputs as {ERROR_DIR / stem}_*", flush=True)


def run_relation_case(
    case: int,
    test_number: int,
    n: int,
    bpp: int,
    input1_path: Path,
    input2_path: Path,
    mkset: Path,
    mkset_new: Path,
    setcmp: Path,
    setcmp_new: Path,
) -> None:
    words1 = generate_words(n)
    words2 = generate_second_words(case, words1)
    write_words(input1_path, words1)
    write_words(input2_path, words2)

    old_set1 = run_mkset(mkset, input1_path, bpp)
    new_set1 = run_mkset(mkset_new, input1_path, bpp)
    old_set2 = run_mkset(mkset, input2_path, bpp)
    new_set2 = run_mkset(mkset_new, input2_path, bpp)

    comparisons = [
        compare_pair(
            str(case),
            setcmp,
            setcmp_new,
            old_set1,
            old_set2,
            new_set1,
            new_set2,
        )
    ]
    if case == 1:
        comparisons.append(
            compare_pair(
                "-1 (swapped)",
                setcmp,
                setcmp_new,
                old_set2,
                old_set1,
                new_set2,
                new_set1,
            )
        )

    mismatches = [
        comparison
        for comparison in comparisons
        if comparison[1].comparison_key() != comparison[2].comparison_key()
    ]
    if mismatches:
        save_error(
            test_number,
            case,
            bpp,
            {
                "input1.txt": input1_path.read_bytes(),
                "input2.txt": input2_path.read_bytes(),
            },
            comparisons,
        )


def run_invalid_case(
    test_number: int,
    n: int,
    bpp: int,
    input_path: Path,
    mkset: Path,
    mkset_new: Path,
    setcmp: Path,
    setcmp_new: Path,
    invalid_alphabet: str | None = None,
) -> None:
    words = generate_words(n)
    write_words(input_path, words)
    old_valid_set = run_mkset(mkset, input_path, bpp)
    new_valid_set = run_mkset(mkset_new, input_path, bpp)

    if invalid_alphabet is None:
        invalid_alphabet = random.choice((ALPHABET, LARGE_ALPHABET))
    invalid_set = generate_invalid_set(invalid_alphabet)
    alphabet_name = "same" if invalid_alphabet == ALPHABET else "large"

    comparisons = [
        compare_pair(
            f"-3 ({alphabet_name} alphabet)",
            setcmp,
            setcmp_new,
            invalid_set,
            old_valid_set,
            invalid_set,
            new_valid_set,
        ),
        compare_pair(
            f"-4 ({alphabet_name} alphabet, swapped)",
            setcmp,
            setcmp_new,
            old_valid_set,
            invalid_set,
            new_valid_set,
            invalid_set,
        ),
    ]

    mismatches = [
        comparison
        for comparison in comparisons
        if comparison[1].comparison_key() != comparison[2].comparison_key()
    ]
    if mismatches:
        save_error(
            test_number,
            -3,
            bpp,
            {
                "input.txt": input_path.read_bytes(),
                "invalid_set.txt": f"{invalid_set}\n".encode("ascii"),
            },
            comparisons,
        )


def main() -> None:
    if MAX_N < 2 or MAX_M < 1:
        raise ValueError("MAX_N must be at least 2 and MAX_M at least 1")
    if not 10 <= MIN_BPP <= MAX_BPP <= 32:
        raise ValueError("MIN_BPP and MAX_BPP must be in the range 10..32")

    ERROR_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="arsv-rpmsetcmp-") as temporary:
        build_dir = Path(temporary)
        (build_dir / "rpmlib.h").touch()
        (build_dir / "system.h").touch()
        (build_dir / "set.h").write_text(SET_HEADER, encoding="ascii")

        mkset = build_dir / "mkset"
        mkset_new = build_dir / "mkset_new"
        setcmp = build_dir / "setcmp"
        setcmp_new = build_dir / "setcmp_new"
        input1_path = build_dir / "input1.txt"
        input2_path = build_dir / "input2.txt"

        build_binary(OLD_SET_SOURCE, MKSET_SOURCE, mkset, build_dir)
        build_binary(NEW_SET_SOURCE, MKSET_SOURCE, mkset_new, build_dir)
        build_binary(OLD_SET_SOURCE, SETCMP_SOURCE, setcmp, build_dir)
        build_binary(NEW_SET_SOURCE, SETCMP_SOURCE, setcmp_new, build_dir)

        tests_completed = 0
        while True:
            case = random.choice(CASES)
            min_n = 2 if case in (1, -2) else 1
            n = random.randint(min_n, MAX_N)
            bpp = random.randint(MIN_BPP, MAX_BPP)
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"{current_time} tests={tests_completed} case={case} "
                f"bpp={bpp} n={n}",
                flush=True,
            )

            if case == -3:
                run_invalid_case(
                    tests_completed + 1,
                    n,
                    bpp,
                    input1_path,
                    mkset,
                    mkset_new,
                    setcmp,
                    setcmp_new,
                )
            else:
                run_relation_case(
                    case,
                    tests_completed + 1,
                    n,
                    bpp,
                    input1_path,
                    input2_path,
                    mkset,
                    mkset_new,
                    setcmp,
                    setcmp_new,
                )

            tests_completed += 1


if __name__ == "__main__":
    main()
