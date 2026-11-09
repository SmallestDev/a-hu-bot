"""음성 수신 계층.

- AudioReceiver: 교체 가능한 수신 구현 인터페이스.
- FrameCollector: 수신 구현과 무관한 공통 로직 (동의 게이트 → 타임스탬프 정렬 → RAM 버퍼).
- VoiceRecvReceiver: discord-ext-voice-recv 기반 구현.

중요: discord.py 본체는 음성 *수신* 을 공식 지원하지 않는다. voice-recv 는 비공식 확장이며,
Discord 의 DAVE(E2EE) 가 강제된 이후에는 DAVE 복호화 패치가 포함된 빌드가 있어야 한다.
이 모듈은 수신이 실제로 되는 척하지 않는다: probe() 로 능력을 점검하고, 통계로 실제 수신 여부를 보여 준다.
"""
from __future__ import annotations

import importlib.metadata as md
import inspect
import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from src.audio.buffer import Frame, VoiceBuffer, effect_key
from src.audio.decoder import duration_of, frame_bytes, validate_frame
from src.config import SAMPLE_RATE

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- 공통 수집 로직
@dataclass
class CollectorStats:
    accepted: int = 0
    rejected_no_consent: int = 0
    rejected_unknown_user: int = 0
    rejected_bad_frame: int = 0
    effects: int = 0
    errors: int = 0
    last_frame_at: float | None = None
    per_user: dict[int, int] = field(default_factory=dict)


class _Aligner:
    """RTP 타임스탬프(48kHz tick)로 도착 지터를 제거해 프레임 시작 시각을 계산한다."""

    DRIFT_LIMIT = 0.5  # 초. 이보다 어긋나면 새로 앵커링 (재연결, 긴 무음 등)

    def __init__(self) -> None:
        self.wall0 = 0.0
        self.rtp0 = 0
        self.last_rtp = 0
        self.wraps = 0
        self.ready = False

    def stamp(self, now: float, rtp: int | None, dur: float) -> float:
        arrival_start = now - dur
        if rtp is None:
            return arrival_start
        if self.ready and rtp < self.last_rtp and self.last_rtp - rtp > 2**31:
            self.wraps += 1
        extended = rtp + self.wraps * 2**32
        self.last_rtp = rtp
        if not self.ready:
            self.wall0, self.rtp0, self.ready = arrival_start, extended, True
            return arrival_start
        ts = self.wall0 + (extended - self.rtp0) / SAMPLE_RATE
        if abs(ts - arrival_start) > self.DRIFT_LIMIT:
            self.wall0, self.rtp0 = arrival_start, extended
            return arrival_start
        return ts


class FrameCollector:
    """수신 스레드에서 호출된다. 동의하지 않은 사용자의 PCM 은 버퍼에 닿기 전에 폐기한다."""

    def __init__(self, guild_id: int, buffer: VoiceBuffer,
                 is_consented: Callable[[int, int], bool],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.guild_id = guild_id
        self.buffer = buffer
        self._is_consented = is_consented
        self._clock = clock
        self._aligners: dict[int, _Aligner] = {}
        self.stats = CollectorStats()
        self._lock = threading.Lock()

    def on_pcm(self, user_id: int | None, pcm: bytes, rtp_timestamp: int | None = None,
               now: float | None = None) -> bool:
        """프레임 처리. 버퍼에 저장되면 True."""
        try:
            if user_id is None:
                self.stats.rejected_unknown_user += 1
                return False
            if not self._is_consented(self.guild_id, user_id):
                self.stats.rejected_no_consent += 1  # 내용은 읽지도 저장하지도 않는다
                return False
            if not validate_frame(pcm):
                self.stats.rejected_bad_frame += 1
                return False
            now = self._clock() if now is None else now
            dur = duration_of(pcm)
            with self._lock:
                al = self._aligners.setdefault(user_id, _Aligner())
                start = al.stamp(now, rtp_timestamp, dur)
            self.buffer.add(user_id, Frame(start=start, pcm=pcm))
            self.stats.accepted += 1
            self.stats.last_frame_at = now
            self.stats.per_user[user_id] = self.stats.per_user.get(user_id, 0) + 1
            return True
        except Exception:  # 수신 스레드가 죽지 않도록
            self.stats.errors += 1
            log.exception("프레임 처리 중 오류")
            return False

    def add_effect(self, user_id: int, pcm: bytes, start: float) -> bool:
        """사운드보드 재생음(디코딩된 48kHz 스테레오 PCM)을 start 시각부터 버퍼에 넣는다.
        음성과 같은 동의 규칙을 적용한다: 동의하지 않은 사용자의 재생음은 저장하지 않는다."""
        try:
            if not self._is_consented(self.guild_id, user_id):
                self.stats.rejected_no_consent += 1
                return False
            fb = frame_bytes(20)
            key = effect_key(user_id)
            for i in range(0, len(pcm) - fb + 1, fb):
                self.buffer.add(key, Frame(start=start + (i // fb) * 0.02, pcm=pcm[i:i + fb]))
            self.stats.effects += 1
            return True
        except Exception:
            self.stats.errors += 1
            log.exception("사운드보드 소리 저장 중 오류")
            return False

    def forget_user(self, user_id: int) -> None:
        """철회 시: 버퍼와 정렬 상태 즉시 삭제."""
        with self._lock:
            self._aligners.pop(user_id, None)
        self.buffer.purge_user(user_id)
        self.stats.per_user.pop(user_id, None)

    def clear(self) -> None:
        with self._lock:
            self._aligners.clear()
        self.buffer.clear()


# ---------------------------------------------------------------- 수신 구현 인터페이스
@dataclass
class ReceiverCapability:
    available: bool
    dave_ready: bool | None      # None = 확인 불가
    summary: str
    details: list[str] = field(default_factory=list)


class AudioReceiver(ABC):
    """수신 구현 교체 지점. 새 라이브러리를 쓰려면 이 클래스를 구현하고 bot.py 에서 주입한다."""

    @property
    @abstractmethod
    def voice_client_cls(self) -> type | None:
        """channel.connect(cls=...) 에 넘길 VoiceClient 클래스. None 이면 기본 클래스."""

    @abstractmethod
    def probe(self) -> ReceiverCapability: ...

    @abstractmethod
    def start(self, voice_client: Any, collector: FrameCollector,
              resolve_user_id: Callable[[Any], int | None] | None = None) -> None: ...

    @abstractmethod
    def stop(self, voice_client: Any) -> None: ...


def _ver(pkg: str) -> str | None:
    try:
        return md.version(pkg)
    except md.PackageNotFoundError:
        return None


class VoiceRecvReceiver(AudioReceiver):
    """discord-ext-voice-recv 기반 구현 (비공식)."""

    def __init__(self) -> None:
        try:
            from discord.ext import voice_recv  # type: ignore
        except Exception:
            voice_recv = None
        self._vr = voice_recv

    @property
    def voice_client_cls(self) -> type | None:
        return self._vr.VoiceRecvClient if self._vr else None

    def probe(self) -> ReceiverCapability:
        d = [f"discord.py={_ver('discord.py')}", f"discord-ext-voice-recv={_ver('discord-ext-voice-recv')}",
             f"davey={_ver('davey')}", f"PyNaCl={_ver('PyNaCl')}"]
        if self._vr is None:
            return ReceiverCapability(False, None, "discord-ext-voice-recv 미설치 — 음성 수신 불가", d)
        # 휴리스틱: 설치된 voice-recv 소스가 DAVE 복호화를 호출하는지 검사한다. 확정 증거는 아니다.
        dave = None
        try:
            from discord.ext.voice_recv import opus as vr_opus  # type: ignore
            dave = "dave" in inspect.getsource(vr_opus).lower()
        except Exception:
            dave = None
        if dave is False:
            return ReceiverCapability(
                True, False,
                "voice-recv 가 DAVE 복호화를 포함하지 않는 것으로 보임 — DAVE 강제 환경에서는 수신이 실패할 수 있음", d)
        if dave is None:
            return ReceiverCapability(True, None, "voice-recv 설치됨, DAVE 복호화 여부 확인 불가", d)
        return ReceiverCapability(True, True, "voice-recv 에 DAVE 관련 코드가 있음 (실서버 수신은 /status 통계로 확인)", d)

    def start(self, voice_client: Any, collector: FrameCollector,
              resolve_user_id: Callable[[Any], int | None] | None = None) -> None:
        if self._vr is None:
            raise RuntimeError("discord-ext-voice-recv 가 설치되어 있지 않습니다")
        vr = self._vr
        resolve = resolve_user_id or (lambda u: getattr(u, "id", None))

        class _Sink(vr.AudioSink):  # type: ignore[misc, name-defined]
            def wants_opus(self) -> bool:
                return False  # 디코딩된 PCM 을 받는다

            def write(self, user, data) -> None:
                try:
                    pkt = getattr(data, "packet", None)
                    rtp = getattr(pkt, "timestamp", None)
                    collector.on_pcm(resolve(user), data.pcm, rtp)
                except Exception:
                    collector.stats.errors += 1

            def cleanup(self) -> None:
                pass

        voice_client.listen(_Sink())

    def stop(self, voice_client: Any) -> None:
        try:
            if hasattr(voice_client, "stop_listening"):
                voice_client.stop_listening()
        except Exception:
            log.exception("stop_listening 실패")
