#!/usr/bin/env python3
"""Measure P(hash output bit = 1) over a line-oriented ASCII corpus."""

from __future__ import annotations

import argparse
import csv
import hashlib
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import TextIO

from plot_probability_map import ProbabilityRow, render_bit_probabilities
from probability_map import HASHES, HASH_FUNCS_DIR

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = ROOT / "corpus_bit_distribution"


class DistributionError(RuntimeError):
    """Raised when a corpus or hash adapter cannot be processed."""


@dataclass(frozen=True)
class HashSpec:
    bits: int
    expression: str


HASH_SPECS = {
    "jenkinsOAAT": HashSpec(32, "jenkins_oaat(word, length)"),
    "xxh64": HashSpec(64, "xxh64(word, length, XXH64_SEED)"),
    "t1ha2": HashSpec(64, "t1ha2_atonce(word, length, T1HA2_SEED)"),
}


@dataclass(frozen=True)
class Distribution:
    hash_name: str
    samples: int
    bits: int
    ones: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.samples < 1:
            raise ValueError("число образцов должно быть положительным")
        if self.bits < 1 or len(self.ones) != self.bits:
            raise ValueError("число счётчиков должно совпадать с шириной хэша")
        if any(count < 0 or count > self.samples for count in self.ones):
            raise ValueError("счётчик единиц должен находиться между 0 и samples")

    @property
    def probabilities(self) -> tuple[float, ...]:
        return tuple(count / self.samples for count in self.ones)


def c_include_path(path: Path) -> str:
    """Return a C string-safe absolute include path."""
    return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')


def make_adapter_source(source: Path, spec: HashSpec) -> str:
    """Build a C program that aggregates bit counts using an existing hash source."""
    return f'''#define _POSIX_C_SOURCE 200809L
#define main arsv_original_hash_cli_main
#include "{c_include_path(source)}"
#undef main
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>

int main(void)
{{
    char *buffer = NULL;
    size_t capacity = 0;
    uint64_t samples = UINT64_C(0);
    uint64_t ones[{spec.bits}] = {{UINT64_C(0)}};
    ssize_t bytes_read;

    while ((bytes_read = getline(&buffer, &capacity, stdin)) >= 0) {{
        size_t length = (size_t)bytes_read;
        const unsigned char *word = (const unsigned char *)buffer;

        while (length > 0U && is_ascii_trailing_space(word[length - 1U])) {{
            --length;
        }}
        if (length == 0U) {{
            fprintf(stderr, "corpus contains an empty line\\n");
            free(buffer);
            return EXIT_FAILURE;
        }}
        for (size_t index = 0; index < length; ++index) {{
            if (word[index] > 0x7fU) {{
                fprintf(stderr, "corpus must contain ASCII characters only\\n");
                free(buffer);
                return EXIT_FAILURE;
            }}
        }}
        if (samples == UINT64_MAX) {{
            fprintf(stderr, "sample counter overflow\\n");
            free(buffer);
            return EXIT_FAILURE;
        }}

        uint64_t hash = (uint64_t)({spec.expression});
        for (unsigned int bit = 0U; bit < {spec.bits}U; ++bit) {{
            ones[bit] += (hash >> bit) & UINT64_C(1);
        }}
        ++samples;
    }}
    if (ferror(stdin)) {{
        fprintf(stderr, "failed to read corpus\\n");
        free(buffer);
        return EXIT_FAILURE;
    }}
    free(buffer);

    printf("%" PRIu64 ",{spec.bits}", samples);
    for (unsigned int bit = 0U; bit < {spec.bits}U; ++bit) {{
        printf(",%" PRIu64, ones[bit]);
    }}
    putchar('\\n');
    return EXIT_SUCCESS;
}}
'''


def measure_hash(
    hash_name: str,
    corpus: Path,
    hash_funcs_dir: Path = HASH_FUNCS_DIR,
) -> Distribution:
    """Compile a batch adapter and count set output bits for every corpus line."""
    try:
        spec = HASH_SPECS[hash_name]
    except KeyError as error:
        supported = ", ".join(HASH_SPECS)
        raise DistributionError(
            f"нет batch-адаптера для {hash_name!r}; доступны: {supported}"
        ) from error

    source = hash_funcs_dir / hash_name / "bin_hash.c"
    if not source.is_file():
        raise DistributionError(f"не найден исходник хэша: {source}")
    if not corpus.is_file():
        raise DistributionError(f"не найден corpus: {corpus}")

    with tempfile.TemporaryDirectory(prefix=f"arsv-{hash_name}-") as temporary:
        root = Path(temporary)
        adapter_source = root / "batch_counts.c"
        adapter = root / "batch_counts"
        adapter_source.write_text(make_adapter_source(source, spec), encoding="utf-8")
        command = [
            "cc",
            "-std=c11",
            "-O3",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            str(adapter_source),
            "-o",
            str(adapter),
        ]
        compiled = subprocess.run(command, text=True, capture_output=True, check=False)
        if compiled.returncode != 0:
            details = compiled.stderr.strip() or compiled.stdout.strip()
            raise DistributionError(f"не удалось собрать адаптер {hash_name}: {details}")

        try:
            with corpus.open("rb") as stream:
                measured = subprocess.run(
                    [str(adapter)],
                    stdin=stream,
                    text=True,
                    capture_output=True,
                    check=False,
                )
        except OSError as error:
            raise DistributionError(f"не удалось прочитать {corpus}: {error}") from error
        if measured.returncode != 0:
            details = measured.stderr.strip() or measured.stdout.strip()
            raise DistributionError(f"batch-прогон {hash_name} завершился ошибкой: {details}")

    fields = measured.stdout.strip().split(",")
    if len(fields) != spec.bits + 2:
        raise DistributionError(
            f"batch-прогон {hash_name} вернул {len(fields)} полей вместо {spec.bits + 2}"
        )
    try:
        samples = int(fields[0])
        bits = int(fields[1])
        ones = tuple(int(value) for value in fields[2:])
    except ValueError as error:
        raise DistributionError(
            f"batch-прогон {hash_name} вернул некорректные счётчики"
        ) from error
    if bits != spec.bits:
        raise DistributionError(
            f"batch-прогон {hash_name} сообщил ширину {bits} вместо {spec.bits}"
        )
    try:
        return Distribution(hash_name, samples, bits, ones)
    except ValueError as error:
        raise DistributionError(f"некорректный результат {hash_name}: {error}") from error


def write_distribution_csv(stream: TextIO, distribution: Distribution) -> None:
    """Write bit counts and probabilities from LSB to MSB."""
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["bit", "ones", "zeros", "probability", "deviation_from_0.5"])
    for bit, (ones, probability) in enumerate(
        zip(distribution.ones, distribution.probabilities, strict=True)
    ):
        writer.writerow(
            [
                bit,
                ones,
                distribution.samples - ones,
                f"{probability:.9f}",
                f"{abs(probability - 0.5):.9f}",
            ]
        )


def write_summary_csv(stream: TextIO, distributions: Sequence[Distribution]) -> None:
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        [
            "hash",
            "samples",
            "bits",
            "mean_probability",
            "mean_abs_deviation",
            "max_abs_deviation",
            "min_probability",
            "max_probability",
        ]
    )
    for distribution in distributions:
        probabilities = distribution.probabilities
        deviations = [abs(value - 0.5) for value in probabilities]
        writer.writerow(
            [
                distribution.hash_name,
                distribution.samples,
                distribution.bits,
                f"{fmean(probabilities):.9f}",
                f"{fmean(deviations):.9f}",
                f"{max(deviations):.9f}",
                f"{min(probabilities):.9f}",
                f"{max(probabilities):.9f}",
            ]
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_manifest(
    path: Path,
    corpus: Path,
    distributions: Sequence[Distribution],
) -> None:
    samples = {distribution.samples for distribution in distributions}
    if len(samples) != 1:
        raise DistributionError("хэши обработали разное число строк corpus")
    content = "\n".join(
        [
            f"corpus={corpus}",
            f"corpus_bytes={corpus.stat().st_size}",
            f"corpus_sha256={sha256_file(corpus)}",
            f"samples={samples.pop()}",
            f"hashes={','.join(item.hash_name for item in distributions)}",
            "metric=P(hash_output_bit=1)",
            "bit_order=bit_0_is_LSB",
            "input_format=one_ASCII_symbol_per_line",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")


def run(
    corpus: Path,
    output_directory: Path,
    hash_names: Sequence[str],
) -> list[Distribution]:
    """Measure all requested hashes and create CSV, manifest, and one PNG."""
    names = list(dict.fromkeys(hash_names))
    if not names:
        raise DistributionError("нужно указать хотя бы один хэш")

    distributions = [measure_hash(name, corpus) for name in names]
    sample_counts = {item.samples for item in distributions}
    if len(sample_counts) != 1:
        raise DistributionError("хэши обработали разное число строк corpus")

    output_directory.mkdir(parents=True, exist_ok=True)
    for distribution in distributions:
        path = output_directory / f"{distribution.hash_name}.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            write_distribution_csv(stream, distribution)

    with (output_directory / "summary.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        write_summary_csv(stream, distributions)
    write_manifest(output_directory / "manifest.txt", corpus, distributions)

    rows = [
        ProbabilityRow(
            operation=item.hash_name,
            pairs=item.samples,
            probabilities=list(item.probabilities),
        )
        for item in distributions
    ]
    render_bit_probabilities(
        rows,
        output_directory / "bit_distribution.png",
        "C++ Provides corpus: P(hash output bit = 1)",
    )
    return distributions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Считает прямое распределение единиц по выходным битам хэша "
            "для ASCII corpus (одна строка — одно значение). bit_0 — младший бит."
        )
    )
    parser.add_argument("corpus", type=Path, help="текстовый corpus")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"отдельный каталог результатов (по умолчанию: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--hash",
        dest="hashes",
        action="append",
        help="проверить указанный хэш; можно повторять (по умолчанию все)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        distributions = run(
            corpus=arguments.corpus,
            output_directory=arguments.output,
            hash_names=arguments.hashes or HASHES,
        )
    except (DistributionError, OSError, ValueError) as error:
        parser.error(str(error))

    for distribution in distributions:
        probabilities = distribution.probabilities
        mean_deviation = fmean(abs(value - 0.5) for value in probabilities)
        print(
            f"hash={distribution.hash_name} samples={distribution.samples} "
            f"bits={distribution.bits} mean_abs_deviation={mean_deviation:.9f}"
        )
    print(f"wrote {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
