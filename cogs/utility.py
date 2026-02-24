import platform
import sys
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

COMMAND_DETAILS: dict[str, dict[str, str]] = {
    "help": {
        "categoria": "Utilitarios",
        "uso": "/help [comando]",
        "permissoes": "Nenhuma",
        "escopo": "Servidor e DM",
        "detalhes": "Mostra todos os comandos ou detalhes de um comando especifico.",
    },
    "ping": {
        "categoria": "Utilitarios",
        "uso": "/ping",
        "permissoes": "Nenhuma",
        "escopo": "Servidor e DM",
        "detalhes": "Exibe latencias, uptime, memoria, shards, cache, estado WS e contexto da guilda atual.",
    },
    "userinfo": {
        "categoria": "Utilitarios",
        "uso": "/userinfo [member]",
        "permissoes": "Nenhuma",
        "escopo": "Apenas servidor",
        "detalhes": "Mostra ID, datas, cargo mais alto e quantidade de cargos do membro.",
    },
    "serverinfo": {
        "categoria": "Utilitarios",
        "uso": "/serverinfo",
        "permissoes": "Nenhuma",
        "escopo": "Apenas servidor",
        "detalhes": "Mostra ID, dono, membros, canais, cargos e data de criacao do servidor.",
    },
    "rank": {
        "categoria": "Utilitarios",
        "uso": "/rank [member]",
        "permissoes": "Nenhuma",
        "escopo": "Apenas servidor",
        "detalhes": "Gera um card em canvas com nivel, XP, posicao e progresso do membro.",
    },
    "leaderboard": {
        "categoria": "Utilitarios",
        "uso": "/leaderboard [limit]",
        "permissoes": "Nenhuma",
        "escopo": "Apenas servidor",
        "detalhes": "Gera um canvas com o ranking de niveis do servidor por XP.",
    },
    "nekosia": {
        "categoria": "Imagens",
        "uso": "/nekosia [category] [count] [additional_tags] [blacklisted_tags] [rating]",
        "permissoes": "Nenhuma",
        "escopo": "Servidor e DM",
        "detalhes": "Busca imagens da API NekoSia por categoria com filtros opcionais.",
    },
    "nekosia_id": {
        "categoria": "Imagens",
        "uso": "/nekosia_id <image_id>",
        "permissoes": "Nenhuma",
        "escopo": "Servidor e DM",
        "detalhes": "Busca uma imagem especifica da API NekoSia pelo ID.",
    },
    "nekosia_tags": {
        "categoria": "Imagens",
        "uso": "/nekosia_tags [tipo] [termo]",
        "permissoes": "Nenhuma",
        "escopo": "Servidor e DM",
        "detalhes": "Lista tags, animes ou personagens disponiveis na API NekoSia.",
    },
    "clear": {
        "categoria": "Moderacao",
        "uso": "/clear <amount>",
        "permissoes": "Manage Messages",
        "escopo": "Apenas servidor",
        "detalhes": "Apaga de 1 a 100 mensagens no canal atual.",
    },
    "kick": {
        "categoria": "Moderacao",
        "uso": "/kick <member> [reason]",
        "permissoes": "Kick Members",
        "escopo": "Apenas servidor",
        "detalhes": "Expulsa um membro respeitando hierarquia de cargos.",
    },
    "ban": {
        "categoria": "Moderacao",
        "uso": "/ban <member> [reason]",
        "permissoes": "Ban Members",
        "escopo": "Apenas servidor",
        "detalhes": "Bane um membro respeitando hierarquia de cargos.",
    },
    "unban": {
        "categoria": "Moderacao",
        "uso": "/unban <usuario_banido_ou_id> [reason]",
        "permissoes": "Ban Members",
        "escopo": "Apenas servidor",
        "detalhes": "Remove o banimento via autocomplete de banidos ou por ID.",
    },
    "timeout": {
        "categoria": "Moderacao",
        "uso": "/timeout <member> <duration> [reason]",
        "permissoes": "Moderate Members",
        "escopo": "Apenas servidor",
        "detalhes": "Aplica timeout com duracao em `s`, `m`, `h` ou `d`.",
    },
    "untimeout": {
        "categoria": "Moderacao",
        "uso": "/untimeout <member> [reason]",
        "permissoes": "Moderate Members",
        "escopo": "Apenas servidor",
        "detalhes": "Remove o timeout ativo de um membro.",
    },
    "warn": {
        "categoria": "Moderacao",
        "uso": "/warn <member> <reason>",
        "permissoes": "Moderate Members",
        "escopo": "Apenas servidor",
        "detalhes": "Registra um aviso no historico do membro (MySQL).",
    },
    "warnings": {
        "categoria": "Moderacao",
        "uso": "/warnings <member>",
        "permissoes": "Moderate Members",
        "escopo": "Apenas servidor",
        "detalhes": "Mostra warns ativos/expirados do membro.",
    },
    "clearwarnings": {
        "categoria": "Moderacao",
        "uso": "/clearwarnings <member>",
        "permissoes": "Moderate Members",
        "escopo": "Apenas servidor",
        "detalhes": "Remove todos os avisos registrados de um membro.",
    },
    "infractions": {
        "categoria": "Moderacao",
        "uso": "/infractions <member> [limit]",
        "permissoes": "Moderate Members",
        "escopo": "Apenas servidor",
        "detalhes": "Historico unificado de punicoes e eventos do AutoMod.",
    },
    "settings": {
        "categoria": "Moderacao",
        "uso": "/settings",
        "permissoes": "Manage Guild",
        "escopo": "Apenas servidor",
        "detalhes": "Mostra configuracoes de warns, AutoMod e canais de log.",
    },
    "setmodlog": {
        "categoria": "Moderacao",
        "uso": "/setmodlog [channel]",
        "permissoes": "Manage Guild",
        "escopo": "Apenas servidor",
        "detalhes": "Define/limpa canal de mod-log.",
    },
    "setautomodlog": {
        "categoria": "Moderacao",
        "uso": "/setautomodlog [channel]",
        "permissoes": "Manage Guild",
        "escopo": "Apenas servidor",
        "detalhes": "Define/limpa canal de log especifico do AutoMod.",
    },
    "setwarnpolicy": {
        "categoria": "Moderacao",
        "uso": "/setwarnpolicy [timeout_warns] [ban_warns] [expiration_days] [timeout_duration_minutes]",
        "permissoes": "Manage Guild",
        "escopo": "Apenas servidor",
        "detalhes": "Configura escalonamento automatico e expiracao de warns.",
    },
    "setautomod": {
        "categoria": "Moderacao",
        "uso": "/setautomod [enabled] [anti_spam] [anti_link] [anti_mention_flood] ...",
        "permissoes": "Manage Guild",
        "escopo": "Apenas servidor",
        "detalhes": "Configura regras, limites e bypass roles do AutoMod.",
    },
    "restaurar": {
        "categoria": "Moderacao",
        "uso": "/restaurar",
        "permissoes": "Manage Channels + dono do sistema (DONO_ID)",
        "escopo": "Apenas servidor",
        "detalhes": "Recria o canal atual com mesmo nome/tipo para limpar mensagens.",
    },
}

CATEGORY_ORDER = ("Utilitarios", "Imagens", "Moderacao", "Outros")


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

    async def _system_owner_profile(self) -> str:
        owner_id = self.bot.owner_id
        if owner_id is None:
            return "Nao configurado. Defina `DONO_ID` no `.env`."

        owner = self._cached_owner if self._cached_owner and self._cached_owner.id == owner_id else None
        if owner is None:
            owner = self.bot.get_user(owner_id)
        if owner is None:
            try:
                owner = await self.bot.fetch_user(owner_id)
            except discord.HTTPException:
                return f"ID: `{owner_id}`\nNao foi possivel carregar o perfil agora."

        self._cached_owner = owner
        display_name = owner.global_name or owner.name
        return (
            f"Nome: `{display_name}`\n"
            f"Usuario: `{owner}`\n"
            f"ID: `{owner.id}`\n"
            f"Criado em: {ts(owner.created_at)}"
        )

    @app_commands.command(name="ping", description="Mostra a latencia atual do bot.")
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
            description="Status atual da conexao do bot.",
            color=discord.Color.green(),
            timestamp=now,
        )
        embed.add_field(name="Latencia gateway", value=f"`{latency_ms}ms`", inline=True)
        embed.add_field(name="Atraso da interacao", value=f"`{interaction_delay_ms}ms`", inline=True)
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

    @app_commands.command(name="help", description="Lista os comandos disponiveis.")
    @app_commands.describe(comando="Nome do comando para ver detalhes. Ex.: kick")
    async def help(self, interaction: discord.Interaction, comando: str | None = None) -> None:
        slash_commands = self._slash_commands()
        command_index = {cmd.qualified_name: cmd for cmd in slash_commands}

        if comando:
            lookup = comando.strip().lower().removeprefix("/")
            target = command_index.get(lookup)
            if target is None:
                await interaction.response.send_message(
                    f"Comando `{lookup}` nao encontrado. Use `/help` para ver a lista.",
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
                value=details.get("escopo", "Nao informado"),
                inline=True,
            )
            embed.add_field(
                name="Permissoes",
                value=details.get("permissoes", "Nao informado"),
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
            perms = details.get("permissoes", "Nenhuma")
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
