"""/tts: 음성 채널 채팅을 TTS로 읽기.

- /tts on / /tts off: 길드별 자동 읽기 켬/끔
- 봇이 같은 길드의 음성 채널에 참가 중일 때, 연동된 텍스트 채널에 올라온
  일반 메시지(content)를 자동으로 읽는다.
- gTTS(Google TTS)로 MP3를 만든 뒤 ffmpeg로 디코딩해 discord.py의 공식 송신 경로
  (FFmpegPCMAudio → Opus 인코더 → DAVE 암호화 RTP)로 보낸다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

import discord
from gtts import gTTS
from discord import app_commands
from discord.ext import commands

NO_MENTION = discord.AllowedMentions.none()

DEFAULT_LANG = os.getenv("TTS_LANG", "ko")
TTS_MAX_TEXT = int(os.getenv("TTS_MAX_TEXT", "4000"))
TTS_PLAY_TIMEOUT = int(os.getenv("TTS_PLAY_TIMEOUT", "120"))

log = logging.getLogger(__name__)


def _voice_ready(vc: discord.VoiceClient | None) -> bool:
    if vc is None:
        return False
    try:
        return vc.channel is not None and vc.is_connected()
    except Exception:
        return False


async def _await_voice_ready(vc: discord.VoiceClient, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if _voice_ready(vc):
            await asyncio.sleep(0.3)
            return
        await asyncio.sleep(0.2)
    raise TimeoutError("음성 연결 송신 준비가 시간 안에 완료되지 않았습니다")


class TTSState:
    def __init__(self) -> None:
        self.enabled: set[int] = set()
        self._locks: dict[int, asyncio.Lock] = {}

    def lock_for(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = self._locks[guild_id] = asyncio.Lock()
        return lock


class TTSCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.state = TTSState()

    @app_commands.command(name="tts", description="음성 채널 채팅 자동 읽기 켬/끔")
    @app_commands.guild_only()
    @app_commands.describe(state="on: 읽기 시작, off: 읽기 중단")
    @app_commands.choices(state=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    async def tts(self, interaction: discord.Interaction, state: app_commands.Choice[str]) -> None:
        gid = interaction.guild_id
        if gid is None:
            await interaction.response.send_message("서버에서만 사용할 수 있어요.", ephemeral=True)
            return
        if state.value == "on":
            self.state.enabled.add(gid)
            await interaction.response.send_message("🔊 채팅 자동 읽기를 켰어요.", ephemeral=True)
        else:
            self.state.enabled.discard(gid)
            await interaction.response.send_message("🔇 채팅 자동 읽기를 껐어요.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        gid = message.guild.id
        if gid not in self.state.enabled:
            return
        sess = self.bot.sessions.get(gid)
        if sess is None:
            return
        # /join 을 친 텍스트 채널의 메시지만 읽는다. (음성 채널에는 텍스트 메시지가 올라오지 않음)
        if message.channel.id != sess.text_channel_id:
            return
        text = (message.content or "").strip()
        if not text or text.startswith("/"):
            return
        vc = sess.voice_client
        if vc is None or not _voice_ready(vc):
            log.debug("TTS 재생 스킵: 음성 연결 준비 안 됨 (guild=%s)", gid)
            return
        text = text[:TTS_MAX_TEXT]
        async with self.state.lock_for(gid):
            try:
                await self._say(vc, text)
            except Exception as exc:
                log.exception("TTS 재생 실패 (guild=%s)", gid)
                try:
                    await message.channel.send(
                        f"⚠️ TTS 재생 실패: {exc}", allowed_mentions=NO_MENTION, delete_after=10)
                except discord.DiscordException:
                    pass

    async def _say(self, vc: discord.VoiceClient, text: str) -> None:
        lang = os.getenv("TTS_LANG", DEFAULT_LANG)
        path: Path | None = None
        try:
            fd, tmp = tempfile.mkstemp(prefix="tts-", suffix=".mp3")
            os.close(fd)
            path = Path(tmp)

            # gTTS → MP3 임시 파일
            def _gen() -> None:
                tts = gTTS(text=text, lang=lang)
                tts.save(str(path))

            await asyncio.to_thread(_gen)

            # 음성 연결 송신 준비 완료 대기
            await _await_voice_ready(vc)

            # ffmpeg로 MP3 디코딩 → discord.py 공식 송신 경로
            source = discord.FFmpegPCMAudio(str(path), executable=self.bot.config.ffmpeg_path)

            loop = asyncio.get_running_loop()
            done = asyncio.Event()

            def after(err: Exception | None) -> None:
                if err:
                    log.error("TTS 재생 중 오류: %s", err)
                try:
                    loop.call_soon_threadsafe(done.set)
                except RuntimeError:
                    done.set()

            vc.play(source, after=after)
            await asyncio.wait_for(done.wait(), timeout=TTS_PLAY_TIMEOUT)
        except asyncio.TimeoutError:
            vc.stop()
            raise RuntimeError("TTS 재생이 시간 안에 종료되지 않았습니다")
        except Exception as exc:
            vc.stop()
            log.error("TTS 생성/재생 실패: %s", exc)
            raise RuntimeError(f"TTS 음성을 생성하지 못했습니다: {exc}")
        finally:
            vc.stop()
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    async def cog_unload(self) -> None:
        pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TTSCog(bot))
