from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "compare_cmp_realization.py"


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

int rpmsetcmp(const char *set1, const char *set2)
{
    return strcmp(set1, set2) == 0 ? 0 : -2;
}
''',
        encoding="utf-8",
    )
    return rpm_root, set_source


def make_elf_fixture(tmp_path: Path) -> tuple[Path, Path]:
    import subprocess

    provider_source = tmp_path / "provider.c"
    provider_source.write_text("int cmp_benchmark_label(void) { return 7; }\n", encoding="utf-8")
    provider = tmp_path / "libcmp-provider.so.1"
    subprocess.run(
        [
            "cc",
            "-shared",
            "-fPIC",
            "-Wl,-soname,libcmp-provider.so.1",
            str(provider_source),
            "-o",
            str(provider),
        ],
        check=True,
    )

    consumer_source = tmp_path / "consumer.c"
    consumer_source.write_text(
        "extern int cmp_benchmark_label(void); int main(void) { return cmp_benchmark_label() == 7 ? 0 : 1; }\n",
        encoding="utf-8",
    )
    consumer = tmp_path / "consumer"
    subprocess.run(
        [
            "cc",
            str(consumer_source),
            str(provider),
            f"-Wl,-rpath,{tmp_path}",
            "-o",
            str(consumer),
        ],
        check=True,
    )
    return consumer, provider


def load_module():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("compare_cmp_realization_tested", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_compiles_cmp_runner_runs_hardcoded_case_n_times_and_writes_median(
    tmp_path: Path, capsys
) -> None:
    module = load_module()
    rpm_root, set_source = make_fake_rpm_tree(tmp_path)
    consumer, provider = make_elf_fixture(tmp_path)
    result_root = tmp_path / "res_cmp"
    setattr(
        module,
        "HARDCODED_CASES",
        (
            module.ComparisonCase(
                name="consumer-provider",
                binary_candidates=(str(consumer),),
                library_candidates=(str(provider),),
            ),
        ),
    )

    return_code = module.main(
        [
            str(set_source),
            "-n",
            "3",
            "--rpm-root",
            str(rpm_root),
            "--res-dir",
            str(result_root),
            "--name",
            "fake",
        ]
    )

    assert return_code == 0
    implementation_dir = result_root / "fake"
    assert (implementation_dir / "build" / "run_set").is_file()
    assert (implementation_dir / "build" / "run_cmp").is_file()
    assert (implementation_dir / "build" / "build.log").is_file()

    set_outputs = sorted((implementation_dir / "sets").glob("*.tsv"))
    assert len(set_outputs) == 2
    raw_outputs = sorted((implementation_dir / "runs" / "consumer-provider").glob("run-*.tsv"))
    assert len(raw_outputs) == 3
    assert all("cmp_result\t0" in path.read_text(encoding="utf-8") for path in raw_outputs)
    assert all("rpmsetcmp_ns\t" in path.read_text(encoding="utf-8") for path in raw_outputs)

    with (implementation_dir / "summary.tsv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    assert len(rows) == 1
    row = rows[0]
    assert row["case"] == "consumer-provider"
    assert row["binary"] == str(consumer.resolve())
    assert row["library"] == str(provider.resolve())
    assert row["runs"] == "3"
    assert row["cmp_result"] == "0"
    assert row["outputs_consistent"] == "yes"
    assert int(row["median_rpmsetcmp_ns"]) >= 0

    captured = capsys.readouterr()
    assert "median_rpmsetcmp_ns" in captured.out
    assert "consumer-provider" in captured.out
