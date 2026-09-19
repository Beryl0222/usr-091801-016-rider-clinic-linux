"""分钟制时钟与时间窗运算。

系统内部一律使用"自纪元起的分钟数"整数，避免时区/秒级精度问题；
对外接受 "YYYY-MM-DDTHH:MM" 形式的本地时间字符串。
"""

from datetime import datetime, timedelta

EPOCH = datetime(2024, 1, 1)


def parse_minutes(value):
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise ValueError(f"无法解析时间: {value!r}")
    text = value.strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
            return int((dt - EPOCH).total_seconds() // 60)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {value!r}")


def format_minutes(value):
    return (EPOCH + timedelta(minutes=value)).strftime("%Y-%m-%dT%H:%M")


def day_key(value):
    """返回该分钟所属自然日（YYYY-MM-DD），供跨日随访判断。"""
    return (EPOCH + timedelta(minutes=value)).strftime("%Y-%m-%d")


def overlaps(start_a, end_a, start_b, end_b):
    return start_a < end_b and start_b < end_a
