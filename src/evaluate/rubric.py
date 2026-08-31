"""评分规则：掌握度档位划分。

费曼模式产出的 0-10 分在这里解释成可展示的掌握等级。
"""

SCORE_MAX = 10

# (分数线, 档位)，从高到低
RUBRIC_LEVELS: list[tuple[float, str]] = [
    (8.0, "掌握"),
    (6.0, "基本掌握"),
    (4.0, "部分理解"),
    (0.0, "待重新学习"),
]


def level_of(score: float | None) -> str:
    """分数 → 掌握等级；无分数返回「未评分」"""
    if score is None:
        return "未评分"
    for threshold, level in RUBRIC_LEVELS:
        if score >= threshold:
            return level
    return RUBRIC_LEVELS[-1][1]
