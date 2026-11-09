"""사용자별 RAM 링버퍼. 디스크에는 아무것도 쓰지 않는다."""
from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from src.audio.decoder import duration_of


def effect_key(user_id: int) -> int:
    """사운드보드 재생음은 사용자 음성과 분리해 -user_id 키에 저장한다."""
    return -user_id


@dataclass(frozen=True, slots=True)
class Frame:
    start: float   # 프레임 시작 시각 (초, 단조 증가 시계 기준)
    pcm: bytes     # s16le / 48kHz / stereo

    @property
    def end(self) -> float:
        return self.start + duration_of(self.pcm)


class UserBuffer:
    """한 사용자의 프레임 큐. 시간/용량 두 기준으로 오래된 것부터 버린다."""

    def __init__(self, max_seconds: float, max_bytes: int) -> None:
        self.max_seconds = max_seconds
        self.max_bytes = max_bytes
        self._frames: deque[Frame] = deque()
        self.bytes = 0
        self.dropped_frames = 0

    def add(self, frame: Frame) -> None:
        self._frames.append(frame)
        self.bytes += len(frame.pcm)
        self._evict(frame.end)

    def _evict(self, now: float) -> None:
        cutoff = now - self.max_seconds
        while self._frames and (
            self._frames[0].end <= cutoff or self.bytes > self.max_bytes
        ):
            old = self._frames.popleft()
            self.bytes -= len(old.pcm)
            self.dropped_frames += 1

    def window(self, start: float, end: float) -> list[Frame]:
        return [f for f in self._frames if f.end > start and f.start < end]

    def __len__(self) -> int:
        return len(self._frames)

    def clear(self) -> None:
        self._frames.clear()
        self.bytes = 0


class VoiceBuffer:
    """길드 단위 버퍼 컨테이너 (스레드 안전). 수신 스레드와 이벤트 루프가 함께 접근한다."""

    def __init__(self, max_seconds: float = 60, max_bytes_per_user: int = 16 * 2**20,
                 max_bytes_total: int = 128 * 2**20) -> None:
        self.max_seconds = max_seconds
        self.max_bytes_per_user = max_bytes_per_user
        self.max_bytes_total = max_bytes_total
        self._users: dict[int, UserBuffer] = {}
        self._lock = threading.Lock()

    def add(self, user_id: int, frame: Frame) -> None:
        with self._lock:
            ub = self._users.get(user_id)
            if ub is None:
                ub = self._users[user_id] = UserBuffer(self.max_seconds, self.max_bytes_per_user)
            ub.add(frame)
            self._enforce_total()

    def _enforce_total(self) -> None:
        total = sum(u.bytes for u in self._users.values())
        while total > self.max_bytes_total:
            # 가장 오래된 프레임을 가진 사용자부터 한 프레임씩 제거
            candidates = [u for u in self._users.values() if u._frames]
            if not candidates:
                break
            victim = min(candidates, key=lambda u: u._frames[0].start)
            old = victim._frames.popleft()
            victim.bytes -= len(old.pcm)
            victim.dropped_frames += 1
            total -= len(old.pcm)

    def window(self, start: float, end: float,
               user_id: int | Iterable[int] | None = None) -> dict[int, list[Frame]]:
        """user_id: None=전체, 정수 하나, 또는 여러 명. 지정한 사용자의 사운드보드 재생음도 함께 나온다."""
        with self._lock:
            if user_id is None:
                ids = list(self._users)
            else:
                base = [user_id] if isinstance(user_id, int) else list(user_id)
                ids = base + [effect_key(u) for u in base]
            out: dict[int, list[Frame]] = {}
            for uid in ids:
                ub = self._users.get(uid)
                if ub:
                    frames = ub.window(start, end)
                    if frames:
                        out[uid] = frames
            return out

    def purge_user(self, user_id: int) -> None:
        with self._lock:
            for key in (user_id, effect_key(user_id)):
                ub = self._users.pop(key, None)
                if ub:
                    ub.clear()

    def clear(self) -> None:
        with self._lock:
            for ub in self._users.values():
                ub.clear()
            self._users.clear()

    def user_ids(self) -> list[int]:
        with self._lock:
            return [u for u, ub in self._users.items() if len(ub)]

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return sum(u.bytes for u in self._users.values())
