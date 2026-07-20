from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "rpmsetcmp" / "check_sisyphus_set_relations.sh"
PROVIDER_SET = "set:jg2ZwFjdTD2B0"
REQUIRED_SET = "set:jiXMb"


def run_with_metadata(
    tmp_path: Path, metadata: str, *, locale: str | None = None
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    metadata_file = tmp_path / "available.txt"
    metadata_file.write_text(metadata, encoding="utf-8")

    (fake_bin / "apt-get").write_text(
        """#!/bin/sh
archive=
rpm_root=
for argument in "$@"; do
    case $argument in
        Dir::Cache::archives=*) archive=${argument#*=} ;;
        RPM::RootDir=*) rpm_root=${argument#*=} ;;
    esac
done
[ -n "$archive" ] && [ -d "$archive/partial" ]
[ -n "$rpm_root" ] && [ -d "$rpm_root/var/lib/rpm" ]
""",
        encoding="utf-8",
    )
    (fake_bin / "apt-cache").write_text(
        '#!/bin/sh\ncat "$FAKE_APT_DUMP"\n', encoding="utf-8"
    )
    (fake_bin / "apt-get").chmod(0o755)
    (fake_bin / "apt-cache").chmod(0o755)

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["FAKE_APT_DUMP"] = str(metadata_file)
    if locale is not None:
        environment["LC_ALL"] = locale
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=environment,
    )


def test_accepts_a_real_provider_subset_relation_and_continuation_line(tmp_path: Path) -> None:
    metadata = f"""\
Package: libexample
Provides: libexample.so.1()(64bit) (= {PROVIDER_SET})

Package: example
Depends: plain-dependency,
 libexample.so.1()(64bit) (>= {REQUIRED_SET})

"""

    completed = run_with_metadata(tmp_path, metadata)

    assert completed.returncode == 0, completed.stderr
    assert "requirements: 1" in completed.stdout
    assert "compatible:   1" in completed.stdout
    assert "incompatible: 0" in completed.stdout


def test_rejects_a_provider_that_does_not_contain_the_required_set(tmp_path: Path) -> None:
    metadata = f"""\
Package: libexample
Provides: libexample.so.1()(64bit) (= {REQUIRED_SET})

Package: example
Depends: libexample.so.1()(64bit) (>= {PROVIDER_SET})

"""

    completed = run_with_metadata(tmp_path, metadata)

    assert completed.returncode == 1
    assert "requirements: 1" in completed.stdout
    assert "compatible:   0" in completed.stdout
    assert "incompatible: 1" in completed.stdout
    assert "example" in completed.stdout


def test_uses_one_collation_order_for_sort_and_join(tmp_path: Path) -> None:
    metadata = f"""\
Package: userdb-provider
Provides: /usr/lib64/courier-authlib/libauthuserdb.so.0()(64bit) (= {PROVIDER_SET})

Package: auth-provider
Provides: /usr/lib64/courier-authlib/libcourierauth.so.0()(64bit) (= {PROVIDER_SET})

Package: auth-consumer
Depends: /usr/lib64/courier-authlib/libcourierauth.so.0()(64bit) (>= {REQUIRED_SET})

Package: common-consumer
Depends: /usr/lib64/courier-authlib/libcourierauthcommon.so.0()(64bit) (>= {REQUIRED_SET})

"""

    completed = run_with_metadata(tmp_path, metadata, locale="ru_RU.utf8")

    assert completed.returncode == 1
    assert "is not sorted" not in completed.stderr
    assert "requirements: 2" in completed.stdout
    assert "compatible:   1" in completed.stdout
    assert "no provider:  1" in completed.stdout


def test_large_set_is_not_passed_as_a_command_line_argument(tmp_path: Path) -> None:
    oversized_set = "set:" + "a" * 140_000
    metadata = f"""\
Package: libexample
Provides: libexample.so.1()(64bit) (= {oversized_set})

Package: example
Depends: libexample.so.1()(64bit) (>= {REQUIRED_SET})

"""

    completed = run_with_metadata(tmp_path, metadata)

    assert completed.returncode == 1
    assert "Argument list too long" not in completed.stderr
    assert "decode errors:1" in completed.stdout
