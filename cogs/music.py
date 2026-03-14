from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

try:
    import wavelink
except ImportError:  # pragma: no cover - optional dependency in runtime
    wavelink = None

try:
    import nacl  # noqa: F401
except ImportError:  # pragma: no cover - optional dependency in runtime
    nacl = None

try:
    import davey  # noqa: F401
except ImportError:  # pragma: no cover - optional dependency in runtime
    davey = None

LOGGER = logging.getLogger("ayana.cogs.music")

URL_RE = re.compile(r"^https?://", re.IGNORECASE)
IDLE_TIMEOUT_SECONDS = 300
DEFAULT_VOLUME_PERCENT = 60
MAX_QUEUE_ITEMS = 100


@dataclass(slots=True)
class GuildMusicState:
    guild_id: int
    announce_channel_id: int | None = None
    idle_task: asyncio.Task[None] | None = None


class MusicCog(commands.Cog):
    music = app_commands.Group(name="music", description="Comandos para reproduzir música em canal de voz.")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._states: dict[int, GuildMusicState] = {}

    async def cog_load(self) -> None:
        await self._ensure_lavalink_node()

    def cog_unload(self) -> None:
        for state in self._states.values():
            self._cancel_idle_task(state)

        if wavelink is None:
            self._states.clear()
            return

        for voice_client in list(self.bot.voice_clients):
            if isinstance(voice_client, wavelink.Player) and voice_client.connected:
                asyncio.create_task(voice_client.disconnect())

        self._states.clear()

    @staticmethod
    async def _respond(interaction: discord.Interaction, message: str, *, ephemeral: bool = False) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
            return
        await interaction.response.send_message(message, ephemeral=ephemeral)

    @staticmethod
    async def _respond_embed(
        interaction: discord.Interaction,
        *,
        embed: discord.Embed,
        message: str | None = None,
        ephemeral: bool = False,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content=message, embed=embed, ephemeral=ephemeral)
            return
        await interaction.response.send_message(content=message, embed=embed, ephemeral=ephemeral)

    @staticmethod
    def _is_url(value: str | None) -> bool:
        if not value:
            return False
        return bool(URL_RE.match(value.strip()))

    @staticmethod
    def _format_duration_ms(length_ms: int | None) -> str:
        if length_ms is None or length_ms <= 0:
            return "ao vivo/desconhecida"

        total_seconds = int(length_ms // 1000)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    @staticmethod
    def _parse_bool_env(raw_value: str | None, default: bool = False) -> bool:
        if raw_value is None:
            return default

        lowered = raw_value.strip().lower()
        truthy = {"1", "true", "yes", "y", "on", "enable", "enabled"}
        falsy = {"0", "false", "no", "n", "off", "disable", "disabled"}

        if lowered in truthy:
            return True
        if lowered in falsy:
            return False
        return default

    def _lavalink_uri(self) -> str:
        return os.getenv("LAVALINK_URI", "http://127.0.0.1:2333").strip()

    def _lavalink_password(self) -> str:
        return os.getenv("LAVALINK_PASSWORD", "").strip()

    def _lavalink_cookies_path(self) -> str | None:
        configured = os.getenv("LAVALINK_COOKIES_PATH")
        if configured is None:
            return None
        normalized = configured.strip()
        return normalized or None

    @staticmethod
    def _node_is_connected(node: object) -> bool:
        status = getattr(node, "status", None)
        if status is None:
            # Some versions don't expose status cleanly. If we have a node object,
            # consider it usable and let runtime operations validate it.
            return True

        status_name = getattr(status, "name", str(status))
        return str(status_name).upper() == "CONNECTED"

    async def _ensure_lavalink_node(self) -> bool:
        if wavelink is None:
            return False

        try:
            existing = wavelink.Pool.get_node()
        except Exception:
            existing = None

        if existing is not None and self._node_is_connected(existing):
            return True

        uri = self._lavalink_uri()
        password = self._lavalink_password()
        if not uri or not password:
            return False

        identifier = os.getenv("LAVALINK_NODE_ID", "ayana-node").strip() or "ayana-node"
        nodes = [wavelink.Node(identifier=identifier, uri=uri, password=password)]

        cache_capacity_raw = os.getenv("LAVALINK_CACHE_CAPACITY", "100")
        try:
            cache_capacity = max(0, int(cache_capacity_raw))
        except ValueError:
            cache_capacity = 100

        try:
            await wavelink.Pool.connect(nodes=nodes, client=self.bot, cache_capacity=cache_capacity)
        except Exception as exc:
            LOGGER.warning(
                "Falha ao conectar no Lavalink (%s).",
                uri,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

        try:
            node = wavelink.Pool.get_node()
        except Exception:
            return False

        return self._node_is_connected(node)

    def _get_state(self, guild_id: int) -> GuildMusicState:
        state = self._states.get(guild_id)
        if state is None:
            state = GuildMusicState(guild_id=guild_id)
            self._states[guild_id] = state
        return state

    def _cancel_idle_task(self, state: GuildMusicState) -> None:
        if state.idle_task and not state.idle_task.done():
            state.idle_task.cancel()
        state.idle_task = None

    async def _send_music_message(
        self,
        channel_id: int | None,
        message: str | None = None,
        *,
        embed: discord.Embed | None = None,
    ) -> None:
        if channel_id is None:
            return
        if message is None and embed is None:
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        try:
            await channel.send(content=message, embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Não consegui enviar mensagem de música no canal %s.", channel_id)

    async def _dependency_issues(self, *, require_connected_node: bool = True) -> list[str]:
        issues: list[str] = []

        if wavelink is None:
            issues.append("Dependência ausente: instale `wavelink` (`pip install wavelink`).")
        if nacl is None:
            issues.append("Dependência ausente: instale `PyNaCl` (`pip install PyNaCl`).")
        if discord.version_info >= (2, 7, 0) and davey is None:
            issues.append("Dependência ausente: instale `davey` (`pip install davey`).")

        if not self._lavalink_uri():
            issues.append("Configuração ausente: defina `LAVALINK_URI`.")
        if not self._lavalink_password():
            issues.append("Configuração ausente: defina `LAVALINK_PASSWORD`.")

        if require_connected_node and not issues:
            connected = await self._ensure_lavalink_node()
            if not connected:
                issues.append(
                    "Não consegui conectar no Lavalink. Verifique `LAVALINK_URI`, `LAVALINK_PASSWORD` e o servidor."
                )

        return issues

    @staticmethod
    def _track_requester_id(track: object) -> int | None:
        extras = getattr(track, "extras", None)
        candidate: object | None = None

        if isinstance(extras, dict):
            candidate = extras.get("requester_id")
        else:
            candidate = getattr(extras, "requester_id", None)
            if candidate is None:
                getter = getattr(extras, "get", None)
                if callable(getter):
                    candidate = getter("requester_id")

        if isinstance(candidate, bool):
            return None
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, float):
            return int(candidate)
        if isinstance(candidate, str) and candidate.strip().isdigit():
            return int(candidate.strip())
        return None

    @staticmethod
    def _set_track_requester_id(track: object, requester_id: int) -> None:
        try:
            setattr(track, "extras", {"requester_id": requester_id})
            return
        except Exception:
            pass

        extras = getattr(track, "extras", None)
        if isinstance(extras, dict):
            extras["requester_id"] = requester_id

    def _build_track_embed(
        self,
        track: object,
        *,
        header: str,
        queue_position: int | None = None,
    ) -> discord.Embed:
        title = str(getattr(track, "title", "Faixa desconhecida") or "Faixa desconhecida")
        uri_value = getattr(track, "uri", None)
        uri = str(uri_value).strip() if isinstance(uri_value, str) and uri_value.strip() else None

        description = f"[{title}]({uri})" if self._is_url(uri) else title
        embed = discord.Embed(title=header, description=description, color=discord.Color.blurple())

        length = getattr(track, "length", None)
        duration_ms = int(length) if isinstance(length, (int, float)) else None
        embed.add_field(name="Duração", value=f"`{self._format_duration_ms(duration_ms)}`", inline=True)

        requester_id = self._track_requester_id(track)
        if requester_id is not None:
            embed.add_field(name="Pedido por", value=f"<@{requester_id}>", inline=True)

        if queue_position is not None:
            embed.add_field(name="Posição", value=f"`{queue_position}`", inline=True)

        author = getattr(track, "author", None)
        if isinstance(author, str) and author.strip():
            embed.add_field(name="Canal", value=author.strip(), inline=True)

        source = getattr(track, "source", None)
        if source is not None:
            source_name = getattr(source, "name", str(source))
            normalized_source = str(source_name).strip()
            if normalized_source:
                embed.add_field(name="Fonte", value=normalized_source, inline=True)

        identifier = getattr(track, "identifier", None)
        if isinstance(identifier, str) and identifier.strip():
            embed.set_footer(text=f"track_id: {identifier.strip()}")

        artwork = getattr(track, "artwork", None)
        artwork_url = str(artwork).strip() if artwork is not None else ""
        if self._is_url(artwork_url):
            embed.set_thumbnail(url=artwork_url)

        return embed

    @staticmethod
    def _queue_length(queue: object) -> int:
        count = getattr(queue, "count", None)
        if isinstance(count, int):
            return count

        try:
            return len(queue)  # type: ignore[arg-type]
        except Exception:
            return 0

    @staticmethod
    def _queue_items(queue: object) -> list[object]:
        try:
            return list(queue)  # type: ignore[arg-type]
        except Exception:
            return []

    def _schedule_idle_disconnect(self, player: "wavelink.Player") -> None:
        guild = player.guild
        state = self._get_state(guild.id)
        self._cancel_idle_task(state)

        async def idle_disconnect_task() -> None:
            try:
                await asyncio.sleep(IDLE_TIMEOUT_SECONDS)

                if not player.connected:
                    return
                if player.playing or player.paused:
                    return
                if not player.queue.is_empty:
                    return

                await self._send_music_message(
                    state.announce_channel_id,
                    "Fila vazia por 5 minutos. Saindo do canal de voz.",
                )
                await player.disconnect()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                LOGGER.warning(
                    "Falha ao desconectar por inatividade na guild %s.",
                    guild.id,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        state.idle_task = asyncio.create_task(
            idle_disconnect_task(),
            name=f"music-idle-disconnect-{guild.id}",
        )

    async def _play_next(self, player: "wavelink.Player") -> bool:
        if player.queue.is_empty:
            self._schedule_idle_disconnect(player)
            return False

        try:
            next_track = player.queue.get()
        except Exception:
            self._schedule_idle_disconnect(player)
            return False

        await player.play(next_track)
        return True

    async def _connect_to_member_channel(
        self,
        guild: discord.Guild,
        member: discord.Member,
        state: GuildMusicState,
    ) -> "wavelink.Player":
        if wavelink is None:
            raise RuntimeError("Dependência `wavelink` não está instalada.")

        if member.voice is None or member.voice.channel is None:
            raise RuntimeError("Entre em um canal de voz antes de usar comandos de música.")

        target_channel = member.voice.channel
        if isinstance(target_channel, discord.VoiceChannel):
            limit = target_channel.user_limit
            is_full = limit > 0 and len(target_channel.members) >= limit
            bot_already_inside = guild.me in target_channel.members if guild.me else False
            if is_full and not bot_already_inside:
                raise RuntimeError("O canal de voz está lotado. Libere uma vaga e tente novamente.")

        voice_client = guild.voice_client
        if voice_client and not isinstance(voice_client, wavelink.Player):
            raise RuntimeError("Já existe outro cliente de voz ativo neste servidor. Desconecte-o e tente novamente.")

        if voice_client and isinstance(voice_client, wavelink.Player) and voice_client.connected:
            player = voice_client
            if player.channel != target_channel:
                await player.move_to(target_channel)
        else:
            player = await target_channel.connect(cls=wavelink.Player, self_deaf=True)

        if hasattr(player, "autoplay") and hasattr(wavelink, "AutoPlayMode"):
            try:
                player.autoplay = wavelink.AutoPlayMode.disabled
            except Exception:
                pass

        current_volume = getattr(player, "volume", 0)
        if not isinstance(current_volume, (int, float)) or current_volume <= 0:
            try:
                await player.set_volume(DEFAULT_VOLUME_PERCENT)
            except Exception:
                pass

        self._cancel_idle_task(state)
        return player

    @staticmethod
    def _can_control(member: discord.Member, player: "wavelink.Player") -> tuple[bool, str | None]:
        user_channel = member.voice.channel if member.voice else None
        bot_channel = player.channel

        if user_channel is None:
            return False, "Entre no canal de voz do bot para controlar a fila."
        if bot_channel is None:
            return False, "Não estou conectado em nenhum canal de voz."
        if user_channel.id != bot_channel.id:
            return False, f"Você precisa estar em **{bot_channel.name}** para controlar a música."
        return True, None

    async def _search_track(self, query: str) -> object:
        if wavelink is None:
            raise RuntimeError("Dependência `wavelink` não está instalada.")

        sanitized = query.strip()
        if not sanitized:
            raise RuntimeError("Informe uma URL ou termo de busca válido.")

        search_query = sanitized if URL_RE.match(sanitized) else f"ytsearch:{sanitized}"
        results = await wavelink.Playable.search(search_query)
        if not results:
            raise RuntimeError("Nenhum resultado encontrado para a busca.")

        if isinstance(results, wavelink.Playlist):
            if not results.tracks:
                raise RuntimeError("Nenhum resultado encontrado para a busca.")
            return results.tracks[0]

        return results[0]

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: object) -> None:
        if wavelink is None:
            return

        player = getattr(payload, "player", None)
        if not isinstance(player, wavelink.Player):
            return

        guild = player.guild
        state = self._get_state(guild.id)
        self._cancel_idle_task(state)

        track = getattr(payload, "original", None) or getattr(payload, "track", None)
        if track is None:
            return

        await self._send_music_message(
            state.announce_channel_id,
            "Reprodução iniciada.",
            embed=self._build_track_embed(track, header="Tocando Agora"),
        )

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: object) -> None:
        if wavelink is None:
            return

        player = getattr(payload, "player", None)
        if not isinstance(player, wavelink.Player):
            return
        if not player.connected:
            return

        try:
            await self._play_next(player)
        except Exception as exc:
            state = self._get_state(player.guild.id)
            await self._send_music_message(
                state.announce_channel_id,
                "Falha ao iniciar a próxima faixa da fila.",
            )
            LOGGER.warning(
                "Falha ao iniciar próxima faixa na guild %s.",
                player.guild.id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: object) -> None:
        if wavelink is None:
            return

        player = getattr(payload, "player", None)
        if not isinstance(player, wavelink.Player):
            return

        state = self._get_state(player.guild.id)
        await self._send_music_message(
            state.announce_channel_id,
            "Falha de reprodução na faixa atual. Tentando a próxima da fila.",
        )

        try:
            await self._play_next(player)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: object) -> None:
        if wavelink is None:
            return

        player = getattr(payload, "player", None)
        if not isinstance(player, wavelink.Player):
            return

        state = self._get_state(player.guild.id)
        await self._send_music_message(
            state.announce_channel_id,
            "A faixa travou durante a reprodução. Pulando para a próxima.",
        )

        try:
            await self._play_next(player)
        except Exception:
            pass

    @music.command(
        name="setup",
        description="Diagnostica dependências de áudio (Lavalink, cookies, PyNaCl e Davey).",
    )
    async def music_setup(self, interaction: discord.Interaction) -> None:
        issues = await self._dependency_issues(require_connected_node=False)
        lavalink_connected = await self._ensure_lavalink_node() if wavelink is not None else False

        cookies_path = self._lavalink_cookies_path()
        cookies_configured = bool(cookies_path)
        cookies_exists = bool(cookies_path and os.path.exists(cookies_path))

        status_lines = [
            f"Wavelink: {'OK' if wavelink is not None else 'FALHOU'}",
            f"PyNaCl: {'OK' if nacl is not None else 'FALHOU'}",
            (
                f"Davey: {'OK' if davey is not None else 'FALHOU'}"
                if discord.version_info >= (2, 7, 0)
                else "Davey: opcional nesta versão do discord.py"
            ),
            f"Lavalink URI: {self._lavalink_uri() or 'nenhuma'}",
            f"Lavalink Node: {'ONLINE' if lavalink_connected else 'OFFLINE'}",
            (
                f"Cookies: OK ({cookies_path})"
                if cookies_exists
                else (
                    f"Cookies: arquivo não encontrado ({cookies_path})"
                    if cookies_configured
                    else "Cookies: não configurado (defina `LAVALINK_COOKIES_PATH`)"
                )
            ),
        ]

        setup_warnings: list[str] = []
        if cookies_configured and not cookies_exists:
            setup_warnings.append("O caminho de cookies informado não existe no host do bot.")
        if not cookies_configured:
            setup_warnings.append("Cookies não configurados: defina `LAVALINK_COOKIES_PATH` para reduzir bloqueios anti-bot.")
        if wavelink is not None and not lavalink_connected:
            setup_warnings.append("Lavalink offline: verifique se o servidor está ativo e acessível.")

        if issues or setup_warnings:
            lines = [
                "Diagnóstico de música:\n" + "\n".join(f"- {line}" for line in status_lines),
            ]
            if issues:
                lines.append("Ajustes obrigatórios:\n" + "\n".join(f"- {issue}" for issue in issues))
            if setup_warnings:
                lines.append("Recomendações:\n" + "\n".join(f"- {warning}" for warning in setup_warnings))
            await self._respond(interaction, "\n\n".join(lines), ephemeral=True)
            return

        await self._respond(
            interaction,
            "Diagnóstico de música:\n"
            + "\n".join(f"- {line}" for line in status_lines)
            + "\n\nTudo pronto para tocar áudio com Lavalink.",
            ephemeral=True,
        )

    @music.command(name="join", description="Faz o bot entrar no seu canal de voz atual.")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(connect=True, speak=True)
    async def music_join(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        issues = await self._dependency_issues(require_connected_node=True)
        if issues:
            await self._respond(interaction, "\n".join(f"- {issue}" for issue in issues), ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        state = self._get_state(guild.id)
        state.announce_channel_id = interaction.channel_id

        try:
            player = await self._connect_to_member_channel(guild, member, state)
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except (discord.ConnectionClosed, discord.ClientException, discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning(
                "Falha ao conectar no canal de voz na guild %s.",
                guild.id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            detail = ""
            if isinstance(exc, discord.ConnectionClosed):
                detail = f" (código gateway de voz: {exc.code})"
            await interaction.followup.send(
                "Não consegui entrar no canal de voz. Verifique permissões de `Connect` e `Speak`" f"{detail}.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(f"Conectado em **{player.channel.name}**.")

    @music.command(name="play", description="Adiciona uma música na fila (URL ou busca).")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(connect=True, speak=True)
    @app_commands.describe(busca_ou_url="URL da faixa/vídeo ou termo de busca.")
    async def music_play(
        self,
        interaction: discord.Interaction,
        busca_ou_url: app_commands.Range[str, 2, 300],
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        issues = await self._dependency_issues(require_connected_node=True)
        if issues:
            await self._respond(interaction, "\n".join(f"- {issue}" for issue in issues), ephemeral=True)
            return

        query = busca_ou_url.strip()
        if not query:
            await self._respond(interaction, "Informe uma URL ou termo de busca válido.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        state = self._get_state(guild.id)
        state.announce_channel_id = interaction.channel_id

        try:
            player = await self._connect_to_member_channel(guild, member, state)
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except (discord.ConnectionClosed, discord.ClientException, discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning(
                "Falha ao conectar no canal de voz antes de tocar na guild %s.",
                guild.id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            detail = ""
            if isinstance(exc, discord.ConnectionClosed):
                detail = f" (código gateway de voz: {exc.code})"
            await interaction.followup.send(
                "Não consegui entrar no canal de voz. Verifique permissões de `Connect` e `Speak`" f"{detail}.",
                ephemeral=True,
            )
            return

        queue_len = self._queue_length(player.queue)
        if queue_len >= MAX_QUEUE_ITEMS:
            await interaction.followup.send(
                f"A fila já atingiu o limite de {MAX_QUEUE_ITEMS} faixas.",
                ephemeral=True,
            )
            return

        try:
            track = await self._search_track(query)
        except Exception as exc:
            LOGGER.warning(
                "Falha ao buscar faixa para /music play na guild %s.",
                guild.id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            message = "Não consegui carregar essa faixa. Tente outra URL ou termo de busca."
            detail = str(exc).strip()
            if detail:
                message = detail
            await interaction.followup.send(message, ephemeral=True)
            return

        self._set_track_requester_id(track, member.id)

        was_idle = player.current is None and not player.playing and not player.paused and player.queue.is_empty
        await player.queue.put_wait(track)

        if was_idle:
            try:
                await self._play_next(player)
            except Exception as exc:
                LOGGER.warning(
                    "Falha ao iniciar reprodução na guild %s.",
                    guild.id,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                await interaction.followup.send(
                    "Não consegui iniciar essa faixa agora. Tente novamente em instantes.",
                    ephemeral=True,
                )
                return
            header = "Preparando para tocar"
            queue_position = None
        else:
            header = "Adicionado na Fila"
            queue_position = self._queue_length(player.queue) + (1 if player.current else 0)

        await interaction.followup.send(
            embed=self._build_track_embed(
                track,
                header=header,
                queue_position=queue_position,
            )
        )

    @music.command(name="queue", description="Mostra a fila atual de reprodução.")
    @app_commands.guild_only()
    @app_commands.describe(limite="Quantidade de itens a mostrar (1 a 20).")
    async def music_queue(self, interaction: discord.Interaction, limite: app_commands.Range[int, 1, 20] = 10) -> None:
        guild = interaction.guild
        if guild is None:
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        if wavelink is None:
            await self._respond(interaction, "Dependência `wavelink` não está instalada.", ephemeral=True)
            return

        voice_client = guild.voice_client
        if voice_client is None or not isinstance(voice_client, wavelink.Player) or not voice_client.connected:
            await self._respond(interaction, "Não há fila ativa nesta guilda.", ephemeral=True)
            return

        player = voice_client
        pending = self._queue_items(player.queue)
        lines: list[str] = []

        if player.current:
            current_length = getattr(player.current, "length", None)
            current_ms = int(current_length) if isinstance(current_length, (int, float)) else None
            lines.append(f"Tocando agora: **{player.current.title}** (`{self._format_duration_ms(current_ms)}`)")
        else:
            lines.append("Tocando agora: nada")

        if not pending:
            lines.append("Próximas faixas: fila vazia.")
        else:
            lines.append("Próximas faixas:")
            for index, track in enumerate(pending[:limite], start=1):
                track_length = getattr(track, "length", None)
                duration_ms = int(track_length) if isinstance(track_length, (int, float)) else None
                track_title = str(getattr(track, "title", "Faixa desconhecida") or "Faixa desconhecida")
                lines.append(f"{index}. **{track_title}** (`{self._format_duration_ms(duration_ms)}`)")
            if len(pending) > limite:
                lines.append(f"... e mais {len(pending) - limite} faixas.")

        await self._respond(interaction, "\n".join(lines))

    @music.command(name="now", description="Mostra a faixa que está tocando agora.")
    @app_commands.guild_only()
    async def music_now(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        if wavelink is None:
            await self._respond(interaction, "Dependência `wavelink` não está instalada.", ephemeral=True)
            return

        voice_client = guild.voice_client
        if voice_client is None or not isinstance(voice_client, wavelink.Player) or voice_client.current is None:
            await self._respond(interaction, "Não há música tocando agora.", ephemeral=True)
            return

        await self._respond_embed(
            interaction,
            embed=self._build_track_embed(voice_client.current, header="Tocando Agora"),
        )

    @music.command(name="pause", description="Pausa a reprodução atual.")
    @app_commands.guild_only()
    async def music_pause(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        if wavelink is None:
            await self._respond(interaction, "Dependência `wavelink` não está instalada.", ephemeral=True)
            return

        voice_client = guild.voice_client
        if voice_client is None or not isinstance(voice_client, wavelink.Player):
            await self._respond(interaction, "Não há player ativo nesta guilda.", ephemeral=True)
            return

        player = voice_client
        allowed, reason = self._can_control(member, player)
        if not allowed:
            await self._respond(interaction, reason or "Ação negada.", ephemeral=True)
            return

        if not player.playing:
            await self._respond(interaction, "Não há música tocando neste momento.", ephemeral=True)
            return

        await player.pause(True)
        await self._respond(interaction, "Música pausada.")

    @music.command(name="resume", description="Retoma a reprodução pausada.")
    @app_commands.guild_only()
    async def music_resume(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        if wavelink is None:
            await self._respond(interaction, "Dependência `wavelink` não está instalada.", ephemeral=True)
            return

        voice_client = guild.voice_client
        if voice_client is None or not isinstance(voice_client, wavelink.Player):
            await self._respond(interaction, "Não há player ativo nesta guilda.", ephemeral=True)
            return

        player = voice_client
        allowed, reason = self._can_control(member, player)
        if not allowed:
            await self._respond(interaction, reason or "Ação negada.", ephemeral=True)
            return

        if not player.paused:
            await self._respond(interaction, "Nenhuma música pausada para retomar.", ephemeral=True)
            return

        await player.pause(False)
        await self._respond(interaction, "Reprodução retomada.")

    @music.command(name="skip", description="Pula para a próxima música da fila.")
    @app_commands.guild_only()
    async def music_skip(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        if wavelink is None:
            await self._respond(interaction, "Dependência `wavelink` não está instalada.", ephemeral=True)
            return

        voice_client = guild.voice_client
        if voice_client is None or not isinstance(voice_client, wavelink.Player):
            await self._respond(interaction, "Não há player ativo nesta guilda.", ephemeral=True)
            return

        player = voice_client
        allowed, reason = self._can_control(member, player)
        if not allowed:
            await self._respond(interaction, reason or "Ação negada.", ephemeral=True)
            return

        if not player.playing and not player.paused:
            await self._respond(interaction, "Não há faixa para pular agora.", ephemeral=True)
            return

        current_title = str(getattr(player.current, "title", "Faixa atual") or "Faixa atual")

        try:
            await player.skip(force=True)
        except TypeError:
            await player.skip()
        except Exception:
            await player.stop()

        await self._respond(interaction, f"Pulada: **{current_title}**")

    @music.command(name="stop", description="Para a música atual e limpa a fila.")
    @app_commands.guild_only()
    async def music_stop(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        if wavelink is None:
            await self._respond(interaction, "Dependência `wavelink` não está instalada.", ephemeral=True)
            return

        voice_client = guild.voice_client
        if voice_client is None or not isinstance(voice_client, wavelink.Player):
            await self._respond(interaction, "Não há player ativo nesta guilda.", ephemeral=True)
            return

        player = voice_client
        allowed, reason = self._can_control(member, player)
        if not allowed:
            await self._respond(interaction, reason or "Ação negada.", ephemeral=True)
            return

        cleared = self._queue_length(player.queue)
        player.queue.clear()

        if player.playing or player.paused:
            await player.stop()

        await self._respond(interaction, f"Fila limpa e reprodução interrompida. Itens removidos: `{cleared}`.")

    @music.command(name="leave", description="Desconecta o bot do canal de voz e limpa a fila.")
    @app_commands.guild_only()
    async def music_leave(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        if wavelink is None:
            await self._respond(interaction, "Dependência `wavelink` não está instalada.", ephemeral=True)
            return

        voice_client = guild.voice_client
        if voice_client is None or not isinstance(voice_client, wavelink.Player):
            await self._respond(interaction, "Não estou conectado em canal de voz nesta guilda.", ephemeral=True)
            return

        player = voice_client
        allowed, reason = self._can_control(member, player)
        if not allowed:
            await self._respond(interaction, reason or "Ação negada.", ephemeral=True)
            return

        player.queue.clear()
        await player.disconnect()

        state = self._get_state(guild.id)
        self._cancel_idle_task(state)

        await self._respond(interaction, "Desconectado do canal de voz e fila removida.")

    @music.command(name="volume", description="Ajusta o volume da reprodução (0 a 200).")
    @app_commands.guild_only()
    @app_commands.describe(valor="Novo volume em porcentagem.")
    async def music_volume(self, interaction: discord.Interaction, valor: app_commands.Range[int, 0, 200]) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        if wavelink is None:
            await self._respond(interaction, "Dependência `wavelink` não está instalada.", ephemeral=True)
            return

        voice_client = guild.voice_client
        if voice_client is None or not isinstance(voice_client, wavelink.Player):
            await self._respond(interaction, "Não há player ativo nesta guilda.", ephemeral=True)
            return

        player = voice_client
        allowed, reason = self._can_control(member, player)
        if not allowed:
            await self._respond(interaction, reason or "Ação negada.", ephemeral=True)
            return

        await player.set_volume(int(valor))
        await self._respond(interaction, f"Volume ajustado para `{valor}%`.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
