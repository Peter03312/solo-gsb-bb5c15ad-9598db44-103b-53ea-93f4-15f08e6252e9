"""不依赖数据库的核心逻辑测试：python -m unittest tests.test_redaction -v"""
import unittest

from app.redaction import (
    MASK,
    InvalidRange,
    merge_ranges,
    normalize_ranges,
    redact,
)


class NormalizeTests(unittest.TestCase):
    def test_sorts_and_keeps_codepoint_semantics(self):
        # "你好abc" 码点长度 5；区间为 [start,end)
        text = "你好abc"
        out = normalize_ranges([[3, 5], [0, 2]], len(text))
        self.assertEqual(out, [(0, 2), (3, 5)])

    def test_empty_range_is_dropped(self):
        self.assertEqual(normalize_ranges([[2, 2]], 5), [])

    def test_empty_list_ok(self):
        self.assertEqual(normalize_ranges([], 3), [])

    def test_end_boundary_allowed(self):
        self.assertEqual(normalize_ranges([[5, 5]], 5), [])
        self.assertEqual(normalize_ranges([[4, 5]], 5), [(4, 5)])

    def test_out_of_bounds_rejected(self):
        with self.assertRaises(InvalidRange):
            normalize_ranges([[0, 6]], 5)
        with self.assertRaises(InvalidRange):
            normalize_ranges([[-1, 2]], 5)
        with self.assertRaises(InvalidRange):
            normalize_ranges([[3, 2]], 5)

    def test_shape_errors(self):
        for bad in ([1], ["a", 1], [[1, 2, 3]], [True, 2], [[1.0, 2]]):
            with self.assertRaises(InvalidRange):
                normalize_ranges(bad, 5)

    def test_not_a_list(self):
        with self.assertRaises(InvalidRange):
            normalize_ranges({"a": 1}, 5)


class MergeTests(unittest.TestCase):
    def test_overlapping(self):
        self.assertEqual(merge_ranges([(0, 5), (3, 8)]), [(0, 8)])

    def test_adjacent_merged(self):
        # 相邻（前闭 == 后开）必须合并
        self.assertEqual(merge_ranges([(0, 3), (3, 5)]), [(0, 5)])

    def test_separate_kept(self):
        self.assertEqual(merge_ranges([(0, 2), (3, 5)]), [(0, 2), (3, 5)])

    def test_nested(self):
        self.assertEqual(merge_ranges([(0, 10), (2, 3), (4, 6)]), [(0, 10)])

    def test_chain(self):
        self.assertEqual(
            merge_ranges([(0, 2), (1, 4), (4, 9), (10, 11)]),
            [(0, 9), (10, 11)],
        )


class RedactTests(unittest.TestCase):
    def test_adjacent_single_mask(self):
        text = "abcdef"
        merged = merge_ranges(normalize_ranges([[0, 2], [2, 4]], len(text)))
        self.assertEqual(redact(text, merged), MASK + "ef")

    def test_preserve_rest(self):
        text = "abcdef"
        out = redact(text, merge_ranges(normalize_ranges([[1, 3]], len(text))))
        self.assertEqual(out, "a" + MASK + "def")

    def test_unicode_codepoints(self):
        text = "你好，世界"  # 6 个码点
        merged = merge_ranges(normalize_ranges([[0, 2], [3, 5]], len(text)))
        self.assertEqual(redact(text, merged), MASK + "，" + MASK)

    def test_full_text(self):
        text = "敏感内容"
        out = redact(text, merge_ranges(normalize_ranges([[0, 4]], 4)))
        self.assertEqual(out, MASK)

    def test_empty_ranges_returns_original(self):
        self.assertEqual(redact("原样保留", []), "原样保留")


if __name__ == "__main__":
    unittest.main()
