"""纪要生成的动态输出预算；不依赖模型名称或供应商。"""

from __future__ import annotations

from math import inf


def calculate_minutes_max_tokens(
    *,
    transcript_chars: int,
    segment_count: int,
    settings_limit: int | None,
    profile_output_limit: int | None,
) -> int:
    """按任务规模计算预算，并同时受 settings/profile 两个上限约束。"""

    if transcript_chars < 0 or segment_count < 0:
        raise ValueError("minutes task size must be non-negative")
    if profile_output_limit is None or profile_output_limit < 1:
        raise ValueError("fresh capability output limit is required")
    if settings_limit is not None and settings_limit < 1:
        raise ValueError("settings minutes limit must be positive")

    # 对中文逐字稿不做 tokenizer 假设；用字符量与分段量中较大的任务信号
    # 选择有限的纪要输出档位。纪要不是逐字稿回显，因此输出预算远小于输入。
    task_size = max(transcript_chars, segment_count * 80)
    if task_size <= 1_200:
        desired = 512
    elif task_size <= 6_000:
        desired = 1_024
    elif task_size <= 18_000:
        desired = 2_048
    else:
        desired = 4_096

    setting_cap = settings_limit if settings_limit is not None else inf
    return int(max(1, min(desired, setting_cap, profile_output_limit)))
