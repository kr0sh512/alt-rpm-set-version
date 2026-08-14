#!/usr/bin/env python3
"""Behavior tests for probability_map.py."""

from __future__ import annotations

import csv
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import generate_input
import probability_map


class PrepareHashTests(unittest.TestCase):
    def test_compiles_c_source_when_binary_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hash_directory = root / "constant"
            hash_directory.mkdir()
            (hash_directory / "bin_hash.c").write_text(
                "#include <stdio.h>\n"
                "int main(void) { puts(\"00000001\"); return 0; }\n",
                encoding="utf-8",
            )

            executable = probability_map.prepare_hash("constant", root)

            self.assertEqual(executable, hash_directory / "bin_hash")
            self.assertEqual(
                subprocess.check_output([executable, "word"], text=True).strip(),
                "00000001",
            )

    def test_adds_python_shebang_and_execute_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hash_directory = root / "python_hash"
            hash_directory.mkdir()
            source = hash_directory / "bin_hash.py"
            source.write_text("print('00000002')\n", encoding="utf-8")

            executable = probability_map.prepare_hash("python_hash", root)

            self.assertEqual(executable, source)
            self.assertTrue(os.access(source, os.X_OK))
            self.assertTrue(
                source.read_text(encoding="utf-8").startswith(
                    "#!/usr/bin/env python3\n"
                )
            )
            self.assertEqual(
                subprocess.check_output([executable, "word"], text=True).strip(),
                "00000002",
            )


class ProbabilityTests(unittest.TestCase):
    def test_counts_changed_hash_bits_relative_to_source(self) -> None:
        probabilities = probability_map.bit_probabilities(
            source_hash=0b0000,
            changed_hashes=[0b0001, 0b0011, 0b0010, 0b0000],
            bits=4,
        )

        self.assertEqual(probabilities, [0.5, 0.5, 0.0, 0.0])

    def test_csv_table_has_operation_rows_and_bit_columns(self) -> None:
        stream = io.StringIO()

        probability_map.write_csv_table(
            stream,
            {
                "replace": (4, [0.25, 0.75]),
                "delete": (2, [0.5, 0.0]),
            },
        )

        rows = list(csv.reader(io.StringIO(stream.getvalue())))
        self.assertEqual(rows[0], ["operation", "pairs", "bit_0", "bit_1"])
        self.assertEqual(rows[1], ["replace", "4", "0.250000", "0.750000"])
        self.assertEqual(rows[2], ["delete", "2", "0.500000", "0.000000"])

    def test_parser_accepts_multiple_source_words(self) -> None:
        arguments = probability_map.build_parser().parse_args(["first", "second"])

        self.assertEqual(arguments.words, ["first", "second"])

    def test_aggregates_samples_from_multiple_words_and_warns_on_shortfall(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "hash.py"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "print(f'{sum(sys.argv[1].encode()):08x}')\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            warnings = io.StringIO()

            with redirect_stderr(warnings):
                table = probability_map.build_probability_table(
                    executable=executable,
                    sources=["A", "B"],
                    operations=["delete"],
                    count=10,
                    operation_count=1,
                    alphabet=generate_input.DEFAULT_ALPHABET,
                    seed=1,
                    max_attempts=None,
                )

            pair_count, probabilities = table["delete"]
            self.assertEqual(pair_count, 2)
            self.assertEqual(
                probabilities[:8],
                [0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
            )
            self.assertEqual(warnings.getvalue().count("delete"), 2)


class GenerateWordsTests(unittest.TestCase):
    def test_count_is_an_upper_bound_when_unique_results_are_exhausted(self) -> None:
        words = generate_input.generate_words(
            source="abc",
            count=100,
            operation="delete",
            operation_count=1,
            seed=42,
        )

        self.assertEqual(set(words), {"ab", "ac", "bc"})


if __name__ == "__main__":
    unittest.main()
