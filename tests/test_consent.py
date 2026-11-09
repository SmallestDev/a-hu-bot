"""동의 게이트 / 철회 / 타임스탬프 정렬 / DB 테스트."""
from src.audio.buffer import VoiceBuffer
from src.audio.receiver import FrameCollector
from src.database.db import ConsentStore
from tests.conftest import make_pcm

G = 100


def make(tmp=":memory:"):
    store = ConsentStore(tmp)
    buf = VoiceBuffer()
    col = FrameCollector(G, buf, store.is_consented)
    return store, buf, col


# 5. 동의 철회 후 수집 안 됨 ------------------------------------------------------
def test_no_consent_means_nothing_is_stored():
    store, buf, col = make()
    assert col.on_pcm(1, make_pcm(), 0, now=1.0) is False
    assert buf.user_ids() == [] and col.stats.rejected_no_consent == 1


def test_consent_then_collect():
    store, buf, col = make()
    store.consent(G, 1)
    assert col.on_pcm(1, make_pcm(), 0, now=1.0) is True
    assert buf.user_ids() == [1]


def test_withdraw_stops_collection_and_purges_buffer():
    store, buf, col = make()
    store.consent(G, 1)
    store.consent(G, 2)
    for i in range(5):
        col.on_pcm(1, make_pcm(), i * 960, now=1.0 + i * 0.02)
        col.on_pcm(2, make_pcm(), i * 960, now=1.0 + i * 0.02)
    store.withdraw(G, 1)
    col.forget_user(1)
    assert 1 not in buf.user_ids() and 2 in buf.user_ids()          # 즉시 삭제, 타인 영향 없음
    assert col.on_pcm(1, make_pcm(), 99999, now=2.0) is False        # 철회 후 프레임 거부
    assert 1 not in buf.user_ids()


def test_withdraw_blocks_even_before_buffer_purge():
    """철회(캐시 갱신)와 purge 사이에 도착한 프레임도 저장되지 않아야 한다."""
    store, buf, col = make()
    store.consent(G, 1)
    store.withdraw(G, 1)
    assert col.on_pcm(1, make_pcm(), 0, now=1.0) is False


def test_unknown_user_and_bad_frames_dropped():
    store, buf, col = make()
    store.consent(G, 1)
    assert col.on_pcm(None, make_pcm(), 0, now=1.0) is False
    assert col.on_pcm(1, b"", 0, now=1.0) is False
    assert col.on_pcm(1, b"\x00\x01\x02", 0, now=1.0) is False
    assert col.stats.rejected_unknown_user == 1 and col.stats.rejected_bad_frame == 2


def test_clear_empties_everything():
    store, buf, col = make()
    store.consent(G, 1)
    col.on_pcm(1, make_pcm(), 0, now=1.0)
    col.clear()
    assert buf.total_bytes == 0 and buf.user_ids() == []


# 타임스탬프 정렬 -------------------------------------------------------------------
def test_rtp_timestamps_remove_arrival_jitter():
    store, buf, col = make()
    store.consent(G, 1)
    # 도착은 불규칙(지터)하지만 RTP 는 960 tick(20ms) 간격
    arrivals = [1.000, 1.031, 1.040, 1.075, 1.080]
    for i, t in enumerate(arrivals):
        col.on_pcm(1, make_pcm(), 5000 + i * 960, now=t)
    starts = [f.start for f in buf.window(0, 10, 1)[1]]
    gaps = [round(b - a, 4) for a, b in zip(starts, starts[1:])]
    assert gaps == [0.02] * 4


def test_rtp_32bit_wraparound():
    store, buf, col = make()
    store.consent(G, 1)
    base = 2**32 - 960
    col.on_pcm(1, make_pcm(), base, now=1.0)
    col.on_pcm(1, make_pcm(), (base + 960) % 2**32, now=1.02)
    starts = [f.start for f in buf.window(0, 10, 1)[1]]
    assert round(starts[1] - starts[0], 4) == 0.02


def test_large_gap_reanchors():
    store, buf, col = make()
    store.consent(G, 1)
    col.on_pcm(1, make_pcm(), 0, now=1.0)
    col.on_pcm(1, make_pcm(), 960 * 1000, now=3.0)  # RTP 는 20초 뒤지만 실제로는 2초 뒤 → 재앵커링
    starts = [f.start for f in buf.window(0, 100, 1)[1]]
    assert abs(starts[1] - (3.0 - 0.02)) < 1e-6


# DB ---------------------------------------------------------------------------------
def test_consent_persists_across_restart(tmp_path):
    p = tmp_path / "db.sqlite3"
    s1 = ConsentStore(p)
    assert s1.consent(G, 7) is True and s1.consent(G, 7) is False
    s1.close()
    s2 = ConsentStore(p)
    assert s2.is_consented(G, 7) and not s2.is_consented(G + 1, 7)
    assert s2.withdraw(G, 7) is True and s2.withdraw(G, 7) is False
    s2.close()
    assert not ConsentStore(p).is_consented(G, 7)


def test_db_stores_no_audio_columns():
    store = ConsentStore()
    tables = sorted(r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    assert tables == ["consents", "proposals"]
    for t in tables:
        types = [r[2].upper() for r in store._conn.execute(f"PRAGMA table_info({t})")]
        assert "BLOB" not in types  # 오디오 데이터는 DB 에 넣지 않는다
    cols = [r[1] for r in store._conn.execute("PRAGMA table_info(consents)")]
    assert cols == ["guild_id", "user_id", "consented_at"]
