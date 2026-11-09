"""사운드보드 관련 오디오 처리: 건의 파일 변환(5.2초/512KB 제한), 재생음 다운로드·디코딩."""
from __future__ import annotations

import asyncio
import logging
import shutil
from collections import OrderedDict
from pathlib import Path

import numpy as np

from src.config import SAMPLE_RATE, SOUND_MAX_BYTES, SOUND_MAX_SECONDS

log = logging.getLogger(__name__)

CDN_URL = "https://cdn.discordapp.com/soundboard-sounds/{}"


class SoundError(Exception):
    """사용자에게 그대로 보여줘도 되는 짧은 메시지를 담는다."""


async def _ffmpeg(ffmpeg: str, args: list[str], data: bytes, timeout: int) -> bytes:
    if shutil.which(ffmpeg) is None and not Path(ffmpeg).exists():
        raise SoundError("FFmpeg를 찾을 수 없어요")
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-loglevel", "error", *args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except (OSError, NotImplementedError) as exc:
        raise SoundError(f"FFmpeg 실행 실패: {exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(data), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise SoundError("변환이 너무 오래 걸려요") from None
    if proc.returncode != 0 or not out:
        log.warning("ffmpeg 실패: %s", err.decode(errors="replace")[:300])
        raise SoundError("오디오 파일을 읽을 수 없어요")
    return out


async def decode_pcm(data: bytes, ffmpeg: str = "ffmpeg", timeout: int = 60, *,
                     channels: int = 2, max_seconds: float | None = None) -> bytes:
    """아무 오디오 → s16le / 48kHz PCM."""
    args = ["-i", "pipe:0"]
    if max_seconds:
        args += ["-t", str(max_seconds)]
    args += ["-f", "s16le", "-ac", str(channels), "-ar", str(SAMPLE_RATE), "pipe:1"]
    return await _ffmpeg(ffmpeg, args, data, timeout)


async def prepare_sound(data: bytes, ffmpeg: str = "ffmpeg", timeout: int = 60) -> tuple[bytes, float]:
    """업로드 가능한 모노 MP3 로 변환. (mp3 바이트, 길이(초)) 를 돌려준다.
    5.2초를 넘으면 잘라서 올리지 않고 거절한다 (어디를 자를지는 사람이 정해야 하므로)."""
    pcm = await decode_pcm(data, ffmpeg, timeout, channels=1, max_seconds=SOUND_MAX_SECONDS + 0.8)
    dur = len(pcm) / (SAMPLE_RATE * 2)
    if dur > SOUND_MAX_SECONDS:
        raise SoundError(f"{SOUND_MAX_SECONDS}초를 넘어요. 더 짧게 잘라주세요")
    if dur < 0.1:
        raise SoundError("너무 짧아요")
    for bitrate in ("128k", "64k"):
        mp3 = await _ffmpeg(
            ffmpeg,
            ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
             "-codec:a", "libmp3lame", "-b:a", bitrate, "-f", "mp3", "pipe:1"],
            pcm, timeout)
        if len(mp3) <= SOUND_MAX_BYTES - 8192:
            return mp3, dur
    raise SoundError("파일이 너무 커요")


async def download(session, sound_id: int, limit: int = 2 * 2**20) -> bytes | None:
    """Discord CDN 에서 사운드보드 소리 파일 다운로드. 실패하면 None."""
    try:
        async with session.get(CDN_URL.format(sound_id)) as resp:
            if resp.status != 200:
                log.warning("사운드보드 다운로드 실패: %s (%s)", sound_id, resp.status)
                return None
            data = await resp.content.read(limit + 1)
            return data if len(data) <= limit else None
    except Exception:
        log.exception("사운드보드 다운로드 오류")
        return None


def scale_pcm(pcm: bytes, volume: float) -> bytes:
    if volume == 1.0:
        return pcm
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32) * volume
    return np.clip(np.rint(a), -32768, 32767).astype("<i2").tobytes()


class PCMCache:
    """sound_id → 디코딩된 PCM 의 작은 LRU. 같은 소리를 반복 재생해도 다시 받지 않는다."""

    def __init__(self, size: int = 16) -> None:
        self.size = size
        self._d: OrderedDict[int, bytes] = OrderedDict()

    def get(self, key: int) -> bytes | None:
        if key in self._d:
            self._d.move_to_end(key)
            return self._d[key]
        return None

    def put(self, key: int, value: bytes) -> None:
        self._d[key] = value
        self._d.move_to_end(key)
        while len(self._d) > self.size:
            self._d.popitem(last=False)
