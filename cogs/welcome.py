import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from warn_store import DEFAULT_GUILD_SETTINGS

LOGGER = logging.getLogger("ayana.cogs.welcome")


class _SafeTemplateMap(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class WelcomeCog(commands.Cog):
    SETTINGS_CACHE_TTL = 30.0

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._settings_cache: dict[int, tuple[float, dict[str, Any]]] = {}

    def _warn_store(self):
        warn_store = getattr(self.bot, "warn_store", None)
        if warn_store is None:
            raise RuntimeError("WarnStore nao inicializado.")
        return warn_store

    def _invalidate_settings_cache(self, guild_id: int) -> None:
        self._settings_cache.pop(guild_id, None)

    async def _get_settings(self, guild_id: int) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._settings_cache.get(guild_id)
        if cached and (now - cached[0]) <= self.SETTINGS_CACHE_TTL:
            return cached[1]

        settings = await self._warn_store().get_guild_settings(guild_id)
        self._settings_cache[guild_id] = (now, settings)
        return settings

    async def _update_settings(self, guild_id: int, **updates: Any) -> dict[str, Any]:
        settings = await self._warn_store().update_guild_settings(guild_id, **updates)
        self._settings_cache[guild_id] = (time.monotonic(), settings)
        return settings

    @staticmethod
    def _bool_status(value: bool) -> str:
        return "Ligado" if value else "Desligado"

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: max(1, limit - 3)] + "..."

    @staticmethod
    def _format_role_list(role_ids: list[int], limit: int = 8) -> str:
        if not role_ids:
            return "Nenhum"
        rendered = ", ".join(f"<@&{role_id}>" for role_id in role_ids[:limit])
        if len(role_ids) > limit:
            rendered += f" ... (+{len(role_ids) - limit})"
        return rendered

    async def _resolve_welcome_channel(
        self,
        guild: discord.Guild,
        settings: dict[str, Any],
    ) -> discord.TextChannel | None:
        channel_id = settings.get("welcome_channel_id")
        if channel_id:
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                try:
                    fetched = await guild.fetch_channel(int(channel_id))
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    return None
                if not isinstance(fetched, discord.TextChannel):
                    return None
                channel = fetched
            return channel

        if isinstance(guild.system_channel, discord.TextChannel):
            return guild.system_channel
        return None

    @staticmethod
    def _format_template(
        template: str,
        member: discord.Member,
        guild: discord.Guild,
        *,
        mention_user: bool,
    ) -> str:
        member_count = guild.member_count or len(guild.members)
        mapping = _SafeTemplateMap(
            user=member.mention,
            user_mention=member.mention,
            user_name=member.display_name,
            user_username=member.name,
            user_id=str(member.id),
            guild=guild.name,
            guild_name=guild.name,
            guild_id=str(guild.id),
            member_count=str(member_count),
            owner_mention=(guild.owner.mention if guild.owner else ""),
        )
        rendered = (template or "").format_map(mapping).strip()
        if mention_user and member.mention not in rendered:
            rendered = f"{member.mention}\n{rendered}" if rendered else member.mention
        if not rendered:
            rendered = f"Bem-vindo {member.mention} ao {guild.name}!"
        return rendered[:2000]

    @staticmethod
    def _welcome_allowed_mentions(
        member: discord.Member,
        *,
        mention_user: bool,
    ) -> discord.AllowedMentions:
        if mention_user:
            return discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=[member],
                replied_user=False,
            )
        return discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=False,
            replied_user=False,
        )

    @staticmethod
    def _can_assign_role(guild: discord.Guild, role: discord.Role) -> tuple[bool, str | None]:
        if role.is_default():
            return False, "Nao use @everyone como cargo automatico."
        if role.managed:
            return False, "Esse cargo e gerenciado por integracao e nao pode ser atribuido manualmente."

        me = guild.me
        if me is None:
            return False, "Nao consegui validar a hierarquia de cargos do bot."
        if role >= me.top_role:
            return False, "Esse cargo esta acima (ou igual) ao meu maior cargo."
        return True, None

    async def _apply_auto_roles(self, member: discord.Member, settings: dict[str, Any]) -> None:
        role_ids = settings.get("welcome_auto_role_ids", [])
        if not role_ids:
            return

        guild = member.guild
        me = guild.me
        if me is None:
            return

        to_add: list[discord.Role] = []
        for role_id in role_ids:
            role = guild.get_role(int(role_id))
            if role is None:
                continue
            if role.is_default() or role.managed:
                continue
            if role >= me.top_role:
                LOGGER.warning(
                    "Nao foi possivel atribuir cargo automatico por hierarquia. guild=%s role=%s",
                    guild.id,
                    role.id,
                )
                continue
            if role in member.roles:
                continue
            to_add.append(role)

        if not to_add:
            return

        try:
            await member.add_roles(*to_add, reason="Welcome: atribuicao automatica de cargo")
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning(
                "Falha ao adicionar cargos automaticos no welcome. guild=%s user=%s",
                guild.id,
                member.id,
            )

    async def _send_welcome_channel_message(self, member: discord.Member, settings: dict[str, Any]) -> None:
        guild = member.guild
        channel = await self._resolve_welcome_channel(guild, settings)
        if channel is None:
            return

        template = settings.get("welcome_message") or DEFAULT_GUILD_SETTINGS["welcome_message"]
        mention_user = bool(settings.get("welcome_mention_user", True))
        content = self._format_template(template, member, guild, mention_user=mention_user)
        delete_after_seconds = int(settings.get("welcome_delete_after_seconds", 0) or 0)
        delete_after = None
        if delete_after_seconds > 0:
            delete_after = min(delete_after_seconds, 86400)

        try:
            await channel.send(
                content,
                delete_after=delete_after,
                allowed_mentions=self._welcome_allowed_mentions(
                    member,
                    mention_user=mention_user,
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning(
                "Falha ao enviar mensagem de welcome no canal. guild=%s canal=%s",
                guild.id,
                channel.id,
            )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild is None or member.bot:
            return

        try:
            settings = await self._get_settings(member.guild.id)
        except Exception as exc:
            LOGGER.error(
                "Falha ao carregar configuracao de welcome.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return

        if not settings.get("welcome_enabled", False):
            return

        await self._apply_auto_roles(member, settings)
        await self._send_welcome_channel_message(member, settings)

    @app_commands.command(name="welcomesettings", description="Mostra as configuracoes do sistema de boas-vindas.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcomesettings(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        try:
            settings = await self._get_settings(guild.id)
        except Exception as exc:
            LOGGER.error(
                "Falha ao ler configuracoes de welcome.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.response.send_message(
                "Falha ao ler configuracoes no banco de dados.",
                ephemeral=True,
            )
            return

        channel_id = settings.get("welcome_channel_id")
        channel_text = f"<#{channel_id}>" if channel_id else "`system_channel` (fallback)"
        roles_text = self._format_role_list(settings.get("welcome_auto_role_ids", []))
        delete_after = int(settings.get("welcome_delete_after_seconds", 0) or 0)

        embed = discord.Embed(
            title=f"Welcome settings: {guild.name}",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Status",
            value=(
                f"Welcome: `{self._bool_status(bool(settings.get('welcome_enabled', False)))}`\n"
                f"Canal: {channel_text}\n"
                f"Mencionar usuario: `{self._bool_status(bool(settings.get('welcome_mention_user', True)))}`\n"
                f"Delete after: `{delete_after}s` (0 = nao apagar)\n"
                f"Auto-role(s): {roles_text}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Mensagem no canal",
            value=f"```{self._truncate(str(settings.get('welcome_message') or ''), 1000)}```",
            inline=False,
        )
        embed.add_field(
            name="DM",
            value=(
                "Status: `Desligado (fixo)`\n"
                "```Envio de DM de boas-vindas desativado por seguranca.```"
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                "Placeholders: {user_mention}, {user_name}, {user_username}, {user_id}, "
                "{guild_name}, {guild_id}, {member_count}, {owner_mention}"
            )
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="setwelcome",
        description="Configura o sistema de boas-vindas (canal, cargo auto e mensagens no canal).",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        enabled="Liga/desliga o sistema de boas-vindas.",
        channel="Canal de boas-vindas.",
        auto_role="Cargo automatico para novos membros.",
        mention_user="Mencionar o usuario na mensagem do canal.",
        delete_after_seconds="Apagar mensagem apos X segundos (0 nao apaga).",
        dm_enabled="(Desativado) mantido apenas por compatibilidade.",
        message="Template da mensagem no canal.",
        dm_message="(Desativado) mantido apenas por compatibilidade.",
        clear_channel="Limpar canal de boas-vindas configurado.",
        clear_auto_role="Limpar cargo automatico configurado.",
        reset_message="Voltar mensagem do canal para o padrao.",
        reset_dm_message="(Desativado) mantido apenas por compatibilidade.",
    )
    async def setwelcome(
        self,
        interaction: discord.Interaction,
        enabled: bool | None = None,
        channel: discord.TextChannel | None = None,
        auto_role: discord.Role | None = None,
        mention_user: bool | None = None,
        delete_after_seconds: app_commands.Range[int, 0, 86400] | None = None,
        dm_enabled: bool | None = None,
        message: str | None = None,
        dm_message: str | None = None,
        clear_channel: bool = False,
        clear_auto_role: bool = False,
        reset_message: bool = False,
        reset_dm_message: bool = False,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        if clear_channel and channel is not None:
            await interaction.response.send_message(
                "Nao use `channel` junto com `clear_channel`.",
                ephemeral=True,
            )
            return
        if clear_auto_role and auto_role is not None:
            await interaction.response.send_message(
                "Nao use `auto_role` junto com `clear_auto_role`.",
                ephemeral=True,
            )
            return
        if reset_message and message is not None:
            await interaction.response.send_message(
                "Nao use `message` junto com `reset_message`.",
                ephemeral=True,
            )
            return
        if reset_dm_message and dm_message is not None:
            await interaction.response.send_message(
                "Nao use `dm_message` junto com `reset_dm_message`.",
                ephemeral=True,
            )
            return

        updates: dict[str, Any] = {}
        dm_option_requested = dm_enabled is not None or dm_message is not None or reset_dm_message

        if enabled is not None:
            updates["welcome_enabled"] = enabled

        if clear_channel:
            updates["welcome_channel_id"] = None
        elif channel is not None:
            updates["welcome_channel_id"] = channel.id

        if clear_auto_role:
            updates["welcome_auto_role_ids"] = []
        elif auto_role is not None:
            can_assign, reason = self._can_assign_role(guild, auto_role)
            if not can_assign:
                await interaction.response.send_message(reason or "Cargo invalido.", ephemeral=True)
                return
            updates["welcome_auto_role_ids"] = [auto_role.id]

        if mention_user is not None:
            updates["welcome_mention_user"] = mention_user

        if delete_after_seconds is not None:
            updates["welcome_delete_after_seconds"] = int(delete_after_seconds)

        if reset_message:
            updates["welcome_message"] = DEFAULT_GUILD_SETTINGS["welcome_message"]
        elif message is not None:
            clean_message = message.strip()
            if not clean_message:
                await interaction.response.send_message(
                    "A mensagem de welcome nao pode ser vazia.",
                    ephemeral=True,
                )
                return
            updates["welcome_message"] = clean_message[:1500]

        if dm_option_requested:
            updates["welcome_dm_enabled"] = False
            updates["welcome_dm_message"] = DEFAULT_GUILD_SETTINGS["welcome_dm_message"]

        if not updates:
            await interaction.response.send_message(
                "Informe pelo menos uma configuracao para atualizar.",
                ephemeral=True,
            )
            return

        try:
            settings = await self._update_settings(guild.id, **updates)
        except Exception as exc:
            LOGGER.error(
                "Falha ao atualizar configuracoes de welcome.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.response.send_message(
                "Falha ao salvar configuracoes no banco de dados.",
                ephemeral=True,
            )
            return

        channel_id = settings.get("welcome_channel_id")
        channel_text = f"<#{channel_id}>" if channel_id else "`system_channel` (fallback)"
        delete_after = int(settings.get("welcome_delete_after_seconds", 0) or 0)
        roles_text = self._format_role_list(settings.get("welcome_auto_role_ids", []))

        await interaction.response.send_message(
            (
                "Welcome atualizado.\n"
                f"Status: `{self._bool_status(bool(settings.get('welcome_enabled', False)))}`\n"
                f"Canal: {channel_text}\n"
                f"Auto-role(s): {roles_text}\n"
                f"Mencionar usuario: `{self._bool_status(bool(settings.get('welcome_mention_user', True)))}`\n"
                f"Delete after: `{delete_after}s`\n"
                "DM: `Desligado (fixo)`\n"
                + (
                    "Obs: opcoes de DM de boas-vindas foram ignoradas por seguranca."
                    if dm_option_requested
                    else "Obs: envio de DM de boas-vindas permanece desativado por seguranca."
                )
            ),
            ephemeral=True,
        )

    @app_commands.command(name="welcometest", description="Envia um preview da mensagem de welcome no canal atual.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(member="Membro para simular a mensagem. Padrao: voce.")
    async def welcometest(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        target = member
        if target is None:
            if isinstance(interaction.user, discord.Member):
                target = interaction.user
            else:
                await interaction.response.send_message(
                    "Nao consegui identificar um membro para o preview.",
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

        try:
            settings = await self._get_settings(guild.id)
        except Exception as exc:
            LOGGER.error(
                "Falha ao carregar config de welcome no preview.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.response.send_message(
                "Falha ao carregar configuracao de welcome.",
                ephemeral=True,
            )
            return

        template = settings.get("welcome_message") or DEFAULT_GUILD_SETTINGS["welcome_message"]
        message = self._format_template(
            template,
            target,
            guild,
            mention_user=bool(settings.get("welcome_mention_user", True)),
        )

        await interaction.response.send_message(
            "Preview enviado neste canal (sem ping real).",
            ephemeral=True,
        )

        try:
            await channel.send(
                message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "Nao consegui enviar o preview neste canal.",
                ephemeral=True,
            )
            return


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeCog(bot))
