#!/usr/bin/env python3
"""Extract ALT Linux set:version strings and optional ELF nm symbol stats.

The script uses the public rdb.altlinux.org API to read package dependency
metadata. With --download-nm it also downloads binary RPMs, extracts ELF files
with bsdtar, and runs nm on dynamic symbols.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

API_BASE = "https://rdb.altlinux.org/api"
DEFAULT_PACKAGES = [
    "glibc-core",
    "libcrypto3",
    "libssl3",
    "zlib",
    "libgcc1",
    "libcurl",
    "libsystemd",
    "libqt6-core",
    "libgtk+3",
    "libsqlite3",
    "libxml2",
    "libX11",
    "libxcb",
    "coreutils",
    "curl",
    "openssl",
    "systemd",
    "python3-base",
]

SET_PREFIX = "set:"
SET_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
IDENTISH_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:@@?[A-Za-z0-9_.]+)?$")


@dataclass(frozen=True)
class SetDependency:
    package: str
    dep_type: str
    name: str
    set_string: str

    @property
    def payload(self) -> str:
        return self.set_string[len(SET_PREFIX) :]

    @property
    def bpp(self) -> int | None:
        return set_char_to_int(self.payload[0]) if self.payload else None

    @property
    def mshift(self) -> int | None:
        return set_char_to_int(self.payload[1]) if len(self.payload) > 1 else None


@dataclass
class NmReport:
    package: str
    mode: str
    files_seen: int
    symbols: list[str]
    file_examples: list[str]


def set_char_to_int(char: str) -> int | None:
    try:
        return SET_ALPHABET.index(char)
    except ValueError:
        return None


def api_json(path: str, params: dict[str, object] | None = None) -> dict:
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": "arsv-set-symbol-extractor/1.0"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def get_pkghash(package: str, branch: str, arch: str) -> str:
    data = api_json(
        "/site/pkghash_by_binary_name",
        {"branch": branch, "name": package, "arch": arch},
    )
    return str(data["pkghash"])


def get_set_dependencies(package: str, branch: str, arch: str) -> tuple[str, list[SetDependency]]:
    pkghash = get_pkghash(package, branch, arch)
    deps = api_json(f"/dependencies/binary_package_dependencies/{pkghash}")["dependencies"]
    set_deps = []
    for dep in deps:
        version = dep.get("version") or ""
        if version.startswith(SET_PREFIX):
            set_deps.append(
                SetDependency(
                    package=package,
                    dep_type=dep.get("type", ""),
                    name=dep.get("name", ""),
                    set_string=version,
                )
            )
    return pkghash, set_deps


def package_download_url(pkghash: str, branch: str, arch: str) -> str:
    data = api_json(
        f"/site/package_downloads_bin/{pkghash}",
        {"branch": branch, "arch": arch},
    )
    downloads = data.get("downloads") or []
    if not downloads or not downloads[0].get("packages"):
        raise RuntimeError(f"no download URL for pkghash={pkghash}")
    return downloads[0]["packages"][0]["url"]


def download_file(url: str, destination: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "arsv-set-symbol-extractor/1.0"})
    with urllib.request.urlopen(req, timeout=120) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out)


def run_text(command: list[str], cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        joined = " ".join(command)
        raise RuntimeError(f"{joined} failed with {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def rpm_members(rpm_path: Path) -> list[str]:
    return [line for line in run_text(["bsdtar", "-tf", str(rpm_path)]).splitlines() if line]


def select_library_members(members: Iterable[str], max_files: int) -> list[str]:
    selected = [
        member
        for member in members
        if not member.endswith("/") and re.search(r"(^|/)lib[^/]*\.so(?:\.|$)", member)
    ]
    return selected[:max_files]


def select_executable_members(members: Iterable[str], max_files: int) -> list[str]:
    prefixes = ("./bin/", "./usr/bin/", "./sbin/", "./usr/sbin/", "./usr/lib/systemd/")
    selected = [member for member in members if not member.endswith("/") and member.startswith(prefixes)]
    return selected[:max_files]


def nm_symbols(path: Path, mode: str) -> list[str]:
    flag = "-U" if mode == "defined" else "-u"
    proc = subprocess.run(
        ["nm", "--dynamic", "-j", flag, str(path)],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def extract_nm_report(
    package: str,
    pkghash: str,
    branch: str,
    arch: str,
    mode: str,
    max_files: int,
    workdir: Path,
) -> NmReport:
    rpm_url = package_download_url(pkghash, branch, arch)
    rpm_path = workdir / Path(urllib.parse.urlparse(rpm_url).path).name
    download_file(rpm_url, rpm_path)
    members = rpm_members(rpm_path)
    selected = (
        select_library_members(members, max_files)
        if mode == "defined"
        else select_executable_members(members, max_files)
    )
    extract_dir = workdir / f"extract-{package.replace('/', '_').replace('+', '_')}-{mode}"
    extract_dir.mkdir(parents=True, exist_ok=True)
    if selected:
        run_text(["bsdtar", "-xf", str(rpm_path), "-C", str(extract_dir), *selected])

    symbols: list[str] = []
    for member in selected:
        member_path = extract_dir / member
        if member_path.exists():
            symbols.extend(nm_symbols(member_path, mode))

    return NmReport(
        package=package,
        mode=mode,
        files_seen=len(selected),
        symbols=symbols,
        file_examples=selected[:3],
    )


def char_classes(chars: Iterable[str]) -> dict[str, object]:
    char_set = sorted(set(chars))
    return {
        "alphabet": "".join(char_set),
        "upper": sum(ch.isupper() for ch in char_set),
        "lower": sum(ch.islower() for ch in char_set),
        "digit": sum(ch.isdigit() for ch in char_set),
        "underscore": "_" in char_set,
        "at": "@" in char_set,
        "dot": "." in char_set,
        "dollar": "$" in char_set,
        "other": "".join(ch for ch in char_set if not (ch.isalnum() or ch in "_.@$")),
    }


def print_set_report(set_deps: list[SetDependency], prefix_len: int) -> None:
    print("# set:version dependency strings")
    if not set_deps:
        print("No set: dependencies found.")
        return

    lengths = [len(dep.payload) for dep in set_deps]
    alphabet = "".join(sorted(set("".join(dep.payload for dep in set_deps))))
    print(f"count: {len(set_deps)}")
    print(f"payload length min/median/max: {min(lengths)}/{statistics.median(lengths)}/{max(lengths)}")
    print(f"observed encoded alphabet: {alphabet}")
    print(f"bpp counts: {dict(sorted(Counter(dep.bpp for dep in set_deps).items()))}")
    print(f"Mshift counts: {dict(sorted(Counter(dep.mshift for dep in set_deps).items()))}")
    print()
    print("package\ttype\tbpp\tMshift\tlen\tdependency\tset-prefix")
    for dep in sorted(set_deps, key=lambda item: (item.package, item.dep_type, item.name)):
        print(
            f"{dep.package}\t{dep.dep_type}\t{dep.bpp}\t{dep.mshift}\t"
            f"{len(dep.payload)}\t{dep.name}\t{dep.payload[:prefix_len]}"
        )


def print_nm_report(reports: list[NmReport], sample_limit: int) -> None:
    print("\n# nm dynamic symbol strings")
    if not reports:
        print("Skipped. Pass --download-nm to download RPMs and run bsdtar/nm.")
        return

    for mode in ("defined", "undefined"):
        mode_reports = [report for report in reports if report.mode == mode]
        if not mode_reports:
            continue
        all_symbols = [symbol for report in mode_reports for symbol in report.symbols]
        print(f"\n## {mode} symbols")
        print(f"packages: {len(mode_reports)}")
        print(f"symbols total/unique: {len(all_symbols)}/{len(set(all_symbols))}")
        if all_symbols:
            print(f"max symbol length: {max(len(symbol) for symbol in all_symbols)}")
            print(f"character classes: {char_classes(''.join(all_symbols))}")
            odd = sorted({symbol for symbol in all_symbols if not IDENTISH_SYMBOL_RE.match(symbol)})
            mangled = sorted({symbol for symbol in all_symbols if symbol.startswith("_Z")})
            print(f"non identifier-ish examples: {odd[:sample_limit]}")
            print(f"C++ mangled examples: {mangled[:sample_limit]}")
        print("package\tfiles\tsymbols\tfile-examples\tsymbol-examples")
        for report in mode_reports:
            print(
                f"{report.package}\t{report.files_seen}\t{len(report.symbols)}\t"
                f"{', '.join(report.file_examples)}\t{', '.join(report.symbols[:sample_limit])}"
            )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print ALT package set:version strings and optional nm symbol statistics."
    )
    parser.add_argument("packages", nargs="*", default=DEFAULT_PACKAGES, help="binary package names")
    parser.add_argument("--branch", default="sisyphus", help="ALT repository branch/packageset")
    parser.add_argument("--arch", default="x86_64", help="binary package architecture")
    parser.add_argument("--download-nm", action="store_true", help="download RPMs and run nm on ELF files")
    parser.add_argument("--nm-defined", action="store_true", help="with --download-nm, inspect defined symbols from libraries")
    parser.add_argument("--nm-undefined", action="store_true", help="with --download-nm, inspect undefined symbols from executables")
    parser.add_argument("--max-libs", type=int, default=8, help="max library files per package for defined-symbol nm")
    parser.add_argument("--max-bins", type=int, default=20, help="max executable files per package for undefined-symbol nm")
    parser.add_argument("--prefix-len", type=int, default=48, help="number of set payload chars to print per row")
    parser.add_argument("--sample-limit", type=int, default=12, help="number of symbol examples to print")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.download_nm and not args.nm_defined and not args.nm_undefined:
        args.nm_defined = True
        args.nm_undefined = True

    if args.download_nm:
        missing = [cmd for cmd in ("bsdtar", "nm") if shutil.which(cmd) is None]
        if missing:
            print(f"missing required command(s) for --download-nm: {', '.join(missing)}", file=sys.stderr)
            return 2

    package_hashes: dict[str, str] = {}
    set_deps: list[SetDependency] = []
    errors: list[str] = []

    for package in args.packages:
        try:
            pkghash, deps = get_set_dependencies(package, args.branch, args.arch)
            package_hashes[package] = pkghash
            set_deps.extend(deps)
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, RuntimeError) as exc:
            errors.append(f"{package}: {exc}")

    print(f"branch: {args.branch}")
    print(f"arch: {args.arch}")
    print(f"packages requested: {', '.join(args.packages)}")
    if errors:
        print("\n# lookup errors", file=sys.stderr)
        for error in errors:
            print(error, file=sys.stderr)
    print()
    print_set_report(set_deps, args.prefix_len)

    nm_reports: list[NmReport] = []
    if args.download_nm:
        with tempfile.TemporaryDirectory(prefix="arsv-alt-rpms-") as tmp:
            workdir = Path(tmp)
            for package, pkghash in package_hashes.items():
                if args.nm_defined:
                    try:
                        nm_reports.append(
                            extract_nm_report(
                                package,
                                pkghash,
                                args.branch,
                                args.arch,
                                "defined",
                                args.max_libs,
                                workdir,
                            )
                        )
                    except Exception as exc:  # keep processing other packages
                        errors.append(f"{package} defined nm: {exc}")
                if args.nm_undefined:
                    try:
                        nm_reports.append(
                            extract_nm_report(
                                package,
                                pkghash,
                                args.branch,
                                args.arch,
                                "undefined",
                                args.max_bins,
                                workdir,
                            )
                        )
                    except Exception as exc:  # keep processing other packages
                        errors.append(f"{package} undefined nm: {exc}")

    print_nm_report(nm_reports, args.sample_limit)

    if errors:
        print("\n# errors", file=sys.stderr)
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
