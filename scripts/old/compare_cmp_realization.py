#!/usr/bin/env python3
"""Benchmark rpmsetcmp from a selected set.c on hardcoded ELF pairs."""

from __future__ import annotations

import argparse
import csv
import os
import shlex
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import compare_realization as common


REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_CMP_SOURCE = REPO_ROOT / "scripts" / "run_cmp.cpp"
DEFAULT_RPM_ROOT = REPO_ROOT / "rpm-build"
DEFAULT_RESULT_ROOT = REPO_ROOT / "res_cmp"

LIBC_CANDIDATES = (
    "/usr/lib/libc.so.6",
    "/lib64/libc.so.6",
    "/usr/lib64/libc.so.6",
    "/lib/libc.so.6",
    "/lib/x86_64-linux-gnu/libc.so.6",
)


@dataclass(frozen=True)
class ComparisonCase:
    name: str
    binary_candidates: tuple[str, ...]
    library_candidates: tuple[str, ...]


# The benchmark corpus is intentionally fixed here rather than supplied via CLI.
HARDCODED_CASES = (
    ComparisonCase("true-libc", ("/usr/bin/true", "/bin/true"), LIBC_CANDIDATES),
    ComparisonCase("ls-libc", ("/usr/bin/ls", "/bin/ls"), LIBC_CANDIDATES),
    ComparisonCase("bash-libc", ("/usr/bin/bash", "/bin/bash"), LIBC_CANDIDATES),
    ComparisonCase(
        "python-libc",
        ("/usr/bin/python3", "/usr/local/bin/python3"),
        LIBC_CANDIDATES,
    ),
)

SUMMARY_FIELDS = (
    "case",
    "binary",
    "library",
    "runs",
    "cmp_result",
    "outputs_consistent",
    "median_rpmsetcmp_ns",
)


@dataclass(frozen=True)
class ResolvedCase:
    name: str
    binary: Path
    library: Path


@dataclass(frozen=True)
class PreparedCase:
    case: ResolvedCase
    provider_set: str
    required_set: str


@dataclass(frozen=True)
class ComparisonSummary:
    prepared: PreparedCase
    runs: int
    cmp_result: int
    outputs_consistent: bool
    median_ns: int | float

    def as_row(self) -> dict[str, str]:
        return {
            "case": self.prepared.case.name,
            "binary": str(self.prepared.case.binary),
            "library": str(self.prepared.case.library),
            "runs": str(self.runs),
            "cmp_result": str(self.cmp_result),
            "outputs_consistent": "yes" if self.outputs_consistent else "no",
            "median_rpmsetcmp_ns": common.format_number(self.median_ns),
        }


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compile a selected set.c and benchmark only rpmsetcmp on the "
            "hardcoded binary/library corpus."
        )
    )
    parser.add_argument("set_c", type=Path, help="path to the set.c implementation")
    parser.add_argument(
        "-n",
        "--runs",
        type=common.positive_int,
        default=10,
        help="rpmsetcmp process runs per hardcoded case (default: 10)",
    )
    parser.add_argument(
        "--rpm-root",
        type=Path,
        default=DEFAULT_RPM_ROOT,
        help="ALT rpm source root",
    )
    parser.add_argument(
        "--res-dir",
        type=Path,
        default=DEFAULT_RESULT_ROOT,
        help="result root (default: repository res_cmp/)",
    )
    parser.add_argument("--name", help="implementation directory name under res_cmp/")
    parser.add_argument(
        "--bpp",
        type=common.bpp_value,
        help="force one bpp value while preparing provider/required sets",
    )
    parser.add_argument("--cc", default="cc", help="C compiler (default: cc)")
    parser.add_argument("--cxx", default="g++", help="C++ compiler (default: g++)")
    return parser.parse_args(argv)


def first_existing(candidates: Sequence[str]) -> Path | None:
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path.resolve()
    return None


def resolve_cases(cases: Sequence[ComparisonCase]) -> list[ResolvedCase]:
    resolved: list[ResolvedCase] = []
    for case in cases:
        binary = first_existing(case.binary_candidates)
        library = first_existing(case.library_candidates)
        if binary is None or library is None:
            missing = "binary" if binary is None else "library"
            print(f"warning: skipping {case.name}: no hardcoded {missing} path exists", file=sys.stderr)
            continue
        resolved.append(ResolvedCase(common.safe_name(case.name), binary, library))
    if not resolved:
        raise RuntimeError("none of the hardcoded binary/library cases is available")
    return resolved


def compile_cmp_runner(build_dir: Path, rpm_root: Path, cxx: str) -> Path:
    runner = build_dir / "run_cmp"
    command = [
        cxx,
        "-O2",
        "-std=c++17",
        "-Wall",
        "-Wextra",
        "-Werror",
        str(RUN_CMP_SOURCE),
        str(build_dir / "set.o"),
        str(build_dir / "rpmmalloc.o"),
        "-o",
        str(runner),
    ]
    log: list[str] = ["\n[rpmsetcmp runner]", "$ " + shlex.join(command)]
    completed = subprocess.run(command, cwd=rpm_root, text=True, capture_output=True)
    if completed.stdout:
        log.append(completed.stdout.rstrip())
    if completed.stderr:
        log.append(completed.stderr.rstrip())
    log.append(f"[exit {completed.returncode}]")
    build_log = build_dir / "build.log"
    with build_log.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(log) + "\n")
    if completed.returncode != 0:
        raise RuntimeError(f"run_cmp compilation failed; see {build_log}")
    return runner


def run_set_once(
    runner: Path,
    target: Path,
    sets_dir: Path,
    slug: str,
    bpp: int | None,
) -> common.ParsedRun:
    command = [str(runner)]
    if bpp is not None:
        command.extend(("--bpp", str(bpp)))
    command.append(str(target))
    completed = subprocess.run(command, text=True, capture_output=True)
    stdout_path = sets_dir / f"{slug}.tsv"
    stderr_path = sets_dir / f"{slug}.stderr"
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"run_set failed for {target}; see {stderr_path}")
    return common.parse_run_set_output(completed.stdout)


def same_file(left: str, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return Path(left).resolve() == right.resolve()


def prepare_cases(
    cases: Sequence[ResolvedCase],
    run_set: Path,
    sets_dir: Path,
    bpp: int | None,
) -> list[PreparedCase]:
    sets_dir.mkdir(parents=True)
    cache: dict[Path, common.ParsedRun] = {}
    used_slugs: set[str] = set()

    def inspect(target: Path) -> common.ParsedRun:
        parsed = cache.get(target)
        if parsed is not None:
            return parsed
        slug = common.target_slug(target, used_slugs)
        parsed = run_set_once(run_set, target, sets_dir, slug, bpp)
        cache[target] = parsed
        return parsed

    prepared: list[PreparedCase] = []
    for case in cases:
        library_run = inspect(case.library)
        provider_rows = [row for row in library_run.rows if row.get("role") == "provided"]
        if len(provider_rows) != 1:
            raise RuntimeError(
                f"expected one provider set for {case.library}, got {len(provider_rows)}"
            )

        binary_run = inspect(case.binary)
        required_rows = [
            row
            for row in binary_run.rows
            if row.get("role") == "required"
            and row.get("object")
            and same_file(row["object"], case.library)
        ]
        if len(required_rows) != 1:
            raise RuntimeError(
                f"expected one {case.library.name} requirement from {case.binary}, "
                f"got {len(required_rows)}"
            )

        provider_row = provider_rows[0]
        required_row = required_rows[0]
        if provider_row["bpp"] != required_row["bpp"]:
            raise RuntimeError(
                f"bpp mismatch for {case.name}: provider={provider_row['bpp']} "
                f"required={required_row['bpp']}"
            )
        prepared.append(
            PreparedCase(
                case=case,
                provider_set=provider_row["set"],
                required_set=required_row["set"],
            )
        )
    return prepared


def parse_cmp_output(output: str) -> tuple[int, int]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "\t" not in line:
            continue
        key, value = line.split("\t", 1)
        values[key] = value
    try:
        result = int(values["cmp_result"])
        elapsed = int(values["rpmsetcmp_ns"])
    except (KeyError, ValueError) as exception:
        raise RuntimeError("invalid run_cmp output") from exception
    if elapsed < 0:
        raise RuntimeError("run_cmp returned a negative duration")
    return result, elapsed


def benchmark_case(
    runner: Path,
    prepared: PreparedCase,
    runs: int,
    output_dir: Path,
) -> ComparisonSummary:
    output_dir.mkdir(parents=True)
    results: list[int] = []
    timings: list[int] = []
    for run_number in range(1, runs + 1):
        completed = subprocess.run(
            [str(runner), prepared.provider_set, prepared.required_set],
            text=True,
            capture_output=True,
        )
        stem = f"run-{run_number:03d}"
        stdout_path = output_dir / f"{stem}.tsv"
        stderr_path = output_dir / f"{stem}.stderr"
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            raise RuntimeError(
                f"run_cmp failed for {prepared.case.name} on run {run_number}; "
                f"see {stderr_path}"
            )
        result, elapsed = parse_cmp_output(completed.stdout)
        results.append(result)
        timings.append(elapsed)

    return ComparisonSummary(
        prepared=prepared,
        runs=runs,
        cmp_result=results[0],
        outputs_consistent=len(set(results)) == 1,
        median_ns=statistics.median(timings),
    )


def write_summary(path: Path, summaries: Sequence[ComparisonSummary]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(SUMMARY_FIELDS), delimiter="\t")
        writer.writeheader()
        for summary in summaries:
            writer.writerow(summary.as_row())


def print_summary(summaries: Sequence[ComparisonSummary]) -> None:
    print("\t".join(SUMMARY_FIELDS))
    for summary in summaries:
        row = summary.as_row()
        print("\t".join(row[field] for field in SUMMARY_FIELDS))


def write_metadata(
    path: Path,
    set_source: Path,
    rpm_root: Path,
    runs: int,
    cases: Sequence[ResolvedCase],
) -> None:
    lines = [
        f"set_c\t{set_source}",
        f"rpm_root\t{rpm_root}",
        f"runs\t{runs}",
    ]
    for case in cases:
        lines.append(f"case\t{case.name}\t{case.binary}\t{case.library}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    try:
        common.require_commands((args.cc, args.cxx, "eu-readelf", "eu-elfclassify", "nm"))
        set_source = common.resolve_file(args.set_c, "set.c")
        rpm_root = common.resolve_directory(args.rpm_root, "rpm root")
        common.resolve_file(rpm_root / "system.h", "system.h")
        cases = resolve_cases(HARDCODED_CASES)
        name = common.safe_name(args.name) if args.name else common.default_name(set_source)
        result_dir = args.res_dir.expanduser().resolve() / name
        if result_dir.exists():
            shutil.rmtree(result_dir)
        build_dir = result_dir / "build"
        sets_dir = result_dir / "sets"
        runs_dir = result_dir / "runs"
        build_dir.mkdir(parents=True)
        runs_dir.mkdir()

        run_set = common.compile_runner(set_source, rpm_root, build_dir, args.cc, args.cxx)
        run_cmp = compile_cmp_runner(build_dir, rpm_root, args.cxx)
        prepared = prepare_cases(cases, run_set, sets_dir, args.bpp)
        summaries = [
            benchmark_case(run_cmp, item, args.runs, runs_dir / item.case.name)
            for item in prepared
        ]

        write_summary(result_dir / "summary.tsv", summaries)
        write_metadata(result_dir / "metadata.tsv", set_source, rpm_root, args.runs, cases)
        print_summary(summaries)
        print(f"\nresults\t{result_dir}")
        return 0
    except (OSError, RuntimeError, ValueError) as exception:
        print(f"compare_cmp_realization: {exception}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
