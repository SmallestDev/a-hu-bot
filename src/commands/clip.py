"""/clip: 버퍼에서 구간 추출 → 믹싱 → 파일 출력 → 업로드. 업로드된 클립은 자르기/사운드보드 건의 가능."""
from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from src.audio import exporter
from src.audio.mixer import clipped_ratio, mix
from src.config import SOUND_MAX_SECONDS

log = logging.getLogger(__name__)
NO_MENTION = discord.AllowedMentions.none()


def _parse_secs(raw: str) -> float:
    raw = raw.strip().replace(",", ".")
    if ":" in raw:  # 1:05 형식도 허용
        m, sec = raw.split(":", 1)
        return int(m) * 60 + float(sec)
    return float(raw)


class TrimModal(discord.ui.Modal, title="클립 자르기"):
    def __init__(self, bot, path, seconds: float, guild_id: int) -> None:
        super().__init__()
        self.bot, self.path, self.seconds, self.guild_id = bot, path, seconds, guild_id
        self.start = discord.ui.TextInput(label="시작 (초)", default="0", max_length=8)
        self.end = discord.ui.TextInput(label="끝 (초)", default=f"{seconds:g}", max_length=8)
        self.add_item(self.start)
        self.add_item(self.end)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            a, b = _parse_secs(self.start.value), _parse_secs(self.end.value)
        except ValueError:
            await interaction.response.send_message("숫자로 적어주세요. 예: 3.5", ephemeral=True)
            return
        b = min(b, self.seconds)
        if not 0 <= a < b:
            await interaction.response.send_message(f"0~{self.seconds:g}초 안에서 시작 < 끝이어야 해요.", ephemeral=True)
            return
        cfg = self.bot.config
        await interaction.response.defer()
        try:
            out = await exporter.trim_clip(
                self.path, a, b, cfg.clips_dir, self.guild_id,
                max_bytes=cfg.max_file_mb * 2**20, ffmpeg=cfg.ffmpeg_path, timeout=cfg.ffmpeg_timeout)
        except exporter.ExportError as exc:
            await interaction.followup.send(f"실패: {exc}", ephemeral=True)
            return
        except Exception:
            log.exception("자르기 중 오류")
            await interaction.followup.send("오류가 났어요.", ephemeral=True)
            return
        view = TrimView(self.bot, out, b - a, interaction.user.id, self.guild_id)
        view.message = await interaction.followup.send(
            f"{a:g}~{b:g}초", file=discord.File(out), view=view, wait=True)


class TrimView(discord.ui.View):
    def __init__(self, bot, path, seconds: float, owner_id: int, guild_id: int) -> None:
        super().__init__(timeout=1800)
        self.bot, self.path, self.seconds = bot, path, seconds
        self.owner_id, self.guild_id = owner_id, guild_id
        self.message: discord.Message | None = None

    async def _owner_only(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("만든 사람만 쓸 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="자르기", style=discord.ButtonStyle.secondary)
    async def trim(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._owner_only(interaction):
            await interaction.response.send_modal(TrimModal(self.bot, self.path, self.seconds, self.guild_id))

    @discord.ui.button(label="사운드보드", style=discord.ButtonStyle.primary)
    async def to_soundboard(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._owner_only(interaction):
            return
        if self.seconds > SOUND_MAX_SECONDS:
            await interaction.response.send_message(
                f"{SOUND_MAX_SECONDS}초 이하여야 해요. 자르기로 먼저 줄여주세요.", ephemeral=True)
            return
        cog = self.bot.get_cog("SoundboardCog")
        if cog is None:
            await interaction.response.send_message("사운드보드 기능이 꺼져 있어요.", ephemeral=True)
            return
        from src.commands.soundboard import HintModal
        await interaction.response.send_modal(HintModal(cog, self.path))

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.DiscordException:
                pass


class ClipCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(name="clip", description="직전 음성 클립")
    @app_commands.describe(duration="초 (기본 30, 최대 60)",
                           user="이 사용자만", user2="한 명 더", user3="한 명 더",
                           format="wav / mp3")
    @app_commands.choices(format=[app_commands.Choice(name="wav", value="wav"),
                                  app_commands.Choice(name="mp3", value="mp3")])
    @app_commands.guild_only()
    async def clip(self, interaction: discord.Interaction, duration: int | None = None,
                   user: discord.Member | None = None,
                   user2: discord.Member | None = None,
                   user3: discord.Member | None = None,
                   format: app_commands.Choice[str] | None = None) -> None:
        bot, cfg = self.bot, self.bot.config
        sess = bot.sessions.get(interaction.guild_id)
        if sess is None:
            await interaction.response.send_message("먼저 /join 하세요.", ephemeral=True)
            return
        me = interaction.user
        if not isinstance(me, discord.Member) or not me.voice or me.voice.channel is None \
                or me.voice.channel.id != sess.voice_channel_id:
            await interaction.response.send_message("같은 음성 채널에 있어야 해요.", ephemeral=True)
            return

        seconds = cfg.default_clip_seconds if duration is None else duration
        if not 1 <= seconds <= cfg.max_clip_seconds:
            await interaction.response.send_message(f"duration은 1~{cfg.max_clip_seconds}초", ephemeral=True)
            return

        picked = list(dict.fromkeys(m for m in (user, user2, user3) if m is not None))
        refused = [m.display_name for m in picked if not bot.store.is_consented(interaction.guild_id, m.id)]
        if refused:
            await interaction.response.send_message(f"{', '.join(refused)}님은 동의 안 했어요.", ephemeral=True)
            return

        if sess.clip_sem.locked():
            await interaction.response.send_message("다른 클립 만드는 중이에요.", ephemeral=True)
            return
        await interaction.response.defer()

        async with sess.clip_sem:
            end = time.monotonic()
            start = end - seconds
            frames = sess.buffer.window(start, end, [m.id for m in picked] if picked else None)  # 스냅샷
            # 사운드보드 재생음은 키가 음수(-user_id) 이므로 abs 로 동의 확인
            frames = {k: f for k, f in frames.items()
                      if bot.store.is_consented(interaction.guild_id, abs(k))}
            if not frames:
                await interaction.followup.send("수집된 음성이 없어요. /status 확인")
                return

            hot = []  # 원본부터 이미 깨져 있는 사람
            for k, f in frames.items():
                if k > 0 and clipped_ratio(f) > 0.002:
                    m = interaction.guild.get_member(k)
                    hot.append(m.display_name if m else str(k))
            if hot:
                log.info("원본 클리핑 감지: %s", hot)

            try:
                samples = await asyncio.to_thread(mix, frames, start, end, normalize=True)
                path = await exporter.export_clip(
                    samples, cfg.clips_dir, interaction.guild_id,
                    fmt=(format.value if format else "wav"),
                    max_bytes=cfg.max_file_mb * 2**20, ffmpeg=cfg.ffmpeg_path, timeout=cfg.ffmpeg_timeout)
            except exporter.FileTooLargeError as exc:
                await interaction.followup.send(f"{exc}")
                return
            except exporter.ExportError as exc:
                log.warning("클립 출력 실패: %s", exc)
                await interaction.followup.send(f"실패: {exc}")
                return
            except Exception:
                log.exception("클립 생성 중 예기치 않은 오류")
                await interaction.followup.send("오류가 났어요.")
                return

            names = []
            for uid in dict.fromkeys(abs(k) for k in frames):
                m = interaction.guild.get_member(uid)
                names.append(m.display_name if m else str(uid))
            text = f"최근 {seconds}초 · {', '.join(names)}"
            if hot:
                text += f"\n{', '.join(hot)}님 마이크가 너무 커서 원본부터 깨져 있어요."
            try:
                view = TrimView(bot, path, float(seconds), interaction.user.id, interaction.guild_id)
                view.message = await interaction.followup.send(
                    text, file=discord.File(path), view=view, allowed_mentions=NO_MENTION, wait=True)
            except discord.DiscordException as exc:
                log.warning("업로드 실패: %s", exc)
                await interaction.followup.send(f"업로드 실패. 파일: {path.name}")


async def setup(bot) -> None:
    await bot.add_cog(ClipCog(bot))
