import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from warn_store import MySQLConfig, WarnStore

LOGGER = logging.getLogger("ayana")
EXTENSIONS = ("cogs.utility", "cogs.leveling", "cogs.moderation", "cogs.welcome", "cogs.nekosia")


def sanitize_env_value(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    cleaned = raw_value.strip().strip('"').strip("'")
    return cleaned or None


def sanitize_token(raw_value: str | None) -> str | None:
    token = sanitize_env_value(raw_value)
    if not token:
        return None
    if token.lower().startswith("bot "):
        token = token[4:].strip()
    return token or None


def looks_like_discord_token(value: str) -> bool:
    # Bot token has three parts separated by dots and is much longer than 32 chars.
    return value.count(".") == 2 and len(value) >= 50


def parse_discord_id(raw_value: str | None) -> int | None:
    if not raw_value:
        return None

    cleaned = sanitize_env_value(raw_value)
    if cleaned is None:
        return None
    if cleaned.startswith("<") and cleaned.endswith(">"):
        cleaned = cleaned[1:-1]

    matches = re.findall(r"\d{17,20}", cleaned)
    candidate = matches[0] if matches else cleaned

    try:
        discord_id = int(candidate)
        if not (17 <= len(str(discord_id)) <= 20):
            raise ValueError
        return discord_id
    except ValueError:
        return None


def parse_positive_int(raw_value: str | None, var_name: str, default: int) -> int:
    normalized = sanitize_env_value(raw_value)
    if normalized is None:
        return default

    try:
        parsed = int(normalized)
    except ValueError as exc:
        raise RuntimeError(f"{var_name} deve ser um numero inteiro positivo.") from exc

    if parsed <= 0:
        raise RuntimeError(f"{var_name} deve ser maior que zero.")
    return parsed


def parse_bool_env(raw_value: str | None, var_name: str, default: bool = False) -> bool:
    normalized = sanitize_env_value(raw_value)
    if normalized is None:
        return default

    lowered = normalized.lower()
    truthy = {"1", "true", "yes", "y", "on", "enable", "enabled"}
    falsy = {"0", "false", "no", "n", "off", "disable", "disabled"}
    if lowered in truthy:
        return True
    if lowered in falsy:
        return False
    raise RuntimeError(f"{var_name} deve ser true/false (ou 1/0).")


def load_mysql_config_from_env() -> MySQLConfig:
    host = sanitize_env_value(os.getenv("DB_HOST")) or "localhost"
    user = sanitize_env_value(os.getenv("DB_USER"))
    password = sanitize_env_value(os.getenv("DB_PASSWORD")) or ""
    database = sanitize_env_value(os.getenv("DB_NAME"))
    port = parse_positive_int(os.getenv("DB_PORT"), "DB_PORT", default=3306)
    pool_limit = parse_positive_int(os.getenv("DB_POOL_LIMIT"), "DB_POOL_LIMIT", default=10)

    if not user:
        raise RuntimeError("A variavel DB_USER nao foi encontrada no .env.")
    if not database:
        raise RuntimeError("A variavel DB_NAME nao foi encontrada no .env.")

    return MySQLConfig(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        pool_limit=pool_limit,
    )


async def send_ephemeral(interaction: discord.Interaction, message: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        LOGGER.warning(
            "Nao foi possivel responder a interacao (expirada, sem permissao ou canal removido)."
        )


def setup_logging() -> None:
    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "bot.log"

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    logging.getLogger("discord.http").setLevel(logging.WARNING)
    LOGGER.info("Log configurado em %s", log_file.resolve())


def ensure_utf8_runtime() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        stream.reconfigure(encoding="utf-8", errors="replace")


class AyanaBot(commands.Bot):
    def __init__(
        self,
        guild_id: int | None,
        owner_id: int | None,
        warn_store: WarnStore,
        *,
        members_intent_enabled: bool,
        message_content_intent_enabled: bool,
    ) -> None:
        intents = discord.Intents.default()
        intents.members = members_intent_enabled
        intents.message_content = message_content_intent_enabled
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            owner_id=owner_id,
        )
        self.sync_guild_id = guild_id
        self.warn_store = warn_store
        self.tree.on_error = self.on_app_command_error

    async def _delete_overlapping_global_commands(self) -> int:
        local_root_names = {
            cmd.qualified_name.split(" ", 1)[0]
            for cmd in self.tree.walk_commands()
            if isinstance(cmd, app_commands.Command)
        }
        if not local_root_names:
            return 0

        removed_count = 0
        global_commands = await self.tree.fetch_commands()
        for global_command in global_commands:
            if global_command.name not in local_root_names:
                continue
            try:
                await global_command.delete()
                removed_count += 1
            except discord.HTTPException:
                LOGGER.warning(
                    "Nao foi possivel remover comando global '%s' (id=%s).",
                    global_command.name,
                    global_command.id,
                )
        return removed_count

    async def setup_hook(self) -> None:
        await self.warn_store.connect()
        LOGGER.info(
            "MySQL conectado em %s:%s/%s",
            self.warn_store.config.host,
            self.warn_store.config.port,
            self.warn_store.config.database,
        )

        for extension in EXTENSIONS:
            await self.load_extension(extension)
            LOGGER.info("Extensao carregada: %s", extension)

        try:
            if self.sync_guild_id:
                guild = discord.Object(id=self.sync_guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                removed_globals = await self._delete_overlapping_global_commands()
                LOGGER.info(
                    "Comandos sincronizados na guild %s: %s",
                    self.sync_guild_id,
                    len(synced),
                )
                if removed_globals:
                    LOGGER.info(
                        "Comandos globais removidos para evitar duplicacao na guild: %s",
                        removed_globals,
                    )
            else:
                synced = await self.tree.sync()
                LOGGER.info("Comandos globais sincronizados: %s", len(synced))
        except Exception as exc:
            LOGGER.error(
                "Falha ao sincronizar comandos.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def on_ready(self) -> None:
        if self.user is None:
            return
        LOGGER.info("Logado como %s (id=%s)", self.user, self.user.id)

    async def close(self) -> None:
        try:
            await self.warn_store.close()
            LOGGER.info("Pool MySQL finalizado.")
        except Exception as exc:
            LOGGER.warning(
                "Falha ao finalizar pool MySQL.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        await super().close()

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await send_ephemeral(
                interaction,
                "Voce nao tem permissao para usar este comando.",
            )
            return

        if isinstance(error, app_commands.BotMissingPermissions):
            await send_ephemeral(
                interaction,
                "Eu nao tenho permissao para executar este comando.",
            )
            return

        if isinstance(error, app_commands.NoPrivateMessage):
            await send_ephemeral(
                interaction,
                "Este comando so funciona dentro de servidor.",
            )
            return

        if isinstance(error, app_commands.CommandOnCooldown):
            await send_ephemeral(
                interaction,
                f"Comando em cooldown. Tente novamente em {error.retry_after:.1f}s.",
            )
            return

        if isinstance(error, app_commands.CheckFailure):
            await send_ephemeral(
                interaction,
                "Voce nao passou na validacao deste comando.",
            )
            return

        root_error = error.original if isinstance(error, app_commands.CommandInvokeError) else error
        command_name = interaction.command.qualified_name if interaction.command else "desconhecido"
        LOGGER.error(
            "Erro nao tratado no comando /%s",
            command_name,
            exc_info=(type(root_error), root_error, root_error.__traceback__),
        )
        await send_ephemeral(
            interaction,
            "Ocorreu um erro inesperado ao executar o comando.",
        )


def main() -> None:
    ensure_utf8_runtime()
    load_dotenv()
    setup_logging()

    token = sanitize_token(os.getenv("DISCORD_TOKEN"))
    guild_id = parse_discord_id(os.getenv("GUILD_ID"))
    owner_id = parse_discord_id(os.getenv("DONO_ID"))
    members_intent_enabled = parse_bool_env(
        os.getenv("ENABLE_MEMBERS_INTENT"),
        "ENABLE_MEMBERS_INTENT",
        default=False,
    )
    message_content_intent_enabled = parse_bool_env(
        os.getenv("ENABLE_MESSAGE_CONTENT_INTENT"),
        "ENABLE_MESSAGE_CONTENT_INTENT",
        default=False,
    )
    mysql_config = load_mysql_config_from_env()

    if not token:
        raise RuntimeError("A variavel DISCORD_TOKEN nao foi encontrada no .env.")
    if not looks_like_discord_token(token):
        raise RuntimeError(
            "DISCORD_TOKEN parece invalido. Use o token do Bot em Developer Portal > Bot > Reset Token."
        )
    if os.getenv("GUILD_ID") and guild_id is None:
        LOGGER.warning("GUILD_ID invalido. Sync sera global.")
    if os.getenv("DONO_ID") and owner_id is None:
        LOGGER.warning("DONO_ID invalido. owner_id nao sera definido.")
    if not members_intent_enabled:
        LOGGER.warning(
            "ENABLE_MEMBERS_INTENT desativado: welcome por entrada e operacoes em massa podem ficar limitados."
        )
    if not message_content_intent_enabled:
        LOGGER.warning(
            "ENABLE_MESSAGE_CONTENT_INTENT desativado: AutoMod por conteudo e leveling por mensagens ficam limitados."
        )

    bot = AyanaBot(
        guild_id=guild_id,
        owner_id=owner_id,
        warn_store=WarnStore(mysql_config),
        members_intent_enabled=members_intent_enabled,
        message_content_intent_enabled=message_content_intent_enabled,
    )
    try:
        bot.run(token, log_handler=None)
    except discord.errors.PrivilegedIntentsRequired as exc:
        raise RuntimeError(
            "Intents privilegiados nao habilitados no Developer Portal. "
            "Habilite os intents necessarios no portal ou defina "
            "ENABLE_MEMBERS_INTENT/ENABLE_MESSAGE_CONTENT_INTENT=false no .env."
        ) from exc


if __name__ == "__main__":
    main()
