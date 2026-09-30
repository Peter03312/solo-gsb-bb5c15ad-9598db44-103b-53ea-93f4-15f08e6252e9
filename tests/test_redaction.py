"""脱敏核心逻辑的单元测试（仅依赖标准库）。"""

import unittest

from app.redaction import (
    REDACTION_PLACEHOLDER,
    IntervalOutOfBoundsError,
    merge_intervals,
    redact_text,
    validate_intervals,
)

TEXT = "患者张三，身份证号 110101199003077777，联系电话 13800001111。"
# 码点位置：张三=[2,4)  身份证号=[10,28)  电话=[34,45)


class ValidateTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_intervals(len(TEXT), [(2, 4)]), [(2, 4)])

    def test_out_of_bounds(self):
        for bad in [(-1, 2), (0, 0), (3, 2), (10, len(TEXT) + 1)]:
            with self.assertRaises(IntervalOutOfBoundsError):
                validate_intervals(len(TEXT), [bad])

    def test_empty_list_ok(self):
        self.assertEqual(validate_intervals(0, []), [])


class MergeTests(unittest.TestCase):
    def test_overlapping(self):
        self.assertEqual(merge_intervals([(2, 5), (4, 8)]), [(2, 8)])

    def test_adjacent(self):
        self.assertEqual(merge_intervals([(0, 3), (3, 6)]), [(0, 6)])

    def test_unsorted_and_contained(self):
        self.assertEqual(merge_intervals([(10, 20), (0, 5), (12, 15)]), [(0, 5), (10, 20)])

    def test_disjoint_kept(self):
        self.assertEqual(merge_intervals([(0, 2), (4, 6)]), [(0, 2), (4, 6)])


class RedactTests(unittest.TestCase):
    def test_basic(self):
        result = redact_text(TEXT, [(2, 4), (10, 28), (34, 45)])
        self.assertEqual(
            result,
            "患者[已遮蔽]，身份证号 [已遮蔽]，联系电话 [已遮蔽]。",
        )

    def test_merge_adjacent_and_overlap(self):
        result = redact_text(TEXT, [(2, 4), (4, 5), (10, 28), (20, 30), (34, 45)])
        self.assertEqual(
            result,
            "患者[已遮蔽]身份证号 [已遮蔽]系电话 [已遮蔽]。",
        )

    def test_placeholder(self):
        self.assertEqual(REDACTION_PLACEHOLDER, "[已遮蔽]")

    def test_empty_intervals_keeps_text(self):
        self.assertEqual(redact_text(TEXT, []), TEXT)

    def test_full_coverage(self):
        self.assertEqual(redact_text(TEXT, [(0, len(TEXT))]), "[已遮蔽]")

    def test_unicode_code_points(self):
        # 🙂 是 BMP 之外的字符，仍按 1 个码点计
        self.assertEqual(redact_text("a🙂bc", [(1, 2)]), "a[已遮蔽]bc")


if __name__ == "__main__":
    unittest.main()
