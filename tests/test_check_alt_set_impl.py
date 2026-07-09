import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_alt_set_impl.py"


def load_script():
    spec = importlib.util.spec_from_file_location("check_alt_set_impl", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_select_elf_members_separates_providers_and_requirers():
    check = load_script()
    members = [
        "./usr/lib64/libfoo.so.1",
        "./usr/lib64/libfoo.so.1.2.3",
        "./usr/lib64/libfoo.a",
        "./usr/bin/tool",
        "./usr/share/doc/readme",
    ]

    assert check.select_provider_members(members) == [
        "./usr/lib64/libfoo.so.1",
        "./usr/lib64/libfoo.so.1.2.3",
    ]
    assert check.select_requirer_members(members) == [
        "./usr/lib64/libfoo.so.1",
        "./usr/lib64/libfoo.so.1.2.3",
        "./usr/bin/tool",
    ]


def test_generate_label_set_and_compare_uses_only_local_set_py():
    check = load_script()
    provided = check.generate_label_set("provider", "libfoo.so.1", ["foo", "bar", "baz"], bpp=16)
    required = check.generate_label_set("requirer", "tool", ["foo", "bar"], bpp=16)
    missing = check.generate_label_set("requirer", "badtool", ["foo", "quux"], bpp=16)

    ok = check.compare_label_sets(provided, required)
    bad = check.compare_label_sets(provided, missing)

    assert ok.status == "compatible"
    assert ok.cmp_result == 1
    assert bad.status == "incompatible"
    assert bad.cmp_result == -2


def test_normalize_required_symbol_version_can_match_provided_symbol_version():
    check = load_script()

    assert check.normalize_required_symbol("foo@LIB_1") == "foo@@LIB_1"
    assert check.normalize_required_symbol("foo@@LIB_1") == "foo@@LIB_1"
    assert check.normalize_required_symbol("foo") == "foo"


def test_parse_required_nm_output_filters_weak_undefined_symbols():
    check = load_script()
    nm_output = """
                 w __gmon_start__
                 w _ITM_deregisterTMCloneTable
                 U close@GLIBC_2.2.5
                 U memcpy@GLIBC_2.14
                 W optional_hook
    """

    assert check.parse_required_nm_output(nm_output) == [
        "close@GLIBC_2.2.5",
        "memcpy@GLIBC_2.14",
    ]


def test_default_bpp_is_large_enough_for_real_alt_symbol_sets():
    check = load_script()

    assert check.parse_args([]).bpp == 32


def test_build_dependency_results_splits_required_labels_by_provider_library():
    check = load_script()
    libc = check.generate_label_set(
        "provided", "libc.so.6", ["close@@GLIBC_2.2.5", "read@@GLIBC_2.2.5"], bpp=16, package="glibc-core"
    )
    libz = check.generate_label_set(
        "provided", "libz.so.1", ["inflate", "deflate"], bpp=16, package="zlib"
    )
    tool = check.generate_label_set(
        "required", "tool", ["close@@GLIBC_2.2.5", "inflate"], bpp=16, package="consumer"
    )

    results = check.build_dependency_results([libc, libz], [tool], bpp=16)

    assert [(result.provider_member, result.required_labels, result.status) for result in results] == [
        ("libc.so.6", 1, "compatible"),
        ("libz.so.1", 1, "compatible"),
    ]
