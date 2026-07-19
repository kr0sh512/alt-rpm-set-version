#!/usr/bin/env python3
"""Build one set.c implementation and benchmark it through run_set.cpp.

The benchmark stores every raw run_set.cpp stdout/stderr stream and a TSV
summary with median set.c API timings under res/<implementation>/.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import shlex
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_SET_SOURCE = REPO_ROOT / "scripts" / "run_set.cpp"
DEFAULT_RPM_ROOT = REPO_ROOT / "rpm-build"

TIMING_FIELDS = (
    "set_new_ns",
    "set_add_total_ns",
    "set_fini_ns",
    "set_free_ns",
    "set_api_total_ns",
)
SUMMARY_FIELDS = (
    "target",
    "kind",
    "runs",
    "sets_per_run",
    "outputs_consistent",
    *(f"median_{field}" for field in TIMING_FIELDS),
)

# Each tuple describes one logical target.  The first existing path is used.
DEFAULT_TARGET_GROUPS = (
    ("/usr/bin/true", "/bin/true"),
    ("/usr/bin/ls", "/bin/ls"),
    ("/usr/bin/bash", "/bin/bash"),
    ("/usr/bin/python3", "/usr/local/bin/python3"),
    ("/usr/lib/libc.so.6", "/lib64/libc.so.6", "/lib/x86_64-linux-gnu/libc.so.6"),
    ("/usr/lib/libm.so.6", "/lib64/libm.so.6", "/lib/x86_64-linux-gnu/libm.so.6"),
    ("/usr/lib/libz.so.1", "/usr/lib64/libz.so.1", "/lib/x86_64-linux-gnu/libz.so.1"),
    (
        "/usr/lib/libstdc++.so.6",
        "/usr/lib64/libstdc++.so.6",
        "/lib/x86_64-linux-gnu/libstdc++.so.6",
    ),
)


@dataclass(frozen=True)
class ParsedRun:
    kind: str
    rows: tuple[dict[str, str], ...]
    totals: dict[str, int]
    output_signature: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class TargetSummary:
    target: Path
    kind: str
    runs: int
    sets_per_run: int
    outputs_consistent: bool
    medians: dict[str, int | float]

    def as_row(self) -> dict[str, str]:
        row = {
            "target": str(self.target),
            "kind": self.kind,
            "runs": str(self.runs),
            "sets_per_run": str(self.sets_per_run),
            "outputs_consistent": "yes" if self.outputs_consistent else "no",
        }
        row.update(
            {
                f"median_{field}": format_number(value)
                for field, value in self.medians.items()
            }
        )
        return row


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def bpp_value(value: str) -> int:
    number = int(value)
    if not 10 <= number <= 32:
        raise argparse.ArgumentTypeError("must be in [10, 32]")
    return number


def safe_name(value: str) -> str:
    name = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in value
    )
    name = name.strip(".-")
    if not name or name in {".", ".."}:
        raise ValueError(f"invalid result name: {value!r}")
    return name


def default_name(set_source: Path) -> str:
    return safe_name(f"{set_source.parent.name}-{set_source.stem}")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compile a selected set.c, run scripts/run_set.cpp repeatedly on ELF targets, "
            "and store raw outputs plus median set.c timings."
        )
    )
    parser.add_argument("set_c", type=Path, help="path to the set.c implementation")
    parser.add_argument(
        "-n",
        "--runs",
        type=positive_int,
        default=10,
        help="runs per ELF target (default: 10)",
    )
    parser.add_argument(
        "--rpm-root", type=Path, default=DEFAULT_RPM_ROOT, help="ALT rpm source root"
    )
    parser.add_argument(
        "--res-dir",
        type=Path,
        default=REPO_ROOT / "res",
        help="result root (default: repository res/)",
    )
    parser.add_argument("--name", help="implementation directory name under res/")
    parser.add_argument(
        "--target",
        action="append",
        type=Path,
        default=[],
        help="ELF target; repeat to override the built-in binary/library set",
    )
    parser.add_argument(
        "--bpp", type=bpp_value, help="force one bpp value for every run_set invocation"
    )
    parser.add_argument("--cc", default="cc", help="C compiler (default: cc)")
    parser.add_argument("--cxx", default="g++", help="C++ compiler (default: g++)")
    return parser.parse_args(argv)


def resolve_file(path: Path, description: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError(f"{description} is not a file: {path}")
    return resolved


def resolve_directory(path: Path, description: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise RuntimeError(f"{description} is not a directory: {path}")
    return resolved


def select_default_targets() -> list[Path]:
    selected: list[Path] = []
    seen: set[Path] = set()
    for candidates in DEFAULT_TARGET_GROUPS:
        for candidate in candidates:
            path = Path(candidate)
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved not in seen:
                selected.append(resolved)
                seen.add(resolved)
            break
    return selected


def resolve_targets(explicit: Iterable[Path]) -> list[Path]:
    supplied = list(explicit)
    targets = (
        [resolve_file(path, "target") for path in supplied]
        if supplied
        else select_default_targets()
    )
    unique: list[Path] = []
    seen: set[Path] = set()
    for target in targets:
        if target not in seen:
            unique.append(target)
            seen.add(target)
    if not unique:
        raise RuntimeError("no ELF targets were found; pass at least one --target")
    return unique


def require_commands(commands: Iterable[str]) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise RuntimeError("missing required commands: " + ", ".join(missing))


def create_header_shim(rpm_root: Path, include_dir: Path) -> None:
    include_dir.mkdir(parents=True, exist_ok=True)
    by_name: dict[str, Path] = {}
    for header in sorted(rpm_root.glob("*/*.h")):
        existing = by_name.get(header.name)
        if existing is not None and existing.resolve() != header.resolve():
            raise RuntimeError(
                f"duplicate rpm header basename {header.name}: {existing} and {header}"
            )
        by_name[header.name] = header
    if not by_name:
        raise RuntimeError(f"no */*.h headers found below {rpm_root}")
    for name, source in by_name.items():
        (include_dir / name).symlink_to(source.resolve())


def run_build_command(
    command: Sequence[str], cwd: Path, log: list[str]
) -> subprocess.CompletedProcess[str]:
    log.append("$ " + shlex.join(str(part) for part in command))
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if completed.stdout:
        log.append(completed.stdout.rstrip())
    if completed.stderr:
        log.append(completed.stderr.rstrip())
    log.append(f"[exit {completed.returncode}]")
    return completed


def compile_runner(
    set_source: Path,
    rpm_root: Path,
    build_dir: Path,
    cc: str,
    cxx: str,
) -> Path:
    include_dir = build_dir / "include" / "rpm"
    create_header_shim(rpm_root, include_dir)
    set_object = build_dir / "set.o"
    malloc_object = build_dir / "rpmmalloc.o"
    runner = build_dir / "run_set"
    rpmmalloc_source = resolve_file(rpm_root / "rpmio" / "rpmmalloc.c", "rpmmalloc.c")
    log: list[str] = [f"set_c={set_source}", f"rpm_root={rpm_root}"]

    common_flags = [
        "-O3",
        "-std=gnu11",
        "-include",
        "stddef.h",
        f"-I{rpm_root}",
        f"-I{include_dir}",
    ]
    compatibility_flags = [
        "-D_GNU_SOURCE=1",
        "-DSTDC_HEADERS=1",
        "-DHAVE_STRING_H=1",
        "-DHAVE_SETENV=1",
        "-DHAVE_STPCPY=1",
        "-DHAVE_STPNCPY=1",
        "-DHAVE_S_IFSOCK=1",
        "-DHAVE_S_ISLNK=1",
        "-DHAVE_S_ISSOCK=1",
    ]

    def attempt(extra_flags: Sequence[str], label: str) -> bool:
        log.append(f"\n[{label}]")
        set_command = [
            cc,
            *common_flags,
            *extra_flags,
            "-c",
            str(set_source),
            "-o",
            str(set_object),
        ]
        malloc_command = [
            cc,
            *common_flags,
            *extra_flags,
            "-include",
            "stdarg.h",
            "-c",
            str(rpmmalloc_source),
            "-o",
            str(malloc_object),
        ]
        link_command = [
            cxx,
            "-O3",
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(RUN_SET_SOURCE),
            str(set_object),
            str(malloc_object),
            "-o",
            str(runner),
        ]
        for command in (set_command, malloc_command, link_command):
            if run_build_command(command, rpm_root, log).returncode != 0:
                return False
        return True

    succeeded = attempt((), "minimal ALT-style build")
    if not succeeded:
        set_object.unlink(missing_ok=True)
        malloc_object.unlink(missing_ok=True)
        runner.unlink(missing_ok=True)
        succeeded = attempt(compatibility_flags, "modern libc compatibility retry")

    build_log = build_dir / "build.log"
    build_log.write_text("\n".join(log) + "\n", encoding="utf-8")
    if not succeeded:
        raise RuntimeError(f"compilation failed; see {build_log}")
    return runner


def parse_run_set_output(output: str) -> ParsedRun:
    lines = output.splitlines()
    try:
        table_start = next(
            index for index, line in enumerate(lines) if line.startswith("role\t")
        )
    except StopIteration as exception:
        raise RuntimeError("run_set output has no TSV result table") from exception

    metadata: dict[str, str] = {}
    for line in lines[:table_start]:
        if "\t" not in line:
            continue
        key, value = line.split("\t", 1)
        metadata[key] = value
    rows = tuple(
        csv.DictReader(io.StringIO("\n".join(lines[table_start:])), delimiter="\t")
    )
    if not rows:
        raise RuntimeError("run_set output contains no result rows")

    totals = {field: 0 for field in TIMING_FIELDS}
    for row in rows:
        for field in TIMING_FIELDS:
            try:
                totals[field] += int(row[field])
            except (KeyError, ValueError) as exception:
                raise RuntimeError(f"invalid {field} in run_set output") from exception
    signature_fields = ("role", "object", "labels", "bpp", "set")
    signature = tuple(tuple(row[field] for field in signature_fields) for row in rows)
    return ParsedRun(
        kind=metadata.get("kind", "unknown"),
        rows=rows,
        totals=totals,
        output_signature=signature,
    )


def target_slug(target: Path, used: set[str]) -> str:
    base = safe_name(target.name)
    candidate = base
    if candidate in used:
        digest = hashlib.sha256(str(target).encode()).hexdigest()[:8]
        candidate = f"{base}-{digest}"
    used.add(candidate)
    return candidate


def benchmark_target(
    runner: Path,
    target: Path,
    runs: int,
    output_dir: Path,
    bpp: int | None,
) -> TargetSummary:
    output_dir.mkdir(parents=True, exist_ok=True)
    parsed_runs: list[ParsedRun] = []
    for run_number in range(1, runs + 1):
        command = [str(runner)]
        if bpp is not None:
            command.extend(("--bpp", str(bpp)))
        command.append(str(target))
        completed = subprocess.run(command, text=True, capture_output=True)
        stem = f"run-{run_number:03d}"
        (output_dir / f"{stem}.tsv").write_text(completed.stdout, encoding="utf-8")
        (output_dir / f"{stem}.stderr").write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            raise RuntimeError(
                f"run_set failed for {target} on run {run_number}; "
                f"see {output_dir / f'{stem}.stderr'}"
            )
        parsed_runs.append(parse_run_set_output(completed.stdout))

    signatures = {parsed.output_signature for parsed in parsed_runs}
    kinds = {parsed.kind for parsed in parsed_runs}
    row_counts = {len(parsed.rows) for parsed in parsed_runs}
    medians = {
        field: statistics.median(parsed.totals[field] for parsed in parsed_runs)
        for field in TIMING_FIELDS
    }
    return TargetSummary(
        target=target,
        kind=parsed_runs[0].kind if len(kinds) == 1 else "inconsistent",
        runs=runs,
        sets_per_run=len(parsed_runs[0].rows) if len(row_counts) == 1 else -1,
        outputs_consistent=len(signatures) == 1
        and len(kinds) == 1
        and len(row_counts) == 1,
        medians=medians,
    )


def format_number(value: int | float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def write_summary(path: Path, summaries: Iterable[TargetSummary]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=SUMMARY_FIELDS, delimiter="\t")
        writer.writeheader()
        for summary in summaries:
            writer.writerow(summary.as_row())


def print_summary(summaries: Iterable[TargetSummary]) -> None:
    fields = (
        "target",
        "kind",
        "runs",
        "sets_per_run",
        "outputs_consistent",
        "median_set_api_total_ns",
    )
    print("\t".join(fields))
    for summary in summaries:
        row = summary.as_row()
        print("\t".join(row[field] for field in fields))


def write_metadata(
    path: Path,
    set_source: Path,
    rpm_root: Path,
    runner: Path,
    runs: int,
    targets: Iterable[Path],
) -> None:
    lines = [
        f"set_c\t{set_source}",
        f"rpm_root\t{rpm_root}",
        f"runner\t{runner}",
        f"runs\t{runs}",
    ]
    lines.extend(f"target\t{target}" for target in targets)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    try:
        require_commands((args.cc, args.cxx, "eu-readelf", "eu-elfclassify", "nm"))
        set_source = resolve_file(args.set_c, "set.c")
        rpm_root = resolve_directory(args.rpm_root, "rpm root")
        resolve_file(rpm_root / "system.h", "system.h")
        targets = resolve_targets(args.target)
        name = safe_name(args.name) if args.name else default_name(set_source)
        result_root = args.res_dir.expanduser().resolve()
        result_dir = result_root / name
        if result_dir.exists():
            shutil.rmtree(result_dir)
        build_dir = result_dir / "build"
        runs_dir = result_dir / "runs"
        build_dir.mkdir(parents=True)
        runs_dir.mkdir()

        runner = compile_runner(set_source, rpm_root, build_dir, args.cc, args.cxx)
        used_slugs: set[str] = set()
        summaries: list[TargetSummary] = []
        for target in targets:
            slug = target_slug(target, used_slugs)
            summaries.append(
                benchmark_target(runner, target, args.runs, runs_dir / slug, args.bpp)
            )

        write_summary(result_dir / "summary.tsv", summaries)
        write_metadata(
            result_dir / "metadata.tsv",
            set_source,
            rpm_root,
            runner,
            args.runs,
            targets,
        )
        print_summary(summaries)
        print(f"\nresults\t{result_dir}")
        return 0
    except (OSError, RuntimeError, ValueError) as exception:
        print(f"compare_realization: {exception}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
