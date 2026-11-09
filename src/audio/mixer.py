"""타임스탬프 기반 믹싱.

소리 깨짐을 막기 위한 처리:
- 같은 사람의 프레임이 겹치면 더하지 않고 덮어쓴다 (이중 합산으로 생기는 튐 방지)
- 패킷 손실로 생긴 짧은 틈은 선형 보간으로 메우고, 긴 무음 경계에는 2ms 페이드를 넣는다 (딱딱 소리 방지)
- 사람마다 음량을 맞춘 뒤 합친다 (큰 목소리가 합쳐질 때 넘치는 것 방지)
- 하드 클리핑 대신 룩어헤드 리미터를 쓴다 (천장 -1dBFS, MP3 인코딩 여유)
위 처리는 normalize=True 일 때 적용된다. 기본 모드는 단순 합산 + 클리핑.
"""
from __future__ import annotations

import numpy as np

from src.audio.buffer import Frame
from src.audio.decoder import pcm_to_array
from src.config import CHANNELS, SAMPLE_RATE

NOISE_FLOOR = 150.0            # 이 RMS 아래는 무음/잡음으로 보고 음량 계산에서 뺀다
CEILING = 32767 * 0.89         # 약 -1 dBFS
FADE = SAMPLE_RATE * 2 // 1000   # 2ms
MAX_FILL_GAP = SAMPLE_RATE * 5 // 1000  # 5ms 이하의 틈은 보간으로 메운다
CLIP_LEVEL = 32700


def _rms(a: np.ndarray) -> float:
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def _source_gain(frames: list[Frame], target_rms: float, min_gain: float, max_gain: float) -> float:
    """한 사람(또는 한 소리)의 말하는 구간 RMS 가 target_rms 가 되도록 하는 배율."""
    r = np.array([_rms(pcm_to_array(f.pcm).astype(np.float32)) for f in frames])
    if r.size == 0:
        return 1.0
    floor = max(NOISE_FLOOR, 0.1 * float(np.percentile(r, 90)))
    active = r[r > floor]
    if active.size == 0:
        return 1.0
    level = float(np.sqrt(np.mean(active ** 2)))
    return float(np.clip(target_rms / level, min_gain, max_gain))


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


def _track(frames: list[Frame], start: float, total: int, additive: bool = False) -> np.ndarray:
    """한 소스를 시간 축에 배치하고 틈/경계를 매끄럽게 처리한다.
    additive=False(사람 목소리): 겹치면 덮어쓴다. additive=True(사운드보드 재생음): 겹치면 더한다."""
    track = np.zeros((total, CHANNELS), dtype=np.float32)
    cover = np.zeros(total, dtype=bool)
    for f in sorted(frames, key=lambda fr: fr.start):
        data = pcm_to_array(f.pcm)
        offset = int(round((f.start - start) * SAMPLE_RATE))
        src_from = max(0, -offset)
        dst_from = max(0, offset)
        n = min(len(data) - src_from, total - dst_from)
        if n <= 0:
            continue
        if additive:
            track[dst_from:dst_from + n] += data[src_from:src_from + n]
        else:
            track[dst_from:dst_from + n] = data[src_from:src_from + n]
        cover[dst_from:dst_from + n] = True
    if not cover.any():
        return track

    edges = np.diff(cover.astype(np.int8), prepend=0, append=0)
    starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]

    # 짧은 틈은 선형 보간으로 메운다
    for e, s in zip(ends[:-1], starts[1:]):
        gap = s - e
        if 0 < gap <= MAX_FILL_GAP:
            a, b = track[e - 1], track[s]
            t = (np.arange(1, gap + 1, dtype=np.float32) / (gap + 1))[:, None]
            track[e:s] = a + (b - a) * t
            cover[e:s] = True

    # 남은 경계(긴 무음과 맞닿는 곳)에는 짧은 페이드
    edges = np.diff(cover.astype(np.int8), prepend=0, append=0)
    starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]
    for s, e in zip(starts, ends):
        n = min(FADE, (e - s) // 2)
        if n > 1:
            ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
            track[s:s + n] *= ramp
            track[e - n:e] *= ramp[::-1]
    return track


def limit(x: np.ndarray, ceiling: float = CEILING, block: int = 48, look: int = 8) -> np.ndarray:
    """룩어헤드 리미터. 천장을 넘는 부분만 부드럽게 눌러서 찌그러짐 없이 크기를 맞춘다."""
    n = len(x)
    if n == 0:
        return x
    peak = np.abs(x).max(axis=1)
    if float(peak.max()) <= ceiling:
        return x
    nb = -(-n // block)
    pb = np.pad(peak, (0, nb * block - n)).reshape(nb, block).max(axis=1)
    g = np.minimum(1.0, ceiling / np.maximum(pb, 1.0))
    wide = 2 * look
    gp = np.pad(g, (wide, wide), constant_values=1.0)
    gm = np.lib.stride_tricks.sliding_window_view(gp, 2 * wide + 1).min(axis=1)
    k = 2 * look + 1
    kernel = np.hanning(k + 2)[1:-1]
    kernel /= kernel.sum()
    gs = np.convolve(np.pad(gm, (look, look), mode="edge"), kernel, mode="valid")
    centers = (np.arange(nb) + 0.5) * block
    gain = np.interp(np.arange(n), centers, gs).astype(np.float32)
    return x * gain[:, None]


def _mix_raw(frames_by_user: dict[int, list[Frame]], start: float, total: int) -> np.ndarray:
    """기본 모드: 프레임을 제 위치에 놓고 겹치면 합산, 마지막에 int16 범위로 클리핑."""
    acc = np.zeros((total, CHANNELS), dtype=np.int32)
    for frames in frames_by_user.values():
        for f in frames:
            data = pcm_to_array(f.pcm)
            offset = int(round((f.start - start) * SAMPLE_RATE))
            src_from = max(0, -offset)
            dst_from = max(0, offset)
            n = min(len(data) - src_from, total - dst_from)
            if n > 0:
                acc[dst_from:dst_from + n] += data[src_from:src_from + n]
    return np.clip(acc, -32768, 32767).astype(np.int16)


def mix(frames_by_user: dict[int, list[Frame]], start: float, end: float, *,
        normalize: bool = False, target_rms: float = 3000.0,
        min_gain: float = 0.25, max_gain: float = 6.0) -> np.ndarray:
    """[start, end) 구간을 (samples, 2) int16 배열로 믹싱. 빈 구간은 무음.

    normalize=False: 단순 합산 + 하드 클리핑 (원본 그대로, 테스트/분석용).
    normalize=True : 사람마다(사운드보드 재생음은 별도 소스) 음량을 target_rms 에 맞추고,
                     틈 보간/페이드/리미터로 깨짐을 막는다. /clip 이 사용하는 모드.
    """
    if end <= start:
        raise ValueError("end 는 start 보다 커야 합니다")
    total = int(round((end - start) * SAMPLE_RATE))
    if not normalize:
        return _mix_raw(frames_by_user, start, total)
    acc = np.zeros((total, CHANNELS), dtype=np.float32)
    for key, frames in frames_by_user.items():
        if not frames:
            continue
        gain = _source_gain(frames, target_rms, min_gain, max_gain)
        acc += _track(frames, start, total, additive=key < 0) * gain
    acc = limit(acc)
    return np.clip(np.rint(acc), -32768, 32767).astype(np.int16)
