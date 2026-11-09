"""사운드보드: 클립 건의 → 서버장 DM → 수락하면 이름/이모지 입력 → 서버 사운드보드에 업로드.
그리고 사용자가 사운드보드로 낸 소리를 클립 버퍼에 합성한다."""
from __future__ import annotations

import asyncio
import logging
import re
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from src.audio import soundboard as sb

log = logging.getLogger(__name__)
MAX_UPLOAD = 12 * 2**20
_CUSTOM_EMOJI = re.compile(r"^<(a?):(\w+):(\d+)>$")


def approver_id(bot, guild: discord.Guild) -> int | None:
    return bot.config.soundboard_approver_id or guild.owner_id


def parse_emoji(text: str):
    text = text.strip()
    if not text:
        return None
    m = _CUSTOM_EMOJI.match(text)
    if m:
        return discord.PartialEmoji(name=m.group(2), id=int(m.group(3)), animated=bool(m.group(1)))
    return text


# ---- 서버장 DM 의 수락/거절 버튼 (봇을 재시작해도 동작하도록 DynamicItem 사용) ----------
class AcceptButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sb:accept:(?P<id>[0-9]+)"):
    def __init__(self, pid: int) -> None:
        super().__init__(discord.ui.Button(label="수락", style=discord.ButtonStyle.success,
                                           custom_id=f"sb:accept:{pid}"))
        self.pid = pid

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.get_cog("SoundboardCog").on_accept(interaction, self.pid)


class RejectButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sb:reject:(?P<id>[0-9]+)"):
    def __init__(self, pid: int) -> None:
        super().__init__(discord.ui.Button(label="거절", style=discord.ButtonStyle.danger,
                                           custom_id=f"sb:reject:{pid}"))
        self.pid = pid

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.get_cog("SoundboardCog").on_reject(interaction, self.pid)


class ProposalView(discord.ui.View):
    def __init__(self, pid: int) -> None:
        super().__init__(timeout=None)
        self.add_item(AcceptButton(pid))
        self.add_item(RejectButton(pid))


class AcceptModal(discord.ui.Modal, title="사운드보드 등록"):
    def __init__(self, cog: "SoundboardCog", pid: int, hint: str) -> None:
        super().__init__()
        self.cog, self.pid = cog, pid
        self.name_in = discord.ui.TextInput(label="이름", default=hint or None, min_length=2, max_length=32)
        self.emoji_in = discord.ui.TextInput(label="이모지", required=False, max_length=64, placeholder="😂")
        self.add_item(self.name_in)
        self.add_item(self.emoji_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.finish_accept(interaction, self.pid, self.name_in.value.strip(), self.emoji_in.value)


class HintModal(discord.ui.Modal, title="사운드보드 건의"):
    """클립 메시지의 '사운드보드' 버튼에서 쓰는 이름 입력창."""

    def __init__(self, cog: "SoundboardCog", path) -> None:
        super().__init__()
        self.cog, self.path = cog, path
        self.name_in = discord.ui.TextInput(label="이름 (비워도 돼요)", required=False, max_length=32)
        self.add_item(self.name_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            data = await asyncio.to_thread(self.path.read_bytes)
        except OSError:
            await interaction.followup.send("클립 파일을 못 읽었어요.", ephemeral=True)
            return
        await self.cog.submit(interaction, data, self.name_in.value.strip() or None)


class SoundboardCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.cache = sb.PCMCache()
        self._session: aiohttp.ClientSession | None = None
        self._tasks: set[asyncio.Task] = set()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(AcceptButton, RejectButton)
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def cog_unload(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._session:
            await self._session.close()

    def _file(self, pid: int):
        return self.bot.config.proposals_dir / f"{pid}.mp3"

    # ---- 건의 -------------------------------------------------------------------------
    @app_commands.command(name="soundboard", description="클립을 서버 사운드보드로 건의")
    @app_commands.describe(file="오디오 파일 (5.2초 이하)", name="이름 (선택)")
    @app_commands.guild_only()
    async def soundboard(self, interaction: discord.Interaction, file: discord.Attachment,
                         name: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        if file.size > MAX_UPLOAD:
            await interaction.followup.send("파일이 너무 커요.", ephemeral=True)
            return
        data = await file.read()
        await self.submit(interaction, data, (name or "").strip()[:32] or None)

    async def submit(self, interaction: discord.Interaction, data: bytes, name_hint: str | None) -> None:
        """interaction 은 이미 defer(ephemeral=True) 된 상태여야 한다."""
        bot, cfg = self.bot, self.bot.config
        guild = interaction.guild

        async def reply(msg: str) -> None:
            await interaction.followup.send(msg, ephemeral=True)

        if bot.store.count_pending(guild.id, interaction.user.id) >= cfg.max_pending_per_user:
            await reply(f"대기 중인 건의가 {cfg.max_pending_per_user}개 있어요. 처리된 뒤에 다시 보내주세요.")
            return
        owner_id = approver_id(bot, guild)
        if owner_id is None:
            await reply("서버장을 찾지 못했어요.")
            return
        try:
            mp3, dur = await sb.prepare_sound(data, cfg.ffmpeg_path, cfg.ffmpeg_timeout)
        except sb.SoundError as exc:
            await reply(str(exc))
            return

        pid = bot.store.add_proposal(guild.id, interaction.user.id, name_hint, dur)
        path = self._file(pid)
        try:
            cfg.proposals_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_bytes, mp3)
            owner = await bot.fetch_user(owner_id)
            text = (f"**{guild.name}** 사운드보드 건의\n"
                    f"보낸 사람: {interaction.user.display_name} · {dur:.1f}초"
                    + (f" · 이름 제안: {name_hint}" if name_hint else ""))
            await owner.send(text, file=discord.File(path, filename=f"{name_hint or 'sound'}.mp3"),
                             view=ProposalView(pid))
        except discord.DiscordException as exc:
            log.warning("서버장 DM 실패: %s", exc)
            bot.store.set_proposal_status(pid, "failed")
            path.unlink(missing_ok=True)
            await reply("서버장에게 DM을 보내지 못했어요. (DM이 막혀 있을 수 있어요)")
            return
        except OSError:
            log.exception("건의 파일 저장 실패")
            bot.store.set_proposal_status(pid, "failed")
            await reply("파일을 저장하지 못했어요.")
            return
        await reply("서버장에게 보냈어요.")

    # ---- 서버장 응답 ---------------------------------------------------------------------
    async def _check(self, interaction: discord.Interaction, pid: int):
        bot = self.bot
        row = bot.store.get_proposal(pid)
        if row is None or row["status"] != "pending":
            await interaction.response.send_message("이미 처리된 건의예요.", ephemeral=True)
            return None
        guild = bot.get_guild(row["guild_id"])
        if guild is None:
            await interaction.response.send_message("서버를 찾을 수 없어요.", ephemeral=True)
            return None
        if interaction.user.id != approver_id(bot, guild):
            await interaction.response.send_message("서버장만 처리할 수 있어요.", ephemeral=True)
            return None
        return row, guild

    async def on_accept(self, interaction: discord.Interaction, pid: int) -> None:
        checked = await self._check(interaction, pid)
        if checked:
            row, _ = checked
            await interaction.response.send_modal(AcceptModal(self, pid, row["name_hint"] or ""))

    async def on_reject(self, interaction: discord.Interaction, pid: int) -> None:
        checked = await self._check(interaction, pid)
        if not checked:
            return
        row, guild = checked
        if not self.bot.store.set_proposal_status(pid, "rejected"):
            await interaction.response.send_message("이미 처리된 건의예요.", ephemeral=True)
            return
        self._file(pid).unlink(missing_ok=True)
        await interaction.response.edit_message(content="거절했어요.", view=None)
        await self._notify(row["requester_id"], f"{guild.name} 사운드보드 건의가 거절됐어요.")

    async def finish_accept(self, interaction: discord.Interaction, pid: int, name: str, emoji_text: str) -> None:
        bot = self.bot
        row = bot.store.get_proposal(pid)
        guild = bot.get_guild(row["guild_id"]) if row else None
        if row is None or guild is None or not bot.store.set_proposal_status(pid, "processing"):
            await interaction.response.send_message("이미 처리된 건의예요.", ephemeral=True)
            return
        await interaction.response.defer()
        path = self._file(pid)
        try:
            data = await asyncio.to_thread(path.read_bytes)
            await guild.create_soundboard_sound(
                name=name, sound=data, emoji=parse_emoji(emoji_text),
                reason=f"사운드보드 건의 #{pid}")
        except discord.Forbidden:
            bot.store.set_proposal_status(pid, "pending", expect="processing")
            await interaction.followup.send("봇에 '표현 만들기' 권한이 없어요.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            bot.store.set_proposal_status(pid, "pending", expect="processing")
            await interaction.followup.send(f"올리지 못했어요: {exc.text or exc.status}", ephemeral=True)
            return
        except Exception:
            log.exception("사운드보드 업로드 오류")
            bot.store.set_proposal_status(pid, "pending", expect="processing")
            await interaction.followup.send("오류가 났어요. 다시 눌러보세요.", ephemeral=True)
            return

        bot.store.set_proposal_status(pid, "accepted", expect="processing")
        path.unlink(missing_ok=True)
        await interaction.edit_original_response(content=f"등록했어요: {emoji_text.strip()} {name}", view=None)
        await self._notify(row["requester_id"], f"{guild.name} 사운드보드에 '{name}' 올라갔어요.")

    async def _notify(self, user_id: int, text: str) -> None:
        try:
            user = await self.bot.fetch_user(user_id)
            await user.send(text)
        except discord.DiscordException:
            pass  # DM 이 막혀 있으면 조용히 넘어간다

    # ---- 사운드보드 재생음 캡처 -----------------------------------------------------------
    # 사운드보드 소리는 사용자의 음성 스트림에 섞이지 않고 각 클라이언트가 직접 재생한다.
    # 그래서 수신 오디오에는 없고, 재생 이벤트(on_voice_channel_effect)로 알아내
    # CDN 에서 같은 파일을 받아 이벤트 시각에 맞춰 버퍼에 넣는다.
    @commands.Cog.listener()
    async def on_voice_channel_effect(self, effect) -> None:
        sound = getattr(effect, "sound", None)
        channel = getattr(effect, "channel", None)
        user = getattr(effect, "user", None)
        if sound is None or channel is None or user is None:
            return
        sess = self.bot.sessions.get(channel.guild.id)
        if sess is None or channel.id != sess.voice_channel_id:
            return
        if not self.bot.store.is_consented(channel.guild.id, user.id):
            return
        started = time.monotonic()
        task = asyncio.create_task(self._ingest(sess, user.id, int(sound.id), started,
                                                 float(getattr(sound, 'volume', 1.0) or 1.0)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _ingest(self, sess, user_id: int, sound_id: int, started: float, volume: float = 1.0) -> None:
        try:
            pcm = self.cache.get(sound_id)
            if pcm is None:
                raw = await sb.download(self._session, sound_id)
                if raw is None:
                    return
                cfg = self.bot.config
                pcm = await sb.decode_pcm(raw, cfg.ffmpeg_path, cfg.ffmpeg_timeout, channels=2,
                                          max_seconds=6)
                self.cache.put(sound_id, pcm)
            sess.collector.add_effect(user_id, sb.scale_pcm(pcm, min(max(volume, 0.0), 1.0)), started)
        except sb.SoundError as exc:
            log.warning("사운드보드 소리 처리 실패: %s", exc)
        except Exception:
            log.exception("사운드보드 소리 처리 오류")


async def setup(bot) -> None:
    await bot.add_cog(SoundboardCog(bot))
