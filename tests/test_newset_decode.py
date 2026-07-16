from __future__ import annotations

import subprocess
import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def load_script(name: str):
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Produced by the original ALT mkset at bpp=16 from sym0..sym3 and sym0.
# The provider payload includes a base62 Z escape; the required payload does not.
PROVIDER_SET = "set:jg2ZwFjdTD2B0"
REQUIRED_SET = "set:jiXMb"


def test_newset_decodes_base62_escape_before_comparing_subset(tmp_path: Path) -> None:
    compare_set = load_script("compare_realization")
    compare_cmp = load_script("compare_cmp_realization")
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    run_set = compare_set.compile_runner(
        REPO_ROOT / "reimplement" / "newset.c",
        REPO_ROOT / "rpm-build",
        build_dir,
        "cc",
        "g++",
    )
    assert run_set.is_file()
    run_cmp = compare_cmp.compile_cmp_runner(build_dir, REPO_ROOT / "rpm-build", "g++")

    completed = subprocess.run(
        [str(run_cmp), PROVIDER_SET, REQUIRED_SET],
        text=True,
        capture_output=True,
        check=True,
    )
    values = dict(
        line.split("\t", 1) for line in completed.stdout.splitlines() if "\t" in line
    )

    assert values["cmp_result"] == "1"
