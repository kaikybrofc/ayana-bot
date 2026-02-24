from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands


def ts(dt: datetime | None) -> str:
    if dt is None:
        return "N/A"
    return f"<t:{int(dt.timestamp())}:F>"


class UtilityCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Mostra a latencia atual do bot.")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"Pong! `{latency_ms}ms`")

    @app_commands.command(name="help", description="Lista os comandos disponiveis.")
    async def help(self, interaction: discord.Interaction) -> None:
        slash_commands = sorted(
            (
                cmd
                for cmd in self.bot.tree.walk_commands()
                if isinstance(cmd, app_commands.Command)
            ),
            key=lambda cmd: cmd.qualified_name,
        )

        description_lines = []
        for cmd in slash_commands:
            cmd_description = cmd.description or "Sem descricao."
            description_lines.append(f"`/{cmd.qualified_name}` - {cmd_description}")

        embed = discord.Embed(
            title="Comandos disponiveis",
            description="\n".join(description_lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="userinfo", description="Mostra informacoes de um usuario.")
    @app_commands.guild_only()
    @app_commands.describe(member="Membro para consultar. Se vazio, usa voce.")
    async def userinfo(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        if member is None:
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    "Nao consegui ler os dados do usuario neste servidor.",
                    ephemeral=True,
                )
                return
            member = interaction.user

        embed = discord.Embed(
            title=f"User info: {member}",
            color=member.color if member.color.value else discord.Color.blurple(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=str(member.id), inline=False)
        embed.add_field(name="Conta criada", value=ts(member.created_at), inline=False)
        embed.add_field(name="Entrou no servidor", value=ts(member.joined_at), inline=False)
        embed.add_field(name="Maior cargo", value=member.top_role.mention, inline=False)
        embed.add_field(name="Quantidade de cargos", value=str(len(member.roles) - 1), inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="serverinfo", description="Mostra informacoes do servidor atual.")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        icon_url = guild.icon.url if guild.icon else None
        embed = discord.Embed(
            title=f"Server info: {guild.name}",
            color=discord.Color.green(),
        )
        if icon_url:
            embed.set_thumbnail(url=icon_url)
        embed.add_field(name="ID", value=str(guild.id), inline=False)
        embed.add_field(name="Dono", value=f"<@{guild.owner_id}>", inline=False)
        embed.add_field(name="Membros", value=str(guild.member_count or "N/A"), inline=False)
        embed.add_field(name="Canais", value=str(len(guild.channels)), inline=False)
        embed.add_field(name="Cargos", value=str(len(guild.roles)), inline=False)
        embed.add_field(name="Criado em", value=ts(guild.created_at), inline=False)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UtilityCog(bot))
