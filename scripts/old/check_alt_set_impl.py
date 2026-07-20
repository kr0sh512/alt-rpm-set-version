#!/usr/bin/env python3
"""Check package compatibility using this repo's Python set implementation.

This script intentionally does *not* compare with ALT's existing set.c-produced
set strings. It uses ALT only as a source of real binary RPMs:

* Provided labels:  `nm --dynamic -j -U <shared-library>`
* Required labels:  `nm --dynamic -j -u <elf-file>`

Both sides are encoded with `reimplement/set.py`, then compared with that same
implementation's `rpmsetcmp()`. In other words, it checks whether the current
Python implementation is internally useful for real ALT package symbol labels.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reimplement import set as rpmset  # noqa: E402

API_BASE = "https://rdb.altlinux.org/api"
DEFAULT_PROVIDERS = ["glibc-core", "zlib", "libssl3", "libcrypto3"]
DEFAULT_REQUIRERS = ["coreutils", "curl", "openssl"]


@dataclass(frozen=True)
class PackageRPM:
    name: str
    pkghash: str
    rpm_path: Path
    extract_dir: Path
    members: list[str]


@dataclass(frozen=True)
class LabelSet:
    role: str
    package: str
    member: str
    labels: tuple[str, ...]
    set_string: str

    @property
    def label_count(self) -> int:
        return len(self.labels)

    @property
    def set_len(self) -> int:
        return len(self.set_string)


@dataclass(frozen=True)
class CompatibilityResult:
    provider_package: str
    provider_member: str
    requirer_package: str
    requirer_member: str
    provider_labels: int
    required_labels: int
    cmp_result: int
    status: str


def api_json(path: str, params: dict[str, object] | None = None) -> dict:
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": "arsv-alt-set-compat/1.0"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def get_pkghash(package: str, branch: str, arch: str) -> str:
    data = api_json(
        "/site/pkghash_by_binary_name",
        {"branch": branch, "name": package, "arch": arch},
    )
    return str(data["pkghash"])


def package_download_url(pkghash: str, branch: str, arch: str) -> str:
    data = api_json(
        f"/site/package_downloads_bin/{pkghash}",
        {"branch": branch, "arch": arch},
    )
    downloads = data.get("downloads") or []
    if not downloads or not downloads[0].get("packages"):
        raise RuntimeError(f"no download URL for pkghash={pkghash}")
    return downloads[0]["packages"][0]["url"]


def run_text(command: list[str], cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(command)} failed with {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def download_file(url: str, destination: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "arsv-alt-set-compat/1.0"})
    with urllib.request.urlopen(req, timeout=120) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out)


def rpm_members(rpm_path: Path) -> list[str]:
    return [line for line in run_text(["bsdtar", "-tf", str(rpm_path)]).splitlines() if line]


def is_shared_library_member(member: str) -> bool:
    name = Path(member).name
    return not member.endswith("/") and re.search(r"(?:^|/)lib[^/]*\.so(?:\.|$)", member) is not None and ".debug" not in name


def select_provider_members(members: Iterable[str]) -> list[str]:
    """Files whose defined dynamic symbols are Provided labels."""
    return [member for member in members if is_shared_library_member(member)]


def select_requirer_members(members: Iterable[str]) -> list[str]:
    """Files whose undefined dynamic symbols are Required labels."""
    executable_prefixes = ("./bin/", "./usr/bin/", "./sbin/", "./usr/sbin/", "./usr/lib/systemd/")
    selected = []
    for member in members:
        if member.endswith("/"):
            continue
        if is_shared_library_member(member) or member.startswith(executable_prefixes):
            selected.append(member)
    return selected


def extract_members(rpm_path: Path, extract_dir: Path, members: Iterable[str]) -> None:
    unique_members = sorted(set(members))
    if unique_members:
        run_text(["bsdtar", "-xf", str(rpm_path), "-C", str(extract_dir), *unique_members])


def nm_symbols(path: Path, mode: str) -> list[str]:
    command = ["nm", "--dynamic", "-j", "-U", str(path)] if mode == "provided" else ["nm", "--dynamic", "-u", str(path)]
    proc = subprocess.run(command, text=True, capture_output=True)
    if proc.returncode != 0:
        return []
    if mode == "required":
        return parse_required_nm_output(proc.stdout)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def parse_required_nm_output(output: str) -> list[str]:
    """Parse `nm --dynamic -u` output, ignoring weak undefined references.

    Plain `nm -j -u` discards the symbol type, but real ALT RPMs contain weak
    undefined hooks like `__gmon_start__` and `_ITM_*`. Those are optional ELF
    references, not hard Required labels, so keep only strong `U` entries.
    """
    symbols = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        symbol_type, symbol = parts[-2], parts[-1]
        if symbol_type == "U":
            symbols.append(symbol)
    return symbols


def normalize_required_symbol(symbol: str) -> str:
    """Normalize `foo@VER` from undefined nm output to provider-like `foo@@VER`.

    `nm -u` prints required versioned symbols with a single `@`, while defined
    default-version symbols commonly use `@@`. This normalization is for this
    script's compatibility model only; it is not a set.c compatibility shim.
    """
    if "@@" in symbol or "@" not in symbol:
        return symbol
    name, version = symbol.split("@", 1)
    if not name or not version:
        return symbol
    return f"{name}@@{version}"


def labels_to_set_string(labels: Iterable[str], bpp: int) -> str | None:
    item_set = rpmset.set_new()
    for label in labels:
        rpmset.set_add(item_set, label)
    # reimplement/set.py prints collision warnings to stdout; keep this script's
    # stdout machine-readable and route those warnings to stderr instead.
    with contextlib.redirect_stdout(sys.stderr):
        return rpmset.set_fini(item_set, bpp)


def generate_label_set(role: str, member: str, labels: Iterable[str], bpp: int, package: str = "") -> LabelSet:
    unique_labels = tuple(sorted(set(label for label in labels if label)))
    set_string = labels_to_set_string(unique_labels, bpp)
    if set_string is None:
        raise ValueError(f"no labels for {role} {package}:{member}")
    return LabelSet(role=role, package=package, member=member, labels=unique_labels, set_string=set_string)


def compare_status(cmp_result: int) -> str:
    # provider is first argument; compatible means provider is equal or superset.
    if cmp_result in (0, 1):
        return "compatible"
    return "incompatible"


def compare_label_sets(provider: LabelSet, requirer: LabelSet) -> CompatibilityResult:
    cmp_result = rpmset.rpmsetcmp(provider.set_string, requirer.set_string)
    return CompatibilityResult(
        provider_package=provider.package,
        provider_member=provider.member,
        requirer_package=requirer.package,
        requirer_member=requirer.member,
        provider_labels=provider.label_count,
        required_labels=requirer.label_count,
        cmp_result=cmp_result,
        status=compare_status(cmp_result),
    )


def build_dependency_results(provider_sets: list[LabelSet], requirer_sets: list[LabelSet], bpp: int) -> list[CompatibilityResult]:
    """Compare each library only with the symbols actually required from it.

    A package executable/library has one undefined-symbol list containing symbols
    required from all of its DT_NEEDED libraries. Comparing that whole list with
    one provider library gives false ``-2`` results: e.g. ``/usr/bin/curl`` needs
    symbols from libc, libssl, libcrypto, zlib, etc., and no single library is
    supposed to provide all of them.

    For this local set.py check, keep the symbol-level ground truth around and
    split every requirer's labels by provider library: provider labels ∩ required
    labels. Each non-empty subset is then encoded as the requirement for exactly
    that provider and compared with ``rpmsetcmp(provider, required_subset)``.
    """
    results: list[CompatibilityResult] = []
    for requirer in requirer_sets:
        required_labels = set(requirer.labels)
        for provider in provider_sets:
            required_from_provider = sorted(required_labels.intersection(provider.labels))
            if not required_from_provider:
                continue
            split_requirer = generate_label_set(
                "required",
                requirer.member,
                required_from_provider,
                bpp,
                package=requirer.package,
            )
            results.append(compare_label_sets(provider, split_requirer))
    return results


def fetch_package_rpm(package: str, branch: str, arch: str, workdir: Path) -> PackageRPM:
    pkghash = get_pkghash(package, branch, arch)
    rpm_url = package_download_url(pkghash, branch, arch)
    rpm_path = workdir / Path(urllib.parse.urlparse(rpm_url).path).name
    if not rpm_path.exists():
        download_file(rpm_url, rpm_path)
    members = rpm_members(rpm_path)
    extract_dir = workdir / f"extract-{package.replace('/', '_').replace('+', '_')}"
    extract_dir.mkdir(parents=True, exist_ok=True)
    return PackageRPM(package, pkghash, rpm_path, extract_dir, members)


def build_provider_sets(package_rpm: PackageRPM, bpp: int, max_files: int) -> list[LabelSet]:
    members = select_provider_members(package_rpm.members)[:max_files]
    extract_members(package_rpm.rpm_path, package_rpm.extract_dir, members)
    sets = []
    for member in members:
        labels = nm_symbols(package_rpm.extract_dir / member, "provided")
        if labels:
            sets.append(generate_label_set("provided", member, labels, bpp, package_rpm.name))
    return sets


def build_requirer_sets(
    package_rpm: PackageRPM,
    bpp: int,
    max_files: int,
    normalize_versions: bool,
) -> list[LabelSet]:
    members = select_requirer_members(package_rpm.members)[:max_files]
    extract_members(package_rpm.rpm_path, package_rpm.extract_dir, members)
    sets = []
    for member in members:
        labels = nm_symbols(package_rpm.extract_dir / member, "required")
        if normalize_versions:
            labels = [normalize_required_symbol(label) for label in labels]
        if labels:
            sets.append(generate_label_set("required", member, labels, bpp, package_rpm.name))
    return sets


def aggregate_label_sets(role: str, package_names: list[str], label_sets: list[LabelSet], bpp: int) -> LabelSet:
    labels = [label for label_set in label_sets for label in label_set.labels]
    return generate_label_set(role, "+".join(package_names), labels, bpp, package="aggregate")


def print_label_sets(title: str, label_sets: list[LabelSet]) -> None:
    print(f"\n# {title}")
    print("role\tpackage\tmember\tlabels\tset_len\tset_prefix")
    for label_set in label_sets:
        print(
            f"{label_set.role}\t{label_set.package}\t{label_set.member}\t"
            f"{label_set.label_count}\t{label_set.set_len}\t{label_set.set_string[:48]}"
        )


def print_results(results: list[CompatibilityResult]) -> None:
    print("\n# compatibility")
    print("status\tcmp\tprovider_pkg\tprovider_member\tprovider_labels\trequirer_pkg\trequirer_member\trequired_labels")
    for result in results:
        print(
            f"{result.status}\t{result.cmp_result}\t{result.provider_package}\t{result.provider_member}\t"
            f"{result.provider_labels}\t{result.requirer_package}\t{result.requirer_member}\t{result.required_labels}"
        )
    summary = {status: sum(1 for result in results if result.status == status) for status in sorted({r.status for r in results})}
    print(f"summary: {summary}")


def parse_package_list(values: list[str] | None) -> list[str]:
    packages = []
    for value in values or []:
        packages.extend(part for part in value.split(",") if part)
    return packages


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Provided/Required set strings from ALT RPM labels with reimplement/set.py and compare them."
    )
    parser.add_argument("packages", nargs="*", help="packages used as both providers and requirers if explicit lists are omitted")
    parser.add_argument("--provider", action="append", help="provider package; can be repeated or comma-separated")
    parser.add_argument("--requirer", action="append", help="requirer package; can be repeated or comma-separated")
    parser.add_argument("--branch", default="sisyphus", help="ALT repository branch/packageset")
    parser.add_argument("--arch", default="x86_64", help="binary package architecture")
    parser.add_argument("--bpp", type=int, default=32, help="bits per hash used by local set.py")
    parser.add_argument("--max-provider-files", type=int, default=64, help="max provider ELF files per package")
    parser.add_argument("--max-requirer-files", type=int, default=64, help="max requirer ELF files per package")
    parser.add_argument("--all-pairs", action="store_true", help="compare every provider file set with every requirer file set")
    parser.add_argument(
        "--no-normalize-required-version",
        action="store_true",
        help="keep nm -u single-@ required symbols unchanged instead of converting foo@VER to foo@@VER",
    )
    parser.add_argument("--keep-workdir", action="store_true", help="keep downloaded RPMs/extracted files")
    parser.add_argument("--workdir", type=Path, help="directory for downloads/extraction")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    missing = [cmd for cmd in ("bsdtar", "nm") if shutil.which(cmd) is None]
    if missing:
        print(f"missing required command(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    positional = args.packages or []
    providers = parse_package_list(args.provider) or positional or DEFAULT_PROVIDERS
    requirers = parse_package_list(args.requirer) or positional or DEFAULT_REQUIRERS

    cleanup = False
    if args.workdir:
        workdir = args.workdir
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        workdir = Path(tempfile.mkdtemp(prefix="arsv-alt-set-compat-"))
        cleanup = not args.keep_workdir

    try:
        provider_sets: list[LabelSet] = []
        requirer_sets: list[LabelSet] = []
        for package in providers:
            provider_sets.extend(
                build_provider_sets(fetch_package_rpm(package, args.branch, args.arch, workdir), args.bpp, args.max_provider_files)
            )
        for package in requirers:
            requirer_sets.extend(
                build_requirer_sets(
                    fetch_package_rpm(package, args.branch, args.arch, workdir),
                    args.bpp,
                    args.max_requirer_files,
                    normalize_versions=not args.no_normalize_required_version,
                )
            )

        print(f"branch: {args.branch}")
        print(f"arch: {args.arch}")
        print(f"bpp: {args.bpp}")
        print(f"providers: {', '.join(providers)}")
        print(f"requirers: {', '.join(requirers)}")
        print(f"required symbol version normalization: {not args.no_normalize_required_version}")
        print_label_sets("generated Provided sets", provider_sets)
        print_label_sets("generated Required sets", requirer_sets)

        if not provider_sets or not requirer_sets:
            print("\nNo comparable sets generated.", file=sys.stderr)
            return 1

        if args.all_pairs:
            results = [compare_label_sets(provider, requirer) for provider in provider_sets for requirer in requirer_sets]
        else:
            results = build_dependency_results(provider_sets, requirer_sets, args.bpp)
        print_results(results)
    finally:
        if cleanup:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"workdir: {workdir}")

    return 0 if all(result.status == "compatible" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
