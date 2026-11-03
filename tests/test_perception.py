import math

import numpy as np
import pytest

from interview_graph import FINAL_REPORT_SYSTEM, report_system_prompt
from perception import SessionDelivery, TurnDelivery, measure_turn, summarize
from perception.report_text import build_report_user_content


def _tone(sample_rate: int, seconds: float, frequency_hz: float, amplitude: float) -> np.ndarray:
    count = int(round(sample_rate * seconds))
    time = np.arange(count) / sample_rate
    return amplitude * np.sin(2 * np.pi * frequency_hz * time)


def test_known_signal_reports_rate_pauses_pitch_and_fillers():
    """2 秒纯音、0.4 秒静音、1 秒纯音。语速和停顿按这条波形手工计算。"""
    sample_rate = 16000
    samples = np.concatenate(
        [
            _tone(sample_rate, 2.0, 150, 0.2),
            np.zeros(int(sample_rate * 0.4)),
            _tone(sample_rate, 1.0, 150, 0.2),
        ]
    )

    delivery = measure_turn(samples, sample_rate, "嗯今天天气好")

    assert delivery.missing is False
    assert delivery.duration_seconds == 3.4
    assert delivery.pause_seconds == 0.4
    assert delivery.pause_count == 1
    assert delivery.voiced_seconds == 3.0
    assert delivery.speech_rate_chars_per_second == 2.0  # 6 字 / 3 秒发声
    assert delivery.pause_ratio == 0.4 / 3.4
    assert delivery.mean_pause_seconds == 0.4
    assert delivery.filler_ratio == 1 / 6  # 「嗯」1 字 / 6 字
    assert delivery.pitch_std_hz == pytest_approx_zero()
    assert delivery.intensity_db == pytest_approx_sine_db()


def pytest_approx_zero():
    return pytest.approx(0.0, abs=2.0)


def pytest_approx_sine_db():
    """0.2 帕正弦的均方根声强，容差留给 Praat 的窗。"""
    rms = 0.2 / math.sqrt(2)
    decibels = 20 * math.log10(rms / 2e-5)
    return pytest.approx(decibels, abs=3.0)


def _known_signal(sample_rate: int = 16000) -> np.ndarray:
    return np.concatenate(
        [
            _tone(sample_rate, 2.0, 150, 0.2),
            np.zeros(int(sample_rate * 0.4)),
            _tone(sample_rate, 1.0, 150, 0.2),
        ]
    )


def test_speech_shorter_than_one_second_is_missing():
    sample_rate = 16000
    samples = np.concatenate(
        [
            _tone(sample_rate, 0.4, 150, 0.2),
            np.zeros(int(sample_rate * 2.0)),
        ]
    )

    delivery = measure_turn(samples, sample_rate, "今天天气不错")

    assert delivery.missing is True
    assert delivery.speech_rate_chars_per_second is None
    assert delivery.pause_ratio is None
    assert delivery.pitch_std_hz is None
    assert delivery.intensity_db is None
    assert delivery.filler_ratio is None


def test_silence_is_missing():
    delivery = measure_turn(np.zeros(16000 * 3), 16000, "今天天气不错")

    assert delivery.missing is True


def test_empty_transcript_drops_only_rate_and_fillers():
    delivery = measure_turn(_known_signal(), 16000, "   ")

    assert delivery.missing is False
    assert delivery.speech_rate_chars_per_second is None
    assert delivery.filler_ratio is None
    assert delivery.pause_seconds == 0.4
    assert delivery.pitch_std_hz is not None
    assert delivery.intensity_db is not None


def test_closed_filler_list_uses_transcript_characters():
    sample_rate = 16000
    samples = _tone(sample_rate, 1.2, 150, 0.2)

    delivery = measure_turn(samples, sample_rate, "然后我们")

    assert delivery.filler_ratio == 0.5
    assert delivery.speech_rate_chars_per_second == pytest.approx(4 / 1.2)


def test_bad_audio_is_missing_and_does_not_raise():
    assert measure_turn(np.array([]), 16000, "你好").missing is True
    assert measure_turn(np.array([math.nan, 0.0]), 16000, "你好").missing is True
    assert measure_turn(_tone(16000, 1.2, 150, 0.2), 0, "你好").missing is True


def test_summary_ignores_missing_turns_and_empty_transcripts():
    measured = TurnDelivery(
        missing=False,
        duration_seconds=3.4,
        voiced_seconds=3.0,
        pause_seconds=0.4,
        pause_count=1,
        speech_rate_chars_per_second=2.0,
        pause_ratio=0.4 / 3.4,
        mean_pause_seconds=0.4,
        pitch_std_hz=10.0,
        intensity_db=70.0,
        filler_ratio=0.2,
        transcript_chars=10,
        filler_chars=2,
    )
    unheard = TurnDelivery(
        missing=False,
        duration_seconds=1.0,
        voiced_seconds=1.0,
        pause_seconds=0.0,
        pause_count=0,
        speech_rate_chars_per_second=None,
        pause_ratio=0.0,
        mean_pause_seconds=0.0,
        pitch_std_hz=30.0,
        intensity_db=60.0,
        filler_ratio=None,
        transcript_chars=0,
        filler_chars=0,
    )
    other = TurnDelivery(
        missing=False,
        duration_seconds=2.0,
        voiced_seconds=2.0,
        pause_seconds=0.0,
        pause_count=0,
        speech_rate_chars_per_second=4.0,
        pause_ratio=0.0,
        mean_pause_seconds=0.0,
        pitch_std_hz=20.0,
        intensity_db=80.0,
        filler_ratio=0.25,
        transcript_chars=8,
        filler_chars=2,
    )

    summary = summarize([measured, TurnDelivery(missing=True), unheard, other])

    assert summary.measured_turns == 3
    assert summary.missing_turns == 1
    assert summary.duration_seconds == 6.4
    assert summary.speech_rate_chars_per_second == 3.6  # 18 字 / 5 秒发声，空转写不进分母
    assert summary.pause_ratio == 0.4 / 6.4
    assert summary.mean_pause_seconds == 0.4
    assert summary.pitch_std_hz == 20.0
    assert summary.intensity_db == (70.0 * 3.4 + 60.0 * 1.0 + 80.0 * 2.0) / 6.4
    assert summary.filler_ratio == 4 / 18


def test_summary_of_only_missing_turns_has_no_numbers():
    summary = summarize([TurnDelivery(missing=True), TurnDelivery(missing=True)])

    assert summary == SessionDelivery(measured_turns=0, missing_turns=2)


def _measured_example() -> TurnDelivery:
    return TurnDelivery(
        missing=False,
        duration_seconds=3.4,
        voiced_seconds=3.0,
        pause_seconds=0.4,
        pause_count=1,
        speech_rate_chars_per_second=2.0,
        pause_ratio=0.4 / 3.4,
        mean_pause_seconds=0.4,
        pitch_std_hz=0.0,
        intensity_db=77.0,
        filler_ratio=1 / 6,
        transcript_chars=6,
        filler_chars=1,
    )


def test_report_prompt_stays_text_only_when_delivery_is_off():
    assert report_system_prompt(False) == FINAL_REPORT_SYSTEM
    assert build_report_user_content("【记录】", [], enabled=False) == (
        "这是该考生本场面试的完整文字记录：\n【记录】\n请严格按 JSON 格式输出最终评分报告。"
    )


def test_report_prompt_gives_measurements_only_to_expression():
    text = build_report_user_content(
        "【记录】",
        [_measured_example(), TurnDelivery(missing=True)],
        enabled=True,
    )
    system = report_system_prompt(True)

    assert "【记录】" in text
    assert "语速 2.0 字/秒" in text
    assert "11.8%" in text
    assert "16.7%" in text
    assert "第2轮：缺测" in text
    assert "缺测 1 轮" in text
    assert "exp_logic_note" in system
    assert "专业知识" in system
    assert "不得" in system
    assert "紧张" in system


def test_wav_file_matches_the_known_signal(tmp_path):
    import wave

    from perception.audio import measure_wav

    samples = np.clip(_known_signal(), -1.0, 1.0)
    pcm = (samples * 32767).astype("<i2")
    path = tmp_path / "turn.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(pcm.tobytes())

    delivery = measure_wav(path, "嗯今天天气好")

    assert delivery.missing is False
    assert delivery.speech_rate_chars_per_second == pytest.approx(2.0, abs=0.05)
    assert delivery.pause_seconds == pytest.approx(0.4, abs=0.02)
    assert delivery.filler_ratio == pytest.approx(1 / 6, abs=0.01)


def test_report_prompt_does_not_invent_numbers_when_nothing_was_measured():
    text = build_report_user_content("【记录】", [TurnDelivery(missing=True)], enabled=True)

    assert "无可靠表达特征" in text
    assert "字/秒" not in text
    assert "0.0" not in text
