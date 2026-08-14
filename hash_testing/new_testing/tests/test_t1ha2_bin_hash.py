#!/usr/bin/env python3
"""Behavior tests for the standalone t1ha2_atonce hash CLI."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "hash_funcs" / "t1ha2" / "bin_hash.c"


class T1ha2BinHashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.temporary_directory.name) / "bin_hash"
        subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                "-Werror",
                str(SOURCE),
                "-o",
                str(cls.binary),
            ],
            check=True,
            text=True,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def run_hash(
        self, word: str | None = None, stdin: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        command = [str(self.binary)]
        if word is not None:
            command.append(word)
        return subprocess.run(command, input=stdin, capture_output=True, check=False)

    def test_matches_upstream_t1ha2_atonce_seed_zero_vectors(self) -> None:
        vectors = {
            "": b"0000000000000000\n",
            "hello": b"2a5f2abd74df73b4\n",
            "HashWord": b"3885d16135ce64f0\n",
            "abc": b"16bae0f716c45f2e\n",
            "12345678901234567890123456789012": b"75ed8a8aa66a4602\n",
        }

        for word, expected in vectors.items():
            with self.subTest(word=word):
                result = self.run_hash(word)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, expected)
                self.assertRegex(result.stdout.decode(), r"^[0-9a-f]{16}\n$")

    def test_argv_and_stdin_are_equivalent_and_strip_trailing_ascii_space(self) -> None:
        argv = self.run_hash("hello")
        stdin = self.run_hash(stdin=b"hello \t\r\n")

        self.assertEqual(stdin.returncode, 0)
        self.assertEqual(stdin.stdout, argv.stdout)

    def test_handles_long_ascii_input(self) -> None:
        payload = b"a" * 100_000
        result = self.run_hash(stdin=payload)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"bdf3f8539f0504ea\n")

    def test_rejects_non_ascii_input(self) -> None:
        result = self.run_hash(stdin="ёж".encode())

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"ASCII", result.stderr)
        self.assertEqual(result.stdout, b"")

    def test_unaligned_argv_is_clean_under_undefined_behavior_sanitizer(self) -> None:
        sanitized = Path(self.temporary_directory.name) / "bin_hash_ubsan"
        subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O1",
                "-g",
                "-fsanitize=undefined",
                "-fno-sanitize-recover=undefined",
                str(SOURCE),
                "-o",
                str(sanitized),
            ],
            check=True,
            capture_output=True,
        )

        for word in ("hello", "12345678", "a" * 33):
            with self.subTest(word=word):
                result = subprocess.run(
                    [str(sanitized), word], capture_output=True, check=False
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
