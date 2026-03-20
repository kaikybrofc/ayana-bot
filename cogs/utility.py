import asyncio
import logging
import platform
import sys
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from warn_store import total_xp_for_level

LOGGER = logging.getLogger("ayana.cogs.utility")

ACTION_LABELS = {
    "warn": "Warn",
    "manual_warn": "Warn manual",
    "automod_spam": "AutoMod spam",
    "automod_link": "AutoMod link",
    "automod_mention_flood": "AutoMod mention flood",
    "kick": "Kick",
    "ban": "Ban",
    "unban": "Unban",
    "timeout": "Timeout",
    "untimeout": "Untimeout",
    "clearwarnings": "Clear warnings",
    "auto_timeout_warns": "Timeout automático",
    "auto_ban_warns": "Ban automático",
}

COMMAND_DETAILS: dict[str, dict[str, str]] = {
    "help": {
        "categoria": "Utilitarios",
        "uso": "/help [comando]",
        "permissões": "Nenhuma",
        "escopo": "Servidor e DM",
        "detalhes": "Mostra todos os comandos ou detalhes de um comando específico.",
    },
    "ping": {
        "categoria": "Utilitarios",
        "uso": "/ping",
        "permissões": "Nenhuma",
        "escopo": "Servidor e DM",
        "detalhes": "Exibe latências, uptime, memória, shards, cache, estado WS e contexto da guilda atual.",
    },
    "userinfo": {
        "categoria": "Utilitarios",
        "uso": "/userinfo [member]",
        "permissões": "Nenhuma",
        "escopo": "Apenas servidor",
        "detalhes": "Mostra perfil completo com cargos, XP/rank, comandos usados e histórico de moderação.",
    },
    "serverinfo": {
        "categoria": "Utilitarios",
        "uso": "/serverinfo",
        "permissões": "Nenhuma",
        "escopo": "Apenas servidor",
        "detalhes": "Mostra ID, dono, membros, canais, cargos e data de criação do servidor.",
    },
    "rank": {
        "categoria": "Utilitarios",
        "uso": "/rank [member]",
        "permissões": "Nenhuma",
        "escopo": "Apenas servidor",
        "detalhes": "Gera um card em canvas com nível, XP, posição e progresso do membro.",
    },
    "leaderboard": {
        "categoria": "Utilitarios",
        "uso": "/leaderboard [limit]",
        "permissões": "Nenhuma",
        "escopo": "Apenas servidor",
        "detalhes": "Gera um canvas com o ranking de níveis do servidor por XP.",
    },
    "music setup": {
        "categoria": "Musica",
        "uso": "/music setup",
        "permissões": "Nenhuma",
        "escopo": "Servidor e DM",
        "detalhes": "Diagnostica dependencias de audio: FFmpeg, endpoints YTMP3, PyNaCl e Davey.",
    },
    "music join": {
        "categoria": "Musica",
        "uso": "/music join",
        "permissões": "Connect + Speak (bot)",
        "escopo": "Apenas servidor",
        "detalhes": "Conecta o bot no seu canal de voz atual.",
    },
    "music play": {
        "categoria": "Musica",
        "uso": "/music play <busca_ou_url>",
        "permissões": "Connect + Speak (bot)",
        "escopo": "Apenas servidor",
        "detalhes": "Busca uma música (ou usa URL direta) e adiciona na fila.",
    },
    "music queue": {
        "categoria": "Musica",
        "uso": "/music queue [limite]",
        "permissões": "Nenhuma",
        "escopo": "Apenas servidor",
        "detalhes": "Mostra a fila atual e as próximas faixas.",
    },
    "music now": {
        "categoria": "Musica",
        "uso": "/music now",
        "permissões": "Nenhuma",
        "escopo": "Apenas servidor",
        "detalhes": "Mostra a faixa que esta tocando no momento.",
    },
    "music pause": {
        "categoria": "Musica",
        "uso": "/music pause",
        "permissões": "Estar no mesmo canal de voz do bot",
        "escopo": "Apenas servidor",
        "detalhes": "Pausa a reprodução atual.",
    },
    "music resume": {
        "categoria": "Musica",
        "uso": "/music resume",
        "permissões": "Estar no mesmo canal de voz do bot",
        "escopo": "Apenas servidor",
        "detalhes": "Retoma uma faixa pausada.",
    },
    "music skip": {
        "categoria": "Musica",
        "uso": "/music skip",
        "permissões": "Estar no mesmo canal de voz do bot",
        "escopo": "Apenas servidor",
        "detalhes": "Pula para a próxima faixa da fila.",
    },
    "music stop": {
        "categoria": "Musica",
        "uso": "/music stop",
        "permissões": "Estar no mesmo canal de voz do bot",
        "escopo": "Apenas servidor",
        "detalhes": "Para a música atual e limpa toda a fila.",
    },
    "music leave": {
        "categoria": "Musica",
        "uso": "/music leave",
        "permissões": "Estar no mesmo canal de voz do bot",
        "escopo": "Apenas servidor",
        "detalhes": "Desconecta o bot do canal e limpa a fila.",
    },
    "music volume": {
        "categoria": "Musica",
        "uso": "/music volume <valor>",
        "permissões": "Estar no mesmo canal de voz do bot",
        "escopo": "Apenas servidor",
        "detalhes": "Ajusta o volume de 0% a 200%.",
    },
    "nekosia": {
        "categoria": "Imagens",
        "uso": "/nekosia [category] [count] [additional_tags] [blacklisted_tags] [rating]",
        "permissões": "Nenhuma",
        "escopo": "Servidor e DM",
        "detalhes": "Busca imagens da API NekoSia por categoria com filtros opcionais.",
    },
    "nekosia_id": {
        "categoria": "Imagens",
        "uso": "/nekosia_id <image_id>",
        "permissões": "Nenhuma",
        "escopo": "Servidor e DM",
        "detalhes": "Busca uma imagem específica da API NekoSia pelo ID.",
    },
    "nekosia_tags": {
        "categoria": "Imagens",
        "uso": "/nekosia_tags [tipo] [termo]",
        "permissões": "Nenhuma",
        "escopo": "Servidor e DM",
        "detalhes": "Lista tags, animes ou personagens disponíveis na API NekoSia.",
    },
    "clear": {
        "categoria": "Moderacao",
        "uso": "/clear <amount>",
        "permissões": "Manage Messages",
        "escopo": "Apenas servidor",
        "detalhes": "Apaga de 1 a 100 mensagens no canal atual.",
    },
    "slowmode": {
        "categoria": "Moderacao",
        "uso": "/slowmode <tempo> [canal]",
        "permissões": "Manage Channels",
        "escopo": "Apenas servidor",
        "detalhes": "Ajusta o cooldown em canal de texto, thread ou fórum.",
    },
    "lockdown": {
        "categoria": "Moderacao",
        "uso": "/lockdown [canal] [motivo]",
        "permissões": "Manage Channels",
        "escopo": "Apenas servidor",
        "detalhes": "Tranca canal (texto/fórum/voice/stage) e bloqueia thread com modo de emergência.",
    },
    "nick": {
        "categoria": "Moderacao",
        "uso": "/nick <membro> <novo_nome>",
        "permissões": "Manage Nicknames",
        "escopo": "Apenas servidor",
        "detalhes": "Altera forçadamente o apelido de um membro no servidor.",
    },
    "kick": {
        "categoria": "Moderacao",
        "uso": "/kick <member> [reason]",
        "permissões": "Kick Members",
        "escopo": "Apenas servidor",
        "detalhes": "Expulsa um membro respeitando hierarquia de cargos.",
    },
    "ban": {
        "categoria": "Moderacao",
        "uso": "/ban <member> [reason]",
        "permissões": "Ban Members",
        "escopo": "Apenas servidor",
        "detalhes": "Bane um membro respeitando hierarquia de cargos.",
    },
    "unban": {
        "categoria": "Moderacao",
        "uso": "/unban <usuario_banido_ou_id> [reason]",
        "permissões": "Ban Members",
        "escopo": "Apenas servidor",
        "detalhes": "Remove o banimento via autocomplete de banidos ou por ID.",
    },
    "timeout": {
        "categoria": "Moderacao",
        "uso": "/timeout <member> <duration> [reason]",
        "permissões": "Moderate Members",
        "escopo": "Apenas servidor",
        "detalhes": "Aplica timeout com duração em `s`, `m`, `h` ou `d`.",
    },
    "untimeout": {
        "categoria": "Moderacao",
        "uso": "/untimeout <member> [reason]",
        "permissões": "Moderate Members",
        "escopo": "Apenas servidor",
        "detalhes": "Remove o timeout ativo de um membro.",
    },
    "warn": {
        "categoria": "Moderacao",
        "uso": "/warn <member> <reason>",
        "permissões": "Moderate Members",
        "escopo": "Apenas servidor",
        "detalhes": "Registra um aviso no histórico do membro (MySQL).",
    },
    "warnings": {
        "categoria": "Moderacao",
        "uso": "/warnings <member>",
        "permissões": "Moderate Members",
        "escopo": "Apenas servidor",
        "detalhes": "Mostra warns ativos/expirados do membro.",
    },
    "clearwarnings": {
        "categoria": "Moderacao",
        "uso": "/clearwarnings <member>",
        "permissões": "Moderate Members",
        "escopo": "Apenas servidor",
        "detalhes": "Remove todos os avisos registrados de um membro.",
    },
    "infractions": {
        "categoria": "Moderacao",
        "uso": "/infractions <member> [limit]",
        "permissões": "Moderate Members",
        "escopo": "Apenas servidor",
        "detalhes": "Historico unificado de punicoes e eventos do AutoMod.",
    },
    "settings": {
        "categoria": "Moderacao",
        "uso": "/settings",
        "permissões": "Manage Guild",
        "escopo": "Apenas servidor",
        "detalhes": "Mostra configurações de warns, AutoMod e canais de log.",
    },
    "setmodlog": {
        "categoria": "Moderacao",
        "uso": "/setmodlog [channel]",
        "permissões": "Manage Guild",
        "escopo": "Apenas servidor",
        "detalhes": "Define/limpa canal de mod-log.",
    },
    "setautomodlog": {
        "categoria": "Moderacao",
        "uso": "/setautomodlog [channel]",
        "permissões": "Manage Guild",
        "escopo": "Apenas servidor",
        "detalhes": "Define/limpa canal de log específico do AutoMod.",
    },
    "setwarnpolicy": {
        "categoria": "Moderacao",
        "uso": "/setwarnpolicy [timeout_warns] [ban_warns] [expiration_days] [timeout_duration_minutes]",
        "permissões": "Manage Guild",
        "escopo": "Apenas servidor",
        "detalhes": "Configura escalonamento automático e expiração de warns.",
    },
    "setautomod": {
        "categoria": "Moderacao",
        "uso": "/setautomod [enabled] [anti_spam] [anti_link] [anti_mention_flood] ...",
        "permissões": "Manage Guild",
        "escopo": "Apenas servidor",
        "detalhes": "Configura regras, limites e bypass roles do AutoMod.",
    },
    "restaurar": {
        "categoria": "Moderacao",
        "uso": "/restaurar",
        "permissões": "Manage Channels + dono do sistema (DONO_ID)",
        "escopo": "Apenas servidor",
        "detalhes": "Recria o canal atual com mesmo nome/tipo para limpar mensagens.",
    },
}

CATEGORY_ORDER = ("Utilitarios", "Musica", "Imagens", "Moderacao", "Outros")


def ts(dt: datetime | None) -> str:
    if dt is None:
        return "N/A"
    return f"<t:{int(dt.timestamp())}:F>"


class UtilityCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.started_at = discord.utils.utcnow()
        self._cached_owner: discord.User | None = None

    def _slash_commands(self) -> list[app_commands.Command]:
        unique_commands: dict[str, app_commands.Command] = {}
        for cmd in self.bot.tree.walk_commands():
            if not isinstance(cmd, app_commands.Command):
                continue
            unique_commands.setdefault(cmd.qualified_name, cmd)

        return sorted(unique_commands.values(), key=lambda cmd: cmd.qualified_name)

    def _warn_store(self):
        warn_store = getattr(self.bot, "warn_store", None)
        if warn_store is None:
            raise RuntimeError("WarnStore não inicializado.")
        return warn_store

    @staticmethod
    def _command_category(command_name: str) -> str:
        details = COMMAND_DETAILS.get(command_name)
        if details:
            return details["categoria"]
        return "Outros"

    @staticmethod
    def _split_field_values(entries: list[str], max_length: int = 1024) -> list[str]:
        chunks: list[str] = []
        current_chunk: list[str] = []
        current_length = 0

        for entry in entries:
            safe_entry = entry
            if len(safe_entry) > max_length:
                safe_entry = f"{safe_entry[: max_length - 3]}..."

            extra_length = len(safe_entry) + (2 if current_chunk else 0)
            if current_chunk and current_length + extra_length > max_length:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = [safe_entry]
                current_length = len(safe_entry)
                continue

            current_chunk.append(safe_entry)
            current_length += extra_length

        if current_chunk:
            chunks.append("\n\n".join(current_chunk))

        return chunks

    @staticmethod
    def _format_uptime(total_seconds: int) -> str:
        remaining = max(total_seconds, 0)
        days, remaining = divmod(remaining, 86_400)
        hours, remaining = divmod(remaining, 3_600)
        minutes, seconds = divmod(remaining, 60)

        parts: list[str] = []
        if days:
            parts.append(f"{days}d")
        if hours or days:
            parts.append(f"{hours}h")
        if minutes or hours or days:
            parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        return " ".join(parts)

    @staticmethod
    def _process_memory_mb() -> str:
        try:
            import resource
        except ImportError:
            return "N/A"

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if usage <= 0:
            return "N/A"

        # On macOS ru_maxrss is bytes, on Linux it is kilobytes.
        divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
        return f"{usage / divisor:.1f} MB"

    @staticmethod
    def _format_int(value: int) -> str:
        return f"{int(value):,}".replace(",", ".")

    @staticmethod
    def _shorten(text: str, max_length: int = 180) -> str:
        cleaned = text.strip()
        if len(cleaned) <= max_length:
            return cleaned
        return cleaned[: max_length - 3] + "..."

    @staticmethod
    def _action_label(action: str) -> str:
        normalized = action.strip().lower()
        return ACTION_LABELS.get(normalized, normalized)

    async def _system_owner_profile(self) -> str:
        owner_id = self.bot.owner_id
        if owner_id is None:
            return "Não configurado. Defina `DONO_ID` no `.env`."

        owner = self._cached_owner if self._cached_owner and self._cached_owner.id == owner_id else None
        if owner is None:
            owner = self.bot.get_user(owner_id)
        if owner is None:
            try:
                owner = await self.bot.fetch_user(owner_id)
            except discord.HTTPException:
                return f"ID: `{owner_id}`\nNao foi possível carregar o perfil agora."

        self._cached_owner = owner
        display_name = owner.global_name or owner.name
        return (
            f"Nome: `{display_name}`\n"
            f"Usuario: `{owner}`\n"
            f"ID: `{owner.id}`\n"
            f"Criado em: {ts(owner.created_at)}"
        )

    @app_commands.command(name="ping", description="Mostra a latência atual do bot.")
    async def ping(self, interaction: discord.Interaction) -> None:
        now = discord.utils.utcnow()
        latency_ms = round(self.bot.latency * 1000)
        interaction_delay_ms = max(0, round((now - interaction.created_at).total_seconds() * 1000))
        uptime_seconds = int((now - self.started_at).total_seconds())
        owner_profile = await self._system_owner_profile()
        guild = interaction.guild
        shard = guild.shard_id if guild is not None else None
        shard_count = self.bot.shard_count
        guild_count = len(self.bot.guilds)
        cached_users = len(self.bot.users)

        if guild is not None:
            member_count = guild.member_count if guild.member_count is not None else "N/A"
            guild_context = (
                f"Nome: `{guild.name}`\n"
                f"ID: `{guild.id}`\n"
                f"Membros: `{member_count}`\n"
                f"Canais: `{len(guild.channels)}`\n"
                f"Canal atual: <#{interaction.channel_id}>"
            )
        else:
            guild_context = "Executado em `DM`."

        if shard is not None and shard_count:
            shard_label = f"{shard + 1}/{shard_count}"
        elif shard is not None:
            shard_label = str(shard)
        elif shard_count:
            shard_label = f"{shard_count} total"
        else:
            shard_label = "N/A"

        embed = discord.Embed(
            title="Pong!",
            description="Status atual da conexão do bot.",
            color=discord.Color.green(),
            timestamp=now,
        )
        embed.add_field(name="Latencia gateway", value=f"`{latency_ms}ms`", inline=True)
        embed.add_field(name="Atraso da interação", value=f"`{interaction_delay_ms}ms`", inline=True)
        embed.add_field(name="Uptime", value=f"`{self._format_uptime(uptime_seconds)}`", inline=True)
        embed.add_field(name="Shard", value=f"`{shard_label}`", inline=True)
        embed.add_field(name="Servidores", value=f"`{guild_count}`", inline=True)
        embed.add_field(name="Usuarios em cache", value=f"`{cached_users}`", inline=True)
        embed.add_field(
            name="Estado WS",
            value="`Rate limited`" if self.bot.is_ws_ratelimited() else "`OK`",
            inline=True,
        )
        embed.add_field(name="RAM (processo)", value=f"`{self._process_memory_mb()}`", inline=True)
        embed.add_field(name="Comandos slash", value=f"`{len(self._slash_commands())}`", inline=True)
        embed.add_field(name="Guilda atual", value=guild_context, inline=False)
        embed.add_field(name="Dono do sistema", value=owner_profile, inline=False)
        embed.add_field(
            name="Versoes",
            value=f"`Python {platform.python_version()}`\n`discord.py {discord.__version__}`",
            inline=False,
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="help", description="Lista os comandos disponíveis.")
    @app_commands.describe(comando="Nome do comando para ver detalhes. Ex.: kick")
    async def help(self, interaction: discord.Interaction, comando: str | None = None) -> None:
        slash_commands = self._slash_commands()
        command_index = {cmd.qualified_name: cmd for cmd in slash_commands}

        if comando:
            lookup = comando.strip().lower().removeprefix("/")
            target = command_index.get(lookup)
            if target is None:
                await interaction.response.send_message(
                    f"Comando `{lookup}` não encontrado. Use `/help` para ver a lista.",
                    ephemeral=True,
                )
                return

            details = COMMAND_DETAILS.get(target.qualified_name, {})
            embed = discord.Embed(
                title=f"Ajuda de /{target.qualified_name}",
                description=target.description or "Sem descricao.",
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="Uso",
                value=f"`{details.get('uso', f'/{target.qualified_name}')}`",
                inline=False,
            )
            embed.add_field(
                name="Categoria",
                value=details.get("categoria", "Outros"),
                inline=True,
            )
            embed.add_field(
                name="Escopo",
                value=details.get("escopo", "Não informado"),
                inline=True,
            )
            embed.add_field(
                name="Permissoes",
                value=details.get("permissões", "Não informado"),
                inline=False,
            )
            embed.add_field(
                name="Detalhes",
                value=details.get("detalhes", "Sem detalhes adicionais."),
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        commands_by_category: dict[str, list[str]] = {name: [] for name in CATEGORY_ORDER}
        for cmd in slash_commands:
            category = self._command_category(cmd.qualified_name)
            if category not in commands_by_category:
                commands_by_category[category] = []
            details = COMMAND_DETAILS.get(cmd.qualified_name, {})
            usage = details.get("uso", f"/{cmd.qualified_name}")
            perms = details.get("permissões", "Nenhuma")
            commands_by_category[category].append(f"`{usage}`\nPermissoes: `{perms}`")

        embed = discord.Embed(
            title="Central de Comandos",
            description=(
                "Use `/help comando:<nome>` para ver detalhes completos de um comando.\n"
                "Exemplo: `/help comando:kick`"
            ),
            color=discord.Color.blurple(),
        )

        max_fields = 25
        used_fields = 0
        field_limit_reached = False

        for category in CATEGORY_ORDER:
            entries = commands_by_category.get(category, [])
            if entries:
                chunks = self._split_field_values(entries)
                for index, chunk in enumerate(chunks):
                    if used_fields >= max_fields:
                        field_limit_reached = True
                        break

                    field_name = category if index == 0 else f"{category} (cont.)"
                    embed.add_field(name=field_name, value=chunk, inline=False)
                    used_fields += 1

            if field_limit_reached:
                break

        footer_text = f"Total de comandos: {len(slash_commands)}"
        if field_limit_reached:
            footer_text += " | Alguns itens foram omitidos por limite de embed."
        embed.set_footer(text=footer_text)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @help.autocomplete("comando")
    async def help_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        del interaction
        current_normalized = current.lower().strip().removeprefix("/")
        names = [cmd.qualified_name for cmd in self._slash_commands()]
        filtered = [name for name in names if current_normalized in name.lower()]
        return [app_commands.Choice(name=f"/{name}", value=name) for name in filtered[:25]]

    @app_commands.command(name="userinfo", description="Mostra informações de um usuário.")
    @app_commands.guild_only()
    @app_commands.describe(member="Membro para consultar. Se vazio, usa você.")
    async def userinfo(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando só funciona em servidor.",
                ephemeral=True,
            )
            return

        if member is None:
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    "Não consegui ler os dados do usuário neste servidor.",
                    ephemeral=True,
                )
                return
            member = interaction.user

        await interaction.response.defer(ephemeral=True, thinking=True)

        level_task = self._warn_store().get_member_level(guild.id, member.id)
        moderation_task = self._warn_store().get_member_moderation_overview(guild.id, member.id)
        command_usage_task = self._warn_store().get_member_command_usage(guild.id, member.id, limit=5)

        level_result, moderation_result, command_usage_result = await asyncio.gather(
            level_task,
            moderation_task,
            command_usage_task,
            return_exceptions=True,
        )

        level_profile: dict | None
        if isinstance(level_result, Exception):
            LOGGER.error(
                "Falha ao consultar nível/rank no /userinfo.",
                exc_info=(type(level_result), level_result, level_result.__traceback__),
            )
            level_profile = None
        else:
            level_profile = level_result

        moderation_overview: dict[str, object] | None
        if isinstance(moderation_result, Exception):
            LOGGER.error(
                "Falha ao consultar moderação no /userinfo.",
                exc_info=(type(moderation_result), moderation_result, moderation_result.__traceback__),
            )
            moderation_overview = None
        else:
            moderation_overview = moderation_result

        command_usage: dict[str, object] | None
        if isinstance(command_usage_result, Exception):
            LOGGER.error(
                "Falha ao consultar uso de comandos no /userinfo.",
                exc_info=(type(command_usage_result), command_usage_result, command_usage_result.__traceback__),
            )
            command_usage = None
        else:
            command_usage = command_usage_result

        timeout_text = "Sem timeout ativo"
        now = discord.utils.utcnow()
        if member.timed_out_until is not None and member.timed_out_until > now:
            timeout_text = f"Ate {ts(member.timed_out_until)}"

        embed = discord.Embed(
            title=f"Perfil completo: {member}",
            description=f"Usuario: {member.mention}",
            color=member.color if member.color.value else discord.Color.blurple(),
            timestamp=now,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(
            name="Identificacao",
            value=(
                f"ID: `{member.id}`\n"
                f"Conta criada: {ts(member.created_at)}\n"
                f"Entrou no servidor: {ts(member.joined_at)}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Servidor",
            value=(
                f"Maior cargo: {member.top_role.mention}\n"
                f"Quantidade de cargos: `{len(member.roles) - 1}`\n"
                f"Premium/Booster: `{'Sim' if member.premium_since else 'Não'}`\n"
                f"Timeout: `{timeout_text}`"
            ),
            inline=False,
        )

        if level_profile is None:
            level_value = "Sem XP registrado."
        else:
            level = int(level_profile["level"])
            total_xp = int(level_profile["total_xp"])
            rank_position = int(level_profile["rank_position"])
            message_count = int(level_profile["message_count"])
            current_base = total_xp_for_level(level)
            next_total = total_xp_for_level(level + 1)
            level_progress = total_xp - current_base
            level_needed = max(1, next_total - current_base)
            level_value = (
                f"Nivel: `{self._format_int(level)}`\n"
                f"XP total: `{self._format_int(total_xp)}`\n"
                f"Rank: `#{self._format_int(rank_position)}`\n"
                f"Mensagens com XP: `{self._format_int(message_count)}`\n"
                f"Progresso atual: `{self._format_int(level_progress)}/{self._format_int(level_needed)}`"
            )
        embed.add_field(name="Nivel e Rank", value=level_value, inline=False)

        if not command_usage or int(command_usage.get("total_used", 0)) == 0:
            command_value = "Nenhum comando registrado ainda."
        else:
            top_commands = command_usage.get("top_commands", [])
            lines = [
                f"Total usado: `{self._format_int(int(command_usage.get('total_used', 0)))}`",
                f"Comandos unicos: `{self._format_int(int(command_usage.get('unique_commands', 0)))}`",
            ]
            last_used_at = command_usage.get("last_used_at")
            if isinstance(last_used_at, datetime):
                lines.append(f"Ultimo uso: {ts(last_used_at)}")

            if isinstance(top_commands, list) and top_commands:
                lines.append("Top comandos:")
                for row in top_commands[:5]:
                    command_name = str(row.get("command_name", "desconhecido"))
                    use_count = int(row.get("use_count", 0))
                    lines.append(f"`/{command_name}`: `{self._format_int(use_count)}`")
            command_value = "\n".join(lines)
        embed.add_field(name="Comandos usados", value=command_value, inline=False)

        if not moderation_overview:
            moderation_value = "Sem dados de moderação disponíveis."
        else:
            warnings_total = int(moderation_overview.get("warnings_total", 0))
            warnings_active = int(moderation_overview.get("warnings_active", 0))
            warnings_expired = int(moderation_overview.get("warnings_expired", 0))
            infractions_total = int(moderation_overview.get("infractions_total", 0))
            action_counts_raw = moderation_overview.get("action_counts", {})
            action_counts: dict[str, int] = {}
            if isinstance(action_counts_raw, dict):
                action_counts = {str(action): int(count) for action, count in action_counts_raw.items()}

            ban_actions = sum(count for action, count in action_counts.items() if "ban" in action.lower())
            timeout_actions = sum(count for action, count in action_counts.items() if "timeout" in action.lower())
            kick_actions = int(action_counts.get("kick", 0))

            top_actions = sorted(action_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
            actions_text = "Nenhuma"
            if top_actions:
                actions_text = ", ".join(
                    f"{self._action_label(action)} `{self._format_int(count)}`" for action, count in top_actions
                )

            moderation_value = (
                f"Warns ativos: `{self._format_int(warnings_active)}`\n"
                f"Warns expirados: `{self._format_int(warnings_expired)}`\n"
                f"Warns total: `{self._format_int(warnings_total)}`\n"
                f"Infracoes total: `{self._format_int(infractions_total)}`\n"
                f"Historico de ban: `{self._format_int(ban_actions)}` | "
                f"timeout: `{self._format_int(timeout_actions)}` | "
                f"kick: `{self._format_int(kick_actions)}`\n"
                f"Acoes mais comuns: {actions_text}"
            )
        embed.add_field(name="Moderacao", value=moderation_value, inline=False)

        last_infraction_text = "Nenhuma infracao registrada."
        if moderation_overview:
            last_infraction = moderation_overview.get("last_infraction")
            if isinstance(last_infraction, dict):
                action = self._action_label(str(last_infraction.get("action", "desconhecida")))
                reason = self._shorten(str(last_infraction.get("reason", "Sem motivo informado.")), max_length=180)
                created_at = last_infraction.get("created_at")
                last_infraction_text = f"Acao: `{action}`\nQuando: {ts(created_at) if isinstance(created_at, datetime) else 'N/A'}\nMotivo: {reason}"
        embed.add_field(name="Ultima infracao", value=last_infraction_text, inline=False)
        embed.set_footer(text="Historico de comandos passa a contar a partir desta versao.")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="serverinfo", description="Mostra informações do servidor atual.")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando só funciona em servidor.",
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
