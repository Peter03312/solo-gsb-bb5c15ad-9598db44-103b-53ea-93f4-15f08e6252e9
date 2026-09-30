"""敏感区间校验、合并与脱敏替换。

区间约定：左闭右开 [start, end)，位置按 Unicode 码点计，
与 Python 字符串的下标语义一致（包括对 BMP 之外的字符）。
"""

REDACTION_PLACEHOLDER = "[已遮蔽]"


class IntervalOutOfBoundsError(ValueError):
    """区间为空、倒置或超出原文长度。"""


def validate_intervals(text_length: int, intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """校验并规范化区间；任何越界区间都会让整次请求被拒绝。"""
    normalized = []
    for start, end in intervals:
        if start < 0 or end > text_length or start >= end:
            raise IntervalOutOfBoundsError(
                f"interval [{start}, {end}) is out of bounds for text length {text_length}"
            )
        normalized.append((start, end))
    return normalized


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并相交或相邻的区间，返回按起点排序的不相交区间列表。"""
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def redact_text(text: str, intervals: list[tuple[int, int]]) -> str:
    """将合并后的每个区间替换为占位符，其余原文保持不变。"""
    parts: list[str] = []
    cursor = 0
    for start, end in merge_intervals(intervals):
        parts.append(text[cursor:start])
        parts.append(REDACTION_PLACEHOLDER)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)
