"""区间校验、合并与脱敏的纯函数模块。

位置约定：区间 [start, end) 以 Unicode 码点为单位，左闭右开。
Python 3 字符串索引本身即按 Unicode 码点工作，因此可直接切片。
"""
from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

#: 每段命中区间统一替换为该占位符（多段相邻区间合并后只产生一个占位符）
MASK = "[已遮蔽]"

Range = Tuple[int, int]


class InvalidRange(ValueError):
    """调用方提交的区间结构非法或越过原文边界。"""


def normalize_ranges(raw: object, code_point_length: int) -> List[Range]:
    """校验外部提交的区间列表并转为有序的 (start, end) 元组。

    - 必须是 ``[ [start, end], ... ]`` 形式，元素数量恰为 2；
    - 端点必须为整数（不接受布尔值）；
    - 必须满足 ``0 <= start <= end <= len(content)``，否则整次拒绝；
    - 空区间 (start == end) 合法但不产生遮蔽，直接丢弃。
    """
    if not isinstance(raw, list):
        raise InvalidRange("ranges must be a list")

    normalized: List[Range] = []
    for item in raw:
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 2
        ):
            raise InvalidRange("each range must be [start, end)")
        start, end = item
        # bool 是 int 的子类，显式排除，避免 True/1 这种歧义输入
        if isinstance(start, bool) or isinstance(end, bool):
            raise InvalidRange("range endpoints must be integers")
        if not isinstance(start, int) or not isinstance(end, int):
            raise InvalidRange("range endpoints must be integers")
        if start < 0 or end < start or end > code_point_length:
            raise InvalidRange("range out of bounds")
        if start < end:
            normalized.append((start, end))

    normalized.sort(key=lambda pair: (pair[0], pair[1]))
    return normalized


def merge_ranges(ranges: Iterable[Sequence[int]]) -> List[Range]:
    """合并所有相交（overlap）或相邻（adjacent，即前闭 == 后开）的区间。"""
    merged: List[List[int]] = []
    for start, end in ranges:
        if not merged:
            merged.append([start, end])
            continue
        last_start, last_end = merged[-1]
        if start > last_end:
            # 与上一段之间至少有一个码点的空隙，不能合并
            merged.append([start, end])
        elif end > last_end:
            # 相交或相邻（start == last_end），向右扩展
            merged[-1][1] = end
    return [(start, end) for start, end in merged]


def redact(text: str, merged: Iterable[Sequence[int]]) -> str:
    """按已合并区间生成脱敏正文，区间外原文逐码点保留。"""
    parts: List[str] = []
    cursor = 0
    for start, end in merged:
        parts.append(text[cursor:start])
        parts.append(MASK)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)
