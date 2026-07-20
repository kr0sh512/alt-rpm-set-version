#!/usr/bin/env python3
"""Check reimplement/set.py against ELF libraries installed on this Arch Linux system.

This is the local-system analogue of check_alt_set_impl.py. It treats shared
libraries as providers (defined dynamic symbols) and executables/shared objects
as requirers (undefined dynamic symbols). Required labels are split by provider
library before comparing, so each library is checked only against symbols that
it actually exports.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_alt_set_impl as compat  # noqa: E402

DEFAULT_PROVIDER_DIRS = [Path("/usr/lib")]
DEFAULT_REQUIRER_DIRS = [Path("/usr/bin"), Path("/usr/lib")]


def parse_path_list(values: list[str] | None, defaults: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for value in values or []:
        paths.extend(Path(part) for part in value.split(",") if part)
    return paths or defaults


def is_shared_library_path(path: Path) -> bool:
    name = path.name
    return ".debug" not in name and name.startswith("lib") and ".so" in name


def iter_files(paths: list[Path], recursive: bool) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for root in paths:
        if root.is_file():
            candidates = [root]
        elif recursive:
            candidates = (path for path in root.rglob("*") if path.is_file())
        else:
            candidates = (path for path in root.iterdir() if path.is_file()) if root.is_dir() else []
        for path in candidates:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(path)
    return sorted(files)


def executable_or_library(path: Path) -> bool:
    return is_shared_library_path(path) or os.access(path, os.X_OK)


def build_local_provider_sets(paths: list[Path], bpp: int, recursive: bool, max_files: int) -> list[compat.LabelSet]:
    provider_files = [path for path in iter_files(paths, recursive) if is_shared_library_path(path)][:max_files]
    sets: list[compat.LabelSet] = []
    for path in provider_files:
        labels = compat.nm_symbols(path, "provided")
        if labels:
            sets.append(compat.generate_label_set("provided", str(path), labels, bpp, package="arch-local"))
    return sets


def build_local_requirer_sets(
    paths: list[Path],
    bpp: int,
    recursive: bool,
    max_files: int,
    normalize_versions: bool,
) -> list[compat.LabelSet]:
    requirer_files = [path for path in iter_files(paths, recursive) if executable_or_library(path)][:max_files]
    sets: list[compat.LabelSet] = []
    for path in requirer_files:
        labels = compat.nm_symbols(path, "required")
        if normalize_versions:
            labels = [compat.normalize_required_symbol(label) for label in labels]
        if labels:
            sets.append(compat.generate_label_set("required", str(path), labels, bpp, package="arch-local"))
    return sets


def print_unmatched_required_labels(provider_sets: list[compat.LabelSet], requirer_sets: list[compat.LabelSet], limit: int) -> None:
    provided = {label for provider in provider_sets for label in provider.labels}
    rows: list[tuple[str, int, list[str]]] = []
    for requirer in requirer_sets:
        missing = sorted(set(requirer.labels).difference(provided))
        if missing:
            rows.append((requirer.member, len(missing), missing[:limit]))

    print("\n# required labels not exported by scanned provider libraries")
    print("requirer\tmissing_labels\texamples")
    for member, count, examples in rows[:limit]:
        print(f"{member}\t{count}\t{', '.join(examples)}")
    if len(rows) > limit:
        print(f"... {len(rows) - limit} more requirer files with unmatched labels")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Provided/Required set strings from local Arch Linux ELF files and compare them with reimplement/set.py."
    )
    parser.add_argument("--provider-dir", action="append", help="directory/file containing provider libraries; repeat or comma-separate")
    parser.add_argument("--requirer-dir", action="append", help="directory/file containing requirer ELF files; repeat or comma-separate")
    parser.add_argument("--bpp", type=int, default=32, help="bits per hash used by local set.py")
    parser.add_argument("--max-provider-files", type=int, default=256, help="max provider libraries to inspect")
    parser.add_argument("--max-requirer-files", type=int, default=256, help="max requirer files to inspect")
    parser.add_argument("--recursive", action="store_true", help="scan directories recursively")
    parser.add_argument("--all-pairs", action="store_true", help="compare every provider set with every full requirer set")
    parser.add_argument(
        "--no-normalize-required-version",
        action="store_true",
        help="keep nm -u single-@ required symbols unchanged instead of converting foo@VER to foo@@VER",
    )
    parser.add_argument("--unmatched-limit", type=int, default=20, help="max unmatched-label rows/examples to print")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if shutil.which("nm") is None:
        print("missing required command: nm", file=sys.stderr)
        return 2

    provider_paths = parse_path_list(args.provider_dir, DEFAULT_PROVIDER_DIRS)
    requirer_paths = parse_path_list(args.requirer_dir, DEFAULT_REQUIRER_DIRS)

    provider_sets = build_local_provider_sets(provider_paths, args.bpp, args.recursive, args.max_provider_files)
    requirer_sets = build_local_requirer_sets(
        requirer_paths,
        args.bpp,
        args.recursive,
        args.max_requirer_files,
        normalize_versions=not args.no_normalize_required_version,
    )

    print("system: arch-local")
    print(f"bpp: {args.bpp}")
    print(f"provider paths: {', '.join(str(path) for path in provider_paths)}")
    print(f"requirer paths: {', '.join(str(path) for path in requirer_paths)}")
    print(f"required symbol version normalization: {not args.no_normalize_required_version}")
    compat.print_label_sets("generated Provided sets", provider_sets)
    compat.print_label_sets("generated Required sets", requirer_sets)

    if not provider_sets or not requirer_sets:
        print("\nNo comparable sets generated.", file=sys.stderr)
        return 1

    if args.all_pairs:
        results = [compat.compare_label_sets(provider, requirer) for provider in provider_sets for requirer in requirer_sets]
    else:
        results = compat.build_dependency_results(provider_sets, requirer_sets, args.bpp)
    compat.print_results(results)
    print_unmatched_required_labels(provider_sets, requirer_sets, args.unmatched_limit)

    return 0 if results and all(result.status == "compatible" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
