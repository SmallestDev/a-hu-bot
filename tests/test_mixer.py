"""안전 믹싱 테스트: 단순 합산 + 단일 헤드룸 게인, 사람별 볼륨 보정 없음."""
import numpy as np

from src.audio.buffer import Frame, VoiceBuffer, effect_key
from src.audio.mixer import CEILING, clipped_ratio, mix
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


def test_headroom_keeps_peak_under_ceiling():
    """큰 목소리가 겹쳐도 전체 믹스에 단일 헤드룸 게인을 걸어 천장을 넘지 않는다."""
    out = mix({1: sine_frames(30000, 50), 2: sine_frames(30000, 50, freq=330)}, 0.0, 1.0)
    assert np.abs(out.astype(np.int32)).max() <= CEILING + 1


def test_quiet_audio_passthrough_unchanged():
    """천장을 넘지 않는 음원은 게인 변형 없이 그대로 통과한다(사람별 보정 없음)."""
    frames = run_frames(1000, 5)
    out = mix({1: frames}, 0.0, 0.1)
    middle = int(0.02 * SAMPLE_RATE)  # 페이드 구간(2ms) 이후
    assert (out[middle:-middle, 0] == 1000).all()


def test_loud_overlap_scaled_not_hard_clipped():
    """겹침 시 전체 믹스가 비례 축소되며, 평평하게 잘린 구간이 없어야 한다."""
    out = mix({1: sine_frames(32000, 50), 2: sine_frames(32000, 50, freq=330)}, 0.0, 1.0)
    assert np.abs(out.astype(np.int32)).max() <= CEILING + 2
    # 하드 클리핑은 동일 값이 길게続く 플래토를 만든다; 안전 믹싱은 그런 플래토가 없어야 한다
    same = np.abs(np.diff(out[:, 0].astype(np.int32))) == 0
    padded = np.concatenate(([0], same.astype(np.int8), [0]))
    d = np.diff(padded)
    runs = np.where(d == 1)[0], np.where(d == -1)[0]
    max_run = int((runs[1] - runs[0]).max()) if len(runs[0]) else 0
    assert max_run < 5


def test_short_gap_is_interpolated_without_dropout():
    a = [Frame(0.0, make_pcm(1000)), Frame(0.02 + 0.0005, make_pcm(1000))]  # 0.5ms 틈
    out = mix({1: a}, 0.0, 0.05)
    mid = out[300:int(0.04 * SAMPLE_RATE) - 300, 0]
    assert (mid == 1000).all()


def test_long_gap_edges_are_faded():
    a = [Frame(0.0, make_pcm(8000)), Frame(0.1, make_pcm(8000))]
    out = mix({1: a}, 0.0, 0.2)
    end_of_first = FRAME_SAMPLES
    assert abs(int(out[end_of_first - 1, 0])) < 400  # 끝이 0 으로 내려온다
    assert abs(int(out[0, 0])) < 400                  # 시작도 페이드 인


def test_same_user_overlap_overwrites_instead_of_doubling():
    dup = [Frame(0.0, make_pcm(1000)), Frame(0.0, make_pcm(1000)), Frame(0.01, make_pcm(1000))]
    out = mix({1: dup}, 0.0, 0.03)
    assert out[500:1000, 0].max() == 1000


def test_no_per_person_equalization():
    """사람별 RMS 를 맞추지 않음: 큰 목소리와 작은 목소리의 상대 음량 관계가 그대로 유지된다."""
    quiet = sine_frames(700, 100, freq=200)
    loud = sine_frames(14000, 100, freq=300)
    q = mix({1: quiet}, 0.0, 2.0)
    l = mix({2: loud}, 0.0, 2.0)
    ratio = rms(l[2000:-2000]) / rms(q[2000:-2000])
    assert ratio > 10  # 정규화하지 않으면 격차가 그대로


def test_silent_source_does_not_blow_up():
    out = mix({1: run_frames(0, 20), 2: run_frames(3, 20)}, 0.0, 0.4)
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
