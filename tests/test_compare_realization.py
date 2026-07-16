from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "compare_realization.py"


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=True)


def make_fake_rpm_tree(tmp_path: Path) -> tuple[Path, Path]:
    rpm_root = tmp_path / "rpm"
    (rpm_root / "rpmio").mkdir(parents=True)
    (rpm_root / "lib").mkdir()
    (rpm_root / "system.h").write_text("#pragma once\n", encoding="utf-8")
    (rpm_root / "rpmio" / "rpmmalloc.c").write_text("/* test stub */\n", encoding="utf-8")
    (rpm_root / "lib" / "placeholder.h").write_text("#pragma once\n", encoding="utf-8")

    set_source = tmp_path / "fake_set.c"
    set_source.write_text(
        r'''
#include <stdlib.h>
#include <string.h>

struct set {
    size_t count;
};

struct set *set_new(void)
{
    return calloc(1, sizeof(struct set));
}

void set_add(struct set *set, const char *symbol)
{
    (void) symbol;
    ++set->count;
}

const char *set_fini(struct set *set, int bpp)
{
    (void) set;
    (void) bpp;
    return strdup("fake-payload");
}

struct set *set_free(struct set *set)
{
    free(set);
    return NULL;
}
''',
        encoding="utf-8",
    )
    return rpm_root, set_source


def make_provider(tmp_path: Path) -> Path:
    source = tmp_path / "provider.c"
    source.write_text("int benchmark_label(void) { return 7; }\n", encoding="utf-8")
    provider = tmp_path / "libprovider.so.1"
    run(
        [
            "cc",
            "-shared",
            "-fPIC",
            "-Wl,-soname,libprovider.so.1",
            str(source),
            "-o",
            str(provider),
        ]
    )
    return provider


def test_compiles_selected_set_runs_n_times_and_writes_median_results(tmp_path: Path) -> None:
    rpm_root, set_source = make_fake_rpm_tree(tmp_path)
    provider = make_provider(tmp_path)
    result_root = tmp_path / "res"

    completed = run(
        [
            sys.executable,
            str(SCRIPT),
            str(set_source),
            "-n",
            "3",
            "--rpm-root",
            str(rpm_root),
            "--res-dir",
            str(result_root),
            "--name",
            "fake",
            "--target",
            str(provider),
        ]
    )

    implementation_dir = result_root / "fake"
    assert (implementation_dir / "build" / "run_set").is_file()
    assert (implementation_dir / "build" / "build.log").is_file()

    raw_outputs = sorted((implementation_dir / "runs" / "libprovider.so.1").glob("run-*.tsv"))
    assert len(raw_outputs) == 3
    assert all("set:fake-payload" in output.read_text(encoding="utf-8") for output in raw_outputs)

    with (implementation_dir / "summary.tsv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    assert len(rows) == 1
    row = rows[0]
    assert row["target"] == str(provider.resolve())
    assert row["kind"] == "shared-library"
    assert row["runs"] == "3"
    assert row["sets_per_run"] == "1"
    assert row["outputs_consistent"] == "yes"
    for field in (
        "median_set_new_ns",
        "median_set_add_total_ns",
        "median_set_fini_ns",
        "median_set_free_ns",
        "median_set_api_total_ns",
    ):
        assert int(row[field]) >= 0

    assert "libprovider.so.1" in completed.stdout
    assert "median_set_api_total_ns" in completed.stdout
