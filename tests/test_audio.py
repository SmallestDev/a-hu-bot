"""버퍼 / 구간 추출 / 믹싱 / 용량 제한 / WAV 출력 테스트."""
import asyncio
import wave

import numpy as np
import pytest

from src.audio.buffer import Frame, VoiceBuffer
from src.audio.exporter import ExportError, FileTooLargeError, export_clip, write_wav
from src.audio.mixer import CEILING, FADE, mix
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
    # 전체 구간이 연속 커버되므로 경계 2ms 페이드 구간을 뺀 중간은 원본 그대로
    edge = min(FADE, len(samples) // 2)
    assert (samples[edge:-edge, 0] == 100).all()


def test_window_empty_when_out_of_range():
    buf = VoiceBuffer()
    fill(buf, 1, 0.0, 10, 100)
    assert buf.window(50, 60) == {}


# 3. 타임스탬프 믹싱 -----------------------------------------------------------
def test_mix_places_frames_at_timestamps_with_gap_and_overlap():
    # 연속 세그먼트를 길게 잡아 페이드 구간(경계 2ms) 바깥의 steady 상태를 검증한다.
    a = [Frame(t, make_pcm(1000)) for t in (0.00, 0.02, 0.04, 0.06)]          # user1: 0~80ms
    b = [Frame(t, make_pcm(300)) for t in (0.10, 0.12, 0.14)]                 # user2: 100~160ms
    out = mix({1: a, 2: b}, 0.0, 0.18)
    assert len(out) == int(0.18 * SAMPLE_RATE)
    # user1 단독 구간(20~40ms, 페이드 바깥)
    assert (out[int(0.02 * SAMPLE_RATE):int(0.04 * SAMPLE_RATE), 0] == 1000).all()
    # 구간 사이 무음 보존(80~100ms)
    assert (out[int(0.08 * SAMPLE_RATE):int(0.10 * SAMPLE_RATE)] == 0).all()
    # user2 단독 구간 steady(120~140ms, 페이드 바깥)
    assert (out[int(0.12 * SAMPLE_RATE):int(0.14 * SAMPLE_RATE), 0] == 300).all()
    # 경계에서는 페이드로 0 에 수렴
    assert out[0, 0] == 0 and out[-1, 0] == 0


def test_mix_loud_overlap_is_scaled_not_wrapped():
    """두 큰 목소리가 겹쳐도 int16 래핑(음수 뒤집힘) 없이 전체 믹스가 천장 이하로 축소된다."""
    out = mix({1: [Frame(0, make_pcm(30000))], 2: [Frame(0, make_pcm(30000))]}, 0.0, 0.02)
    assert np.abs(out.astype(np.int32)).max() <= CEILING + 1
    assert out.min() >= 0  # 양수 입력만 있으므로 음수 래핑 발생 안 함
    # 중간 steady 값 = 30000+30000 을 헤드룸 비율(CEILING/60000)로 축소한 값 = CEILING
    mid = int(FRAME_SAMPLES // 2)
    assert abs(int(out[mid, 0]) - int(CEILING)) < 3


def test_mix_trims_frames_crossing_boundaries():
    # 프레임이 구간 시작 10ms 전에 걸침: 구간 안쪽(0~10ms)만 채워지고 경계 페이드 적용
    out = mix({1: [Frame(-0.01, make_pcm(700))]}, 0.0, 0.02)
    assert out.max() == 700
    assert out[int(0.005 * SAMPLE_RATE), 0] == 700  # 페이드 접점(5ms)에서 최대치
    assert (out[int(0.01 * SAMPLE_RATE):] == 0).all()  # 10ms 이후 무음


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
    assert data.max() == 1234            # 페이드 접점에서 원본 최대치 도달
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
