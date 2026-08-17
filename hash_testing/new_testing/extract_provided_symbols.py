#!/usr/bin/env python3
"""Extract ALT RPM Provides symbol names into a hash-testing corpus.

Local RPM paths are processed directly.  Other positional arguments are resolved
as binary package names through the ALT Repository Database (RDB).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Sequence, cast


RDB_BASE = "https://rdb.altlinux.org/api"
NEWC_MAGICS = {b"070701", b"070702"}
NEWC_HEADER_SIZE = 110
COPY_CHUNK_SIZE = 1024 * 1024
MAX_CPIO_NAME_SIZE = 1024 * 1024


class CorpusError(RuntimeError):
    """A user-facing extraction error."""


@dataclass(frozen=True)
class PackageInput:
    label: str
    path: Path


@dataclass(frozen=True)
class PackageSymbols:
    label: str
    elf_files: int
    symbols: frozenset[str]


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract canonical ALT rpm-build Provided ELF symbols from multiple "
            "RPM packages, one symbol per output line."
        )
    )
    parser.add_argument(
        "packages",
        nargs="+",
        help="local .rpm path or ALT binary package name",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="-",
        help="output file (default: stdout)",
    )
    parser.add_argument(
        "--cpp-only",
        action="store_true",
        help="keep only Itanium C++ ABI mangled names beginning with _Z",
    )
    parser.add_argument(
        "--branch",
        default="p11",
        help="ALT branch for package-name resolution (default: p11)",
    )
    parser.add_argument(
        "--arch",
        default="x86_64",
        help="binary package architecture (default: x86_64)",
    )
    parser.add_argument(
        "--rpm2cpio",
        default=shutil.which("rpm2cpio") or "rpm2cpio",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--provided-symbols",
        default=(
            "/usr/lib/rpm/provided_symbols"
            if Path("/usr/lib/rpm/provided_symbols").is_file()
            else shutil.which("provided_symbols") or "provided_symbols"
        ),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--file-command",
        default=shutil.which("file") or "file",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def read_json(url: str) -> dict[str, object]:
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return json.load(response)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise CorpusError(f"failed to read ALT RDB response from {url}: {error}") from error


def download_package(name: str, branch: str, arch: str, directory: Path) -> PackageInput:
    query = urllib.parse.urlencode({"branch": branch, "name": name, "arch": arch})
    metadata_url = f"{RDB_BASE}/site/pkghash_by_binary_name?{query}"
    metadata = read_json(metadata_url)
    package_hash = metadata.get("pkghash")
    if not isinstance(package_hash, str) or not package_hash:
        raise CorpusError(f"ALT RDB did not return a package hash for {name!r}")

    download_url = (
        f"{RDB_BASE}/site/package_downloads_bin/{package_hash}?"
        f"{urllib.parse.urlencode({'branch': branch, 'arch': arch})}"
    )
    download_data = read_json(download_url)
    downloads = download_data.get("downloads")
    if not isinstance(downloads, list):
        raise CorpusError(f"ALT RDB did not return downloads for {name!r}")

    package_record: dict[str, object] | None = None
    for architecture_record in downloads:
        if not isinstance(architecture_record, dict):
            continue
        if architecture_record.get("arch") != arch:
            continue
        packages = architecture_record.get("packages")
        if isinstance(packages, list):
            for candidate in packages:
                if isinstance(candidate, dict):
                    package_record = candidate
                    break
        if package_record is not None:
            break

    if package_record is None:
        raise CorpusError(f"ALT RDB has no {arch} RPM download for {name!r}")

    filename = package_record.get("name")
    url = package_record.get("url")
    expected_md5 = package_record.get("md5")
    if not isinstance(filename, str) or not filename.endswith(".rpm"):
        raise CorpusError(f"ALT RDB returned an invalid RPM filename for {name!r}")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise CorpusError(f"ALT RDB returned an invalid RPM URL for {name!r}")

    destination = directory / filename
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.md5(usedforsecurity=False)
    try:
        with urllib.request.urlopen(url, timeout=180) as response, temporary.open("wb") as output:
            while chunk := response.read(COPY_CHUNK_SIZE):
                output.write(chunk)
                digest.update(chunk)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise CorpusError(f"failed to download {name!r} from {url}: {error}") from error

    if isinstance(expected_md5, str) and digest.hexdigest().lower() != expected_md5.lower():
        temporary.unlink(missing_ok=True)
        raise CorpusError(f"MD5 mismatch for downloaded package {name!r}")
    temporary.replace(destination)
    return PackageInput(f"{name} ({filename})", destination)


def resolve_packages(
    specifications: Sequence[str], branch: str, arch: str, directory: Path
) -> list[PackageInput]:
    resolved: list[PackageInput] = []
    seen: set[tuple[str, str]] = set()
    for specification in specifications:
        candidate = Path(specification).expanduser()
        if candidate.is_file():
            package = PackageInput(candidate.name, candidate.resolve())
        elif candidate.exists():
            raise CorpusError(f"package input is not a regular file: {candidate}")
        elif "/" in specification or specification.endswith(".rpm"):
            raise CorpusError(f"RPM file does not exist: {candidate}")
        else:
            package = download_package(specification, branch, arch, directory)

        identity = (package.label, str(package.path))
        if identity not in seen:
            resolved.append(package)
            seen.add(identity)
    return resolved


def read_exact(stream: BinaryIO, size: int, context: str) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise CorpusError(f"truncated newc stream while reading {context}")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def discard_exact(stream: BinaryIO, size: int, context: str) -> None:
    remaining = size
    while remaining:
        chunk = stream.read(min(remaining, COPY_CHUNK_SIZE))
        if not chunk:
            raise CorpusError(f"truncated newc stream while reading {context}")
        remaining -= len(chunk)


def copy_exact(stream: BinaryIO, output: BinaryIO, size: int, context: str) -> None:
    remaining = size
    while remaining:
        chunk = stream.read(min(remaining, COPY_CHUNK_SIZE))
        if not chunk:
            raise CorpusError(f"truncated newc stream while reading {context}")
        output.write(chunk)
        remaining -= len(chunk)


def parse_newc_header(header: bytes) -> tuple[int, int, int]:
    if len(header) != NEWC_HEADER_SIZE or header[:6] not in NEWC_MAGICS:
        raise CorpusError("rpm2cpio output is not a valid newc archive")
    try:
        fields = [int(header[6 + index * 8 : 14 + index * 8], 16) for index in range(13)]
    except ValueError as error:
        raise CorpusError("newc header contains a non-hexadecimal field") from error
    mode = fields[1]
    file_size = fields[6]
    name_size = fields[11]
    if name_size < 1 or name_size > MAX_CPIO_NAME_SIZE:
        raise CorpusError(f"invalid newc pathname size: {name_size}")
    return mode, file_size, name_size


def extract_elf_members(stream: BinaryIO, directory: Path) -> list[Path]:
    elf_files: list[Path] = []
    member_index = 0
    while True:
        header = read_exact(stream, NEWC_HEADER_SIZE, "header")
        mode, file_size, name_size = parse_newc_header(header)
        raw_name = read_exact(stream, name_size, "pathname")
        if raw_name[-1:] != b"\0":
            raise CorpusError("newc pathname is not NUL-terminated")
        discard_exact(stream, -(NEWC_HEADER_SIZE + name_size) % 4, "pathname padding")

        if raw_name[:-1] == b"TRAILER!!!":
            discard_exact(stream, file_size, "trailer data")
            discard_exact(stream, -file_size % 4, "trailer padding")
            break

        member_index += 1
        prefix_size = min(file_size, 4)
        prefix = read_exact(stream, prefix_size, "member data")
        remaining = file_size - prefix_size
        if stat.S_ISREG(mode) and prefix == b"\x7fELF":
            destination = directory / f"elf-{member_index:06d}"
            with destination.open("wb") as output:
                output.write(prefix)
                copy_exact(stream, output, remaining, "ELF member data")
            elf_files.append(destination)
        else:
            discard_exact(stream, remaining, "member data")
        discard_exact(stream, -file_size % 4, "member padding")
    return elf_files


def extract_package_symbols(
    package: PackageInput,
    rpm2cpio: str,
    provided_symbols: str,
    file_command: str,
    directory: Path,
) -> PackageSymbols:
    package_directory = directory / f"package-{len(list(directory.iterdir())):04d}"
    package_directory.mkdir()
    process = subprocess.Popen(
        [rpm2cpio, str(package.path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        extracted_elf_files = extract_elf_members(
            cast(BinaryIO, process.stdout), package_directory
        )
    except Exception:
        process.kill()
        process.communicate()
        raise
    finally:
        process.stdout.close()

    stderr = process.stderr.read().decode(errors="replace")
    return_code = process.wait()
    if return_code != 0:
        raise CorpusError(
            f"rpm2cpio failed for {package.label} with status {return_code}: {stderr.strip()}"
        )

    elf_files: list[Path] = []
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    for path in extracted_elf_files:
        result = subprocess.run(
            [file_command, "--brief", "--", str(path)],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        if result.returncode != 0:
            raise CorpusError(
                f"file failed for {package.label} with status "
                f"{result.returncode}: {result.stderr.strip()}"
            )
        description = f" {result.stdout.strip()} "
        if (
            " ELF " in description
            and " shared object, " in description
            and " shared object, no machine, " not in description
        ):
            elf_files.append(path)

    if not elf_files:
        return PackageSymbols(package.label, 0, frozenset())

    result = subprocess.run(
        [provided_symbols, *(str(path) for path in elf_files)],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise CorpusError(
            f"provided_symbols failed for {package.label} with status "
            f"{result.returncode}: {result.stderr.strip()}"
        )
    symbols = frozenset(line for line in result.stdout.splitlines() if line)
    return PackageSymbols(package.label, len(elf_files), symbols)


def render_symbols(symbols: set[str]) -> str:
    if not symbols:
        return ""
    return "\n".join(sorted(symbols)) + "\n"


def write_output(path: str, content: str) -> None:
    if path == "-":
        sys.stdout.write(content)
        return
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=destination.parent, delete=False
    ) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    temporary_path.replace(destination)


def run(arguments: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="provided-symbols-") as temporary_name:
        temporary = Path(temporary_name)
        download_directory = temporary / "downloads"
        extraction_directory = temporary / "extract"
        download_directory.mkdir()
        extraction_directory.mkdir()
        packages = resolve_packages(
            arguments.packages, arguments.branch, arguments.arch, download_directory
        )

        combined: set[str] = set()
        total_elf_files = 0
        for package in packages:
            package_symbols = extract_package_symbols(
                package,
                arguments.rpm2cpio,
                arguments.provided_symbols,
                arguments.file_command,
                extraction_directory,
            )
            selected = {
                symbol
                for symbol in package_symbols.symbols
                if not arguments.cpp_only or symbol.startswith("_Z")
            }
            combined.update(selected)
            total_elf_files += package_symbols.elf_files
            print(
                f"package={package_symbols.label} "
                f"elf_files={package_symbols.elf_files} "
                f"symbols={len(package_symbols.symbols)} selected={len(selected)}",
                file=sys.stderr,
            )

        write_output(arguments.output, render_symbols(combined))
        print(
            f"packages={len(packages)} elf_files={total_elf_files} "
            f"unique_symbols={len(combined)} output={arguments.output}",
            file=sys.stderr,
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(parse_arguments(argv))
    except (CorpusError, OSError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
