from __future__ import annotations

import csv
import io
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "scripts" / "run_set.cpp"


def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=True)


def build_run_set(tmp_path: Path) -> Path:
    stub = tmp_path / "set_stub.c"
    stub.write_text(
        r'''
#include <stdlib.h>
#include <string.h>

struct set {
    char **items;
    size_t count;
};

struct set *set_new(void)
{
    return calloc(1, sizeof(struct set));
}

void set_add(struct set *set, const char *sym)
{
    set->items = realloc(set->items, sizeof(*set->items) * (set->count + 1));
    set->items[set->count++] = strdup(sym);
}

const char *set_fini(struct set *set, int bpp)
{
    (void) set;
    (void) bpp;
    return strdup("stub-payload");
}

struct set *set_free(struct set *set)
{
    if (set) {
        for (size_t i = 0; i < set->count; ++i)
            free(set->items[i]);
        free(set->items);
        free(set);
    }
    return NULL;
}
''',
        encoding="utf-8",
    )
    stub_object = tmp_path / "set_stub.o"
    executable = tmp_path / "run_set"
    run(["cc", "-std=gnu11", "-O2", "-c", str(stub), "-o", str(stub_object)])
    run(
        [
            "g++",
            "-std=c++17",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(SOURCE),
            str(stub_object),
            "-o",
            str(executable),
        ]
    )
    return executable


def build_elf_fixture(tmp_path: Path) -> tuple[Path, Path]:
    provider_source = tmp_path / "provider.c"
    provider_source.write_text("int provided_label(void) { return 42; }\n", encoding="utf-8")
    provider = tmp_path / "libprovider.so.1"
    run(
        [
            "cc",
            "-shared",
            "-fPIC",
            "-Wl,-soname,libprovider.so.1",
            str(provider_source),
            "-o",
            str(provider),
        ]
    )

    consumer_source = tmp_path / "consumer.c"
    consumer_source.write_text(
        "extern int provided_label(void); int main(void) { return provided_label() == 42 ? 0 : 1; }\n",
        encoding="utf-8",
    )
    consumer = tmp_path / "consumer"
    run(
        [
            "cc",
            str(consumer_source),
            str(provider),
            f"-Wl,-rpath,{tmp_path}",
            "-o",
            str(consumer),
        ]
    )
    return provider, consumer


def parse_output(output: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    lines = output.splitlines()
    metadata: dict[str, str] = {}
    table_start = None
    for index, line in enumerate(lines):
        if line.startswith("role\t"):
            table_start = index
            break
        key, value = line.split("\t", 1)
        metadata[key] = value
    assert table_start is not None
    rows = list(csv.DictReader(io.StringIO("\n".join(lines[table_start:])), delimiter="\t"))
    return metadata, rows


def assert_timing_fields(row: dict[str, str]) -> None:
    fields = ["set_new_ns", "set_add_total_ns", "set_fini_ns", "set_free_ns", "set_api_total_ns"]
    values = {field: int(row[field]) for field in fields}
    assert all(value >= 0 for value in values.values())
    assert values["set_api_total_ns"] == sum(values[field] for field in fields[:-1])


def test_shared_library_builds_provided_set_and_reports_set_api_timings(tmp_path: Path) -> None:
    run_set = build_run_set(tmp_path)
    provider, _ = build_elf_fixture(tmp_path)

    completed = run([str(run_set), "--bpp", "16", str(provider)])
    metadata, rows = parse_output(completed.stdout)

    assert metadata["kind"] == "shared-library"
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "provided"
    assert row["object"] == str(provider.resolve())
    assert int(row["labels"]) >= 1
    assert row["bpp"] == "16"
    assert row["set"] == "set:stub-payload"
    assert_timing_fields(row)


def test_executable_builds_required_sets_grouped_by_provider(tmp_path: Path) -> None:
    run_set = build_run_set(tmp_path)
    provider, consumer = build_elf_fixture(tmp_path)

    completed = run([str(run_set), "--bpp", "16", str(consumer)])
    metadata, rows = parse_output(completed.stdout)

    assert metadata["kind"] == "executable"
    provider_rows = [row for row in rows if Path(row["object"]).name == provider.name]
    assert len(provider_rows) == 1
    row = provider_rows[0]
    assert row["role"] == "required"
    assert int(row["labels"]) >= 1
    assert row["bpp"] == "16"
    assert row["set"] == "set:stub-payload"
    assert_timing_fields(row)
