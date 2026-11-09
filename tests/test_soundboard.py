"""사운드보드 건의: 파일 변환 제한, 건의 상태 관리, 소리 캐시."""
import asyncio
import io
import shutil
import wave

import numpy as np
import pytest

from src.audio import soundboard as sb
from src.config import SAMPLE_RATE, SOUND_MAX_BYTES
from src.database.db import ConsentStore

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg 없음")


def wav_bytes(seconds, amp=8000, ch=2):
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    s = (np.sin(2 * np.pi * 440 * t) * amp).astype("<i2")
    data = np.stack([s] * ch, 1) if ch > 1 else s
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(data.tobytes())
    return buf.getvalue()


@needs_ffmpeg
def test_prepare_sound_makes_small_mono_mp3():
    mp3, dur = asyncio.run(sb.prepare_sound(wav_bytes(3.0)))
    assert 2.9 < dur < 3.1
    assert 0 < len(mp3) <= SOUND_MAX_BYTES
    assert mp3[:3] == b"ID3" or mp3[0] == 0xFF


@needs_ffmpeg
def test_prepare_sound_rejects_over_limit():
    with pytest.raises(sb.SoundError):
        asyncio.run(sb.prepare_sound(wav_bytes(8.0)))


@needs_ffmpeg
def test_prepare_sound_rejects_garbage():
    with pytest.raises(sb.SoundError):
        asyncio.run(sb.prepare_sound(b"this is not audio"))


def test_prepare_sound_missing_ffmpeg():
    with pytest.raises(sb.SoundError):
        asyncio.run(sb.prepare_sound(wav_bytes(1.0), ffmpeg="no-such-ffmpeg-xyz"))


@needs_ffmpeg
def test_decode_pcm_is_48k_stereo():
    pcm = asyncio.run(sb.decode_pcm(wav_bytes(1.0, ch=1)))
    assert len(pcm) == SAMPLE_RATE * 2 * 2


def test_scale_pcm():
    pcm = np.array([1000, -1000], dtype="<i2").tobytes()
    assert np.frombuffer(sb.scale_pcm(pcm, 0.5), dtype="<i2").tolist() == [500, -500]
    assert sb.scale_pcm(pcm, 1.0) == pcm


def test_pcm_cache_lru():
    c = sb.PCMCache(size=2)
    c.put(1, b"a")
    c.put(2, b"b")
    c.get(1)
    c.put(3, b"c")
    assert c.get(2) is None and c.get(1) == b"a" and c.get(3) == b"c"


# ---- 건의 상태 --------------------------------------------------------------------------
def test_proposal_lifecycle_and_double_click_guard():
    s = ConsentStore()
    pid = s.add_proposal(1, 10, "웃음", 2.5)
    assert s.get_proposal(pid)["status"] == "pending"
    assert s.set_proposal_status(pid, "processing") is True
    assert s.set_proposal_status(pid, "processing") is False   # 두 번 눌러도 한 번만 처리
    assert s.set_proposal_status(pid, "pending", expect="processing") is True  # 실패 시 되돌리기
    assert s.set_proposal_status(pid, "rejected") is True
    assert s.set_proposal_status(pid, "accepted") is False     # 이미 끝난 건은 못 바꿈


def test_pending_count_limits_per_user():
    s = ConsentStore()
    a = s.add_proposal(1, 10, None, 1.0)
    s.add_proposal(1, 10, None, 1.0)
    s.add_proposal(1, 11, None, 1.0)
    assert s.count_pending(1, 10) == 2 and s.count_pending(1) == 3
    s.set_proposal_status(a, "rejected")
    assert s.count_pending(1, 10) == 1


def test_unknown_proposal():
    assert ConsentStore().get_proposal(999) is None
