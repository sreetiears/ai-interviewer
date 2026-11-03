"""从一轮波形和转写计算表达特征。"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger("interview-perception")

_FILLERS = ("那个", "就是", "然后", "嗯", "啊", "呃")
_FRAME_SECONDS = 0.01
_SILENCE_RMS = 0.02
_MIN_PAUSE_SECONDS = 0.3
_MIN_VOICED_SECONDS = 1.0


@dataclass(frozen=True)
class TurnDelivery:
    """一轮考生发言的表达特征。``missing`` 为真时，这一轮是缺测。"""

    missing: bool
    duration_seconds: float | None = None
    voiced_seconds: float | None = None
    pause_seconds: float | None = None
    pause_count: int | None = None
    speech_rate_chars_per_second: float | None = None
    pause_ratio: float | None = None
    mean_pause_seconds: float | None = None
    pitch_std_hz: float | None = None
    intensity_db: float | None = None
    filler_ratio: float | None = None
    transcript_chars: int | None = None
    filler_chars: int | None = None


@dataclass(frozen=True)
class SessionDelivery:
    """一场合计。没有可靠轮次时，各项数字为空。"""

    measured_turns: int
    missing_turns: int
    duration_seconds: float | None = None
    speech_rate_chars_per_second: float | None = None
    pause_ratio: float | None = None
    mean_pause_seconds: float | None = None
    pitch_std_hz: float | None = None
    intensity_db: float | None = None
    filler_ratio: float | None = None


def _missing_turn() -> TurnDelivery:
    return TurnDelivery(missing=True)


def measure_turn(samples, sample_rate: int, transcript: str) -> TurnDelivery:
    """测量一轮考生发言。

    ``samples`` 是单声道波形，数值大约在 [-1, 1]。有效说话短于 1 秒、
    没有语音，或计算失败时，返回缺测，不抛出异常。
    """
    try:
        return _measure_turn(samples, sample_rate, transcript)
    except Exception:
        logger.warning("表达特征计算失败", exc_info=True)
        return _missing_turn()


def _measure_turn(samples, sample_rate: int, transcript: str) -> TurnDelivery:
    waveform = np.asarray(samples, dtype=np.float64)
    if waveform.ndim != 1 or sample_rate <= 0 or waveform.size == 0:
        return _missing_turn()
    if not np.isfinite(waveform).all():
        return _missing_turn()

    spans = _pause_spans(waveform, sample_rate)
    total_samples = int(waveform.size)
    pause_samples = sum(end - start for start, end in spans)
    voiced_samples = total_samples - pause_samples
    if voiced_samples < int(round(sample_rate * _MIN_VOICED_SECONDS)):
        return _missing_turn()

    duration_seconds = total_samples / sample_rate
    pause_seconds = pause_samples / sample_rate
    voiced_seconds = voiced_samples / sample_rate
    pause_count = len(spans)
    transcript_chars, filler_chars, filler_ratio = _filler_stats(transcript)
    speech_rate = None
    if transcript_chars:
        speech_rate = transcript_chars / voiced_seconds

    pitch_std, intensity_db = _pitch_and_intensity(waveform, sample_rate, spans)
    return TurnDelivery(
        missing=False,
        duration_seconds=duration_seconds,
        voiced_seconds=voiced_seconds,
        pause_seconds=pause_seconds,
        pause_count=pause_count,
        speech_rate_chars_per_second=speech_rate,
        pause_ratio=pause_seconds / duration_seconds,
        mean_pause_seconds=(pause_seconds / pause_count) if pause_count else 0.0,
        pitch_std_hz=pitch_std,
        intensity_db=intensity_db,
        filler_ratio=filler_ratio,
        transcript_chars=transcript_chars,
        filler_chars=filler_chars,
    )


def summarize(turns: list[TurnDelivery]) -> SessionDelivery:
    """只把有表达特征的轮次收成一场合计。缺测不填 0。"""
    measured = [turn for turn in turns if not turn.missing]
    missing_turns = len(turns) - len(measured)
    if not measured:
        return SessionDelivery(measured_turns=0, missing_turns=missing_turns)

    duration = sum(turn.duration_seconds or 0.0 for turn in measured)
    pause = sum(turn.pause_seconds or 0.0 for turn in measured)
    pause_count = sum(turn.pause_count or 0 for turn in measured)
    rated = [turn for turn in measured if turn.speech_rate_chars_per_second is not None]
    voiced = sum(turn.voiced_seconds or 0.0 for turn in rated)
    transcript_chars = sum(turn.transcript_chars or 0 for turn in rated)
    filled = [turn for turn in measured if turn.filler_ratio is not None]
    filler_chars = sum(turn.filler_chars or 0 for turn in filled)
    filler_transcript_chars = sum(turn.transcript_chars or 0 for turn in filled)

    pitch_values = [turn.pitch_std_hz for turn in measured if turn.pitch_std_hz is not None]
    intensity_weight = 0.0
    intensity_sum = 0.0
    for turn in measured:
        if turn.intensity_db is None or not turn.duration_seconds:
            continue
        intensity_sum += turn.intensity_db * turn.duration_seconds
        intensity_weight += turn.duration_seconds

    return SessionDelivery(
        measured_turns=len(measured),
        missing_turns=missing_turns,
        duration_seconds=duration,
        speech_rate_chars_per_second=(transcript_chars / voiced) if transcript_chars and voiced else None,
        pause_ratio=(pause / duration) if duration else None,
        mean_pause_seconds=(pause / pause_count) if pause_count else 0.0,
        pitch_std_hz=(sum(pitch_values) / len(pitch_values)) if pitch_values else None,
        intensity_db=(intensity_sum / intensity_weight) if intensity_weight else None,
        filler_ratio=(filler_chars / filler_transcript_chars) if filler_transcript_chars else None,
    )


def _pause_spans(samples: np.ndarray, sample_rate: int) -> list[tuple[int, int]]:
    frame = max(1, int(round(sample_rate * _FRAME_SECONDS)))
    min_pause_samples = int(round(sample_rate * _MIN_PAUSE_SECONDS))
    spans: list[tuple[int, int]] = []
    start: int | None = None
    offset = 0
    total = int(samples.size)
    while offset < total:
        end = min(total, offset + frame)
        chunk = samples[offset:end]
        rms = float(np.sqrt(np.mean(np.square(chunk))))
        if rms < _SILENCE_RMS:
            if start is None:
                start = offset
        elif start is not None:
            if offset - start >= min_pause_samples:
                spans.append((start, offset))
            start = None
        offset = end
    if start is not None and total - start >= min_pause_samples:
        spans.append((start, total))
    return spans


def _filler_stats(transcript: str) -> tuple[int, int, float | None]:
    text = "".join(str(transcript or "").split())
    if not text:
        return 0, 0, None
    filler_chars = 0
    index = 0
    while index < len(text):
        word = next((item for item in _FILLERS if text.startswith(item, index)), "")
        if word:
            filler_chars += len(word)
            index += len(word)
        else:
            index += 1
    return len(text), filler_chars, filler_chars / len(text)


def _pitch_and_intensity(
    samples: np.ndarray, sample_rate: int, pause_spans: list[tuple[int, int]]
) -> tuple[float, float]:
    import parselmouth

    sound = parselmouth.Sound(samples, sampling_frequency=sample_rate)
    pitch = sound.to_pitch()
    frequencies = pitch.selected_array["frequency"]
    times = pitch.xs()
    voiced_f0 = [
        float(frequency)
        for frequency, time in zip(frequencies, times)
        if frequency > 0 and not _in_pause(time, sample_rate, pause_spans)
    ]
    if len(voiced_f0) < 2:
        raise ValueError("有声段基频不足")
    pitch_std = float(np.std(np.asarray(voiced_f0, dtype=np.float64)))

    intensity = sound.to_intensity()
    values = np.asarray(intensity.values[0], dtype=np.float64)
    intensity_times = intensity.xs()
    voiced_db = [
        float(value)
        for value, time in zip(values, intensity_times)
        if math.isfinite(value) and not _in_pause(time, sample_rate, pause_spans)
    ]
    if not voiced_db:
        raise ValueError("有声段强度不足")
    return pitch_std, float(np.mean(np.asarray(voiced_db, dtype=np.float64)))


def _in_pause(time: float, sample_rate: int, pause_spans: list[tuple[int, int]]) -> bool:
    position = time * sample_rate
    return any(start <= position < end for start, end in pause_spans)
