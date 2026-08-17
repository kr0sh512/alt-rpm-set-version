#!/usr/bin/env python3
"""Behavior tests for extract_provided_symbols.py."""

from __future__ import annotations

import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "extract_provided_symbols.py"


def newc_entry(name: str, data: bytes, mode: int = stat.S_IFREG | 0o644) -> bytes:
    encoded_name = name.encode("utf-8") + b"\0"
    fields = (
        1,
        mode,
        0,
        0,
        1,
        0,
        len(data),
        0,
        0,
        0,
        0,
        len(encoded_name),
        0,
    )
    header = b"070701" + b"".join(f"{value:08x}".encode() for value in fields)
    record = header + encoded_name
    record += b"\0" * (-len(record) % 4)
    record += data
    record += b"\0" * (-len(data) % 4)
    return record


def newc_archive(entries: list[tuple[str, bytes, int]]) -> bytes:
    payload = b"".join(newc_entry(*entry) for entry in entries)
    return payload + newc_entry("TRAILER!!!", b"", 0)


class ExtractProvidedSymbolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary_directory.name)
        self.rpm2cpio = self.work / "rpm2cpio"
        self.provided_symbols = self.work / "provided_symbols"
        self.file_command = self.work / "file"

        self.rpm2cpio.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())\n"
        )
        self.provided_symbols.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "symbols = set()\n"
            "for name in sys.argv[1:]:\n"
            "    symbols.update(pathlib.Path(name).read_bytes()[4:].decode().splitlines())\n"
            "print(*sorted(symbols), sep='\\n')\n"
        )
        self.file_command.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "data = pathlib.Path(sys.argv[-1]).read_bytes()\n"
            "kind = 'pie executable' if b'EXECUTABLE' in data else 'shared object'\n"
            "print(f'ELF 64-bit LSB {kind}, x86-64, version 1 (SYSV)')\n"
        )
        self.rpm2cpio.chmod(0o755)
        self.provided_symbols.chmod(0o755)
        self.file_command.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--rpm2cpio",
                str(self.rpm2cpio),
                "--provided-symbols",
                str(self.provided_symbols),
                "--file-command",
                str(self.file_command),
                *arguments,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_combines_unique_cpp_symbols_from_multiple_packages(self) -> None:
        first = self.work / "first.rpm"
        second = self.work / "second.rpm"
        output = self.work / "symbols.txt"
        first.write_bytes(
            newc_archive(
                [
                    ("./usr/lib64/libfirst.so", b"\x7fELF_ZN3Foo3barEv\nplain_c_symbol\n", stat.S_IFREG | 0o755),
                    ("./usr/share/doc/readme", b"not ELF", stat.S_IFREG | 0o644),
                    ("./usr/lib64/libalias.so", b"libfirst.so", stat.S_IFLNK | 0o777),
                ]
            )
        )
        second.write_bytes(
            newc_archive(
                [
                    (
                        "./usr/lib64/libsecond.so",
                        b"\x7fELF_ZN3Foo3barEv\n_ZN3Foo3bazEv\nanother_c_symbol\n",
                        stat.S_IFREG | 0o755,
                    ),
                ]
            )
        )

        result = self.run_script(
            "--cpp-only", "--output", str(output), str(first), str(second)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            output.read_text().splitlines(),
            ["_ZN3Foo3barEv", "_ZN3Foo3bazEv"],
        )
        self.assertIn("package=first.rpm elf_files=1 symbols=2 selected=1", result.stderr)
        self.assertIn("package=second.rpm elf_files=1 symbols=3 selected=2", result.stderr)
        self.assertIn("unique_symbols=2", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_without_cpp_filter_preserves_all_provided_symbols(self) -> None:
        package = self.work / "all.rpm"
        package.write_bytes(
            newc_archive(
                [
                    (
                        "./usr/lib64/liball.so",
                        b"\x7fELF_ZN3Foo3barEv\nplain_c_symbol\n",
                        stat.S_IFREG | 0o755,
                    )
                ]
            )
        )

        result = self.run_script(str(package))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["_ZN3Foo3barEv", "plain_c_symbol"])

    def test_excludes_elf_executables_that_do_not_generate_library_provides(self) -> None:
        package = self.work / "mixed.rpm"
        package.write_bytes(
            newc_archive(
                [
                    (
                        "./usr/lib64/libmixed.so",
                        b"\x7fELF_ZN3Lib3runEv\n",
                        stat.S_IFREG | 0o755,
                    ),
                    (
                        "./usr/bin/mixed",
                        b"\x7fELFEXECUTABLE\n_ZN4Exec3runEv\n",
                        stat.S_IFREG | 0o755,
                    ),
                ]
            )
        )

        result = self.run_script("--cpp-only", str(package))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["_ZN3Lib3runEv"])
        self.assertIn("elf_files=1", result.stderr)

    def test_rejects_malformed_cpio_stream(self) -> None:
        package = self.work / "broken.rpm"
        package.write_bytes(b"not a newc archive")

        result = self.run_script(str(package))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("newc", result.stderr)


if __name__ == "__main__":
    unittest.main()
