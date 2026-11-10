"""진입점: python -m src.main 또는 python src/main.py"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# 대시보드 등이 `python src/main.py` 로 직접 실행할 때도 src 패키지를 import 할 수 있게
# 프로젝트 루트(이 파일의 부모의 부모) 를 sys.path 에 추가한다.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bot import ClipBot
from src.config import Config


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = Config.load()
    except (RuntimeError, ValueError) as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2
    config.clips_dir.mkdir(parents=True, exist_ok=True)
    bot = ClipBot(config)
    try:
        bot.run(config.token, log_handler=None)  # KeyboardInterrupt 시 discord.py 가 close() 호출
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
