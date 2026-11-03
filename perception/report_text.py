"""把表达特征写成成绩单能直接引用的文字。"""

from __future__ import annotations

from dataclasses import asdict

from perception.delivery import SessionDelivery, TurnDelivery, summarize

_TEXT_ONLY_TAIL = "请严格按 JSON 格式输出最终评分报告。"


def text_only_report_request(archive: str) -> str:
    return f"这是该考生本场面试的完整文字记录：\n{archive}\n{_TEXT_ONLY_TAIL}"


def build_report_user_content(archive: str, turns, enabled: bool) -> str:
    """成绩单的用户消息。关掉时与纯文字成绩单相同。"""
    if not enabled:
        return text_only_report_request(archive)
    report = delivery_report(turns)
    if not report["measured_turns"]:
        gap = "本场无可靠表达特征。"
        if report["missing_turns"]:
            gap = f"本场无可靠表达特征（{report['missing_turns']} 轮）。"
        return (
            f"这是该考生本场面试的完整文字记录：\n{archive}\n"
            f"{gap}表达逻辑只按文字记录评分，exp_logic_note 留空字符串。"
            "不要编造语速、停顿、音量或填充词。\n"
            f"{_TEXT_ONLY_TAIL}"
        )
    turn_lines = [
        _turn_line(index, turn) for index, turn in enumerate(report["turns"], start=1)
    ]
    summary = "一场合计：" + _joined(report["display"]) + "。"
    if report["missing_turns"]:
        summary += f"缺测 {report['missing_turns']} 轮。"
    evidence = (
        "以下是本场表达特征，只允许用于表达逻辑和 exp_logic_note。\n"
        "逐轮：\n"
        + "\n".join(turn_lines)
        + "\n"
        + summary
        + "\n"
    )
    return (
        f"这是该考生本场面试的完整文字记录：\n{archive}\n"
        f"{evidence}{_TEXT_ONLY_TAIL}"
    )


def delivery_report(turns) -> dict:
    """供成绩单数据和提示词共用的一场合计。"""
    coerced = _coerce_turns(turns)
    summary = summarize(coerced)
    return {
        "measured_turns": summary.measured_turns,
        "missing_turns": summary.missing_turns,
        "duration_seconds": summary.duration_seconds,
        "speech_rate_chars_per_second": summary.speech_rate_chars_per_second,
        "pause_ratio": summary.pause_ratio,
        "mean_pause_seconds": summary.mean_pause_seconds,
        "pitch_std_hz": summary.pitch_std_hz,
        "intensity_db": summary.intensity_db,
        "filler_ratio": summary.filler_ratio,
        "display": _display(summary),
        "turns": [asdict(turn) for turn in coerced],
    }


def _coerce_turns(turns) -> list[TurnDelivery]:
    coerced: list[TurnDelivery] = []
    for item in turns or []:
        if isinstance(item, TurnDelivery):
            coerced.append(item)
        elif isinstance(item, dict):
            known = {field: item.get(field) for field in TurnDelivery.__dataclass_fields__}
            known["missing"] = bool(item.get("missing"))
            coerced.append(TurnDelivery(**known))
    return coerced


def _display(summary: SessionDelivery) -> dict[str, str]:
    return _display_values(
        summary.duration_seconds,
        summary.speech_rate_chars_per_second,
        summary.pause_ratio,
        summary.mean_pause_seconds,
        summary.pitch_std_hz,
        summary.intensity_db,
        summary.filler_ratio,
    )


def _display_values(duration, speech_rate, pause_ratio, mean_pause, pitch_std, intensity, filler_ratio):
    return {
        "duration": _quantity(duration, "秒"),
        "speech_rate": _quantity(speech_rate, "字/秒"),
        "pause_ratio": _percent(pause_ratio),
        "mean_pause": _quantity(mean_pause, "秒"),
        "pitch_std": _quantity(pitch_std, "赫兹"),
        "intensity": _quantity(intensity, "分贝"),
        "filler_ratio": _percent(filler_ratio),
    }


def _turn_line(index: int, turn: dict) -> str:
    if turn.get("missing"):
        return f"第{index}轮：缺测"
    view = _display_values(
        turn.get("duration_seconds"),
        turn.get("speech_rate_chars_per_second"),
        turn.get("pause_ratio"),
        turn.get("mean_pause_seconds"),
        turn.get("pitch_std_hz"),
        turn.get("intensity_db"),
        turn.get("filler_ratio"),
    )
    return f"第{index}轮：" + _joined(view)


def _joined(view: dict[str, str]) -> str:
    return (
        f"回答时长 {view['duration']}，语速 {view['speech_rate']}，"
        f"停顿比例 {view['pause_ratio']}，平均停顿 {view['mean_pause']}，"
        f"音高变化 {view['pitch_std']}，音量 {view['intensity']}，"
        f"填充词比例 {view['filler_ratio']}"
    )


def _quantity(value, unit: str) -> str:
    if value is None:
        return "缺测"
    return f"{float(value):.1f} {unit}"


def _percent(value) -> str:
    if value is None:
        return "缺测"
    return f"{float(value) * 100:.1f}%"
