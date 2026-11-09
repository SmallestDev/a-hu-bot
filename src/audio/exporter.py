"""WAV / MP3 출력. 클립 파일만 디스크에 쓴다."""
from __future__ import annotations
 
import asyncio
import shutil
import wave
from datetime import datetime
from pathlib import Path
 
import numpy as np
 
from src.config import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH
 
 
class ExportError(RuntimeError):
    pass
 
 
class FileTooLargeError(ExportError):
    pass
 
 
def write_wav(path: Path, samples: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(SAMPLE_WIDTH)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(np.ascontiguousarray(samples, dtype="<i2").tobytes())
        tmp.replace(path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise ExportError(f"WAV 저장 실패: {exc}") from exc
    return path
 
 
async def wav_to_mp3(wav: Path, ffmpeg: str = "ffmpeg", timeout: int = 60, bitrate: str = "128k") -> Path:
    """FFmpeg 로 MP3 변환. 실행 파일 없음/타임아웃/비정상 종료를 ExportError 로 변환."""
    if shutil.which(ffmpeg) is None and not Path(ffmpeg).exists():
        raise ExportError(f"FFmpeg 를 찾을 수 없습니다 ({ffmpeg}). PATH 또는 FFMPEG_PATH 를 확인하세요.")
    mp3 = wav.with_suffix(".mp3")
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-loglevel", "error", "-i", str(wav),
            "-codec:a", "libmp3lame", "-b:a", bitrate, str(mp3),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, NotImplementedError) as exc:
        raise ExportError(f"FFmpeg 실행 실패: {exc}") from exc
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        mp3.unlink(missing_ok=True)
        raise ExportError(f"FFmpeg 변환이 {timeout}초를 넘어 중단되었습니다") from None
    if proc.returncode != 0:
        mp3.unlink(missing_ok=True)
        raise ExportError(f"FFmpeg 오류(code {proc.returncode}): {err.decode(errors='replace')[:300]}")
    return mp3
 
 
def clip_path(clips_dir: Path, guild_id: int, ext: str = "wav") -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return clips_dir / f"clip-{guild_id}-{stamp}.{ext}"
 
 
async def export_clip(samples: np.ndarray, clips_dir: Path, guild_id: int, *, fmt: str = "wav",
                      max_bytes: int, ffmpeg: str = "ffmpeg", timeout: int = 60) -> Path:
    """fmt='wav' 이어도 크기 상한을 넘으면 MP3 로 자동 대체한다. 그래도 넘으면 FileTooLargeError."""
    wav = write_wav(clip_path(clips_dir, guild_id, "wav"), samples)
    if fmt == "wav" and wav.stat().st_size <= max_bytes:
        return wav
    try:
        mp3 = await wav_to_mp3(wav, ffmpeg, timeout)
    except ExportError:
        if fmt == "wav":
            wav.unlink(missing_ok=True)
            raise FileTooLargeError("WAV 가 크기 제한을 넘었고 MP3 변환도 실패했습니다") from None
        wav.unlink(missing_ok=True)
        raise
    wav.unlink(missing_ok=True)
    if mp3.stat().st_size > max_bytes:
        mp3.unlink(missing_ok=True)
        raise FileTooLargeError("클립이 파일 크기 제한을 넘습니다. 더 짧게 요청하세요.")
    return mp3
 
 
async def trim_clip(src: Path, start: float, end: float, clips_dir: Path, guild_id: int, *,
                    max_bytes: int, ffmpeg: str = "ffmpeg", timeout: int = 60) -> Path:
    """이미 만든 클립 파일에서 [start, end] 초 구간만 새 파일로 자른다."""
    if not (0 <= start < end):
        raise ExportError("구간이 올바르지 않아요")
    if not src.exists():
        raise ExportError("원본 클립 파일이 없어요")
    if src.suffix == ".wav":
        with wave.open(str(src), "rb") as w:
            rate, ch = w.getframerate(), w.getnchannels()
            n = w.getnframes()
            a, b = int(start * rate), min(int(end * rate), n)
            if a >= b:
                raise ExportError("구간이 클립 길이를 벗어났어요")
            w.setpos(a)
            raw = w.readframes(b - a)
        samples = np.frombuffer(raw, dtype="<i2").reshape(-1, ch)
        return await export_clip(samples, clips_dir, guild_id, fmt="wav",
                                 max_bytes=max_bytes, ffmpeg=ffmpeg, timeout=timeout)
    # mp3: FFmpeg 로 자르기
    if shutil.which(ffmpeg) is None and not Path(ffmpeg).exists():
        raise ExportError(f"FFmpeg 를 찾을 수 없습니다 ({ffmpeg})")
    out = clip_path(clips_dir, guild_id, "mp3")
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", "-loglevel", "error", "-ss", str(start), "-to", str(end), "-i", str(src),
        "-codec:a", "libmp3lame", "-b:a", "128k", str(out),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        out.unlink(missing_ok=True)
        raise ExportError("FFmpeg 시간 초과") from None
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        raise ExportError(f"FFmpeg 오류: {err.decode(errors='replace')[:200]}")
    if out.stat().st_size > max_bytes:
        out.unlink(missing_ok=True)
        raise FileTooLargeError("파일이 너무 커요")
    return out