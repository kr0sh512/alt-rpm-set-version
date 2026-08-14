#!/usr/bin/env python3
"""Build bit-probability maps for hash functions and word mutation types.

For every hash listed in HASHES, the script compares each source word hash with
hashes of generated similar words.  A table cell contains the probability that
the corresponding output bit changed (XOR with its source word hash).
"""

from __future__ import annotations

import argparse
import csv
import re
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from generate_input import (
    DEFAULT_ALPHABET,
    MUTATIONS,
    MutationError,
    generate_words,
    parse_operation,
)

ROOT = Path(__file__).resolve().parent
HASH_FUNCS_DIR = ROOT / "hash_funcs"
DEFAULT_OUTPUT_DIR = ROOT / "probability_maps"

# Add directory names from hash_funcs here to include more implementations.
HASHES = [
    "jenkinsOAAT",
    "xxh64",
]

HEX_HASH = re.compile(r"(?:0[xX])?([0-9a-fA-F]+)")
ProbabilityRow = tuple[int, list[float]]


class HashToolError(RuntimeError):
    """Raised when a hash executable cannot be prepared or invoked."""


def prepare_hash(hash_name: str, hash_funcs_dir: Path = HASH_FUNCS_DIR) -> Path:
    """Return an executable hash tool, building or preparing it if necessary."""
    if not hash_name or Path(hash_name).name != hash_name:
        raise HashToolError(f"некорректное имя хэша: {hash_name!r}")

    hash_directory = hash_funcs_dir / hash_name
    if not hash_directory.is_dir():
        raise HashToolError(f"не найдена папка хэша: {hash_directory}")

    binary = hash_directory / "bin_hash"
    if binary.is_file():
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        return binary

    sources = (
        (hash_directory / "bin_hash.c", "cc"),
        (hash_directory / "bin_hash.cpp", "c++"),
        (hash_directory / "bin_hash.cc", "c++"),
        (hash_directory / "bin_hash.cxx", "c++"),
    )
    for source, compiler in sources:
        if not source.is_file():
            continue
        command = [
            compiler,
            "-O2",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            str(source),
            "-o",
            str(binary),
        ]
        if compiler == "cc":
            command[1:1] = ["-std=c11"]
        else:
            command[1:1] = ["-std=c++17"]
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip()
            raise HashToolError(f"не удалось скомпилировать {source}: {details}")
        return binary

    python_source = hash_directory / "bin_hash.py"
    if python_source.is_file():
        content = python_source.read_text(encoding="utf-8")
        if not content.startswith("#!"):
            python_source.write_text(
                "#!/usr/bin/env python3\n" + content,
                encoding="utf-8",
            )
        python_source.chmod(
            python_source.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        return python_source

    raise HashToolError(
        f"для {hash_name} не найден bin_hash, bin_hash.c/cpp/cc/cxx или bin_hash.py"
    )


def run_hash(executable: Path, word: str) -> tuple[int, int]:
    """Run a hash tool and return its integer value and explicit output width."""
    try:
        result = subprocess.run(
            [str(executable), word],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise HashToolError(f"не удалось запустить {executable}: {error}") from error

    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        raise HashToolError(
            f"{executable} завершился с кодом {result.returncode}: {details}"
        )

    output = result.stdout.strip()
    match = HEX_HASH.fullmatch(output)
    if match is None:
        raise HashToolError(f"{executable} вернул не шестнадцатеричный хэш: {output!r}")

    digits = match.group(1)
    return int(digits, 16), len(digits) * 4


def bit_probabilities(
    source_hash: int, changed_hashes: Sequence[int], bits: int
) -> list[float]:
    """Calculate per-bit change probabilities, ordered from LSB to MSB."""
    if bits < 1:
        raise ValueError("число бит должно быть положительным")
    if not changed_hashes:
        raise ValueError("список изменённых хэшей не должен быть пустым")

    changed_counts = [0] * bits
    for changed_hash in changed_hashes:
        difference = source_hash ^ changed_hash
        for bit in range(bits):
            changed_counts[bit] += (difference >> bit) & 1

    sample_count = len(changed_hashes)
    return [count / sample_count for count in changed_counts]


def write_csv_table(stream: TextIO, rows: Mapping[str, ProbabilityRow]) -> None:
    """Write one operation-by-bit probability table as CSV."""
    if not rows:
        raise ValueError("таблица вероятностей не должна быть пустой")

    widths = {len(probabilities) for _pair_count, probabilities in rows.values()}
    if len(widths) != 1:
        raise ValueError("все строки таблицы должны иметь одинаковое число бит")
    bits = widths.pop()

    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["operation", "pairs", *(f"bit_{bit}" for bit in range(bits))])
    for operation, (pair_count, probabilities) in rows.items():
        writer.writerow(
            [
                operation,
                pair_count,
                *(f"{probability:.6f}" for probability in probabilities),
            ]
        )


def build_probability_table(
    executable: Path,
    sources: Sequence[str],
    operations: Sequence[str],
    count: int,
    operation_count: int,
    alphabet: str,
    seed: int | None,
    max_attempts: int | None,
) -> dict[str, ProbabilityRow]:
    """Aggregate avalanche probabilities across all source words."""
    if not sources:
        raise ValueError("нужно указать хотя бы одно исходное слово")

    source_hashes: list[int] = []
    bits: int | None = None
    for source in sources:
        source_hash, source_bits = run_hash(executable, source)
        if bits is None:
            bits = source_bits
        elif source_bits != bits:
            raise HashToolError(
                f"{executable} вернул хэши разной ширины: "
                f"{bits} и {source_bits} бит"
            )
        source_hashes.append(source_hash)

    assert bits is not None
    table: dict[str, ProbabilityRow] = {}

    for operation in operations:
        differences: list[int] = []
        for source, source_hash in zip(sources, source_hashes, strict=True):
            words = generate_words(
                source=source,
                count=count,
                operation=operation,
                operation_count=operation_count,
                alphabet=alphabet,
                seed=seed,
                max_attempts=max_attempts,
            )
            if len(words) < count:
                print(
                    f"warning: operation={operation} word={source!r}: "
                    f"generated {len(words)} of at most {count} unique words",
                    file=sys.stderr,
                )
            for word in words:
                changed_hash, changed_bits = run_hash(executable, word)
                if changed_bits != bits:
                    raise HashToolError(
                        f"{executable} вернул хэши разной ширины: "
                        f"{bits} и {changed_bits} бит"
                    )
                differences.append(source_hash ^ changed_hash)

        if differences:
            table[operation] = (
                len(differences),
                bit_probabilities(0, differences, bits),
            )
        else:
            table[operation] = (0, [float("nan")] * bits)

    return table


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Строит для каждого хэша CSV-таблицу вероятностей изменения "
            "выходных битов. bit_0 — младший бит."
        )
    )
    parser.add_argument(
        "words",
        nargs="+",
        help="одно или несколько исходных ASCII-слов",
    )
    parser.add_argument(
        "-o",
        "--operation",
        action="append",
        type=parse_operation,
        help=(
            "тип изменения (номер или имя как в generate_input.py); "
            "можно повторять, по умолчанию используются все типы"
        ),
    )
    parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=10,
        help=(
            "верхняя граница числа уникальных изменённых слов для каждого "
            "исходного слова и типа (по умолчанию: 10)"
        ),
    )
    parser.add_argument(
        "-k",
        "--operations",
        type=int,
        default=1,
        help="число операций над каждым словом (по умолчанию: 1)",
    )
    parser.add_argument(
        "--alphabet",
        default=DEFAULT_ALPHABET,
        help="алфавит для добавления и замены",
    )
    parser.add_argument(
        "--seed", type=int, help="seed генератора для воспроизводимого результата"
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        help="предельное число попыток собрать уникальные слова",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=("каталог для CSV-таблиц " f"(по умолчанию: {DEFAULT_OUTPUT_DIR})"),
    )
    parser.add_argument(
        "--hash",
        dest="hashes",
        action="append",
        help="проверить только указанный хэш; можно повторять (по умолчанию HASHES)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.count < 1:
        parser.error("--count должен быть положительным")
    if args.operations < 1:
        parser.error("--operations должен быть положительным")
    if args.max_attempts is not None and args.max_attempts < 1:
        parser.error("--max-attempts должен быть положительным")

    operations = list(dict.fromkeys(args.operation or MUTATIONS.keys()))
    hashes = list(dict.fromkeys(args.hashes or HASHES))
    if not hashes:
        parser.error("массив HASHES не должен быть пустым")

    args.output.mkdir(parents=True, exist_ok=True)
    try:
        for hash_name in hashes:
            executable = prepare_hash(hash_name)
            table = build_probability_table(
                executable=executable,
                sources=args.words,
                operations=operations,
                count=args.count,
                operation_count=args.operations,
                alphabet=args.alphabet,
                seed=args.seed,
                max_attempts=args.max_attempts,
            )
            output_path = args.output / f"{hash_name}.csv"
            with output_path.open("w", encoding="utf-8", newline="") as stream:
                write_csv_table(stream, table)
            print(f"wrote {output_path}")
    except (HashToolError, MutationError, ValueError) as error:
        parser.error(str(error))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
