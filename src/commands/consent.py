"""/consent 및 안내 메시지의 동의 버튼."""
from __future__ import annotations
 
import discord
from discord import app_commands
from discord.ext import commands
 
NOTICE = "🔴 녹음 중입니다. 동의한 사람의 음성만 최근 60초간 메모리에 보관되고, /clip 할 때만 파일로 저장돼요. 동의: /consent"
 
 
def do_consent(bot, guild_id: int, user_id: int) -> str:
    return "동의했어요." if bot.store.consent(guild_id, user_id) else "이미 동의했어요."
 
 
class ConsentView(discord.ui.View):
    def __init__(self, bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot
 
    @discord.ui.button(label="동의", style=discord.ButtonStyle.success)
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        msg = do_consent(self.bot, interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(msg, ephemeral=True)
 
 
class ConsentCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
 
    @app_commands.command(name="consent", description="음성 수집에 동의")
    @app_commands.guild_only()
    async def consent(self, interaction: discord.Interaction) -> None:
        msg = do_consent(self.bot, interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(f"{interaction.user.mention} {msg}",
                                                allowed_mentions=discord.AllowedMentions.none())
 
 
async def setup(bot) -> None:
    await bot.add_cog(ConsentCog(bot))