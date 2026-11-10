"""환경변수 기반 설정. 토큰은 코드에 쓰지 않고 .env / 환경변수로만 받는다."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent

# 오디오 포맷 (Discord 음성 수신 PCM: 48kHz / 16-bit / stereo / 20ms 프레임)
SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_WIDTH = 2  # bytes
FRAME_MS = 20

# Discord 사운드보드 제한
SOUND_MAX_SECONDS = 5.2
SOUND_MAX_BYTES = 512 * 1024


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"환경변수 {name} 는 정수여야 합니다: {raw!r}") from exc


def resolve_ffmpeg() -> str:
    """실행 가능한 ffmpeg 경로를 반환한다.

    FFMPEG_PATH 환경변수 → PATH 의 ffmpeg → imageio-ffmpeg 번들 바이너리 순서로 탐색한다.
    Dashcloud 같은 호스팅은 이미지에 ffmpeg 가 없을 수 있으므로 imageio-ffmpeg 를 대안으로 쓴다.
    """
    path = os.getenv("FFMPEG_PATH", "").strip()
    if path:
        return path
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


@dataclass(frozen=True)
class Config:
    token: str
    dev_guild_id: int | None
    clips_dir: Path
    db_path: Path
    ffmpeg_path: str
    buffer_seconds: int          # 사용자별 보관 시간(초)
    max_buffer_mb_per_user: int  # 사용자별 메모리 상한
    max_buffer_mb_total: int     # 길드 전체 메모리 상한
    max_clip_seconds: int
    default_clip_seconds: int
    max_file_mb: int             # 업로드 파일 크기 상한
    ffmpeg_timeout: int
    max_concurrent_clips: int
    proposals_dir: Path
    soundboard_approver_id: int | None  # 비우면 서버장
    max_pending_per_user: int

    @classmethod
    def load(cls, require_token: bool = True) -> "Config":
        load_dotenv(ROOT_DIR / ".env")
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if require_token and not token:
            raise RuntimeError(
                "DISCORD_TOKEN 환경변수가 없습니다. .env.example 을 .env 로 복사한 뒤 토큰을 입력하세요."
            )
        guild = os.getenv("DEV_GUILD_ID", "").strip()
        buffer_seconds = _int("BUFFER_SECONDS", 60)
        max_clip = min(_int("MAX_CLIP_SECONDS", 60), buffer_seconds)
        db_path = (ROOT_DIR / os.getenv("DB_PATH", "data/bot.sqlite3")).resolve()
        approver = os.getenv("SOUNDBOARD_APPROVER_ID", "").strip()
        return cls(
            token=token,
            dev_guild_id=int(guild) if guild else None,
            clips_dir=(ROOT_DIR / os.getenv("CLIPS_DIR", "clips")).resolve(),
            db_path=db_path,
            ffmpeg_path=resolve_ffmpeg(),
            buffer_seconds=buffer_seconds,
            max_buffer_mb_per_user=_int("MAX_BUFFER_MB_PER_USER", 16),
            max_buffer_mb_total=_int("MAX_BUFFER_MB_TOTAL", 128),
            max_clip_seconds=max_clip,
            default_clip_seconds=min(_int("DEFAULT_CLIP_SECONDS", 30), max_clip),
            max_file_mb=_int("MAX_FILE_MB", 8),
            ffmpeg_timeout=_int("FFMPEG_TIMEOUT", 60),
            max_concurrent_clips=_int("MAX_CONCURRENT_CLIPS", 2),
            proposals_dir=db_path.parent / "proposals",
            soundboard_approver_id=int(approver) if approver else None,
            max_pending_per_user=_int("MAX_PENDING_PER_USER", 3),
        )
