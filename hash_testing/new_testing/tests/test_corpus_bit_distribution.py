#!/usr/bin/env python3
"""Behavior tests for corpus_bit_distribution.py."""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import corpus_bit_distribution
import probability_map


class CorpusBitDistributionTests(unittest.TestCase):
    def test_batch_counts_match_scalar_cli_for_every_hash(self) -> None:
        words = ["a", "abc", "HashWord", "_ZN4llvm3fooEv"]
        with tempfile.TemporaryDirectory() as temporary_directory:
            corpus = Path(temporary_directory) / "corpus.txt"
            corpus.write_text("".join(f"{word}\n" for word in words), encoding="ascii")

            for hash_name in probability_map.HASHES:
                with self.subTest(hash_name=hash_name):
                    distribution = corpus_bit_distribution.measure_hash(
                        hash_name,
                        corpus,
                    )
                    executable = probability_map.prepare_hash(hash_name)
                    scalar = [
                        probability_map.run_hash(executable, word)[0] for word in words
                    ]
                    expected_ones = tuple(
                        sum((value >> bit) & 1 for value in scalar)
                        for bit in range(distribution.bits)
                    )

                    self.assertEqual(distribution.samples, len(words))
                    self.assertEqual(distribution.ones, expected_ones)

    def test_writes_one_csv_row_per_lsb_first_bit(self) -> None:
        distribution = corpus_bit_distribution.Distribution(
            hash_name="tiny",
            samples=4,
            bits=3,
            ones=(1, 2, 4),
        )
        stream = io.StringIO()

        corpus_bit_distribution.write_distribution_csv(stream, distribution)

        rows = list(csv.reader(io.StringIO(stream.getvalue())))
        self.assertEqual(
            rows[0],
            ["bit", "ones", "zeros", "probability", "deviation_from_0.5"],
        )
        self.assertEqual(rows[1], ["0", "1", "3", "0.250000000", "0.250000000"])
        self.assertEqual(rows[2], ["1", "2", "2", "0.500000000", "0.000000000"])
        self.assertEqual(rows[3], ["2", "4", "0", "1.000000000", "0.500000000"])

    def test_run_creates_separate_csv_summary_manifest_and_png(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            corpus = root / "corpus.txt"
            corpus.write_text("a\nabc\nHashWord\n", encoding="ascii")
            output = root / "results"

            result = corpus_bit_distribution.run(
                corpus=corpus,
                output_directory=output,
                hash_names=["jenkinsOAAT"],
            )

            self.assertEqual(result[0].samples, 3)
            self.assertTrue((output / "jenkinsOAAT.csv").is_file())
            self.assertTrue((output / "summary.csv").is_file())
            self.assertTrue((output / "manifest.txt").is_file())
            image_path = output / "bit_distribution.png"
            self.assertTrue(image_path.is_file())
            with Image.open(image_path) as image:
                self.assertEqual(image.format, "PNG")
                self.assertGreater(image.width, 300)
                self.assertGreater(image.height, 200)


if __name__ == "__main__":
    unittest.main()
