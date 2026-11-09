"""이미 만든 클립을 구간으로 다시 자르는 기능 테스트."""
import asyncio
import shutil
import wave

import numpy as np
import pytest

from src.audio.exporter import ExportError, trim_clip, write_wav
from src.config import CHANNELS, SAMPLE_RATE


def ramp_wav(tmp_path, seconds=10):
    # 샘플값이 시간(초*1000)과 같도록 만든 클립: 구간 정확도를 검증하기 쉽다
    t = np.arange(seconds * SAMPLE_RATE) // SAMPLE_RATE * 1000
    samples = np.stack([t, t], axis=1).astype(np.int16)
    return write_wav(tmp_path / "src.wav", samples)


def run(src, a, b, tmp_path, **kw):
    return asyncio.run(trim_clip(src, a, b, tmp_path / "out", 1, max_bytes=10**8, **kw))


def test_trim_wav_exact_range(tmp_path):
    src = ramp_wav(tmp_path)
    out = run(src, 3, 5, tmp_path)
    with wave.open(str(out)) as w:
        assert w.getnframes() == 2 * SAMPLE_RATE
        assert w.getnchannels() == CHANNELS
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    assert data[0] == 3000 and data[-1] == 4000


def test_trim_end_clamped_to_clip_length(tmp_path):
    src = ramp_wav(tmp_path, 4)
    out = run(src, 3, 99, tmp_path)
    with wave.open(str(out)) as w:
        assert w.getnframes() == SAMPLE_RATE


def test_trim_rejects_bad_ranges(tmp_path):
    src = ramp_wav(tmp_path, 4)
    for a, b in [(2, 2), (3, 1), (-1, 2), (10, 12)]:
        with pytest.raises(ExportError):
            run(src, a, b, tmp_path)


def test_trim_missing_source(tmp_path):
    with pytest.raises(ExportError):
        run(tmp_path / "nope.wav", 0, 1, tmp_path)


def test_trim_mp3_with_ffmpeg(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg 없음")
    from src.audio.exporter import wav_to_mp3
    src = ramp_wav(tmp_path, 6)
    mp3 = asyncio.run(wav_to_mp3(src))
    out = run(mp3, 1, 3, tmp_path)
    assert out.suffix == ".mp3" and out.stat().st_size > 1000
