"""타임스탬프 기반 믹싱 (사람별 볼륨 자동 보정 없음).

안전하게 믹싱하는 원칙:
- 서로 다른 사용자의 프레임은 단순 합산한다.
- 동일 사용자의 프레임이 시간상 겹치면 더하지 않고 덮어쓴다 (이중 합산 튐 방지).
- 패킷 손실로 생긴 짧은 틈(5ms 이하)은 선형 보간으로 메우고, 긴 무음 경계에는
  2ms 페이드를 넣어 딱딱 소리(클릭)를 방지한다. (이건 음량 보정이 아님)
- 마지막으로 전체 믹스에 단일 헤드룸 게인을 걸어 int16 최대치(±32767) 클리핑,
  즉 겹침 시 소리 깨짐을 방지한다. 사람마다 RMS 를 맞추지 않으므로 조용한 목소리가
  억지로 커지거나 큰 목소리가 억지로 작아지지 않는다.
"""
from __future__ import annotations

import numpy as np

from src.audio.buffer import Frame
from src.audio.decoder import pcm_to_array
from src.config import CHANNELS, SAMPLE_RATE

NOISE_FLOOR = 150.0          # _track 에서는 쓰이지 않음 (호환 상수로 보존)
CEILING = 32767 * 0.89       # 약 -1 dBFS. MP3 인코딩 여유를 둔 천장
FADE = SAMPLE_RATE * 2 // 1000   # 2ms
MAX_FILL_GAP = SAMPLE_RATE * 5 // 1000  # 5ms 이하의 틈은 보간으로 메운다
CLIP_LEVEL = 32700


def _rms(a: np.ndarray) -> float:
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def clipped_ratio(frames: list[Frame]) -> float:
    """원본 프레임에서 이미 한계(±32700 이상)에 붙어 있는 샘플의 비율. 마이크 입력이 과한지 판단용."""
    hit = total = 0
    for f in frames:
        a = pcm_to_array(f.pcm)
        hit += int(np.count_nonzero(np.abs(a.astype(np.int32)) >= CLIP_LEVEL))
        total += a.size
    return hit / total if total else 0.0


def clipping_ratio(samples: np.ndarray) -> float:
    """믹싱 결과에서 한계(±32700 이상)에 붙은 샘플의 비율."""
    if samples.size == 0:
        return 0.0
    return float(np.count_nonzero(np.abs(samples.astype(np.int32)) >= CLIP_LEVEL)) / samples.size


def _track(frames: list[Frame], start: float, total: int) -> np.ndarray:
    """한 소스를 시간 축에 배치한다.

    동일 사용자 프레임이 겹치면 덮어쓴다. 추후 합산 시 서로 다른 사용자는 더해진다.
    """
    track = np.zeros((total, CHANNELS), dtype=np.float32)
    cover = np.zeros(total, dtype=bool)
    for f in sorted(frames, key=lambda fr: fr.start):
        data = pcm_to_array(f.pcm).astype(np.float32)
        offset = int(round((f.start - start) * SAMPLE_RATE))
        src_from = max(0, -offset)
        dst_from = max(0, offset)
        n = min(len(data) - src_from, total - dst_from)
        if n <= 0:
            continue
        track[dst_from:dst_from + n] = data[src_from:src_from + n]
        cover[dst_from:dst_from + n] = True
    if not cover.any():
        return track

    edges = np.diff(cover.astype(np.int8), prepend=0, append=0)
    starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]

    # 짧은 틈은 선형 보간으로 메운다 (무음 드롭아웃 방지)
    for e, s in zip(ends[:-1], starts[1:]):
        gap = s - e
        if 0 < gap <= MAX_FILL_GAP:
            a, b = track[e - 1], track[s]
            t = (np.arange(1, gap + 1, dtype=np.float32) / (gap + 1))[:, None]
            track[e:s] = a + (b - a) * t
            cover[e:s] = True

    # 남은 경계(긴 무음과 맞닿는 곳)에는 짧은 페이드 (딱딱 소리 방지)
    edges = np.diff(cover.astype(np.int8), prepend=0, append=0)
    starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]
    for s, e in zip(starts, ends):
        n = min(FADE, (e - s) // 2)
        if n > 1:
            ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
            track[s:s + n] *= ramp
            track[e - n:e] *= ramp[::-1]
    return track


def mix(frames_by_user: dict[int, list[Frame]], start: float, end: float, *,
        ceiling: float = CEILING) -> np.ndarray:
    """[start, end) 구간을 (samples, 2) int16 배열로 믹싱. 빈 구간은 무음.

    사람별 볼륨 자동 보정(정규화/음량 맞추기/리미터)을 전혀 하지 않는다.
    서로 다른 사용자의 음성을 단순 합산한 뒤, 전체 믹스에 단일 헤드룸 게인을
    적용해 겹침 시 int16 클리핑(소리 깨짐)을 방지한다.
    """
    if end <= start:
        raise ValueError("end 는 start 보다 커야 합니다")
    total = int(round((end - start) * SAMPLE_RATE))
    acc = np.zeros((total, CHANNELS), dtype=np.float32)
    for frames in frames_by_user.values():
        if not frames:
            continue
        acc += _track(frames, start, total)

    # 단일 헤드룸 게인: 천장을 넘는 전체 믹스만 균일하게 줄인다.
    # 퍼-퍼슨 게인이 아니므로 발화 간 상대 음량 관계와 정숙 구간에 '펌핑'이 생기지 않는다.
    peak = float(np.abs(acc).max()) if acc.size else 0.0
    if peak > ceiling:
        acc *= ceiling / peak
    return np.clip(np.rint(acc), -32768, 32767).astype(np.int16)
