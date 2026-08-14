#!/usr/bin/env python3
"""Behavior tests for plot_probability_map.py."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

import plot_probability_map


class ProbabilityMapPlotTests(unittest.TestCase):
    def test_reads_bit_columns_and_ignores_operation_and_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "hash.csv"
            path.write_text(
                "operation,pairs,bit_0,bit_1\n"
                "replace,4,0.25,0.75\n"
                "delete,2,0.5,nan\n",
                encoding="utf-8",
            )

            rows = plot_probability_map.read_probability_map(path)

            self.assertEqual(rows[0].operation, "replace")
            self.assertEqual(rows[0].pairs, 4)
            self.assertEqual(rows[0].probabilities, [0.25, 0.75])
            self.assertEqual(rows[1].operation, "delete")
            self.assertEqual(rows[1].pairs, 2)
            self.assertEqual(len(rows[1].probabilities), 2)

    def test_mean_absolute_deviation_from_half_ignores_nan(self) -> None:
        result = plot_probability_map.mean_absolute_deviation(
            [0.25, 0.5, 0.75, float("nan")]
        )

        self.assertEqual(result, 1 / 6)

    def test_shared_scale_zooms_to_all_values_and_reference(self) -> None:
        scale = plot_probability_map.make_scale(
            [0.45, 0.48, 0.52, 0.55],
            reference=0.5,
            hard_limits=(0.0, 1.0),
        )

        self.assertGreater(scale.minimum, 0.0)
        self.assertLess(scale.maximum, 1.0)
        self.assertLessEqual(scale.minimum, 0.45)
        self.assertGreaterEqual(scale.maximum, 0.55)
        self.assertIn(0.5, scale.ticks)

    def test_deviation_scale_uses_data_range_instead_of_fixed_half(self) -> None:
        scale = plot_probability_map.make_scale(
            [0.05, 0.08], hard_limits=(0.0, 0.5)
        )

        self.assertGreater(scale.minimum, 0.0)
        self.assertLess(scale.maximum, 0.5)
        self.assertLessEqual(scale.minimum, 0.05)
        self.assertGreaterEqual(scale.maximum, 0.08)

    def test_generates_two_nonempty_png_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "sample.csv"
            source.write_text(
                "operation,pairs,bit_0,bit_1,bit_2,bit_3\n"
                "replace,4,0.25,0.50,0.75,1.0\n"
                "delete,2,0.10,0.20,0.30,0.40\n",
                encoding="utf-8",
            )

            outputs = plot_probability_map.generate_plots(source, root / "plots")

            self.assertEqual(len(outputs), 2)
            for output in outputs:
                self.assertTrue(output.is_file())
                with Image.open(output) as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertGreater(image.width, 300)
                    self.assertGreater(image.height, 200)
                    colors = image.convert("RGB").getcolors(maxcolors=1_000_000)
                    self.assertIsNotNone(colors)
                    self.assertGreater(len(colors or []), 2)


if __name__ == "__main__":
    unittest.main()
