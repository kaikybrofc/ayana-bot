import logging
import re
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

LOGGER = logging.getLogger("ayana.cogs.moderation")


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @staticmethod
    def _build_reason(actor: discord.Member, reason: str | None) -> str:
        base = reason.strip() if reason else "Sem motivo informado."
        return f"{base} | Acao por {actor} ({actor.id})"

    @staticmethod
    def _parse_discord_id(raw_value: str) -> int | None:
        cleaned = raw_value.strip()
        match = re.search(r"\d{17,20}", cleaned)
        if match is None:
            return None

        try:
            return int(match.group())
        except ValueError:
            return None

    @staticmethod
    def _parse_duration(raw_value: str) -> timedelta | None:
        compact = raw_value.lower().replace(" ", "")
        match = re.fullmatch(r"(\d+)([smhd])", compact)
        if match is None:
            return None

        amount = int(match.group(1))
        unit = match.group(2)
        if amount <= 0:
            return None

        multipliers = {
            "s": 1,
            "m": 60,
            "h": 60 * 60,
            "d": 60 * 60 * 24,
        }
        return timedelta(seconds=amount * multipliers[unit])

    @staticmethod
    def _choice_label(text: str, max_length: int = 100) -> str:
        if len(text) <= max_length:
            return text
        return text[: max_length - 3] + "..."

    def _warn_store(self):
        warn_store = getattr(self.bot, "warn_store", None)
        if warn_store is None:
            raise RuntimeError("WarnStore nao inicializado.")
        return warn_store

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

    @app_commands.command(name="unban", description="Remove o banimento de um usuario.")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.describe(
        user="Usuario banido (autocomplete) ou ID para desbanir.",
        reason="Motivo do desbanimento.",
    )
    async def unban(
        self,
        interaction: discord.Interaction,
        user: str,
        reason: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        user_id = self._parse_discord_id(user)
        if user_id is None:
            await interaction.response.send_message(
                "Informe um ID valido. Exemplo: `123456789012345678`.",
                ephemeral=True,
            )
            return

        try:
            ban_entry = await guild.fetch_ban(discord.Object(id=user_id))
        except discord.NotFound:
            await interaction.response.send_message(
                "Esse usuario nao esta banido neste servidor.",
                ephemeral=True,
            )
            return

        audit_reason = self._build_reason(interaction.user, reason)
        await guild.unban(ban_entry.user, reason=audit_reason)
        await interaction.response.send_message(
            f"{ban_entry.user} (`{ban_entry.user.id}`) foi desbanido.",
            ephemeral=True,
        )

    @unban.autocomplete("user")
    async def unban_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None:
            return []

        if not isinstance(interaction.user, discord.Member):
            return []

        if not interaction.user.guild_permissions.ban_members:
            return []

        current_text = current.strip().lower()
        choices: list[app_commands.Choice[str]] = []

        typed_id = self._parse_discord_id(current)
        if typed_id is not None:
            choices.append(
                app_commands.Choice(
                    name=self._choice_label(f"Usar ID digitado ({typed_id})"),
                    value=str(typed_id),
                ),
            )

        try:
            async for entry in guild.bans(limit=500):
                user = entry.user
                searchable = f"{user} {user.id} {user.name}".lower()
                if current_text and current_text not in searchable:
                    continue

                value = str(user.id)
                if any(choice.value == value for choice in choices):
                    continue

                label = self._choice_label(f"{user} ({user.id})")
                choices.append(app_commands.Choice(name=label, value=value))
                if len(choices) >= 25:
                    break
        except (discord.Forbidden, discord.HTTPException):
            return choices[:25]

        return choices[:25]

    @app_commands.command(name="timeout", description="Aplica timeout em um membro.")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    @app_commands.describe(
        member="Membro que recebera timeout.",
        duration="Duracao no formato 30m, 2h, 1d ou 45s.",
        reason="Motivo do timeout.",
    )
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: str,
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

        if member.guild_permissions.administrator:
            await interaction.response.send_message(
                "Nao e possivel aplicar timeout em administradores.",
                ephemeral=True,
            )
            return

        timeout_duration = self._parse_duration(duration)
        if timeout_duration is None:
            await interaction.response.send_message(
                "Duracao invalida. Use formatos como `30m`, `2h`, `1d` ou `45s`.",
                ephemeral=True,
            )
            return

        max_timeout = timedelta(days=28)
        if timeout_duration > max_timeout:
            await interaction.response.send_message(
                "A duracao maxima de timeout no Discord e de 28 dias.",
                ephemeral=True,
            )
            return

        timed_out_until = discord.utils.utcnow() + timeout_duration
        audit_reason = self._build_reason(interaction.user, reason)
        await member.edit(timed_out_until=timed_out_until, reason=audit_reason)
        await interaction.response.send_message(
            f"{member.mention} ficou em timeout ate <t:{int(timed_out_until.timestamp())}:F>.",
            ephemeral=True,
        )

    @app_commands.command(name="untimeout", description="Remove o timeout de um membro.")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    @app_commands.describe(member="Membro para remover o timeout.", reason="Motivo da remocao.")
    async def untimeout(
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

        now = discord.utils.utcnow()
        if member.timed_out_until is None or member.timed_out_until <= now:
            await interaction.response.send_message(
                "Esse membro nao esta em timeout.",
                ephemeral=True,
            )
            return

        audit_reason = self._build_reason(interaction.user, reason)
        await member.edit(timed_out_until=None, reason=audit_reason)
        await interaction.response.send_message(
            f"Timeout removido de {member.mention}.",
            ephemeral=True,
        )

    @app_commands.command(name="warn", description="Registra um aviso para um membro.")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.describe(member="Membro que recebera o aviso.", reason="Motivo do aviso.")
    async def warn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str,
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

        sanitized_reason = reason.strip()
        if not sanitized_reason:
            await interaction.response.send_message(
                "Informe um motivo para o aviso.",
                ephemeral=True,
            )
            return

        try:
            warning_id, total = await self._warn_store().add_warning(
                guild_id=guild.id,
                user_id=member.id,
                moderator_id=interaction.user.id,
                reason=sanitized_reason,
            )
        except Exception as exc:
            LOGGER.error(
                "Falha ao salvar warn no MySQL.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.response.send_message(
                "Falha ao salvar o aviso no banco de dados.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            (
                f"{member.mention} recebeu um aviso.\n"
                f"ID do aviso: `{warning_id}` | Total de avisos: `{total}`"
            ),
            ephemeral=True,
        )

    @app_commands.command(name="warnings", description="Lista avisos de um membro.")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.describe(member="Membro para consultar historico de avisos.")
    async def warnings(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        try:
            total, rows = await self._warn_store().get_warnings(
                guild_id=guild.id,
                user_id=member.id,
                limit=10,
            )
        except Exception as exc:
            LOGGER.error(
                "Falha ao consultar warnings no MySQL.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.response.send_message(
                "Falha ao consultar avisos no banco de dados.",
                ephemeral=True,
            )
            return

        if total == 0:
            await interaction.response.send_message(
                f"{member.mention} nao possui avisos registrados.",
                ephemeral=True,
            )
            return

        entries: list[str] = []
        for row in rows:
            warning_id = row.get("id", "?")
            moderator_id = row.get("moderator_id", "desconhecido")
            reason = str(row.get("reason", "Sem motivo informado."))
            created_at = row.get("created_at")
            timestamp = None
            if isinstance(created_at, datetime):
                timestamp = int(created_at.timestamp())

            header = f"**#{warning_id}** por <@{moderator_id}>"
            if timestamp is not None:
                header += f" em <t:{timestamp}:f>"
            entry = f"{header}\nMotivo: {self._choice_label(reason, max_length=220)}"
            entries.append(entry)

        embed = discord.Embed(
            title=f"Historico de avisos: {member}",
            description="\n\n".join(entries),
            color=discord.Color.orange(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(
            text=f"Total de avisos: {total} | Mostrando os {len(entries)} mais recentes",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clearwarnings", description="Remove todos os avisos de um membro.")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.describe(member="Membro que tera o historico limpo.")
    async def clearwarnings(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
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

        try:
            removed = await self._warn_store().clear_warnings(
                guild_id=guild.id,
                user_id=member.id,
            )
        except Exception as exc:
            LOGGER.error(
                "Falha ao limpar warnings no MySQL.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.response.send_message(
                "Falha ao limpar avisos no banco de dados.",
                ephemeral=True,
            )
            return

        if removed == 0:
            await interaction.response.send_message(
                f"{member.mention} nao possui avisos para remover.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"{removed} avisos removidos de {member.mention}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="restaurar",
        description="Clona e recria o canal atual para limpar todas as mensagens.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True, view_channel=True)
    async def restaurar(self, interaction: discord.Interaction) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "Apenas o dono do sistema pode usar este comando.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Nao consegui validar suas permissoes neste servidor.",
                ephemeral=True,
            )
            return

        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "Voce precisa da permissao Gerenciar Canais.",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Use este comando em um canal de texto do servidor.",
                ephemeral=True,
            )
            return

        channel_name = channel.name
        channel_type = str(channel.type)
        reason = f"Restauracao de canal por {interaction.user} ({interaction.user.id})"

        await interaction.response.send_message(
            "Restaurando este canal. Vou recriar e limpar tudo.",
            ephemeral=True,
        )

        try:
            new_channel = await channel.clone(reason=reason)
            await new_channel.edit(position=channel.position, reason=reason)
            await channel.delete(reason=reason)
        except discord.Forbidden:
            try:
                await interaction.followup.send(
                    "Nao tenho permissao suficiente para restaurar este canal.",
                    ephemeral=True,
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Falha ao enviar retorno de erro do /restaurar (Forbidden).")
            return
        except discord.HTTPException:
            try:
                await interaction.followup.send(
                    "Falha ao restaurar o canal. Tente novamente.",
                    ephemeral=True,
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Falha ao enviar retorno de erro do /restaurar (HTTPException).")
            return

        LOGGER.info(
            "Canal restaurado: guild=%s canal=%s tipo=%s por=%s",
            interaction.guild_id,
            channel_name,
            channel_type,
            interaction.user.id,
        )

        try:
            await new_channel.send(
                (
                    f"Canal restaurado por {interaction.user.mention}.\n"
                    f"Nome: `{channel_name}` | Tipo: `{channel_type}`"
                ),
            )
        except discord.HTTPException:
            LOGGER.warning("Canal restaurado, mas nao foi possivel enviar aviso no novo canal.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))
