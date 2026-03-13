import asyncio
import logging
import os
import re
import shutil
import tempfile
import uuid
from collections import deque
from dataclasses import dataclass, field

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

try:
    import yt_dlp
except ImportError:  # pragma: no cover - optional dependency in runtime
    yt_dlp = None

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
DEFAULT_VOLUME = 0.6
MAX_QUEUE_ITEMS = 100

YTDLP_OPTIONS = {
    "format": "bestaudio/best",
    "default_search": "ytsearch",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
    "skip_download": True,
}
# `-reconnect*` quebra em alguns builds/inputs locais; usamos opcoes seguras para arquivos e streams.
FFMPEG_BEFORE_OPTIONS = "-nostdin"
FFMPEG_OPTIONS = "-vn"


@dataclass(slots=True)
class MusicTrack:
    title: str
    stream_url: str
    webpage_url: str
    requester_id: int
    duration_seconds: int | None = None
    thumbnail_url: str | None = None
    cleanup_path: str | None = None


@dataclass(slots=True)
class GuildMusicPlayer:
    guild_id: int
    voice_client: discord.VoiceClient | None = None
    announce_channel_id: int | None = None
    current: MusicTrack | None = None
    queue: deque[MusicTrack] = field(default_factory=deque)
    queue_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    queue_event: asyncio.Event = field(default_factory=asyncio.Event)
    next_track_event: asyncio.Event = field(default_factory=asyncio.Event)
    loop_task: asyncio.Task[None] | None = None
    last_play_error: Exception | None = None
    volume: float = DEFAULT_VOLUME


class MusicCog(commands.Cog):
    music = app_commands.Group(name="music", description="Comandos para reproduzir musica em canal de voz.")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._players: dict[int, GuildMusicPlayer] = {}

    def cog_unload(self) -> None:
        for player in list(self._players.values()):
            if player.loop_task and not player.loop_task.done():
                player.loop_task.cancel()
            if player.voice_client and player.voice_client.is_connected():
                asyncio.create_task(player.voice_client.disconnect(force=True))
        self._players.clear()

    @staticmethod
    def _is_url(value: str) -> bool:
        return bool(URL_RE.match(value.strip()))

    @staticmethod
    def _format_duration(total_seconds: int | None) -> str:
        if total_seconds is None or total_seconds <= 0:
            return "ao vivo/desconhecida"
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    @staticmethod
    async def _respond(interaction: discord.Interaction, message: str, *, ephemeral: bool = False) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
            return
        await interaction.response.send_message(message, ephemeral=ephemeral)

    def _ffmpeg_binary(self) -> str:
        configured = os.getenv("FFMPEG_BINARY", "ffmpeg").strip()
        return configured or "ffmpeg"

    def _ytdls_api_base_url(self) -> str | None:
        configured = os.getenv("YTDLS_API_BASE_URL")
        if configured is None:
            return None

        normalized = configured.strip().rstrip("/")
        if not normalized:
            return None
        return normalized

    @staticmethod
    def _friendly_ytdls_error(status_code: int, payload: str) -> str:
        lowered = payload.lower()
        if "anti-bot" in lowered or "cookies" in lowered or "not a bot" in lowered:
            return (
                "A API de yt-dl recusou esta faixa por verificacao anti-bot/cookies. "
                "Renove os cookies da API e tente novamente."
            )
        if status_code == 429:
            return "A API de yt-dl esta com fila cheia no momento. Tente novamente em instantes."
        if status_code == 413:
            return "A API de yt-dl bloqueou porque o arquivo ultrapassa o limite permitido."
        return f"A API de yt-dl retornou erro {status_code}. Tente novamente."

    @staticmethod
    def _cleanup_track_file(track: MusicTrack) -> None:
        cleanup_path = track.cleanup_path
        if not cleanup_path:
            return
        try:
            if os.path.exists(cleanup_path):
                os.remove(cleanup_path)
        except OSError:
            LOGGER.warning("Falha ao remover arquivo temporario de musica: %s", cleanup_path)

    def _dependency_issues(self) -> list[str]:
        issues: list[str] = []
        if yt_dlp is None and not self._ytdls_api_base_url():
            issues.append("Dependencia ausente: instale `yt-dlp` (`pip install yt-dlp`).")
        if nacl is None:
            issues.append("Dependencia ausente: instale `PyNaCl` (`pip install PyNaCl`).")
        if discord.version_info >= (2, 7, 0) and davey is None:
            issues.append("Dependencia ausente: instale `davey` (`pip install davey`).")

        ffmpeg_binary = self._ffmpeg_binary()
        ffmpeg_path = shutil.which(ffmpeg_binary)
        if ffmpeg_path is None:
            issues.append(
                f"FFmpeg nao encontrado. Instale o binario e/ou defina `FFMPEG_BINARY` (atual: `{ffmpeg_binary}`)."
            )
        return issues

    @staticmethod
    def _extract_track_sync(query: str) -> MusicTrack:
        if yt_dlp is None:
            raise RuntimeError("yt-dlp nao esta disponivel.")

        source_query = query if URL_RE.match(query.strip()) else f"ytsearch1:{query}"

        try:
            with yt_dlp.YoutubeDL(YTDLP_OPTIONS) as ydl:
                info = ydl.extract_info(source_query, download=False)
        except Exception as exc:
            lowered = str(exc).lower()
            if "sign in to confirm" in lowered or "use --cookies" in lowered:
                raise RuntimeError(
                    "YouTube bloqueou esta faixa por anti-bot. Use a API de yt-dl com cookies para reproduzir."
                ) from exc
            raise

        if info is None:
            raise RuntimeError("Nenhum resultado encontrado.")

        if "entries" in info:
            entries = info.get("entries") or []
            info = next((entry for entry in entries if entry), None)
            if info is None:
                raise RuntimeError("Nenhum resultado encontrado para a busca.")

        stream_url = info.get("url")
        if not stream_url:
            raise RuntimeError("Nao foi possivel obter stream de audio para essa faixa.")

        title = str(info.get("title") or "Faixa desconhecida")
        webpage_url = str(info.get("webpage_url") or info.get("original_url") or query)
        duration = info.get("duration")
        duration_seconds = int(duration) if isinstance(duration, (int, float)) and duration > 0 else None
        thumbnail_url = info.get("thumbnail")
        safe_thumbnail = str(thumbnail_url) if isinstance(thumbnail_url, str) else None

        return MusicTrack(
            title=title,
            stream_url=str(stream_url),
            webpage_url=webpage_url,
            requester_id=0,
            duration_seconds=duration_seconds,
            thumbnail_url=safe_thumbnail,
        )

    async def _extract_track_via_ytdls_api(self, api_base_url: str, query: str, requester_id: int) -> MusicTrack:
        timeout = aiohttp.ClientTimeout(total=240, connect=20, sock_read=240)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{api_base_url}/search", params={"q": query}) as search_response:
                search_payload_text = await search_response.text()
                if search_response.status != 200:
                    raise RuntimeError(
                        self._friendly_ytdls_error(
                            status_code=search_response.status,
                            payload=search_payload_text,
                        )
                    )
                try:
                    search_payload = await search_response.json(content_type=None)
                except Exception as exc:
                    raise RuntimeError("A API de yt-dl retornou resposta invalida em /search.") from exc

            result = search_payload.get("resultado") if isinstance(search_payload, dict) else None
            if not isinstance(result, dict):
                raise RuntimeError("A API de yt-dl nao encontrou resultados para esta busca.")

            link = str(result.get("url") or "").strip()
            if not link:
                video_id = result.get("id")
                if isinstance(video_id, str) and video_id.strip():
                    link = f"https://www.youtube.com/watch?v={video_id.strip()}"
            if not link:
                raise RuntimeError("A API de yt-dl nao retornou URL valida para esta faixa.")

            request_id = f"ayana_{uuid.uuid4().hex[:12]}"
            payload = {"link": link, "type": "audio", "request_id": request_id}
            async with session.post(f"{api_base_url}/download", json=payload) as download_response:
                if download_response.status != 200:
                    response_text = await download_response.text()
                    raise RuntimeError(
                        self._friendly_ytdls_error(
                            status_code=download_response.status,
                            payload=response_text,
                        )
                    )

                temp_dir = os.path.join(tempfile.gettempdir(), "ayana-music")
                os.makedirs(temp_dir, exist_ok=True)
                temp_path = os.path.join(temp_dir, f"{request_id}.mp3")
                try:
                    with open(temp_path, "wb") as output_file:
                        async for chunk in download_response.content.iter_chunked(64 * 1024):
                            output_file.write(chunk)
                except Exception:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    raise

            title = str(result.get("title") or "Faixa desconhecida")
            duration = result.get("duration")
            duration_seconds = int(duration) if isinstance(duration, (int, float)) and duration > 0 else None

            thumbnail_url: str | None = None
            thumbnails = result.get("thumbnails")
            if isinstance(thumbnails, list) and thumbnails:
                first_thumb = thumbnails[0]
                if isinstance(first_thumb, dict):
                    thumb_url = first_thumb.get("url")
                    if isinstance(thumb_url, str):
                        thumbnail_url = thumb_url

            return MusicTrack(
                title=title,
                stream_url=temp_path,
                webpage_url=link,
                requester_id=requester_id,
                duration_seconds=duration_seconds,
                thumbnail_url=thumbnail_url,
                cleanup_path=temp_path,
            )

    async def _extract_track(self, query: str, requester_id: int) -> MusicTrack:
        api_base_url = self._ytdls_api_base_url()
        if api_base_url:
            try:
                return await self._extract_track_via_ytdls_api(api_base_url, query, requester_id)
            except Exception as exc:
                LOGGER.warning(
                    "Falha ao extrair faixa via API yt-dl (%s). Tentando fallback local.",
                    api_base_url,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                if yt_dlp is None:
                    raise RuntimeError(str(exc)) from exc

        track = await asyncio.to_thread(self._extract_track_sync, query)
        track.requester_id = requester_id
        return track

    def _build_source(self, track: MusicTrack, *, volume: float) -> discord.PCMVolumeTransformer:
        ffmpeg_binary = self._ffmpeg_binary()
        audio_source = discord.FFmpegPCMAudio(
            track.stream_url,
            executable=ffmpeg_binary,
            before_options=FFMPEG_BEFORE_OPTIONS,
            options=FFMPEG_OPTIONS,
        )
        return discord.PCMVolumeTransformer(audio_source, volume=max(0.0, min(volume, 2.0)))

    def _get_player(self, guild_id: int) -> GuildMusicPlayer:
        player = self._players.get(guild_id)
        if player is None:
            player = GuildMusicPlayer(guild_id=guild_id)
            self._players[guild_id] = player
        return player

    async def _wait_for_next_track(self, player: GuildMusicPlayer) -> MusicTrack:
        while True:
            async with player.queue_lock:
                if player.queue:
                    return player.queue.popleft()
                player.queue_event.clear()
            await asyncio.wait_for(player.queue_event.wait(), timeout=IDLE_TIMEOUT_SECONDS)

    async def _queue_snapshot(self, player: GuildMusicPlayer) -> list[MusicTrack]:
        async with player.queue_lock:
            return list(player.queue)

    async def _clear_queue(self, player: GuildMusicPlayer) -> int:
        queued_tracks: list[MusicTrack]
        async with player.queue_lock:
            queued_tracks = list(player.queue)
            count = len(queued_tracks)
            player.queue.clear()
            player.queue_event.clear()
        for track in queued_tracks:
            self._cleanup_track_file(track)
        return count

    async def _send_music_message(self, channel_id: int | None, message: str) -> None:
        if channel_id is None:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            await channel.send(message)
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Nao consegui enviar mensagem de musica no canal %s.", channel_id)

    async def _disconnect_player(self, guild_id: int, *, from_loop: bool = False) -> None:
        player = self._players.pop(guild_id, None)
        if player is None:
            return

        await self._clear_queue(player)
        if player.current is not None:
            self._cleanup_track_file(player.current)
        player.current = None
        player.next_track_event.set()

        voice_client = player.voice_client
        if voice_client and voice_client.is_connected():
            try:
                await voice_client.disconnect(force=True)
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Falha ao desconectar voice na guild %s.", guild_id)

        if from_loop:
            return

        if player.loop_task and not player.loop_task.done():
            player.loop_task.cancel()
            try:
                await player.loop_task
            except asyncio.CancelledError:
                pass

    def _start_loop_if_needed(self, player: GuildMusicPlayer) -> None:
        if player.loop_task and not player.loop_task.done():
            return
        player.loop_task = asyncio.create_task(self._player_loop(player), name=f"music-player-{player.guild_id}")

    async def _player_loop(self, player: GuildMusicPlayer) -> None:
        while True:
            try:
                track = await self._wait_for_next_track(player)
            except asyncio.TimeoutError:
                await self._send_music_message(
                    player.announce_channel_id,
                    "Fila vazia por 5 minutos. Saindo do canal de voz.",
                )
                await self._disconnect_player(player.guild_id, from_loop=True)
                return
            except asyncio.CancelledError:
                return

            voice_client = player.voice_client
            if voice_client is None or not voice_client.is_connected():
                await self._send_music_message(
                    player.announce_channel_id,
                    "Perdi conexao com o canal de voz. Use `/music join` e `/music play` novamente.",
                )
                await self._disconnect_player(player.guild_id, from_loop=True)
                return

            player.current = track
            player.last_play_error = None
            player.next_track_event.clear()

            try:
                source = self._build_source(track, volume=player.volume)
            except Exception as exc:
                LOGGER.warning(
                    "Falha ao preparar fonte de audio para %s na guild %s.",
                    track.title,
                    player.guild_id,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                await self._send_music_message(
                    player.announce_channel_id,
                    f"Nao consegui reproduzir `{track.title}`. Pulando para a proxima.",
                )
                self._cleanup_track_file(track)
                player.current = None
                continue

            def _after_play(error: Exception | None) -> None:
                self.bot.loop.call_soon_threadsafe(self._on_after_play, player.guild_id, error)

            try:
                voice_client.play(source, after=_after_play)
            except Exception as exc:
                LOGGER.warning(
                    "Falha ao iniciar reproducao na guild %s.",
                    player.guild_id,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                await self._send_music_message(
                    player.announce_channel_id,
                    f"Nao consegui iniciar `{track.title}`. Pulando para a proxima.",
                )
                self._cleanup_track_file(track)
                player.current = None
                continue

            duration_text = self._format_duration(track.duration_seconds)
            await self._send_music_message(
                player.announce_channel_id,
                (
                    f"Tocando agora: **{track.title}**\n"
                    f"Duracao: `{duration_text}` | Pedido por: <@{track.requester_id}>"
                ),
            )

            await player.next_track_event.wait()
            if player.last_play_error is not None:
                LOGGER.warning(
                    "Erro durante reproducao na guild %s: %s",
                    player.guild_id,
                    player.last_play_error,
                )
            self._cleanup_track_file(track)
            player.current = None

    def _on_after_play(self, guild_id: int, error: Exception | None) -> None:
        player = self._players.get(guild_id)
        if player is None:
            return
        if error is not None:
            player.last_play_error = error
        player.next_track_event.set()

    async def _connect_to_member_channel(
        self,
        guild: discord.Guild,
        member: discord.Member,
        player: GuildMusicPlayer,
    ) -> discord.VoiceClient:
        if member.voice is None or member.voice.channel is None:
            raise RuntimeError("Entre em um canal de voz antes de usar comandos de musica.")

        target_channel = member.voice.channel
        if isinstance(target_channel, discord.VoiceChannel):
            limit = target_channel.user_limit
            is_full = limit > 0 and len(target_channel.members) >= limit
            bot_already_inside = guild.me in target_channel.members if guild.me else False
            if is_full and not bot_already_inside:
                raise RuntimeError("O canal de voz esta lotado. Libere uma vaga e tente novamente.")

        voice_client = guild.voice_client
        if voice_client and voice_client.is_connected():
            if voice_client.channel != target_channel:
                await voice_client.move_to(target_channel)
        else:
            # Fail fast on gateway handshake errors to avoid visible join/leave loops.
            voice_client = await target_channel.connect(self_deaf=True, reconnect=False, timeout=20.0)

        player.voice_client = voice_client
        return voice_client

    @staticmethod
    def _can_control(member: discord.Member, player: GuildMusicPlayer) -> tuple[bool, str | None]:
        user_channel = member.voice.channel if member.voice else None
        voice_client = player.voice_client
        bot_channel = voice_client.channel if voice_client else None

        if user_channel is None:
            return False, "Entre no canal de voz do bot para controlar a fila."
        if bot_channel is None:
            return False, "Nao estou conectado em nenhum canal de voz."
        if user_channel.id != bot_channel.id:
            return False, f"Voce precisa estar em **{bot_channel.name}** para controlar a musica."
        return True, None

    @music.command(
        name="setup",
        description="Diagnostica dependencias de audio (FFmpeg, yt-dlp, PyNaCl e Davey).",
    )
    async def music_setup(self, interaction: discord.Interaction) -> None:
        issues = self._dependency_issues()
        ffmpeg_binary = self._ffmpeg_binary()
        ffmpeg_path = shutil.which(ffmpeg_binary)
        api_base_url = self._ytdls_api_base_url()
        status_lines = [
            f"yt-dlp: {'OK' if yt_dlp is not None else 'FALHOU'}",
            f"PyNaCl: {'OK' if nacl is not None else 'FALHOU'}",
            (
                f"Davey: {'OK' if davey is not None else 'FALHOU'}"
                if discord.version_info >= (2, 7, 0)
                else "Davey: opcional nesta versao do discord.py"
            ),
            f"YTDLS API: {'ATIVA' if api_base_url else 'DESATIVADA'} ({api_base_url or 'nenhuma'})",
            f"FFmpeg: {'OK' if ffmpeg_path is not None else 'FALHOU'} ({ffmpeg_path or ffmpeg_binary})",
        ]

        if issues:
            await self._respond(
                interaction,
                "Diagnostico de musica:\n"
                + "\n".join(f"- {line}" for line in status_lines)
                + "\n\nAjustes necessarios:\n"
                + "\n".join(f"- {issue}" for issue in issues),
                ephemeral=True,
            )
            return

        await self._respond(
            interaction,
            "Diagnostico de musica:\n"
            + "\n".join(f"- {line}" for line in status_lines)
            + "\n\nTudo pronto para tocar audio no canal de voz.",
            ephemeral=True,
        )

    @music.command(name="join", description="Faz o bot entrar no seu canal de voz atual.")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(connect=True, speak=True)
    async def music_join(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando so funciona em servidor.", ephemeral=True)
            return

        issues = self._dependency_issues()
        if issues:
            await self._respond(interaction, "\n".join(f"- {issue}" for issue in issues), ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        player = self._get_player(guild.id)
        player.announce_channel_id = interaction.channel_id

        try:
            voice_client = await self._connect_to_member_channel(guild, member, player)
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
                detail = f" (codigo gateway de voz: {exc.code})"
            await interaction.followup.send(
                "Nao consegui entrar no canal de voz. Verifique permissoes de `Connect` e `Speak`" f"{detail}.",
                ephemeral=True,
            )
            return

        self._start_loop_if_needed(player)
        await interaction.followup.send(f"Conectado em **{voice_client.channel.name}**.")

    @music.command(name="play", description="Adiciona uma musica na fila (URL ou busca).")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(connect=True, speak=True)
    @app_commands.describe(busca_ou_url="URL da faixa/video ou termo de busca.")
    async def music_play(
        self,
        interaction: discord.Interaction,
        busca_ou_url: app_commands.Range[str, 2, 300],
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando so funciona em servidor.", ephemeral=True)
            return

        issues = self._dependency_issues()
        if issues:
            await self._respond(interaction, "\n".join(f"- {issue}" for issue in issues), ephemeral=True)
            return

        query = busca_ou_url.strip()
        if not query:
            await self._respond(interaction, "Informe uma URL ou termo de busca valido.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        player = self._get_player(guild.id)
        player.announce_channel_id = interaction.channel_id

        try:
            await self._connect_to_member_channel(guild, member, player)
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
                detail = f" (codigo gateway de voz: {exc.code})"
            await interaction.followup.send(
                "Nao consegui entrar no canal de voz. Verifique permissoes de `Connect` e `Speak`" f"{detail}.",
                ephemeral=True,
            )
            return

        try:
            track = await self._extract_track(query, member.id)
        except Exception as exc:
            LOGGER.warning(
                "Falha ao extrair faixa para /music play na guild %s.",
                guild.id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            message = "Nao consegui carregar essa faixa. Tente outra URL ou termo de busca."
            if isinstance(exc, RuntimeError):
                detail = str(exc).strip()
                if detail:
                    message = detail
            await interaction.followup.send(
                message,
                ephemeral=True,
            )
            return

        async with player.queue_lock:
            if len(player.queue) >= MAX_QUEUE_ITEMS:
                await interaction.followup.send(
                    f"A fila ja atingiu o limite de {MAX_QUEUE_ITEMS} faixas.",
                    ephemeral=True,
                )
                return
            player.queue.append(track)
            queued_position = len(player.queue) + (1 if player.current else 0)
            player.queue_event.set()

        self._start_loop_if_needed(player)

        duration_text = self._format_duration(track.duration_seconds)
        if queued_position <= 1 and player.current is None:
            message = f"Preparando para tocar: **{track.title}** (`{duration_text}`)."
        else:
            message = (
                f"Adicionado na fila: **{track.title}** (`{duration_text}`)\n" f"Posicao na fila: `{queued_position}`"
            )
        await interaction.followup.send(message)

    @music.command(name="queue", description="Mostra a fila atual de reproducao.")
    @app_commands.guild_only()
    @app_commands.describe(limite="Quantidade de itens a mostrar (1 a 20).")
    async def music_queue(self, interaction: discord.Interaction, limite: app_commands.Range[int, 1, 20] = 10) -> None:
        guild = interaction.guild
        if guild is None:
            await self._respond(interaction, "Este comando so funciona em servidor.", ephemeral=True)
            return

        player = self._players.get(guild.id)
        if player is None:
            await self._respond(interaction, "Nao ha fila ativa nesta guilda.", ephemeral=True)
            return

        pending = await self._queue_snapshot(player)
        lines: list[str] = []
        if player.current:
            lines.append(
                f"Tocando agora: **{player.current.title}** (`{self._format_duration(player.current.duration_seconds)}`)"
            )
        else:
            lines.append("Tocando agora: nada")

        if not pending:
            lines.append("Proximas faixas: fila vazia.")
        else:
            lines.append("Proximas faixas:")
            for index, track in enumerate(pending[:limite], start=1):
                lines.append(f"{index}. **{track.title}** (`{self._format_duration(track.duration_seconds)}`)")
            if len(pending) > limite:
                lines.append(f"... e mais {len(pending) - limite} faixas.")

        await self._respond(interaction, "\n".join(lines))

    @music.command(name="now", description="Mostra a faixa que esta tocando agora.")
    @app_commands.guild_only()
    async def music_now(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await self._respond(interaction, "Este comando so funciona em servidor.", ephemeral=True)
            return

        player = self._players.get(guild.id)
        if player is None or player.current is None:
            await self._respond(interaction, "Nao ha musica tocando agora.", ephemeral=True)
            return

        track = player.current
        await self._respond(
            interaction,
            (
                f"Tocando agora: **{track.title}**\n"
                f"Duracao: `{self._format_duration(track.duration_seconds)}`\n"
                f"Pedido por: <@{track.requester_id}>"
            ),
        )

    @music.command(name="pause", description="Pausa a reproducao atual.")
    @app_commands.guild_only()
    async def music_pause(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando so funciona em servidor.", ephemeral=True)
            return

        player = self._players.get(guild.id)
        if player is None or player.voice_client is None:
            await self._respond(interaction, "Nao ha player ativo nesta guilda.", ephemeral=True)
            return

        allowed, reason = self._can_control(member, player)
        if not allowed:
            await self._respond(interaction, reason or "Acao negada.", ephemeral=True)
            return

        voice_client = player.voice_client
        if not voice_client.is_playing():
            await self._respond(interaction, "Nao ha musica tocando neste momento.", ephemeral=True)
            return

        voice_client.pause()
        await self._respond(interaction, "Musica pausada.")

    @music.command(name="resume", description="Retoma a reproducao pausada.")
    @app_commands.guild_only()
    async def music_resume(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando so funciona em servidor.", ephemeral=True)
            return

        player = self._players.get(guild.id)
        if player is None or player.voice_client is None:
            await self._respond(interaction, "Nao ha player ativo nesta guilda.", ephemeral=True)
            return

        allowed, reason = self._can_control(member, player)
        if not allowed:
            await self._respond(interaction, reason or "Acao negada.", ephemeral=True)
            return

        voice_client = player.voice_client
        if not voice_client.is_paused():
            await self._respond(interaction, "Nenhuma musica pausada para retomar.", ephemeral=True)
            return

        voice_client.resume()
        await self._respond(interaction, "Reproducao retomada.")

    @music.command(name="skip", description="Pula para a proxima musica da fila.")
    @app_commands.guild_only()
    async def music_skip(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando so funciona em servidor.", ephemeral=True)
            return

        player = self._players.get(guild.id)
        if player is None or player.voice_client is None:
            await self._respond(interaction, "Nao ha player ativo nesta guilda.", ephemeral=True)
            return

        allowed, reason = self._can_control(member, player)
        if not allowed:
            await self._respond(interaction, reason or "Acao negada.", ephemeral=True)
            return

        voice_client = player.voice_client
        if not voice_client.is_playing() and not voice_client.is_paused():
            await self._respond(interaction, "Nao ha faixa para pular agora.", ephemeral=True)
            return

        current_title = player.current.title if player.current else "Faixa atual"
        voice_client.stop()
        await self._respond(interaction, f"Pulada: **{current_title}**")

    @music.command(name="stop", description="Para a musica atual e limpa a fila.")
    @app_commands.guild_only()
    async def music_stop(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando so funciona em servidor.", ephemeral=True)
            return

        player = self._players.get(guild.id)
        if player is None or player.voice_client is None:
            await self._respond(interaction, "Nao ha player ativo nesta guilda.", ephemeral=True)
            return

        allowed, reason = self._can_control(member, player)
        if not allowed:
            await self._respond(interaction, reason or "Acao negada.", ephemeral=True)
            return

        cleared = await self._clear_queue(player)
        if player.voice_client.is_playing() or player.voice_client.is_paused():
            player.voice_client.stop()
        await self._respond(interaction, f"Fila limpa e reproducao interrompida. Itens removidos: `{cleared}`.")

    @music.command(name="leave", description="Desconecta o bot do canal de voz e limpa a fila.")
    @app_commands.guild_only()
    async def music_leave(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando so funciona em servidor.", ephemeral=True)
            return

        player = self._players.get(guild.id)
        if player is None:
            await self._respond(interaction, "Nao estou conectado em canal de voz nesta guilda.", ephemeral=True)
            return

        allowed, reason = self._can_control(member, player)
        if not allowed:
            await self._respond(interaction, reason or "Acao negada.", ephemeral=True)
            return

        await self._disconnect_player(guild.id)
        await self._respond(interaction, "Desconectado do canal de voz e fila removida.")

    @music.command(name="volume", description="Ajusta o volume da reproducao (0 a 200).")
    @app_commands.guild_only()
    @app_commands.describe(valor="Novo volume em porcentagem.")
    async def music_volume(self, interaction: discord.Interaction, valor: app_commands.Range[int, 0, 200]) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando so funciona em servidor.", ephemeral=True)
            return

        player = self._players.get(guild.id)
        if player is None or player.voice_client is None:
            await self._respond(interaction, "Nao ha player ativo nesta guilda.", ephemeral=True)
            return

        allowed, reason = self._can_control(member, player)
        if not allowed:
            await self._respond(interaction, reason or "Acao negada.", ephemeral=True)
            return

        player.volume = valor / 100
        source = player.voice_client.source
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = player.volume
        await self._respond(interaction, f"Volume ajustado para `{valor}%`.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
