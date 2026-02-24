import logging
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

LOGGER = logging.getLogger("ayana.cogs.moderation")
LINK_RE = re.compile(r"(https?://|www\.|discord\.gg/|discord\.com/invite/)", re.IGNORECASE)
ROLE_ID_RE = re.compile(r"\d{17,20}")


class ModerationCog(commands.Cog):
    SETTINGS_CACHE_TTL = 30.0

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._settings_cache: dict[int, tuple[float, dict[str, Any]]] = {}
        self._spam_buckets: dict[tuple[int, int], deque[float]] = defaultdict(deque)

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

    @staticmethod
    def _parse_role_ids(raw_value: str) -> list[int]:
        role_ids = {int(match) for match in ROLE_ID_RE.findall(raw_value)}
        return sorted(role_ids)

    @staticmethod
    def _to_timestamp(dt: datetime) -> int:
        normalized = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return int(normalized.timestamp())

    @staticmethod
    def _can_moderate(
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
    ) -> tuple[bool, str | None]:
        if target == actor:
            return False, "Voce não pode usar este comando em você mesmo."
        if target == guild.owner:
            return False, "Voce não pode moderar o dono do servidor."
        if actor != guild.owner and target.top_role >= actor.top_role:
            return False, "Esse membro tem cargo igual ou superior ao seu."

        me = guild.me
        if me is None:
            return False, "Nao consegui validar minha hierarquia de cargos."
        if target.top_role >= me.top_role:
            return False, "Esse membro tem cargo igual ou superior ao meu."

        return True, None

    @staticmethod
    def _can_bot_moderate_member(guild: discord.Guild, target: discord.Member) -> bool:
        me = guild.me
        if me is None:
            return False
        if target == guild.owner:
            return False
        return target.top_role < me.top_role

    @staticmethod
    def _can_manage_role(
        guild: discord.Guild,
        actor: discord.Member,
        role: discord.Role,
    ) -> tuple[bool, str | None]:
        if role.is_default():
            return False, "Nao use @everyone para esta acao."
        if role.managed:
            return False, "Esse cargo e gerenciado por integracao e nao pode ser atribuido manualmente."

        if actor != guild.owner and role >= actor.top_role:
            return False, "Esse cargo tem posicao igual ou superior ao seu maior cargo."

        me = guild.me
        if me is None:
            return False, "Nao consegui validar minha hierarquia de cargos."
        if role >= me.top_role:
            return False, "Esse cargo tem posicao igual ou superior ao meu maior cargo."
        return True, None

    async def _collect_members_for_bulk(self, guild: discord.Guild) -> tuple[list[discord.Member], bool]:
        members_by_id: dict[int, discord.Member] = {}
        used_api_listing = False
        if self.bot.intents.members:
            try:
                async for member in guild.fetch_members(limit=None):
                    members_by_id[member.id] = member
                used_api_listing = True
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.warning(
                    "Falha ao listar membros via API para bulk role. guild=%s",
                    guild.id,
                )

        if not members_by_id:
            for member in guild.members:
                members_by_id[member.id] = member

        return list(members_by_id.values()), used_api_listing

    @staticmethod
    def _is_automod_bypass(member: discord.Member, settings: dict[str, Any]) -> bool:
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True
        bypass = set(settings.get("automod_bypass_role_ids", []))
        if not bypass:
            return False
        return any(role.id in bypass for role in member.roles)

    @staticmethod
    def _bool_status(value: bool) -> str:
        return "Ligado" if value else "Desligado"

    @staticmethod
    def _format_minutes(total_minutes: int) -> str:
        if total_minutes < 60:
            return f"{total_minutes}m"
        hours, minutes = divmod(total_minutes, 60)
        if hours < 24:
            return f"{hours}h {minutes}m" if minutes else f"{hours}h"
        days, rem_hours = divmod(hours, 24)
        if rem_hours:
            return f"{days}d {rem_hours}h"
        return f"{days}d"

    def _warn_store(self):
        warn_store = getattr(self.bot, "warn_store", None)
        if warn_store is None:
            raise RuntimeError("WarnStore nao inicializado.")
        return warn_store

    def _invalidate_settings_cache(self, guild_id: int) -> None:
        self._settings_cache.pop(guild_id, None)

    async def _get_guild_settings(self, guild_id: int) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._settings_cache.get(guild_id)
        if cached and (now - cached[0]) <= self.SETTINGS_CACHE_TTL:
            return cached[1]

        settings = await self._warn_store().get_guild_settings(guild_id)
        self._settings_cache[guild_id] = (now, settings)
        return settings

    async def _update_guild_settings(self, guild_id: int, **updates: Any) -> dict[str, Any]:
        settings = await self._warn_store().update_guild_settings(guild_id, **updates)
        self._settings_cache[guild_id] = (time.monotonic(), settings)
        return settings

    async def _safe_log_infraction(
        self,
        *,
        guild_id: int,
        user_id: int,
        actor_id: int,
        action: str,
        reason: str,
        related_warning_id: int | None = None,
        expires_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self._warn_store().log_infraction(
                guild_id=guild_id,
                user_id=user_id,
                actor_id=actor_id,
                action=action,
                reason=reason,
                related_warning_id=related_warning_id,
                expires_at=expires_at,
                metadata=metadata,
            )
        except Exception as exc:
            LOGGER.error(
                "Falha ao registrar infraction no MySQL.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def _send_modlog(
        self,
        guild: discord.Guild,
        settings: dict[str, Any],
        *,
        title: str,
        description: str,
        color: discord.Color,
        automod: bool = False,
    ) -> None:
        channel_id = (
            settings.get("automod_log_channel_id")
            if automod and settings.get("automod_log_channel_id")
            else settings.get("mod_log_channel_id")
        )
        if channel_id is None:
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            try:
                fetched = await guild.fetch_channel(channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                return
            if not isinstance(fetched, discord.TextChannel):
                return
            channel = fetched

        embed = discord.Embed(title=title, description=description, color=color)
        embed.timestamp = discord.utils.utcnow()
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Falha ao enviar mod-log para guild=%s canal=%s", guild.id, channel.id)

    def _is_spam_violation(self, message: discord.Message, settings: dict[str, Any]) -> bool:
        guild = message.guild
        author = message.author
        if guild is None or not isinstance(author, discord.Member):
            return False

        max_messages = max(2, int(settings.get("automod_spam_max_messages", 5)))
        interval_seconds = max(1, int(settings.get("automod_spam_interval_seconds", 8)))
        key = (guild.id, author.id)
        bucket = self._spam_buckets[key]
        now = time.monotonic()

        while bucket and now - bucket[0] > interval_seconds:
            bucket.popleft()

        bucket.append(now)
        if len(bucket) < max_messages:
            return False

        bucket.clear()
        return True

    async def _apply_warn_escalation(
        self,
        guild: discord.Guild,
        member: discord.Member,
        active_warnings: int,
        settings: dict[str, Any],
    ) -> str | None:
        bot_user = self.bot.user
        actor_id = bot_user.id if bot_user else (guild.me.id if guild.me else member.id)
        timeout_threshold = int(settings.get("warn_timeout_threshold", 3))
        ban_threshold = int(settings.get("warn_ban_threshold", 5))

        if ban_threshold > 0 and active_warnings >= ban_threshold:
            if not self._can_bot_moderate_member(guild, member):
                return "Escalonamento para ban acionado, mas sem hierarquia suficiente."

            reason = (
                f"Escalonamento automatico: {active_warnings} warns ativos "
                f"(limite de ban: {ban_threshold})."
            )
            try:
                await member.ban(reason=reason, delete_message_days=0)
            except (discord.Forbidden, discord.HTTPException):
                return "Escalonamento para ban falhou por permissao/hierarquia."

            await self._safe_log_infraction(
                guild_id=guild.id,
                user_id=member.id,
                actor_id=actor_id,
                action="auto_ban_warns",
                reason=reason,
            )
            return "Ban automatico aplicado por escalonamento."

        if timeout_threshold > 0 and active_warnings >= timeout_threshold:
            if member.guild_permissions.administrator:
                return "Escalonamento para timeout ignorado (membro administrador)."
            if not self._can_bot_moderate_member(guild, member):
                return "Escalonamento para timeout acionado, mas sem hierarquia suficiente."

            duration_minutes = max(1, min(int(settings.get("warn_timeout_duration_minutes", 60)), 40320))
            timeout_delta = min(timedelta(minutes=duration_minutes), timedelta(days=28))
            timed_out_until = discord.utils.utcnow() + timeout_delta
            reason = (
                f"Escalonamento automatico: {active_warnings} warns ativos "
                f"(limite de timeout: {timeout_threshold})."
            )
            try:
                await member.edit(timed_out_until=timed_out_until, reason=reason)
            except (discord.Forbidden, discord.HTTPException):
                return "Escalonamento para timeout falhou por permissao/hierarquia."

            await self._safe_log_infraction(
                guild_id=guild.id,
                user_id=member.id,
                actor_id=actor_id,
                action="auto_timeout_warns",
                reason=reason,
                expires_at=timed_out_until,
            )
            return (
                "Timeout automatico aplicado por escalonamento "
                f"({self._format_minutes(duration_minutes)})."
            )

        return None

    async def _register_warning(
        self,
        *,
        guild: discord.Guild,
        member: discord.Member,
        actor_id: int,
        reason: str,
        source_action: str,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current_settings = settings or await self._get_guild_settings(guild.id)
        expiration_days = max(0, int(current_settings.get("warn_expiration_days", 60)))
        expires_at = None
        if expiration_days > 0:
            expires_at = discord.utils.utcnow() + timedelta(days=expiration_days)

        warning_id, total, active = await self._warn_store().add_warning(
            guild_id=guild.id,
            user_id=member.id,
            moderator_id=actor_id,
            reason=reason,
            expires_at=expires_at,
        )
        await self._safe_log_infraction(
            guild_id=guild.id,
            user_id=member.id,
            actor_id=actor_id,
            action="warn",
            reason=reason,
            related_warning_id=warning_id,
            expires_at=expires_at,
            metadata={"source": source_action},
        )

        escalation = await self._apply_warn_escalation(
            guild=guild,
            member=member,
            active_warnings=active,
            settings=current_settings,
        )
        return {
            "warning_id": warning_id,
            "total_warnings": total,
            "active_warnings": active,
            "expires_at": expires_at,
            "escalation": escalation,
            "settings": current_settings,
        }

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return

        guild = message.guild
        member = message.author

        try:
            settings = await self._get_guild_settings(guild.id)
        except Exception as exc:
            LOGGER.error(
                "Falha ao carregar settings de guild para AutoMod.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return

        if not settings.get("automod_enabled", True):
            return
        if self._is_automod_bypass(member, settings):
            return

        action = None
        reason = None
        content = message.content or ""
        mention_limit = max(1, int(settings.get("automod_mention_limit", 5)))

        if settings.get("automod_anti_link", True) and LINK_RE.search(content):
            action = "automod_link"
            reason = "AutoMod: envio de link bloqueado."
        elif settings.get("automod_anti_mention_flood", True) and len(message.mentions) >= mention_limit:
            action = "automod_mention_flood"
            reason = f"AutoMod: mention flood ({len(message.mentions)} mencoes)."
        elif settings.get("automod_anti_spam", True) and self._is_spam_violation(message, settings):
            action = "automod_spam"
            reason = "AutoMod: spam detectado."

        if action is None or reason is None:
            return

        deleted = False
        try:
            await message.delete()
            deleted = True
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning(
                "Nao consegui apagar mensagem do AutoMod. guild=%s canal=%s autor=%s",
                guild.id,
                message.channel.id,
                member.id,
            )

        actor_id = self.bot.user.id if self.bot.user else (guild.me.id if guild.me else member.id)
        await self._safe_log_infraction(
            guild_id=guild.id,
            user_id=member.id,
            actor_id=actor_id,
            action=action,
            reason=reason,
            metadata={
                "channel_id": message.channel.id,
                "message_id": message.id,
                "deleted": deleted,
            },
        )

        try:
            warning_result = await self._register_warning(
                guild=guild,
                member=member,
                actor_id=actor_id,
                reason=reason,
                source_action=action,
                settings=settings,
            )
        except Exception as exc:
            LOGGER.error(
                "Falha ao registrar warn automatico do AutoMod.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return

        warning_id = warning_result["warning_id"]
        active = warning_result["active_warnings"]
        total = warning_result["total_warnings"]
        escalation = warning_result["escalation"]
        short_notice = (
            f"{member.mention}, sua mensagem foi removida pelo AutoMod "
            f"e voce recebeu um warn (#{warning_id})."
        )
        try:
            await message.channel.send(short_notice, delete_after=10)
        except (discord.Forbidden, discord.HTTPException):
            pass

        modlog_description = (
            f"Usuario: {member.mention} (`{member.id}`)\n"
            f"Regra: `{action}`\n"
            f"Warn ID: `{warning_id}`\n"
            f"Warns ativos: `{active}` | Total: `{total}`\n"
            f"Motivo: {reason}"
        )
        if escalation:
            modlog_description += f"\nEscalonamento: {escalation}"

        await self._send_modlog(
            guild=guild,
            settings=settings,
            title="AutoMod acionado",
            description=modlog_description,
            color=discord.Color.red(),
            automod=True,
        )

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
        await self._safe_log_infraction(
            guild_id=guild.id,
            user_id=member.id,
            actor_id=interaction.user.id,
            action="kick",
            reason=reason or "Sem motivo informado.",
        )
        settings = await self._get_guild_settings(guild.id)
        await self._send_modlog(
            guild=guild,
            settings=settings,
            title="Membro expulso",
            description=f"Usuario: {member.mention}\nModerador: {interaction.user.mention}\nMotivo: {reason or 'Sem motivo informado.'}",
            color=discord.Color.orange(),
        )
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
        await self._safe_log_infraction(
            guild_id=guild.id,
            user_id=member.id,
            actor_id=interaction.user.id,
            action="ban",
            reason=reason or "Sem motivo informado.",
        )
        settings = await self._get_guild_settings(guild.id)
        await self._send_modlog(
            guild=guild,
            settings=settings,
            title="Membro banido",
            description=f"Usuario: {member.mention}\nModerador: {interaction.user.mention}\nMotivo: {reason or 'Sem motivo informado.'}",
            color=discord.Color.red(),
        )
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
        await self._safe_log_infraction(
            guild_id=guild.id,
            user_id=ban_entry.user.id,
            actor_id=interaction.user.id,
            action="unban",
            reason=reason or "Sem motivo informado.",
        )
        settings = await self._get_guild_settings(guild.id)
        await self._send_modlog(
            guild=guild,
            settings=settings,
            title="Usuario desbanido",
            description=f"Usuario: `{ban_entry.user}` (`{ban_entry.user.id}`)\nModerador: {interaction.user.mention}\nMotivo: {reason or 'Sem motivo informado.'}",
            color=discord.Color.green(),
        )
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
        await self._safe_log_infraction(
            guild_id=guild.id,
            user_id=member.id,
            actor_id=interaction.user.id,
            action="timeout",
            reason=reason or "Sem motivo informado.",
            expires_at=timed_out_until,
        )
        settings = await self._get_guild_settings(guild.id)
        await self._send_modlog(
            guild=guild,
            settings=settings,
            title="Timeout aplicado",
            description=f"Usuario: {member.mention}\nModerador: {interaction.user.mention}\nAte: <t:{self._to_timestamp(timed_out_until)}:F>\nMotivo: {reason or 'Sem motivo informado.'}",
            color=discord.Color.dark_orange(),
        )
        await interaction.response.send_message(
            f"{member.mention} ficou em timeout ate <t:{self._to_timestamp(timed_out_until)}:F>.",
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
        await self._safe_log_infraction(
            guild_id=guild.id,
            user_id=member.id,
            actor_id=interaction.user.id,
            action="untimeout",
            reason=reason or "Sem motivo informado.",
        )
        settings = await self._get_guild_settings(guild.id)
        await self._send_modlog(
            guild=guild,
            settings=settings,
            title="Timeout removido",
            description=f"Usuario: {member.mention}\nModerador: {interaction.user.mention}\nMotivo: {reason or 'Sem motivo informado.'}",
            color=discord.Color.green(),
        )
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

        settings = await self._get_guild_settings(guild.id)
        try:
            warning_result = await self._register_warning(
                guild=guild,
                member=member,
                actor_id=interaction.user.id,
                reason=sanitized_reason,
                source_action="manual_warn",
                settings=settings,
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

        warning_id = warning_result["warning_id"]
        active = warning_result["active_warnings"]
        total = warning_result["total_warnings"]
        expires_at = warning_result["expires_at"]
        escalation = warning_result["escalation"]

        lines = [
            f"{member.mention} recebeu um aviso.",
            f"ID do aviso: `{warning_id}`",
            f"Warns ativos: `{active}` | Total: `{total}`",
        ]
        if expires_at is not None:
            lines.append(f"Expira em: <t:{self._to_timestamp(expires_at)}:F>")
        if escalation:
            lines.append(f"Escalonamento: {escalation}")

        await self._send_modlog(
            guild=guild,
            settings=settings,
            title="Warn aplicado",
            description=(
                f"Usuario: {member.mention}\nModerador: {interaction.user.mention}\n"
                f"Warn ID: `{warning_id}`\nWarns ativos: `{active}` | Total: `{total}`\n"
                f"Motivo: {sanitized_reason}"
            ),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

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
            total, active, rows = await self._warn_store().get_warnings(
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
            expires_at = row.get("expires_at")
            is_active = bool(row.get("is_active", False))

            header = f"**#{warning_id}** por <@{moderator_id}>"
            if isinstance(created_at, datetime):
                header += f" em <t:{self._to_timestamp(created_at)}:f>"

            status = "Ativo" if is_active else "Expirado"
            expiry = ""
            if isinstance(expires_at, datetime):
                expiry = f" | Expira: <t:{self._to_timestamp(expires_at)}:f>"
            elif expires_at is None:
                expiry = " | Expira: nunca"

            entry = (
                f"{header}\n"
                f"Status: `{status}`{expiry}\n"
                f"Motivo: {self._choice_label(reason, max_length=220)}"
            )
            entries.append(entry)

        embed = discord.Embed(
            title=f"Historico de avisos: {member}",
            description="\n\n".join(entries),
            color=discord.Color.orange(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(
            text=f"Warns ativos: {active} | Total: {total} | Mostrando os {len(entries)} mais recentes",
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

        await self._safe_log_infraction(
            guild_id=guild.id,
            user_id=member.id,
            actor_id=interaction.user.id,
            action="clearwarnings",
            reason=f"{removed} warns removidos.",
        )
        settings = await self._get_guild_settings(guild.id)
        await self._send_modlog(
            guild=guild,
            settings=settings,
            title="Historico de warns limpo",
            description=f"Usuario: {member.mention}\nModerador: {interaction.user.mention}\nRemovidos: `{removed}`",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            f"{removed} avisos removidos de {member.mention}.",
            ephemeral=True,
        )

    @app_commands.command(name="infractions", description="Mostra historico unificado de infracoes de um membro.")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.describe(
        member="Membro para consultar.",
        limit="Quantidade de registros para exibir (1 a 25).",
    )
    async def infractions(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        limit: app_commands.Range[int, 1, 25] = 15,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        try:
            rows = await self._warn_store().get_infractions(
                guild_id=guild.id,
                user_id=member.id,
                limit=limit,
            )
        except Exception as exc:
            LOGGER.error(
                "Falha ao consultar infractions no MySQL.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.response.send_message(
                "Falha ao consultar o historico de infracoes.",
                ephemeral=True,
            )
            return

        if not rows:
            await interaction.response.send_message(
                f"{member.mention} nao possui infracoes registradas.",
                ephemeral=True,
            )
            return

        entries: list[str] = []
        for row in rows:
            infraction_id = row.get("id", "?")
            action = str(row.get("action", "unknown"))
            actor_id = row.get("actor_id", "desconhecido")
            reason = str(row.get("reason", "Sem motivo informado."))
            created_at = row.get("created_at")
            expires_at = row.get("expires_at")
            related_warning_id = row.get("related_warning_id")

            header = f"**#{infraction_id} `{action}`** por <@{actor_id}>"
            if isinstance(created_at, datetime):
                header += f" em <t:{self._to_timestamp(created_at)}:f>"

            details = [header, f"Motivo: {self._choice_label(reason, max_length=240)}"]
            if related_warning_id:
                details.append(f"Warn relacionado: `{related_warning_id}`")
            if isinstance(expires_at, datetime):
                details.append(f"Expira em: <t:{self._to_timestamp(expires_at)}:f>")
            entries.append("\n".join(details))

        embed = discord.Embed(
            title=f"Infracoes de {member}",
            description="\n\n".join(entries),
            color=discord.Color.dark_red(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Mostrando {len(entries)} registro(s)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="settings", description="Mostra as configuracoes de moderacao/automod da guild.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def settings(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        try:
            settings = await self._get_guild_settings(guild.id)
        except Exception as exc:
            LOGGER.error(
                "Falha ao carregar settings no comando /settings.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.response.send_message(
                "Falha ao ler configuracoes no banco de dados.",
                ephemeral=True,
            )
            return

        modlog = settings.get("mod_log_channel_id")
        automodlog = settings.get("automod_log_channel_id")
        bypass_roles = settings.get("automod_bypass_role_ids", [])
        bypass_value = "Nenhum"
        if bypass_roles:
            bypass_value = ", ".join(f"<@&{role_id}>" for role_id in bypass_roles[:10])
            if len(bypass_roles) > 10:
                bypass_value += f" ... (+{len(bypass_roles) - 10})"

        embed = discord.Embed(
            title=f"Configuracoes de moderacao: {guild.name}",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Canais",
            value=(
                f"Mod-log: {f'<#{modlog}>' if modlog else '`nao definido`'}\n"
                f"AutoMod log: {f'<#{automodlog}>' if automodlog else '`nao definido`'}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Politica de warn",
            value=(
                f"Timeout em: `{settings['warn_timeout_threshold']}` warns ativos\n"
                f"Ban em: `{settings['warn_ban_threshold']}` warns ativos\n"
                f"Expiracao: `{settings['warn_expiration_days']}` dias (0 = nunca)\n"
                f"Duracao do timeout automatico: `{self._format_minutes(settings['warn_timeout_duration_minutes'])}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="AutoMod",
            value=(
                f"Status: `{self._bool_status(settings['automod_enabled'])}`\n"
                f"Anti-spam: `{self._bool_status(settings['automod_anti_spam'])}` "
                f"({settings['automod_spam_max_messages']} msgs/{settings['automod_spam_interval_seconds']}s)\n"
                f"Anti-link: `{self._bool_status(settings['automod_anti_link'])}`\n"
                f"Anti-mention flood: `{self._bool_status(settings['automod_anti_mention_flood'])}` "
                f"(limite {settings['automod_mention_limit']})\n"
                f"Bypass roles: {bypass_value}"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="setmodlog", description="Define ou limpa o canal de mod-log.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(channel="Canal de mod-log. Deixe vazio para limpar.")
    async def setmodlog(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        try:
            settings = await self._update_guild_settings(
                guild.id,
                mod_log_channel_id=channel.id if channel else None,
            )
        except Exception as exc:
            LOGGER.error(
                "Falha ao atualizar mod_log_channel_id.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.response.send_message(
                "Falha ao atualizar configuracao no banco de dados.",
                ephemeral=True,
            )
            return

        modlog = settings.get("mod_log_channel_id")
        await interaction.response.send_message(
            f"Canal de mod-log atualizado: {f'<#{modlog}>' if modlog else '`nao definido`'}.",
            ephemeral=True,
        )

    @app_commands.command(name="setautomodlog", description="Define ou limpa o canal de log do AutoMod.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(channel="Canal de log do AutoMod. Deixe vazio para limpar.")
    async def setautomodlog(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        try:
            settings = await self._update_guild_settings(
                guild.id,
                automod_log_channel_id=channel.id if channel else None,
            )
        except Exception as exc:
            LOGGER.error(
                "Falha ao atualizar automod_log_channel_id.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.response.send_message(
                "Falha ao atualizar configuracao no banco de dados.",
                ephemeral=True,
            )
            return

        automodlog = settings.get("automod_log_channel_id")
        await interaction.response.send_message(
            f"Canal de log do AutoMod atualizado: {f'<#{automodlog}>' if automodlog else '`nao definido`'}.",
            ephemeral=True,
        )

    @app_commands.command(name="setwarnpolicy", description="Configura escalonamento/expiracao de warns da guild.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        timeout_warns="Warns ativos para aplicar timeout automatico (0 desativa).",
        ban_warns="Warns ativos para aplicar ban automatico (0 desativa).",
        expiration_days="Dias para expirar warn (0 = nunca expira).",
        timeout_duration_minutes="Duracao do timeout automatico em minutos.",
    )
    async def setwarnpolicy(
        self,
        interaction: discord.Interaction,
        timeout_warns: app_commands.Range[int, 0, 20] | None = None,
        ban_warns: app_commands.Range[int, 0, 30] | None = None,
        expiration_days: app_commands.Range[int, 0, 365] | None = None,
        timeout_duration_minutes: app_commands.Range[int, 1, 40320] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        if (
            timeout_warns is None
            and ban_warns is None
            and expiration_days is None
            and timeout_duration_minutes is None
        ):
            await interaction.response.send_message(
                "Informe pelo menos um campo para atualizar.",
                ephemeral=True,
            )
            return

        current = await self._get_guild_settings(guild.id)
        final_timeout = timeout_warns if timeout_warns is not None else current["warn_timeout_threshold"]
        final_ban = ban_warns if ban_warns is not None else current["warn_ban_threshold"]
        if final_timeout > 0 and final_ban > 0 and final_ban < final_timeout:
            await interaction.response.send_message(
                "O limite de ban deve ser maior ou igual ao limite de timeout.",
                ephemeral=True,
            )
            return

        updates: dict[str, Any] = {}
        if timeout_warns is not None:
            updates["warn_timeout_threshold"] = int(timeout_warns)
        if ban_warns is not None:
            updates["warn_ban_threshold"] = int(ban_warns)
        if expiration_days is not None:
            updates["warn_expiration_days"] = int(expiration_days)
        if timeout_duration_minutes is not None:
            updates["warn_timeout_duration_minutes"] = int(timeout_duration_minutes)

        try:
            settings = await self._update_guild_settings(guild.id, **updates)
        except Exception as exc:
            LOGGER.error(
                "Falha ao atualizar setwarnpolicy.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.response.send_message(
                "Falha ao atualizar configuracao no banco de dados.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            (
                "Politica de warns atualizada.\n"
                f"Timeout em: `{settings['warn_timeout_threshold']}` warns ativos\n"
                f"Ban em: `{settings['warn_ban_threshold']}` warns ativos\n"
                f"Expiracao: `{settings['warn_expiration_days']}` dias\n"
                f"Timeout automatico: `{self._format_minutes(settings['warn_timeout_duration_minutes'])}`"
            ),
            ephemeral=True,
        )

    @app_commands.command(name="setautomod", description="Configura AutoMod (limites, bypass roles e ligas/desligas).")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        enabled="Liga/desliga o AutoMod.",
        anti_spam="Liga/desliga deteccao de spam.",
        anti_link="Liga/desliga bloqueio de links.",
        anti_mention_flood="Liga/desliga bloqueio de mention flood.",
        spam_max_messages="Quantidade de mensagens para disparar spam.",
        spam_interval_seconds="Janela de tempo do spam em segundos.",
        mention_limit="Quantidade de mencoes para disparar mention flood.",
        bypass_roles="Mencoes/IDs de cargos bypass (ex.: @staff @admin). Envie vazio/clear para limpar.",
    )
    async def setautomod(
        self,
        interaction: discord.Interaction,
        enabled: bool | None = None,
        anti_spam: bool | None = None,
        anti_link: bool | None = None,
        anti_mention_flood: bool | None = None,
        spam_max_messages: app_commands.Range[int, 2, 20] | None = None,
        spam_interval_seconds: app_commands.Range[int, 1, 60] | None = None,
        mention_limit: app_commands.Range[int, 2, 20] | None = None,
        bypass_roles: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        updates: dict[str, Any] = {}
        if enabled is not None:
            updates["automod_enabled"] = enabled
        if anti_spam is not None:
            updates["automod_anti_spam"] = anti_spam
        if anti_link is not None:
            updates["automod_anti_link"] = anti_link
        if anti_mention_flood is not None:
            updates["automod_anti_mention_flood"] = anti_mention_flood
        if spam_max_messages is not None:
            updates["automod_spam_max_messages"] = int(spam_max_messages)
        if spam_interval_seconds is not None:
            updates["automod_spam_interval_seconds"] = int(spam_interval_seconds)
        if mention_limit is not None:
            updates["automod_mention_limit"] = int(mention_limit)
        if bypass_roles is not None:
            clean = bypass_roles.strip().lower()
            if not clean or clean in {"clear", "limpar"}:
                updates["automod_bypass_role_ids"] = []
            else:
                updates["automod_bypass_role_ids"] = self._parse_role_ids(bypass_roles)

        if not updates:
            await interaction.response.send_message(
                "Informe pelo menos um campo para atualizar.",
                ephemeral=True,
            )
            return

        try:
            settings = await self._update_guild_settings(guild.id, **updates)
        except Exception as exc:
            LOGGER.error(
                "Falha ao atualizar setautomod.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.response.send_message(
                "Falha ao atualizar configuracao no banco de dados.",
                ephemeral=True,
            )
            return

        bypass_roles_ids = settings.get("automod_bypass_role_ids", [])
        bypass_display = "Nenhum"
        if bypass_roles_ids:
            bypass_display = ", ".join(f"<@&{role_id}>" for role_id in bypass_roles_ids[:10])
            if len(bypass_roles_ids) > 10:
                bypass_display += f" ... (+{len(bypass_roles_ids) - 10})"

        await interaction.response.send_message(
            (
                "AutoMod atualizado.\n"
                f"Status: `{self._bool_status(settings['automod_enabled'])}`\n"
                f"Anti-spam: `{self._bool_status(settings['automod_anti_spam'])}` "
                f"({settings['automod_spam_max_messages']} msgs/{settings['automod_spam_interval_seconds']}s)\n"
                f"Anti-link: `{self._bool_status(settings['automod_anti_link'])}`\n"
                f"Anti-mention flood: `{self._bool_status(settings['automod_anti_mention_flood'])}` "
                f"(limite {settings['automod_mention_limit']})\n"
                f"Bypass roles: {bypass_display}"
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="addroleall",
        description="Adiciona um cargo em massa para os membros do servidor.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    @app_commands.describe(
        role="Cargo para adicionar a todos os membros elegiveis.",
        include_bots="Se true, inclui contas de bot tambem.",
    )
    async def addroleall(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        include_bots: bool = False,
    ) -> None:
        guild = interaction.guild
        actor = interaction.user
        if guild is None or not isinstance(actor, discord.Member):
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        can_manage, reason = self._can_manage_role(guild, actor, role)
        if not can_manage:
            await interaction.response.send_message(reason or "Nao foi possivel usar esse cargo.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        members, used_api_listing = await self._collect_members_for_bulk(guild)
        if not members:
            await interaction.followup.send(
                "Nao consegui listar membros para processar.",
                ephemeral=True,
            )
            return

        audit_reason = self._build_reason(actor, f"Adicao em massa do cargo {role.name} ({role.id})")
        total_seen = 0
        added = 0
        skipped_bots = 0
        skipped_has_role = 0
        skipped_actor_hierarchy = 0
        skipped_bot_hierarchy = 0
        failed = 0

        me = guild.me
        for member in members:
            total_seen += 1
            if not include_bots and member.bot:
                skipped_bots += 1
                continue
            if role in member.roles:
                skipped_has_role += 1
                continue

            if actor != guild.owner and member.top_role >= actor.top_role:
                skipped_actor_hierarchy += 1
                continue

            if me is None or member == guild.owner or member.top_role >= me.top_role:
                skipped_bot_hierarchy += 1
                continue

            try:
                await member.add_roles(role, reason=audit_reason)
                added += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1

        source_text = "API completa" if used_api_listing else "cache local"
        note = ""
        if not used_api_listing:
            note = (
                "\\nObs: usei apenas membros em cache. "
                "Ative `SERVER MEMBERS INTENT` para garantir cobertura total."
            )

        await interaction.followup.send(
            (
                "Processamento de cargo em massa finalizado.\\n"
                f"Cargo: {role.mention}\\n"
                f"Fonte de membros: `{source_text}`\\n"
                f"Total analisado: `{total_seen}`\\n"
                f"Adicionados: `{added}`\\n"
                f"Ja tinham o cargo: `{skipped_has_role}`\\n"
                f"Ignorados (bots): `{skipped_bots}`\\n"
                f"Ignorados (hierarquia do autor): `{skipped_actor_hierarchy}`\\n"
                f"Ignorados (hierarquia do bot): `{skipped_bot_hierarchy}`\\n"
                f"Falhas de API/permissao: `{failed}`"
                f"{note}"
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="dmtodos",
        description="Envia uma mensagem por DM para todos os membros elegiveis.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        mensagem="Conteudo da mensagem direta.",
        dry_run="Se true, apenas mostra quem receberia.",
        include_bots="Se true, inclui contas de bot tambem.",
    )
    async def dmtodos(
        self,
        interaction: discord.Interaction,
        mensagem: str,
        dry_run: bool = True,
        include_bots: bool = False,
    ) -> None:
        guild = interaction.guild
        actor = interaction.user
        if guild is None or not isinstance(actor, discord.Member):
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        clean_message = mensagem.strip()
        if not clean_message:
            await interaction.response.send_message(
                "A mensagem nao pode ser vazia.",
                ephemeral=True,
            )
            return
        if len(clean_message) > 2000:
            await interaction.response.send_message(
                "A mensagem excede o limite de 2000 caracteres do Discord.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        members, used_api_listing = await self._collect_members_for_bulk(guild)
        if not members:
            await interaction.followup.send(
                "Nao consegui listar membros para processar.",
                ephemeral=True,
            )
            return

        recipients: list[discord.Member] = []
        seen = 0
        skipped_bots = 0
        for member in members:
            seen += 1
            if self.bot.user and member.id == self.bot.user.id:
                continue
            if not include_bots and member.bot:
                skipped_bots += 1
                continue
            recipients.append(member)

        source_text = "API completa" if used_api_listing else "cache local"
        note = ""
        if not used_api_listing:
            note = (
                "\nObs: usei membros em cache local. "
                "Ative `SERVER MEMBERS INTENT` para cobertura completa."
            )

        if not recipients:
            await interaction.followup.send(
                (
                    "Nenhum destinatario elegivel para DM em massa.\n"
                    f"Total analisado: `{seen}` | Ignorados (bots): `{skipped_bots}`"
                ),
                ephemeral=True,
            )
            return

        preview_lines: list[str] = []
        for member in recipients[:12]:
            preview_lines.append(f"- {member.mention} (`{member.id}`)")

        if dry_run:
            extra = ""
            if len(recipients) > 12:
                extra = f"\n... e mais `{len(recipients) - 12}` membro(s)."
            await interaction.followup.send(
                (
                    "Dry-run: nenhuma DM foi enviada.\n"
                    f"Fonte de membros: `{source_text}`\n"
                    f"Total analisado: `{seen}`\n"
                    f"Destinatarios: `{len(recipients)}`\n"
                    f"Ignorados (bots): `{skipped_bots}`\n"
                    "Prévia:\n"
                    + "\n".join(preview_lines)
                    + extra
                    + note
                ),
                ephemeral=True,
            )
            return

        sent = 0
        blocked_dm = 0
        failed = 0
        for member in recipients:
            try:
                await member.send(clean_message)
                sent += 1
            except discord.Forbidden:
                blocked_dm += 1
            except discord.HTTPException:
                failed += 1

        await interaction.followup.send(
            (
                "Envio de DM em massa finalizado.\n"
                f"Fonte de membros: `{source_text}`\n"
                f"Total analisado: `{seen}`\n"
                f"Destinatarios: `{len(recipients)}`\n"
                f"Enviadas: `{sent}`\n"
                f"Falha por DM fechada/bloqueada: `{blocked_dm}`\n"
                f"Falhas de API: `{failed}`"
                f"{note}"
            ),
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
