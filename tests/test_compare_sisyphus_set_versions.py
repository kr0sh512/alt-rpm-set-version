from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "compare_sisyphus_set_versions.sh"
PACKAGE_PARSER = REPO_ROOT / "scripts" / "parse_sisyphus_packages.awk"
RESUME_PARSER = REPO_ROOT / "scripts" / "completed_set_versions.awk"
PAYLOAD_HELPERS = REPO_ROOT / "scripts" / "rpm_payload_safety.sh"


def run_script(*arguments: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *arguments],
        cwd=REPO_ROOT,
        text=True,
        input=input_text,
        capture_output=True,
    )


def test_requires_explicit_scope_before_touching_repository() -> None:
    completed = run_script()

    assert completed.returncode == 2
    assert "use --all, --limit, or --package" in completed.stderr


def test_rejects_source_list_and_package_name_injection_before_network() -> None:
    bad_mirror = run_script("--package", "strace", "--mirror", "https://example.test/\nrpm evil")
    assert bad_mirror.returncode == 2
    assert "mirror must not contain whitespace" in bad_mirror.stderr

    bad_package = run_script("--package", "--option")
    assert bad_package.returncode == 2
    assert "invalid package name" in bad_package.stderr


def test_build_mkset_wraps_newset_and_matches_known_alt_payload(tmp_path: Path) -> None:
    output = tmp_path / "new-mkset"
    completed = run_script("--build-mkset", str(output))

    assert completed.returncode == 0, completed.stderr
    assert output.is_file()
    assert os.access(output, os.X_OK)

    encoded = subprocess.run(
        [str(output), "16"],
        input="sym0\nsym1\nsym2\nsym3\nsym0\n",
        text=True,
        capture_output=True,
        check=True,
    )
    assert encoded.stdout == "set:jg2ZwFjdTD2B0\n"

    bad_bpp = subprocess.run(
        [str(output), "16junk"],
        input="sym0\n",
        text=True,
        capture_output=True,
    )
    assert bad_bpp.returncode == 2
    assert "invalid BPP" in bad_bpp.stderr

    empty = subprocess.run(
        [str(output), "16"],
        input="",
        text=True,
        capture_output=True,
    )
    assert empty.returncode == 2
    assert "no symbols" in empty.stderr


def test_package_parser_preserves_empty_md5_field() -> None:
    metadata = """\
Package: libexample
Architecture: x86_64
Version: 1.2-alt1
Filename: libexample-1.2-alt1.x86_64.rpm
Provides: libexample.so.1 = set:abc
Depends: libc.so.6
  libc.so.6(GLIBC_2.34)(64bit) >= set:def

"""
    completed = subprocess.run(
        ["awk", "-f", str(PACKAGE_PARSER)],
        input=metadata,
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout.rstrip("\n").split("\x1c") == [
        "libexample",
        "x86_64",
        "1.2-alt1",
        "libexample-1.2-alt1.x86_64.rpm",
        "",
        "1",
        "1",
    ]


def test_resume_parser_ignores_partial_summary_rows() -> None:
    report = "\n".join(
        [
            "timestamp\trecord\tpackage\tarchitecture\tstatus\tside\tcapability\toperator\texpected\tgenerated\tdetail",
            "t\tSTART\tbroken\tx86_64\tprocessing\t-\t-\t-\t-\t-\t1-alt1",
            "t\tSUMMARY\tbroken\tx86_64\tmatch",
            "t\tSTART\tcomplete\tnoarch\tprocessing\t-\t-\t-\t-\t-\t2-alt1",
            "t\tSUMMARY\tcomplete\tnoarch\tmatch\t-\t-\t-\t-\t-\tdone; complete=1",
            "",
        ]
    )
    completed = subprocess.run(
        ["awk", "-f", str(RESUME_PARSER)],
        input=report,
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout == "complete\tnoarch\t2-alt1\n"


def test_absolute_rpm_symlink_is_rewritten_inside_temporary_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "usr" / "sbin").mkdir(parents=True)
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; rpm_safe_symlink_target "$2" "$3" "$4"',
            "bash",
            str(PAYLOAD_HELPERS),
            str(root),
            "usr/sbin/update-alternatives",
            "/bin/true",
        ],
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "../../bin/true\n"


def test_escaping_relative_rpm_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; rpm_safe_symlink_target "$2" "$3" "$4"',
            "bash",
            str(PAYLOAD_HELPERS),
            str(root),
            "usr/link",
            "../../../outside",
        ],
        text=True,
        capture_output=True,
    )

    assert completed.returncode != 0


def test_cpio_member_pattern_quotes_glob_characters() -> None:
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; rpm_cpio_literal_pattern "$2"',
            "bash",
            str(PAYLOAD_HELPERS),
            "./usr/share/doc/a[1]*?.txt",
        ],
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "./usr/share/doc/a[[]1[]][*][?].txt\n"


def test_directory_symlinks_are_installed_before_children(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    manifest = tmp_path / "symlinks.tsv"
    manifest.write_text("bin/sh\tbash\nbin\tusr/bin\n")
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; rpm_install_symlink_manifest "$2" "$3" "$4"',
            "bash",
            str(PAYLOAD_HELPERS),
            str(root),
            str(manifest),
            str(tmp_path / "extract.log"),
        ],
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert (root / "bin").is_symlink()
    assert os.readlink(root / "bin") == "usr/bin"
    assert (root / "usr" / "bin" / "sh").is_symlink()
    assert os.readlink(root / "usr" / "bin" / "sh") == "bash"


def test_child_target_is_revalidated_after_parent_alias(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    manifest = tmp_path / "symlinks.tsv"
    manifest.write_text("deep/a\t..\ndeep/a/link\t../outside\n")
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; rpm_install_symlink_manifest "$2" "$3" "$4"',
            "bash",
            str(PAYLOAD_HELPERS),
            str(root),
            str(manifest),
            str(tmp_path / "extract.log"),
        ],
        text=True,
        capture_output=True,
    )

    assert completed.returncode != 0
    assert not (tmp_path / "outside").exists()


def test_absolute_target_is_rewritten_after_parent_alias(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    manifest = tmp_path / "symlinks.tsv"
    manifest.write_text("bin\tusr/bin\nbin/tool\t/opt/tool\n")
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; rpm_install_symlink_manifest "$2" "$3" "$4"',
            "bash",
            str(PAYLOAD_HELPERS),
            str(root),
            str(manifest),
            str(tmp_path / "extract.log"),
        ],
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert os.readlink(root / "usr" / "bin" / "tool") == "../../opt/tool"
    assert (root / "usr" / "bin" / "tool").resolve(strict=False) == root / "opt" / "tool"


def test_symlinked_temporary_root_is_canonicalized(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    root_alias = tmp_path / "root-alias"
    root_alias.symlink_to(real_root, target_is_directory=True)
    manifest = tmp_path / "symlinks.tsv"
    manifest.write_text("bin\tusr/bin\n")
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; rpm_install_symlink_manifest "$2" "$3" "$4"',
            "bash",
            str(PAYLOAD_HELPERS),
            str(root_alias),
            str(manifest),
            str(tmp_path / "extract.log"),
        ],
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert (real_root / "bin").is_symlink()


def test_rpm_file_record_with_tab_in_filename_is_rejected() -> None:
    record = "/name\tpart\x1c"
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; rpm_split_file_record "$2"',
            "bash",
            str(PAYLOAD_HELPERS),
            record,
        ],
        text=True,
        capture_output=True,
    )

    assert completed.returncode != 0


def test_help_documents_streaming_report_and_test_scope() -> None:
    completed = run_script("--help")

    assert completed.returncode == 0
    assert "--report FILE" in completed.stdout
    assert "--package NAME" in completed.stdout
    assert "--limit N" in completed.stdout
    assert "--all" in completed.stdout
    assert "appended after every package" in completed.stdout
