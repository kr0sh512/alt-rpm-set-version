#!/usr/bin/env python3
"""Behavior tests for the standalone XXH64 hash CLI."""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "hash_funcs" / "xxh64" / "bin_hash.c"


class Xxh64BinHashTests(unittest.TestCase):
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

    def run_hash(self, word: str | None = None, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        command = [str(self.binary)]
        if word is not None:
            command.append(word)
        return subprocess.run(command, input=stdin, capture_output=True, check=False)

    def test_matches_official_xxh64_seed_zero_vectors(self) -> None:
        vectors = {
            "": b"ef46db3751d8e999\n",
            "hello": b"26c7827d889f6da3\n",
            "HashWord": b"3e26fc2935163fbe\n",
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
        reference = subprocess.run(
            ["xxhsum", "-H64"], input=payload, capture_output=True, check=True
        ).stdout.split()[0]

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), reference)

    def test_rejects_non_ascii_input(self) -> None:
        result = self.run_hash(stdin="ёж".encode())

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"ASCII", result.stderr)
        self.assertEqual(result.stdout, b"")


if __name__ == "__main__":
    unittest.main()
