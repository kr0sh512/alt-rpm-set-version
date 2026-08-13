#!/usr/bin/env python3
"""Create D1 copies of the real Sisyphus x86_64 and noarch pkglist files."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DEFAULT_IMAGE = "registry.altlinux.org/sisyphus/alt:latest"
ARCHITECTURES = ("x86_64", "noarch")
SUMMARY_RE = re.compile(
    r"headers=(?P<headers>\d+) set_occurrences=(?P<set_occurrences>\d+) "
    r"hashes=(?P<hashes>\d+) old_set_bytes=(?P<old_set_bytes>\d+) "
    r"new_set_bytes=(?P<new_set_bytes>\d+)"
)
VERSION_FORMAT = (
    "[%{REQUIREVERSION}\\n]"
    "[%{PROVIDEVERSION}\\n]"
    "[%{CONFLICTVERSION}\\n]"
    "[%{OBSOLETEVERSION}\\n]"
    "[%{RECOMMENDVERSION}\\n]"
    "[%{SUGGESTVERSION}\\n]"
    "[%{SUPPLEMENTVERSION}\\n]"
    "[%{ENHANCEVERSION}\\n]"
)


def classify_pkglist(path: Path) -> str | None:
    name = path.name
    if not name.endswith("_base_pkglist.classic"):
        return None
    if "_Sisyphus_x86%5f64_" in name or "_Sisyphus_x86_64_" in name:
        return "x86_64"
    if "_Sisyphus_noarch_" in name:
        return "noarch"
    return None


def validate_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    home = Path.home().resolve()
    source = ROOT.resolve()
    if resolved == home or resolved == source or source in resolved.parents:
        raise ValueError(f"refusing protected output path: {resolved}")
    if resolved == Path("/"):
        raise ValueError("refusing filesystem root as output")
    return resolved


def run(
    command: list[str],
    *,
    check: bool = True,
    cwd: Path | None = None,
    stdout: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        command,
        cwd=cwd,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}\n{stderr}")
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def compile_converter(destination: Path) -> None:
    include = destination.parent / "compat-include"
    include.mkdir()
    for name in ("rpmlib.h", "system.h", "set.h"):
        (include / name).touch()
    run(
        [
            "cc",
            "-O2",
            "-std=gnu11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-D_GNU_SOURCE",
            "-DARSV_SET9_EXPORT",
            "-DARSV_WITH_RPM",
            "-I",
            str(include),
            "-include",
            str(ROOT / "scripts/rpmsetcmp/newset_compat.h"),
            str(ROOT / "reimplement/set9.c"),
            str(HERE / "rewrite_sisyphus_pkglist.c"),
            "-lrpm",
            "-lrpmio",
            "-o",
            str(destination),
        ]
    )


def find_pkglists(lists_dir: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in lists_dir.glob("*_base_pkglist.classic"):
        architecture = classify_pkglist(path)
        if architecture is None:
            continue
        if architecture in found:
            raise RuntimeError(f"multiple Sisyphus {architecture} pkglist files")
        found[architecture] = path
    missing = set(ARCHITECTURES) - set(found)
    if missing:
        raise RuntimeError(f"missing Sisyphus pkglist for: {', '.join(sorted(missing))}")
    return found


def query_header_count(path: Path) -> int:
    process = subprocess.Popen(
        ["pkglist-query", "%{NAME}\\n", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    count = sum(1 for _ in process.stdout)
    stderr = process.stderr.read() if process.stderr else b""
    status = process.wait()
    if status != 0:
        raise RuntimeError(f"pkglist-query failed for {path}: {stderr.decode(errors='replace')}")
    return count


def query_set_counts(path: Path) -> dict[str, int]:
    process = subprocess.Popen(
        ["pkglist-query", VERSION_FORMAT, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    old = direct = 0
    for raw_line in process.stdout:
        value = raw_line.strip()
        if value.startswith(b"set:D1"):
            direct += 1
        elif value.startswith(b"set:"):
            old += 1
    stderr = process.stderr.read() if process.stderr else b""
    status = process.wait()
    if status != 0:
        raise RuntimeError(f"pkglist-query failed for {path}: {stderr.decode(errors='replace')}")
    return {"set9": old, "d1": direct}


def conversion_summary(stderr: bytes) -> dict[str, int]:
    text = stderr.decode(errors="replace")
    match = SUMMARY_RE.search(text)
    if not match:
        raise RuntimeError(f"converter did not report summary:\n{text}")
    return {name: int(value) for name, value in match.groupdict().items()}


def convert_one(converter: Path, source: Path, destination: Path) -> dict[str, object]:
    result = run([str(converter), "--rewrite", str(source), str(destination)])
    summary = conversion_summary(result.stderr)
    source_headers = query_header_count(source)
    output_headers = query_header_count(destination)
    source_sets = query_set_counts(source)
    output_sets = query_set_counts(destination)
    if source_headers != output_headers or output_headers != summary["headers"]:
        raise RuntimeError(
            f"header count mismatch: source={source_headers} output={output_headers} "
            f"converter={summary['headers']}"
        )
    if source_sets["d1"] != 0:
        raise RuntimeError(f"source already contains {source_sets['d1']} D1 values")
    if output_sets["set9"] != 0:
        raise RuntimeError(f"output still contains {output_sets['set9']} set9 values")
    if source_sets["set9"] != output_sets["d1"] or output_sets["d1"] != summary["set_occurrences"]:
        raise RuntimeError(
            "set occurrence mismatch: "
            f"source={source_sets['set9']} output={output_sets['d1']} "
            f"converter={summary['set_occurrences']}"
        )
    if summary["set_occurrences"] == 0:
        raise RuntimeError("source pkglist contains no set versions")
    return {
        **summary,
        "source": {
            "path": source.name,
            "size": source.stat().st_size,
            "sha256": sha256_file(source),
        },
        "output": {
            "path": destination.name,
            "size": destination.stat().st_size,
            "sha256": sha256_file(destination),
        },
    }


def image_metadata(image: str) -> dict[str, str]:
    result = run(
        [
            "podman",
            "image",
            "inspect",
            image,
            "--format",
            "{{.Digest}}|{{.Id}}",
        ]
    )
    digest, image_id = result.stdout.decode().strip().split("|", 1)
    return {"name": image, "digest": digest, "id": image_id}


def inner(output: Path, image: str, image_digest: str, image_id: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise RuntimeError(f"output directory is not empty: {output}")

    pkglists = find_pkglists(Path("/var/lib/apt/lists"))
    staging = Path(tempfile.mkdtemp(prefix=".d1-staging-", dir=output))
    try:
        converter = staging / "rewrite-sisyphus-pkglist"
        compile_converter(converter)
        architectures: dict[str, object] = {}
        for architecture in ARCHITECTURES:
            destination = staging / f"Sisyphus.{architecture}.pkglist.classic"
            architectures[architecture] = convert_one(
                converter, pkglists[architecture], destination
            )
        converter.unlink()
        shutil.rmtree(staging / "compat-include")
        manifest = {
            "format": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "image": {"name": image, "digest": image_digest, "id": image_id},
            "rpm": run(["rpm", "--version"]).stdout.decode().strip(),
            "apt": run(["rpmquery", "--qf", "%{VERSION}-%{RELEASE}", "apt"])
            .stdout.decode()
            .strip(),
            "converter_source": {
                "set9_sha256": sha256_file(ROOT / "reimplement/set9.c"),
                "rewrite_sha256": sha256_file(HERE / "rewrite_sisyphus_pkglist.c"),
            },
            "architectures": architectures,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
        final = output / "d1-pkglists"
        staging.replace(final)
        print(final)
        for architecture in ARCHITECTURES:
            data = architectures[architecture]
            assert isinstance(data, dict)
            print(
                f"{architecture}: headers={data['headers']} "
                f"set_occurrences={data['set_occurrences']} "
                f"output={final / f'Sisyphus.{architecture}.pkglist.classic'}"
            )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def outer(output: Path, image: str) -> None:
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    run(["podman", "pull", image], stdout=None)
    metadata = image_metadata(image)
    inner_command = [
        "python3",
        "/src/new_version/direct_hash/apt_benchmark/run_sisyphus_pkglist.py",
        "--inner",
        "/out",
        "--image",
        image,
        "--image-digest",
        metadata["digest"],
        "--image-id",
        metadata["id"],
    ]
    command = [
        "podman",
        "run",
        "--rm",
        "-v",
        f"{ROOT}:/src:ro",
        "-v",
        f"{output}:/out:rw",
        image,
        "sh",
        "-lc",
        (
            "apt-get update >/dev/null && "
            "apt-get install -y gcc librpm-devel apt-repo-tools python3 >/dev/null && "
            + " ".join(shlex.quote(argument) for argument in inner_command)
        ),
    ]
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"container conversion failed with status {result.returncode}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create D1 copies of current Sisyphus x86_64/noarch pkglist files"
    )
    parser.add_argument("output", type=Path, help="new or empty output directory")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--image-digest", default="", help=argparse.SUPPRESS)
    parser.add_argument("--image-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = validate_output(args.output)
        if args.inner:
            inner(output, args.image, args.image_digest, args.image_id)
        else:
            outer(output, args.image)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
