#!/usr/bin/env python3
"""Compare one binary's required set string with a few provider libraries.

Usage:
    scripts/compare_binary_with_lib_sets.py [--bpp N] BINARY LIB.so [LIB.so ...]

For every library, this script compares:
    set(defined dynamic symbols from LIB) vs
    set(required dynamic symbols from BINARY that LIB provides)
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reimplement import set as rpmset  # noqa: E402


def run_nm(command: list[str]) -> str:
    proc = subprocess.run(command, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(command)} failed with {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def normalize_required_symbol(symbol: str) -> str:
    """Normalize nm's required `foo@VER` form to provider-like `foo@@VER`."""
    if "@@" in symbol or "@" not in symbol:
        return symbol
    name, version = symbol.split("@", 1)
    if not name or not version:
        return symbol
    return f"{name}@@{version}"


def required_symbols(path: Path, normalize_versions: bool) -> set[str]:
    """Return strong undefined dynamic symbols required by one ELF file."""
    output = run_nm(["nm", "--dynamic", "-u", str(path)])
    symbols: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[-2] != "U":
            continue
        symbol = parts[-1]
        symbols.add(normalize_required_symbol(symbol) if normalize_versions else symbol)
    return symbols


def provided_symbols(path: Path) -> set[str]:
    """Return defined dynamic symbols provided by one ELF shared library."""
    output = run_nm(["nm", "--dynamic", "-j", "-U", str(path)])
    return {line.strip() for line in output.splitlines() if line.strip()}


def labels_to_set_string(labels: Iterable[str], bpp: int) -> str | None:
    item_set = rpmset.set_new()
    for label in sorted(set(labels)):
        rpmset.set_add(item_set, label)
    # set.py may print hash-collision warnings; keep stdout tabular.
    with contextlib.redirect_stdout(sys.stderr):
        return rpmset.set_fini(item_set, bpp)


def compare_library(binary_required: set[str], library: Path, bpp: int) -> tuple[str, str, int, int, str, str]:
    provided = provided_symbols(library)
    required_from_library = binary_required.intersection(provided)
    provider_set = labels_to_set_string(provided, bpp)
    required_set = labels_to_set_string(required_from_library, bpp)

    if not provided:
        return "no-provided-symbols", "", 0, 0, "-", "-"
    if not required_from_library:
        return "not-required", "", len(provided), 0, provider_set or "-", "-"

    assert provider_set is not None
    assert required_set is not None
    cmp_result = rpmset.rpmsetcmp(provider_set, required_set)
    status = "compatible" if cmp_result in (0, 1) else "incompatible"
    return status, str(cmp_result), len(provided), len(required_from_library), provider_set, required_set


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare one binary against a few libraries using local set.py set strings.")
    parser.add_argument("binary", type=Path, help="ELF executable/shared object with required dynamic symbols")
    parser.add_argument("libraries", nargs="+", type=Path, help="provider shared libraries to compare against")
    parser.add_argument("--bpp", type=int, default=32, help="bits per hash used by local set.py")
    parser.add_argument(
        "--no-normalize-required-version",
        action="store_true",
        help="keep nm -u single-@ required symbols unchanged instead of converting foo@VER to foo@@VER",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if shutil.which("nm") is None:
        print("missing required command: nm", file=sys.stderr)
        return 2

    required = required_symbols(args.binary, normalize_versions=not args.no_normalize_required_version)
    if not required:
        print(f"no strong dynamic required symbols found in {args.binary}", file=sys.stderr)
        return 1

    print(f"binary\t{args.binary}")
    print(f"bpp\t{args.bpp}")
    print(f"required_symbols\t{len(required)}")
    print("status\tcmp\tlib\tprovided\trequired_from_lib\tprovider_set\trequired_set")

    failed = False
    for library in args.libraries:
        try:
            status, cmp_result, provided_count, required_count, provider_set, required_set = compare_library(
                required, library, args.bpp
            )
        except RuntimeError as exc:
            print(f"error\t\t{library}\t0\t0\t-\t-", flush=True)
            print(exc, file=sys.stderr)
            failed = True
            continue

        print(
            f"{status}\t{cmp_result}\t{library}\t{provided_count}\t{required_count}\t{provider_set}\t{required_set}"
        )
        failed = failed or status in {"incompatible", "no-provided-symbols"}

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
