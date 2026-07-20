#!/usr/bin/env python3
"""Continuously compare set strings produced by set.c and newset.c."""

from __future__ import annotations

import random
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

# Test parameters. Edit these constants directly; the script has no CLI options.
MAX_N = 1_000
MAX_M = 100
BPP = 31
ALPHABET = ".0123456789@ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"
CC = "cc"
CFLAGS = ("-O2", "-std=gnu11", "-D_GNU_SOURCE")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
OLD_SET_SOURCE = REPO_ROOT / "set.c"
NEW_SET_SOURCE = REPO_ROOT / "reimplement" / "newset.c"
MKSET_SOURCE = SCRIPT_DIR / "mkset.c"
COMPAT_HEADER = SCRIPT_DIR / "newset_compat.h"
ERROR_DIR = SCRIPT_DIR / "error"

SET_HEADER = """\
#ifndef ARSV_SET_H
#define ARSV_SET_H

struct set;
struct set *set_new(void);
void set_add(struct set *set, const char *symbol);
const char *set_fini(struct set *set, int bpp);

#endif
"""


def build_mkset(source: Path, output: Path, build_dir: Path) -> None:
    command = [
        CC,
        *CFLAGS,
        f"-I{build_dir}",
        "-include",
        str(COMPAT_HEADER),
        str(source),
        str(MKSET_SOURCE),
        "-o",
        str(output),
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"Compilation failed: {shlex.join(command)}\n{completed.stderr}"
        )


def run_mkset(binary: Path, input_path: Path) -> str:
    with input_path.open("r", encoding="ascii") as input_file:
        completed = subprocess.run(
            [str(binary), str(BPP)],
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


def generate_input(path: Path, n: int, m: int) -> None:
    with path.open("w", encoding="ascii") as output:
        for _ in range(n):
            output.write("".join(random.choices(ALPHABET, k=m)))
            output.write("\n")


def main() -> None:
    if MAX_N < 1 or MAX_M < 1:
        raise ValueError("MAX_N and MAX_M must be at least 1")
    if not 10 <= BPP <= 32:
        raise ValueError("BPP must be in the range 10..32")

    ERROR_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="arsv-rpmsetinit-") as temporary:
        build_dir = Path(temporary)
        (build_dir / "rpmlib.h").touch()
        (build_dir / "system.h").touch()
        (build_dir / "set.h").write_text(SET_HEADER, encoding="ascii")

        mkset = build_dir / "mkset"
        mkset_new = build_dir / "mkset_new"
        input_path = build_dir / "input.txt"

        build_mkset(OLD_SET_SOURCE, mkset, build_dir)
        build_mkset(NEW_SET_SOURCE, mkset_new, build_dir)

        tests_completed = 0
        while True:
            n = random.randint(1, MAX_N)
            m = random.randint(1, MAX_M)
            print(f"tests={tests_completed} n={n} m={m}", flush=True)

            generate_input(input_path, n, m)
            new_result = run_mkset(mkset_new, input_path)
            old_result = run_mkset(mkset, input_path)

            tests_completed += 1
            if new_result != old_result:
                error_path = ERROR_DIR / (
                    f"test_{tests_completed}_n{n}_m{m}_{time.time_ns()}.txt"
                )
                error_path.write_bytes(input_path.read_bytes())
                print(f"mismatch: saved input to {error_path}", flush=True)


if __name__ == "__main__":
    main()
