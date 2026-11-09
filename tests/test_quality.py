"""음량 균일화 / 소리 깨짐 방지 / 사운드보드 재생음 합성 테스트."""
import numpy as np

from src.audio.buffer import Frame, VoiceBuffer, effect_key
from src.audio.mixer import clipping_ratio, mix
from src.audio.receiver import FrameCollector
from src.database.db import ConsentStore
from src.config import SAMPLE_RATE
from tests.conftest import FRAME_SAMPLES, make_pcm


def track(value, t0, n):
    return [Frame(t0 + i * 0.02, make_pcm(value)) for i in range(n)]


def rms(a):
    return float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))


# ---- 사람마다 음량 비슷하게 ----------------------------------------------------------
def test_normalize_equalizes_loud_and_quiet_speakers():
    frames = {1: track(8000, 0.0, 50), 2: track(800, 1.0, 50)}   # 10배 차이, 안 겹침
    out = mix(frames, 0.0, 2.0, normalize=True)
    loud = rms(out[int(0.2 * SAMPLE_RATE):int(0.8 * SAMPLE_RATE)])
    quiet = rms(out[int(1.2 * SAMPLE_RATE):int(1.8 * SAMPLE_RATE)])
    assert abs(loud - quiet) / loud < 0.05
    raw = mix(frames, 0.0, 2.0)
    assert rms(raw[:SAMPLE_RATE]) > 5 * rms(raw[SAMPLE_RATE:])   # 정규화 전엔 큰 차이


def test_gain_is_capped_for_very_quiet_source():
    out = mix({1: track(400, 0.0, 50)}, 0.0, 1.0, normalize=True)   # 3000/400=7.5 → 6배로 제한
    assert 2300 < rms(out[1000:-1000]) < 2500


def test_background_noise_does_not_inflate_gain():
    # 말(2000) 사이의 잡음(60)은 음량 계산에서 제외돼야 한다
    f = track(2000, 0.0, 10) + track(60, 0.2, 40)
    out = mix({1: f}, 0.0, 1.0, normalize=True)
    assert rms(out[: int(0.18 * SAMPLE_RATE)][1000:]) < 4000     # 말 구간이 과증폭되지 않음


def test_silent_source_is_left_alone():
    out = mix({1: track(0, 0.0, 10)}, 0.0, 0.2, normalize=True)
    assert (out == 0).all()


# ---- 깨짐 방지 ------------------------------------------------------------------------
def test_same_user_overlapping_frames_are_not_doubled():
    f = [Frame(0.00, make_pcm(1000)), Frame(0.01, make_pcm(1000))]   # 10ms 겹침
    out = mix({1: f}, 0.0, 0.03, normalize=True, target_rms=1000)
    assert out.max() <= 1000 + 1
    # 정규화 안 하는 기본 모드는 기존처럼 합산
    assert mix({1: f}, 0.0, 0.03).max() == 2000


def test_soft_limiter_never_exceeds_full_scale_and_avoids_flat_clipping():
    frames = {1: track(30000, 0.0, 25), 2: track(30000, 0.0, 25)}     # 합 60000
    out = mix(frames, 0.0, 0.5, normalize=True, target_rms=30000)
    mid = out[2000:-2000]
    assert mid.max() <= 32767 and mid.min() >= -32768
    assert 28000 < mid.max() <= 29200      # 천장 -1dBFS (MP3 변환 여유)
    assert clipping_ratio(out) == 0.0 or clipping_ratio(out) < 0.001


def test_hard_clip_mode_saturates_as_before():
    out = mix({1: track(30000, 0.0, 5), 2: track(30000, 0.0, 5)}, 0.0, 0.1)
    assert clipping_ratio(out) > 0.9


def test_declick_fades_at_gaps():
    f = [Frame(0.00, make_pcm(1000)), Frame(0.10, make_pcm(1000))]    # 사이에 틈
    out = mix({1: f}, 0.0, 0.14, normalize=True, target_rms=1000)
    first_end = FRAME_SAMPLES
    second_start = int(0.10 * SAMPLE_RATE)
    assert abs(int(out[first_end - 1, 0])) < 100            # 끝이 0 으로 내려감
    assert abs(int(out[second_start, 0])) < 100             # 시작이 0 에서 올라옴
    assert out[first_end // 2, 0] == 1000                   # 가운데는 그대로


def test_clip_edges_fade():
    out = mix({1: track(1000, 0.0, 10)}, 0.0, 0.2, normalize=True, target_rms=1000)
    assert out[0, 0] == 0 and out[-1, 0] == 0


# ---- 사운드보드 재생음 ----------------------------------------------------------------
def make_collector():
    store = ConsentStore()
    buf = VoiceBuffer()
    return store, buf, FrameCollector(7, buf, store.is_consented)


def effect_pcm(seconds=0.2, value=5000):
    return make_pcm(value, int(seconds * 1000))


def test_effect_requires_consent():
    store, buf, col = make_collector()
    assert col.add_effect(1, effect_pcm(), 1.0) is False
    assert buf.user_ids() == []
    store.consent(7, 1)
    assert col.add_effect(1, effect_pcm(), 1.0) is True
    assert effect_key(1) in buf.user_ids()
    assert col.stats.effects == 1


def test_effect_is_placed_at_event_time_and_mixed():
    store, buf, col = make_collector()
    store.consent(7, 1)
    store.consent(7, 2)
    col.on_pcm(2, make_pcm(1000), 0, now=1.02)                  # 2번 음성: 1.00~1.02
    col.add_effect(1, effect_pcm(0.2, 5000), 1.50)              # 1번 사운드보드: 1.50~1.70
    out = mix(buf.window(1.0, 2.0), 1.0, 2.0)
    assert out[100, 0] == 1000
    assert (out[int(0.5 * SAMPLE_RATE) + 10:int(0.7 * SAMPLE_RATE) - 10] == 5000).all()
    assert out[int(0.8 * SAMPLE_RATE), 0] == 0


def test_user_filter_includes_that_users_effects_only():
    store, buf, col = make_collector()
    for u in (1, 2):
        store.consent(7, u)
        col.add_effect(u, effect_pcm(), 1.0)
        col.on_pcm(u, make_pcm(), 0, now=1.02)
    got = buf.window(0, 5, [1])
    assert set(got) == {1, effect_key(1)}
    got = buf.window(0, 5, [1, 2])
    assert set(got) == {1, 2, effect_key(1), effect_key(2)}


def test_forget_user_purges_effects_too():
    store, buf, col = make_collector()
    store.consent(7, 1)
    col.add_effect(1, effect_pcm(), 1.0)
    col.forget_user(1)
    assert buf.window(0, 5) == {}


def test_overlapping_effects_from_same_user_are_both_kept():
    # 사운드보드 소리 두 개가 동시에 울리면 둘 다 들려야 한다 (음성과 달리 겹침 허용)
    f = [Frame(0.0, make_pcm(1000)), Frame(0.0, make_pcm(1000))]
    out = mix({effect_key(1): f}, 0.0, 0.02, normalize=True, target_rms=1000)
    assert out[FRAME_SAMPLES // 2, 0] > 1500
