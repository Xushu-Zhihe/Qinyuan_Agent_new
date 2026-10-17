"""Contract22 planning guidance; strategic diversity remains a model judgment."""

import json


GUIDANCE = (
    "根据任务目标、前置 Intent、执行完成后的有效 Fact、已审核推断和未解决问题，"
    "生成你认为下一轮可能采取的几个大方向上不同的 Intent。"
    "方向指解决问题的整体链路、核心假设或证据推导路径；不是换一个工具、URL、"
    "参数或措辞。Intent 是探索方向，不要求预先知道能否成功或完整执行链路。"
    "内层在该 Intent 内根据实际观察逐步决定下一动作、所需工具及参数；"
    "不要求在生成 Intent 时列完后续 MCP 或固定执行顺序。"
    "同一 Turn 内的 Intent 是从当前已知状态出发的备选解决链路，不得依赖尚未执行的"
    "兄弟 Intent 才能成立。只提出有依据的方向；只有一个方向就返回一个，"
    "四五个有价值的方向也可以，不凑数量，不设固定数量指标。"
    "先查看 active_intent_frontier 及对应 history items，不要重新创建仍在等待或执行中的"
    "同一解决方向。可以沿前置方向继续深入，但必须说明新 Fact 带来的新问题或进展。"
    "内层只提出建议，外层根据证据接受、调整或否决；证据或能力不足时明确说明。"
)


def enabled(request):
    packet = getattr(request, 'prior_summary', '') or getattr(request, 'history_context', '')
    return bool(packet and json.loads(packet).get('intent_planning', {}).get('version') == 1)


def candidate_limit(request):
    return None if enabled(request) else 2
