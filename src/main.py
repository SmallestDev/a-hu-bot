"""진입점: python -m src.main"""
from __future__ import annotations

import logging
import sys

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
