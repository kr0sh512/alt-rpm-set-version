import contextlib
import io
import runpy
import unittest

from reimplement import set as rpmset


class Base64AlphabetTest(unittest.TestCase):
    def test_int_to_char_and_char_to_int_roundtrip_all_values(self):
        for value in range(64):
            self.assertEqual(rpmset.char_to_int(rpmset.int_to_char(value)), value)

    def test_int_to_char_rejects_out_of_range_values(self):
        with self.assertRaises(ValueError):
            rpmset.int_to_char(64)

    def test_char_to_int_rejects_non_base64_character(self):
        with self.assertRaises(ValueError):
            rpmset.char_to_int("=")


class GolombEncodingTest(unittest.TestCase):
    def test_delta_roundtrip(self):
        values = [1, 3, 6, 10, 31]
        self.assertEqual(rpmset.decode_delta(rpmset.encode_delta(values)), values)

    def test_golomb_roundtrip(self):
        values = [0, 1, 2, 7, 8, 15]
        bits = rpmset.encode_golomb(values, Mshift=3)
        self.assertEqual(rpmset.decode_golomb(bits, Mshift=3), values)

    def test_base64_roundtrip_preserves_bits_with_zero_padding(self):
        bits = [1, 0, 1, 1, 0, 0, 1]
        encoded = rpmset.encode_base64(bits)
        self.assertEqual(rpmset.decode_base64(encoded)[: len(bits)], bits)


class SetStringTest(unittest.TestCase):
    def test_encode_decode_set_roundtrip(self):
        values = [1, 3, 6, 10]
        encoded = rpmset.encode_set(values, bpp=8)
        bpp, mshift = rpmset.decode_set_init(encoded)
        self.assertEqual(bpp, 8)
        self.assertEqual(rpmset.decode_set(encoded, mshift)[: len(values)], values)

    def test_set_add_fini_and_free(self):
        item_set = rpmset.set_new()
        rpmset.set_add(item_set, "label1")
        rpmset.set_add(item_set, "label2")

        encoded = rpmset.set_fini(item_set, bpp=8)
        self.assertIsNotNone(encoded)
        self.assertEqual(item_set.cnt, 2)
        self.assertEqual(item_set.labels, sorted(item_set.labels, key=lambda item: item[1]))

        rpmset.set_free(item_set)
        self.assertEqual(item_set.cnt, 0)
        self.assertEqual(item_set.labels, [])

    def test_hash_is_stable_64_bit_ascii_integer(self):
        self.assertEqual(rpmset.hash("ascii_symbol"), 10827468943333989194)
        self.assertLessEqual(rpmset.hash("ascii_symbol"), 2**64 - 1)

    def test_hash_rejects_non_ascii_labels(self):
        with self.assertRaises(UnicodeEncodeError):
            rpmset.hash("юникод")


class DownsampleSetTest(unittest.TestCase):
    def test_masks_high_half_and_keeps_sorted_unique_values(self):
        self.assertEqual(rpmset.downsample_set([1, 3, 6, 8, 10, 14], 3), [0, 1, 2, 3, 6])

    def test_removes_duplicates_created_by_masking(self):
        self.assertEqual(rpmset.downsample_set([1, 6, 14], 3), [1, 6])

    def test_keeps_low_only_set_unchanged(self):
        self.assertEqual(rpmset.downsample_set([1, 3, 6], 3), [1, 3, 6])

    def test_masks_high_only_set(self):
        self.assertEqual(rpmset.downsample_set([8, 10, 14], 3), [0, 2, 6])


class ModuleEntrypointTest(unittest.TestCase):
    def test_module_has_no_main_side_effects(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            runpy.run_path("reimplement/set.py", run_name="__main__")
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
