"""ClipBot: 길드별 세션 관리, 명령어 로딩, 종료 정리."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import discord
from discord.ext import commands

from src.audio.buffer import VoiceBuffer
from src.audio.receiver import AudioReceiver, FrameCollector, VoiceRecvReceiver
from src.config import Config
from src.database.db import ConsentStore

log = logging.getLogger(__name__)

intents = discord.Intents.default()  # guilds + voice_states + guild_messages.
intents.message_content = True  # /tts 자동 읽기: 일반 채팅 메시지 content 를 읽기 위해 필요

EXTENSIONS = ("src.commands.voice", "src.commands.consent", "src.commands.clip", "src.commands.tts", "src.commands.soundboard")


@dataclass
class GuildSession:
    guild_id: int
    voice_channel_id: int
    text_channel_id: int
    voice_client: discord.VoiceClient
    buffer: VoiceBuffer
    collector: FrameCollector
    clip_sem: asyncio.Semaphore
    started_at: float = field(default_factory=time.monotonic)
    leaving: bool = False


class ClipBot(commands.Bot):
    def __init__(self, config: Config, store: ConsentStore | None = None,
                 receiver: AudioReceiver | None = None) -> None:
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.config = config
        self.store = store or ConsentStore(config.db_path)
        self.receiver: AudioReceiver = receiver or VoiceRecvReceiver()
        self.sessions: dict[int, GuildSession] = {}

    async def setup_hook(self) -> None:
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        if self.config.dev_guild_id:
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("슬래시 명령어를 개발 서버(%s)에 동기화했습니다", guild.id)
        else:
            await self.tree.sync()
            log.info("슬래시 명령어를 전역 동기화했습니다 (반영에 시간이 걸릴 수 있음)")
        cap = self.receiver.probe()
        log.info("수신 능력: available=%s dave_ready=%s — %s", cap.available, cap.dave_ready, cap.summary)
        for line in cap.details:
            log.info("  %s", line)

    # ---- 세션 ---------------------------------------------------------------
    def new_session(self, guild_id: int, voice_channel_id: int, text_channel_id: int,
                    vc: discord.VoiceClient) -> GuildSession:
        c = self.config
        buf = VoiceBuffer(c.buffer_seconds, c.max_buffer_mb_per_user * 2**20, c.max_buffer_mb_total * 2**20)
        collector = FrameCollector(guild_id, buf, self.store.is_consented)
        sess = GuildSession(guild_id, voice_channel_id, text_channel_id, vc, buf, collector,
                            asyncio.Semaphore(c.max_concurrent_clips))
        self.sessions[guild_id] = sess
        return sess

    async def end_session(self, guild_id: int) -> bool:
        """수신 중지 → RAM 버퍼 삭제 → 음성 연결 해제. 여러 번 호출해도 안전."""
        sess = self.sessions.pop(guild_id, None)
        if sess is None:
            return False
        sess.leaving = True
        try:
            self.receiver.stop(sess.voice_client)
        finally:
            sess.collector.clear()
            try:
                await sess.voice_client.disconnect(force=True)
            except Exception:
                log.exception("disconnect 실패")
        return True

    async def close(self) -> None:
        for gid in list(self.sessions):
            await self.end_session(gid)
        try:
            self.store.close()
        except Exception:
            pass
        await super().close()
