import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import CHANNELS, SAMPLE_RATE  # noqa: E402

FRAME_SAMPLES = SAMPLE_RATE * 20 // 1000  # 960


def make_pcm(value: int = 1000, ms: int = 20) -> bytes:
    n = SAMPLE_RATE * ms // 1000
    return np.full((n, CHANNELS), value, dtype="<i2").tobytes()


@pytest.fixture
def pcm():
    return make_pcm
