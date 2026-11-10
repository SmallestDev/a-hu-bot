"""/tts: 음성 채널 채팅을 Edge TTS 로 읽기.

- /tts on / /tts off: 길드별 자동 읽기 켬/끔
- 봇이 같은 길드의 음성 채널에 참가 중일 때, 연동된 텍스트 채널에 올라온
  일반 메시지(content)를 자동으로 읽는다.
- Edge TTS 로 MP3 를 만든 뒤 ffmpeg 로 디코딩해 discord.py 의 공식 송신 경로
  (FFmpegPCMAudio → Opus 인코더 → DAVE 암호화 RTP) 로 보낸다.
  평문 PCM 을 그대로 보내지 않으며, 재생 전 음성 연결(DAVE 핸드셰이크 포함)
  이 완전히 완료되도록 기다린다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

import discord
import edge_tts
from discord import app_commands
from discord.ext import commands

from src.config import SAMPLE_RATE  # 참조용: Discord 음성은 고정 48kHz 스테레오

log = logging.getLogger(__name__)

DEFAULT_VOICE = os.getenv("TTS_VOICE", "ko-KR-SunHiNeural")
TTS_MAX_TEXT = int(os.getenv("TTS_MAX_TEXT", "4000"))
TTS_PLAY_TIMEOUT = int(os.getenv("TTS_PLAY_TIMEOUT", "120"))

NO_MENTION = discord.AllowedMentions.none()


def _voice_ready(vc: discord.VoiceClient | None) -> bool:
    """음성 연결(핸드셰이크/DAVE 포함)이 실제로 열려 송신 가능한지."""
    if vc is None:
        return False
    try:
        return vc.channel is not None and vc.is_connected()
    except Exception:
        return False


async def _await_voice_ready(vc: discord.VoiceClient, timeout: float = 10.0) -> None:
    """connect() 이후 송신 경로(DAVE 핸드셰이크·Opus 인코더·UDP 소켓)가 열릴 때까지 대기.

    discord.py 의 VoiceClient.connect() 는 반환 시점에 DAVE 핸드셰이크까지 끝난
    상태이므로, 여기서는 실제로 연결 플래그가 켜질 때까지 짧게 폴링한다.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if _voice_ready(vc):
            # 실제 소켓/인코더 경로가 완전히 열릴 짧은 안정화 시간
            await asyncio.sleep(0.3)
            return
        await asyncio.sleep(0.2)
    raise TimeoutError("음성 연결 송신 준비(DAVE 핸드셰이크 포함)가 시간 안에 완료되지 않았습니다")


class TTSState:
    """길드별 TTS 동작 상태: 켬/끔 플래그 + 재생 순차화 잠금."""

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

    # ---- 명령어 -----------------------------------------------------------
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

    # ---- 이벤트: 연동 텍스트 채널의 일반 메시지 자동 읽기 ------------------
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
        # /join 을 친 텍스트 채널의 메시지만 읽는다.
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
            # 같은 길드에서는 재생을 순차화해 소리가 겹치지 않게 한다.
            try:
                await self._say(vc, text)
            except Exception as exc:
                log.exception("TTS 재생 실패 (guild=%s)", gid)
                try:
                    await message.channel.send(
                        f"⚠️ TTS 재생 실패: {exc}", allowed_mentions=NO_MENTION, delete_after=10)
                except discord.DiscordException:
                    pass

    # ---- 실제 재생 파이프라인 --------------------------------------------
    async def _say(self, vc: discord.VoiceClient, text: str) -> None:
        cfg = self.bot.config
        voice = os.getenv("TTS_VOICE", DEFAULT_VOICE)
        path: Path | None = None
        try:
            # 1) Edge TTS → MP3 임시 파일
            fd, tmp = tempfile.mkstemp(prefix="tts-", suffix=".mp3")
            os.close(fd)
            path = Path(tmp)
            await edge_tts.Communicate(text, voice).save(str(path))

            # 2) 음성 연결 송신 준비 완료 대기 (DAVE 핸드셰이크 포함)
            await _await_voice_ready(vc)

            # 3) ffmpeg 로 MP3 디코딩 → discord.py 공식 송신 경로.
            #    FFmpegPCMAudio 가 MP3 를 s16le/48kHz/스테레오 PCM 으로 변환하고,
            #    discord.py 의 Opus 인코더가 Opus 패킷으로 만든 뒤 DAVE 암호화 RTP 로 보낸다.
            #    평문 PCM 직송이 아니다.
            source = discord.FFmpegPCMAudio(str(path), executable=cfg.ffmpeg_path)

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
        except edge_tts.exceptions.NoAudioReceived:
            vc.stop()
            log.error("Edge TTS 에서 오디오를 받지 못했습니다 (호스팅 네트워크 제한 등)")
            raise RuntimeError("TTS 음성을 생성하지 못했습니다. 호스팅 환경에서 Edge TTS 에 접속할 수 없을 수 있습니다.")
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
