"""PCM 프레임 정규화 유틸.

Opus → PCM 디코딩 자체는 수신 라이브러리(discord-ext-voice-recv, libopus)가 수행한다.
여기서는 수신된 PCM 바이트를 mixer/exporter 가 쓰는 표준 포맷(48kHz, 16-bit, stereo)으로
검증/변환하는 일만 한다.
"""
from __future__ import annotations

import numpy as np

from src.config import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH


class PCMFormatError(ValueError):
    pass


def frame_bytes(duration_ms: int = 20, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS) -> int:
    return sample_rate * duration_ms // 1000 * channels * SAMPLE_WIDTH


def pcm_to_array(pcm: bytes, channels: int = CHANNELS) -> np.ndarray:
    """s16le PCM 바이트 → (samples, channels) int16 배열."""
    if len(pcm) % (SAMPLE_WIDTH * channels) != 0:
        raise PCMFormatError(f"PCM 길이({len(pcm)})가 프레임 크기의 배수가 아닙니다")
    return np.frombuffer(pcm, dtype="<i2").reshape(-1, channels)


def array_to_pcm(arr: np.ndarray) -> bytes:
    return np.ascontiguousarray(arr, dtype="<i2").tobytes()


def validate_frame(pcm: bytes) -> bool:
    """수신 프레임이 비어 있지 않고 스테레오 s16le 정렬을 만족하는지."""
    return bool(pcm) and len(pcm) % (SAMPLE_WIDTH * CHANNELS) == 0


def duration_of(pcm: bytes) -> float:
    return len(pcm) / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH)
