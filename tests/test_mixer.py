"""소리 깨짐 방지(리미터, 틈 보간, 덮어쓰기, 음량 균일화)와 사운드보드 소리 버퍼링 테스트."""
import numpy as np

from src.audio.buffer import Frame, VoiceBuffer, effect_key
from src.audio.mixer import CEILING, clipped_ratio, limit, mix
from src.audio.receiver import FrameCollector
from src.config import CHANNELS, SAMPLE_RATE
from src.database.db import ConsentStore
from tests.conftest import FRAME_SAMPLES, make_pcm


def run_frames(value, n, t0=0.0):
    return [Frame(t0 + i * 0.02, make_pcm(value)) for i in range(n)]


def sine_frames(amp, n, freq=220.0, t0=0.0):
    out = []
    for i in range(n):
        t = (np.arange(FRAME_SAMPLES) + i * FRAME_SAMPLES) / SAMPLE_RATE
        s = (np.sin(2 * np.pi * freq * t) * amp).astype(np.int16)
        out.append(Frame(t0 + i * 0.02, np.stack([s, s], 1).astype("<i2").tobytes()))
    return out


def rms(a):
    return float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))


def test_limiter_never_exceeds_ceiling():
    x = np.zeros((SAMPLE_RATE, CHANNELS), np.float32)
    x[10000:10500] = 60000  # 한계를 훌쩍 넘는 구간
    y = limit(x)
    assert np.abs(y).max() <= CEILING + 1


def test_limiter_leaves_quiet_audio_untouched():
    x = (np.random.default_rng(0).normal(0, 2000, (SAMPLE_RATE, CHANNELS))).astype(np.float32)
    assert np.array_equal(limit(x), x)


def test_loud_overlap_is_limited_not_hard_clipped():
    out = mix({1: sine_frames(30000, 50), 2: sine_frames(30000, 50, freq=330)}, 0.0, 1.0,
              normalize=True, target_rms=20000)
    assert np.abs(out.astype(np.int32)).max() <= CEILING + 2
    # 평평하게 잘린(연속 같은 값) 구간이 없어야 한다
    flat = np.count_nonzero(np.abs(np.diff(out[:, 0].astype(np.int32))) == 0)
    assert flat < 50


def test_gain_changes_are_smooth():
    out = mix({1: sine_frames(32000, 50), 2: sine_frames(32000, 50, freq=330)}, 0.0, 1.0,
              normalize=True, target_rms=22000)
    d = np.abs(np.diff(out[2000:-2000, 0].astype(np.int32)))
    assert d.max() < 9000  # 한 샘플 사이에 급격히 튀지 않는다


def test_short_gap_is_interpolated_without_dropout():
    a = [Frame(0.0, make_pcm(1000)), Frame(0.02 + 0.0005, make_pcm(1000))]  # 0.5ms 틈
    out = mix({1: a}, 0.0, 0.05, normalize=True, target_rms=1000)
    mid = out[300:int(0.04 * SAMPLE_RATE) - 300, 0]
    assert (mid == 1000).all()


def test_long_gap_edges_are_faded():
    a = [Frame(0.0, make_pcm(8000)), Frame(0.1, make_pcm(8000))]
    out = mix({1: a}, 0.0, 0.2, normalize=True, target_rms=8000)
    end_of_first = FRAME_SAMPLES
    assert abs(int(out[end_of_first - 1, 0])) < 400  # 끝이 0 으로 내려온다
    assert abs(int(out[0, 0])) < 400                  # 시작도 페이드 인


def test_same_user_overlap_overwrites_instead_of_doubling():
    dup = [Frame(0.0, make_pcm(1000)), Frame(0.0, make_pcm(1000)), Frame(0.01, make_pcm(1000))]
    out = mix({1: dup}, 0.0, 0.03, normalize=True, target_rms=1000)
    assert out[500:1000, 0].max() == 1000


def test_normalize_evens_out_loud_and_quiet_speakers():
    quiet = sine_frames(700, 100, freq=200)
    loud = sine_frames(14000, 100, freq=300)
    q = mix({1: quiet}, 0.0, 2.0, normalize=True)
    l = mix({2: loud}, 0.0, 2.0, normalize=True)
    ratio = rms(l[2000:-2000]) / rms(q[2000:-2000])
    assert 0.8 < ratio < 1.25
    # 정규화하지 않으면 격차가 그대로
    q0 = mix({1: quiet}, 0.0, 2.0)
    l0 = mix({2: loud}, 0.0, 2.0)
    assert rms(l0) / rms(q0) > 10


def test_normalize_ignores_silence_when_measuring():
    spoken = sine_frames(5000, 50, t0=0.0)             # 1초 발화
    padded = spoken + [Frame(1.0 + i * 0.02, make_pcm(0)) for i in range(150)]  # 뒤에 3초 무음
    a = mix({1: spoken}, 0.0, 1.0, normalize=True)
    b = mix({1: padded}, 0.0, 4.0, normalize=True)
    assert abs(rms(a[2000:40000]) - rms(b[2000:40000])) / rms(a[2000:40000]) < 0.05


def test_normalize_silent_source_does_not_blow_up():
    out = mix({1: run_frames(0, 20), 2: run_frames(3, 20)}, 0.0, 0.4, normalize=True)
    assert np.abs(out).max() < 100


def test_clipped_ratio_detects_overdriven_source():
    hot = run_frames(32767, 10)
    ok = sine_frames(5000, 10)
    assert clipped_ratio(hot) > 0.9
    assert clipped_ratio(ok) == 0.0


# ---- 사운드보드 재생음 ---------------------------------------------------------------
def test_effect_audio_is_gated_by_consent_and_included_for_picked_user():
    store = ConsentStore()
    buf = VoiceBuffer()
    col = FrameCollector(1, buf, store.is_consented)
    pcm = make_pcm(2000, ms=100)
    assert col.add_effect(7, pcm, 1.0) is False        # 동의 전에는 저장 안 함
    assert buf.user_ids() == []
    store.consent(1, 7)
    assert col.add_effect(7, pcm, 1.0) is True
    assert buf.user_ids() == [effect_key(7)]
    assert effect_key(7) in buf.window(0, 10)
    assert effect_key(7) in buf.window(0, 10, [7])      # 특정 사용자를 골라도 그 사람의 재생음은 포함
    assert effect_key(7) not in buf.window(0, 10, [8])  # 다른 사람 선택 시에는 제외


def test_effect_is_placed_at_event_time_in_mix():
    store = ConsentStore()
    buf = VoiceBuffer()
    col = FrameCollector(1, buf, store.is_consented)
    store.consent(1, 7)
    col.add_effect(7, make_pcm(4000, ms=200), 0.5)
    out = mix(buf.window(0.0, 1.0), 0.0, 1.0)
    assert (out[: int(0.5 * SAMPLE_RATE) - 10] == 0).all()
    assert np.abs(out[int(0.55 * SAMPLE_RATE): int(0.65 * SAMPLE_RATE)]).min() > 3000


def test_purge_user_also_removes_effect_audio():
    store = ConsentStore()
    buf = VoiceBuffer()
    col = FrameCollector(1, buf, store.is_consented)
    store.consent(1, 7)
    col.add_effect(7, make_pcm(4000, ms=100), 0.0)
    col.on_pcm(7, make_pcm(), 0, now=1.0)
    col.forget_user(7)
    assert buf.user_ids() == []


def test_multiple_picked_users_only():
    buf = VoiceBuffer()
    for uid in (1, 2, 3):
        for f in run_frames(500 * uid, 5):
            buf.add(uid, f)
    got = buf.window(0, 10, [1, 3])
    assert sorted(got) == [1, 3]
