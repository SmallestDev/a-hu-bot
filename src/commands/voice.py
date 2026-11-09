"""/join, /leave, /status 및 음성 상태 이벤트(신규 참가자 안내, 연결 끊김 처리)."""
from __future__ import annotations
 
import asyncio
import logging
import time
 
import discord
from discord import app_commands
from discord.ext import commands
 
from src.commands.consent import NOTICE, ConsentView
 
log = logging.getLogger(__name__)
NO_MENTION = discord.AllowedMentions.none()
 
 
class VoiceCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
 
    @app_commands.command(name="join", description="음성 채널에 참가")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction) -> None:
        bot = self.bot
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await interaction.response.send_message("음성 채널에 먼저 들어가세요.", ephemeral=True)
            return
        if interaction.guild_id in bot.sessions:
            await interaction.response.send_message("이미 참가 중이에요.", ephemeral=True)
            return
 
        cap = bot.receiver.probe()
        if not cap.available or bot.receiver.voice_client_cls is None:
            await interaction.response.send_message(
                f"수신 불가: {cap.summary}", ephemeral=True)
            return
 
        channel = member.voice.channel
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.connect and perms.view_channel):
            await interaction.response.send_message("채널 연결 권한이 없어요.", ephemeral=True)
            return
 
        await interaction.response.defer()
        try:
            vc = await channel.connect(cls=bot.receiver.voice_client_cls, timeout=20.0, reconnect=True)
        except (asyncio.TimeoutError, discord.ClientException, discord.DiscordException) as exc:
            log.warning("음성 연결 실패: %s", exc)
            await interaction.followup.send(f"연결 실패: {type(exc).__name__}", ephemeral=True)
            return
 
        sess = bot.new_session(interaction.guild_id, channel.id, interaction.channel_id, vc)
        try:
            bot.receiver.start(vc, sess.collector, lambda u: getattr(u, "id", None))
        except Exception as exc:
            log.exception("수신 시작 실패")
            await bot.end_session(interaction.guild_id)
            await interaction.followup.send(f"수신 시작 실패: {exc}", ephemeral=True)
            return
 
        text = f"{channel.name} 참가\n{NOTICE}"
        if cap.dave_ready is not True:
            text += f"\n⚠️ {cap.summary}"
        # 아직 동의 안 한 사람이 있을 때만 동의 버튼을 붙인다
        pending = [m for m in channel.members
                   if not m.bot and not bot.store.is_consented(interaction.guild_id, m.id)]
        await interaction.followup.send(text, view=ConsentView(bot) if pending else discord.utils.MISSING,
                                        allowed_mentions=NO_MENTION)
 
    @app_commands.command(name="leave", description="퇴장")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction) -> None:
        ended = await self.bot.end_session(interaction.guild_id)
        if ended:
            await interaction.response.send_message("퇴장, 버퍼 삭제함")
        else:
            await interaction.response.send_message("참가 중이 아니에요.", ephemeral=True)
 
    @app_commands.command(name="status", description="상태")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        bot = self.bot
        sess = bot.sessions.get(interaction.guild_id)
        cap = bot.receiver.probe()
        embed = discord.Embed(title="상태", color=discord.Color.blurple())
        if not sess:
            embed.description = "참가 안 함"
            embed.add_field(name="수신 능력", value=cap.summary, inline=False)
            await interaction.response.send_message(embed=embed)
            return
 
        channel = interaction.guild.get_channel(sess.voice_channel_id)
        consented = bot.store.consented_users(interaction.guild_id)
        lines = []
        if channel:
            for m in channel.members:
                if m.bot:
                    continue
                lines.append(f"{'✅' if m.id in consented else '⛔'} {m.display_name}")
        st = sess.collector.stats
        up = int(time.monotonic() - sess.started_at)
        last = "없음" if st.last_frame_at is None else f"{time.monotonic() - st.last_frame_at:.1f}초 전"
        embed.description = f"🔴 {channel.name if channel else '?'} ({up}초)"
        embed.add_field(name="동의", value="\n".join(lines) or "없음", inline=False)
        embed.add_field(name="버퍼", value=f"{sess.buffer.total_bytes / 2**20:.1f} MiB", inline=True)
        embed.add_field(name="수신 통계", value=(
            f"수집 {st.accepted} · 미동의 {st.rejected_no_consent}\n오류 {st.errors} · 마지막 {last}"), inline=True)
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_MENTION)
 
    # ---- 이벤트 --------------------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState) -> None:
        bot = self.bot
        sess = bot.sessions.get(member.guild.id)
        if sess is None:
            return
 
        # 봇 자신이 끊긴 경우 (강제 퇴장/네트워크)
        if member.id == bot.user.id:
            if after.channel is None and not sess.leaving:
                await self._handle_disconnect(member.guild.id)
            return
 
        # 새 참가자: 동의 안내 (동의 전까지 음성은 폐기됨)
        if after.channel and after.channel.id == sess.voice_channel_id and \
                (before.channel is None or before.channel.id != sess.voice_channel_id):
            if not bot.store.is_consented(member.guild.id, member.id):
                ch = member.guild.get_channel(sess.text_channel_id)
                if ch:
                    try:
                        await ch.send(f"{member.mention} {NOTICE}",
                                      view=ConsentView(bot), allowed_mentions=discord.AllowedMentions(users=[member]))
                    except discord.DiscordException:
                        log.warning("안내 메시지 전송 실패")
 
    async def _handle_disconnect(self, guild_id: int) -> None:
        """예기치 않은 연결 종료: 정리 후 알림. (일시적 끊김은 discord.py 가 reconnect=True 로 자체 복구)"""
        sess = self.bot.sessions.get(guild_id)
        text_id = sess.text_channel_id if sess else None
        await self.bot.end_session(guild_id)
        guild = self.bot.get_guild(guild_id)
        ch = guild.get_channel(text_id) if guild and text_id else None
        if ch:
            try:
                await ch.send("연결이 끊겨서 종료했어요. 다시 /join 하세요.")
            except discord.DiscordException:
                pass
 
 
async def setup(bot) -> None:
    await bot.add_cog(VoiceCog(bot))