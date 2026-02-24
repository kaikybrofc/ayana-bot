import discord
from discord import app_commands
from discord.ext import commands


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @staticmethod
    def _build_reason(actor: discord.Member, reason: str | None) -> str:
        base = reason.strip() if reason else "Sem motivo informado."
        return f"{base} | Acao por {actor} ({actor.id})"

    @staticmethod
    def _can_moderate(
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
    ) -> tuple[bool, str | None]:
        if target == actor:
            return False, "Voce nao pode usar este comando em voce mesmo."
        if target == guild.owner:
            return False, "Voce nao pode moderar o dono do servidor."
        if actor != guild.owner and target.top_role >= actor.top_role:
            return False, "Esse membro tem cargo igual ou superior ao seu."

        me = guild.me
        if me is None:
            return False, "Nao consegui validar minha hierarquia de cargos."
        if target.top_role >= me.top_role:
            return False, "Esse membro tem cargo igual ou superior ao meu."

        return True, None

    @app_commands.command(name="clear", description="Apaga mensagens do canal atual.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    @app_commands.describe(amount="Quantidade de mensagens para apagar (1 a 100).")
    async def clear(self, interaction: discord.Interaction, amount: int) -> None:
        if amount < 1 or amount > 100:
            await interaction.response.send_message(
                "Escolha um valor entre 1 e 100.",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        if channel is None or not hasattr(channel, "purge"):
            await interaction.response.send_message(
                "Este comando so pode ser usado em canais de texto.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        reason = f"Clear por {interaction.user} ({interaction.user.id})"
        deleted = await channel.purge(limit=amount, reason=reason)
        await interaction.followup.send(
            f"{len(deleted)} mensagens apagadas.",
            ephemeral=True,
        )

    @app_commands.command(name="kick", description="Expulsa um membro do servidor.")
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    @app_commands.describe(member="Membro para expulsar.", reason="Motivo da expulsao.")
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        allowed, message = self._can_moderate(guild, interaction.user, member)
        if not allowed:
            await interaction.response.send_message(message or "Acao negada.", ephemeral=True)
            return

        audit_reason = self._build_reason(interaction.user, reason)
        await member.kick(reason=audit_reason)
        await interaction.response.send_message(
            f"{member.mention} foi expulso.",
            ephemeral=True,
        )

    @app_commands.command(name="ban", description="Bane um membro do servidor.")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.describe(member="Membro para banir.", reason="Motivo do banimento.")
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        allowed, message = self._can_moderate(guild, interaction.user, member)
        if not allowed:
            await interaction.response.send_message(message or "Acao negada.", ephemeral=True)
            return

        audit_reason = self._build_reason(interaction.user, reason)
        await member.ban(reason=audit_reason, delete_message_days=0)
        await interaction.response.send_message(
            f"{member.mention} foi banido.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))
