"""안전 믹싱 테스트: 사람별 볼륨 보정 없음 + 겹침 시 헤드룸 게인으로 클리핑 방지."""
import numpy as np

from src.audio.buffer import Frame, VoiceBuffer, effect_key
from src.audio.mixer import CEILING, clipping_ratio, mix
from src.audio.receiver import FrameCollector
from src.database.db import ConsentStore
from src.config import SAMPLE_RATE
from tests.conftest import FRAME_SAMPLES, make_pcm


def track(value, t0, n):
    return [Frame(t0 + i * 0.02, make_pcm(value)) for i in range(n)]


def rms(a):
    return float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))


# ---- 사람별 음량 보정 없음 ----------------------------------------------------
def test_no_per_person_equalization():
    """사람별 RMS 를 맞추지 않음: 큰 목소리와 작은 목소리의 상대 관계가 그대로 유지된다."""
    quiet = track(700, 0.0, 100)      # 2초, 작게
    loud = track(14000, 0.0, 100)     # 2초, 크게 (20배)
    q = mix({1: quiet}, 0.0, 2.0)
    l = mix({2: loud}, 0.0, 2.0)
    ratio = rms(l[2000:-2000]) / rms(q[2000:-2000])
    assert ratio > 10                 # 정규화 안 했으므로 20배 격차가 그대로


def test_quiet_source_not_boosted():
    """작은 음원을 억지로 키우지 않는다(최대치가 원본 값보다 커지지 않음)."""
    out = mix({1: track(1000, 0.0, 10)}, 0.0, 0.2)
    assert out.max() <= 1000 + 1


def test_silent_source_is_silent():
    out = mix({1: track(0, 0.0, 10)}, 0.0, 0.2)
    assert (out == 0).all()


def test_same_user_overlapping_frames_are_not_doubled():
    f = [Frame(0.00, make_pcm(1000)), Frame(0.01, make_pcm(1000))]   # 10ms 겹침, 동일 사용자
    out = mix({1: f}, 0.0, 0.03)
    assert out.max() == 1000             # 덮어쓰기: 2000 로 증폭되지 않음


# ---- 깨짐 방지(헤드룸 게인) ----------------------------------------------------
def test_headroom_keeps_peak_under_ceiling():
    out = mix({1: track(30000, 0.0, 50), 2: track(30000, 0.0, 50)}, 0.0, 1.0)
    assert np.abs(out.astype(np.int32)).max() <= CEILING + 1


def test_overlap_does_not_wrap_to_negative():
    out = mix({1: [Frame(0, make_pcm(30000))], 2: [Frame(0, make_pcm(30000))]}, 0.0, 0.02)
    assert out.min() >= 0
    assert clipping_ratio(out) == 0.0


def test_headroom_gain_is_global_not_per_source():
    """전체 믹스에 단일 게인만 적용: 축소 비율이 모든 샘플에 동일하다."""
    out = mix({1: [Frame(0, make_pcm(20000))], 2: [Frame(0, make_pcm(20000))]}, 0.0, 0.02)
    expected = int(20000 * 2 * CEILING / 40000)  # = CEILING
    assert abs(int(out[FRAME_SAMPLES // 2, 0]) - expected) < 2


# ---- 사운드보드 재생음(동의 게이트 + 배치) ------------------------------------
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
    base = int((1.0 - 1.0) * SAMPLE_RATE)
    assert out[base + int(0.01 * SAMPLE_RATE), 0] == 1000       # 음성 페이드 접점
    assert out[base + int(0.51 * SAMPLE_RATE), 0] == 5000       # 사운드보드 페이드 접점(최대치)
    assert out[base + int(0.80 * SAMPLE_RATE), 0] == 0          # 재생 끝난 뒤 무음


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


def test_overlapping_effects_from_same_user_overwrite_not_double():
    """동일 사용자의 사운드보드 프레임이 겹치면 더하지 않고 덮어쓴다(클리핑 방지)."""
    f = [Frame(0.0, make_pcm(1000)), Frame(0.0, make_pcm(1000))]
    out = mix({effect_key(1): f}, 0.0, 0.02)
    assert out.max() == 1000       # 2000 로 증폭(클리핑 위험)되지 않음
