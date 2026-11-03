"""从本机 WAV 读取波形。脚本不调用语音转写。"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from perception.delivery import TurnDelivery, measure_turn


def measure_wav(path: str | Path, transcript: str) -> TurnDelivery:
    samples, sample_rate = read_wav(path)
    return measure_turn(samples, sample_rate, transcript)


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        sample_rate = handle.getframerate()
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        frames = handle.readframes(handle.getnframes())
    if sample_width != 2:
        raise ValueError("只接受 16 位 PCM WAV")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float64) / 32768.0
    if channels > 1:
        usable = samples.size - (samples.size % channels)
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(samples), sample_rate
