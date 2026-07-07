import unittest

from reimplement.set import downsample_set


class DownsampleSetTest(unittest.TestCase):
    def test_masks_high_half_and_keeps_sorted_unique_values(self):
        self.assertEqual(downsample_set([1, 3, 6, 8, 10, 14], 3), [0, 1, 2, 3, 6])

    def test_removes_duplicates_created_by_masking(self):
        self.assertEqual(downsample_set([1, 6, 14], 3), [1, 6])

    def test_keeps_low_only_set_unchanged(self):
        self.assertEqual(downsample_set([1, 3, 6], 3), [1, 3, 6])

    def test_masks_high_only_set(self):
        self.assertEqual(downsample_set([8, 10, 14], 3), [0, 2, 6])


if __name__ == "__main__":
    unittest.main()
