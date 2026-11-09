"""버퍼 / 구간 추출 / 믹싱 / 용량 제한 / WAV 출력 테스트."""
import asyncio
import wave

import numpy as np
import pytest

from src.audio.buffer import Frame, VoiceBuffer
from src.audio.exporter import ExportError, FileTooLargeError, export_clip, write_wav
from src.audio.mixer import mix
from src.config import CHANNELS, SAMPLE_RATE
from tests.conftest import FRAME_SAMPLES, make_pcm


def fill(buf, uid, t0, n, value):
    for i in range(n):
        buf.add(uid, Frame(start=t0 + i * 0.02, pcm=make_pcm(value)))


# 1. 버퍼 저장 ---------------------------------------------------------------
def test_buffer_stores_per_user():
    buf = VoiceBuffer()
    fill(buf, 1, 0.0, 10, 100)
    fill(buf, 2, 0.0, 5, 200)
    assert sorted(buf.user_ids()) == [1, 2]
    assert len(buf.window(0, 10, 1)[1]) == 10
    assert len(buf.window(0, 10, 2)[2]) == 5
    assert buf.total_bytes == 15 * len(make_pcm())


# 2. 구간 추출 ---------------------------------------------------------------
def test_window_extracts_only_requested_range():
    buf = VoiceBuffer()
    fill(buf, 1, 0.0, 500, 100)  # 0~10초
    frames = buf.window(2.0, 3.0, 1)[1]
    assert all(f.end > 2.0 and f.start < 3.0 for f in frames)
    assert 49 <= len(frames) <= 51
    samples = mix({1: frames}, 2.0, 3.0)
    assert len(samples) == SAMPLE_RATE
    assert (samples == 100).all()


def test_window_empty_when_out_of_range():
    buf = VoiceBuffer()
    fill(buf, 1, 0.0, 10, 100)
    assert buf.window(50, 60) == {}


# 3. 타임스탬프 믹싱 -----------------------------------------------------------
def test_mix_places_frames_at_timestamps_with_gap_and_overlap():
    a = [Frame(0.00, make_pcm(1000)), Frame(0.02, make_pcm(1000))]  # 0~40ms
    b = [Frame(0.02, make_pcm(500)), Frame(0.10, make_pcm(300))]    # 20~40ms 겹침, 100~120ms
    out = mix({1: a, 2: b}, 0.0, 0.12)
    assert len(out) == int(0.12 * SAMPLE_RATE)
    assert (out[0:FRAME_SAMPLES] == 1000).all()                              # A 단독
    assert (out[FRAME_SAMPLES:2 * FRAME_SAMPLES] == 1500).all()              # 겹침 합산
    assert (out[2 * FRAME_SAMPLES:int(0.10 * SAMPLE_RATE)] == 0).all()       # 무음 구간 보존
    assert (out[int(0.10 * SAMPLE_RATE):] == 300).all()                       # 순서 보존


def test_mix_clips_instead_of_wrapping():
    out = mix({1: [Frame(0, make_pcm(30000))], 2: [Frame(0, make_pcm(30000))]}, 0.0, 0.02)
    assert (out == 32767).all()


def test_mix_trims_frames_crossing_boundaries():
    out = mix({1: [Frame(-0.01, make_pcm(700))]}, 0.0, 0.02)  # 앞 10ms 가 구간 밖
    assert (out[:FRAME_SAMPLES // 2] == 700).all()
    assert (out[FRAME_SAMPLES // 2:] == 0).all()


def test_mix_rejects_bad_range():
    with pytest.raises(ValueError):
        mix({}, 1.0, 1.0)


# 4. 용량 제한 ---------------------------------------------------------------
def test_time_limit_evicts_old_frames():
    buf = VoiceBuffer(max_seconds=1.0)
    fill(buf, 1, 0.0, 200, 1)  # 4초 분량
    frames = buf.window(0, 100, 1)[1]
    assert frames[0].start >= 3.0 - 0.02          # 마지막 1초만 유지
    assert len(frames) <= 51


def test_per_user_byte_limit():
    fb = len(make_pcm())
    buf = VoiceBuffer(max_seconds=1000, max_bytes_per_user=10 * fb)
    fill(buf, 1, 0.0, 100, 1)
    assert buf.total_bytes <= 10 * fb
    assert buf.window(0, 1000, 1)[1][-1].start == pytest.approx(99 * 0.02)  # 최신 프레임 유지


def test_total_byte_limit_across_users():
    fb = len(make_pcm())
    buf = VoiceBuffer(max_seconds=1000, max_bytes_per_user=1000 * fb, max_bytes_total=20 * fb)
    for i in range(30):
        buf.add(1, Frame(i * 0.04, make_pcm()))
        buf.add(2, Frame(i * 0.04 + 0.02, make_pcm()))
    assert buf.total_bytes <= 20 * fb


# 6. WAV 생성 ----------------------------------------------------------------
def test_write_wav_valid(tmp_path):
    samples = mix({1: [Frame(0, make_pcm(1234))]}, 0.0, 0.5)
    path = write_wav(tmp_path / "t.wav", samples)
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (CHANNELS, 2, SAMPLE_RATE)
        assert w.getnframes() == len(samples)
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    assert (data[: FRAME_SAMPLES * CHANNELS] == 1234).all()
    assert not list(tmp_path.glob("*.part"))


def test_export_clip_wav_within_limit(tmp_path):
    samples = np.zeros((SAMPLE_RATE, CHANNELS), dtype=np.int16)
    path = asyncio.run(export_clip(samples, tmp_path, 1, fmt="wav", max_bytes=10**7))
    assert path.suffix == ".wav" and path.exists()


def test_export_mp3_with_ffmpeg(tmp_path):
    shutil = pytest.importorskip("shutil")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg 없음")
    t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
    tone = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
    samples = np.stack([tone, tone], axis=1)
    path = asyncio.run(export_clip(samples, tmp_path, 1, fmt="mp3", max_bytes=10**7))
    assert path.suffix == ".mp3" and path.stat().st_size > 1000
    assert not list(tmp_path.glob("*.wav"))  # 임시 WAV 삭제


def test_oversize_wav_falls_back_to_mp3_or_errors(tmp_path):
    shutil = pytest.importorskip("shutil")
    samples = np.zeros((SAMPLE_RATE * 2, CHANNELS), dtype=np.int16)  # WAV 약 384KB
    if shutil.which("ffmpeg"):
        path = asyncio.run(export_clip(samples, tmp_path, 1, fmt="wav", max_bytes=100_000))
        assert path.suffix == ".mp3"
    with pytest.raises((FileTooLargeError, ExportError)):
        asyncio.run(export_clip(samples, tmp_path, 1, fmt="wav", max_bytes=10))


def test_missing_ffmpeg_is_reported(tmp_path):
    samples = np.zeros((SAMPLE_RATE, CHANNELS), dtype=np.int16)
    with pytest.raises(ExportError):
        asyncio.run(export_clip(samples, tmp_path, 1, fmt="mp3", max_bytes=10**7,
                                ffmpeg="definitely-not-ffmpeg-xyz"))
