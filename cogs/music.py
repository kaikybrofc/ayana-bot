from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

try:
    import yt_dlp
except ImportError:  # pragma: no cover - optional dependency in runtime
    yt_dlp = None  # type: ignore[assignment]

try:
    import nacl  # noqa: F401
except ImportError:  # pragma: no cover - optional dependency in runtime
    nacl = None

LOGGER = logging.getLogger("ayana.cogs.music")

URL_RE = re.compile(r"^https?://", re.IGNORECASE)
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

IDLE_TIMEOUT_SECONDS = 300
DEFAULT_VOLUME_PERCENT = 60
MAX_QUEUE_ITEMS = 100
MUSIC_API_BASE_URL_DEFAULT = "http://127.0.0.1:3013"
MUSIC_API_CONNECT_TIMEOUT_SECONDS = 2.5
MUSIC_API_REQUEST_TIMEOUT_SECONDS = 14
MUSIC_API_PREFETCH_TIMEOUT_SECONDS = 4
MUSIC_API_PREFETCH_WAIT_SECONDS = 1.2
MUSIC_API_PROBE_TIMEOUT_SECONDS = 2.0
LOCAL_RESOLVE_CACHE_TTL_SECONDS = 900
LOCAL_RESOLVE_CACHE_MAX_ITEMS = 4000
STREAM_URL_REFRESH_WINDOW_SECONDS = 90
YTDLP_RESOLVE_TIMEOUT_SECONDS = 11
YTDLP_SEARCH_TIMEOUT_SECONDS = 6
YTDLP_PLAYBACK_TIMEOUT_SECONDS = 8
YTDLP_ERROR_SNIPPET_LIMIT = 600
MAX_SEARCH_CANDIDATES = 1
PLAY_COMMAND_RESOLVE_TIMEOUT_SECONDS = 26
PLAY_COMMAND_INTERNAL_SAFETY_SECONDS = 2.0
YTDLP_PLAYBACK_RETRIES = 0
YTDLP_PRIMARY_FORMAT_SELECTOR = "bestaudio[ext=webm]/bestaudio/best/best"
YTDLP_FALLBACK_FORMAT_SELECTORS = (
    "best",
)
YTDLP_SEARCH_RESULTS = 1


@dataclass(slots=True)
class QueueTrack:
    identifier: str
    title: str
    author: str
    duration_ms: int | None
    webpage_url: str
    stream_url: str
    thumbnail_url: str | None
    requester_id: int
    search_query: str
    lookup_key: str = ""
    original_input: str = ""
    stream_expires_at: float = 0.0
    play_attempts: int = 0
    prefetch_state: str = "idle"
    resolved_at: float = 0.0
    source: str = "youtube"


@dataclass(slots=True)
class ResolveCacheEntry:
    track: QueueTrack
    expires_at: float


class MusicApiRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class GuildMusicState:
    guild_id: int
    announce_channel_id: int | None = None
    idle_task: asyncio.Task[None] | None = None
    queue: deque[QueueTrack] = field(default_factory=deque)
    current: QueueTrack | None = None
    current_source: discord.PCMVolumeTransformer[discord.FFmpegPCMAudio] | None = None
    volume_percent: int = DEFAULT_VOLUME_PERCENT
    playback_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MusicCog(commands.Cog):
    music = app_commands.Group(name="music", description="Comandos para reproduzir música em canal de voz.")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._states: dict[int, GuildMusicState] = {}
        self._resolve_cache: dict[str, ResolveCacheEntry] = {}
        self._resolve_inflight: dict[str, asyncio.Task[QueueTrack]] = {}
        self._resolve_inflight_lock = asyncio.Lock()
        self._http_session: aiohttp.ClientSession | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def cog_unload(self) -> None:
        for state in self._states.values():
            self._cancel_idle_task(state)

        for voice_client in list(self.bot.voice_clients):
            if isinstance(voice_client, discord.VoiceClient) and voice_client.is_connected():
                asyncio.create_task(voice_client.disconnect(force=True))

        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()

        if self._http_session is not None and not self._http_session.closed:
            asyncio.create_task(self._http_session.close())
        self._http_session = None

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
    def _extract_youtube_video_id(url: str) -> str | None:
        try:
            parsed = urlparse(url.strip())
        except Exception:
            return None

        host = parsed.netloc.lower().split(":", 1)[0]
        path = parsed.path.strip("/")
        if not host:
            return None

        candidate: str | None = None

        if host in {"youtu.be", "www.youtu.be"}:
            if path:
                candidate = path.split("/", 1)[0]
        elif host.endswith("youtube.com"):
            query_id = parse_qs(parsed.query).get("v", [None])[0]
            if isinstance(query_id, str) and query_id:
                candidate = query_id
            else:
                for prefix in ("shorts/", "embed/", "live/"):
                    if path.startswith(prefix):
                        candidate = path[len(prefix) :].split("/", 1)[0]
                        break

        if not candidate:
            return None

        normalized = candidate.strip()
        if YOUTUBE_ID_RE.fullmatch(normalized):
            return normalized
        return None

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

    def _ffmpeg_path(self) -> str:
        configured = os.getenv("FFMPEG_PATH")
        if configured and configured.strip():
            return configured.strip()
        return "ffmpeg"

    @staticmethod
    def _music_api_base_url() -> str:
        configured = os.getenv("MUSIC_API_BASE_URL", MUSIC_API_BASE_URL_DEFAULT).strip()
        if not configured:
            return MUSIC_API_BASE_URL_DEFAULT
        return configured.rstrip("/")

    async def _http_client(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(
                total=MUSIC_API_REQUEST_TIMEOUT_SECONDS,
                connect=MUSIC_API_CONNECT_TIMEOUT_SECONDS,
            )
            connector = aiohttp.TCPConnector(
                limit=200,
                ttl_dns_cache=300,
                keepalive_timeout=30,
                enable_cleanup_closed=True,
            )
            self._http_session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={"User-Agent": "ayana-bot/1.0"},
            )
        return self._http_session

    def _ytdlp_path(self) -> str:
        configured = os.getenv("MUSIC_YTDLP_PATH")
        if configured and configured.strip():
            return configured.strip()

        local_default = "/root/.local/bin/yt-dlp"
        if os.path.exists(local_default):
            return local_default
        return "yt-dlp"

    def _ytdlp_runtime(self) -> str:
        configured = os.getenv("MUSIC_YTDLP_JS_RUNTIME")
        if configured and configured.strip():
            return configured.strip()
        return "node"

    def _ytdlp_cookies_path(self) -> str | None:
        configured = os.getenv("MUSIC_YTDLP_COOKIES_PATH")
        if configured is not None and not configured.strip():
            return None

        candidates: list[str] = []
        if configured:
            normalized = configured.strip()
            if normalized:
                candidates.append(normalized)

        # Local padrão do projeto para cookies do yt-dlp.
        candidates.append("/root/ayana-bot/cookies.txt")

        deduplicated: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            deduplicated.append(candidate)
            seen.add(candidate)

        for candidate in deduplicated:
            if os.path.exists(candidate):
                return candidate

        if deduplicated:
            # Retorna o primeiro para diagnóstico claro em /music setup.
            return deduplicated[0]
        return None

    @staticmethod
    def _path_available(executable: str) -> bool:
        if os.path.sep in executable:
            return os.path.exists(executable)
        return shutil.which(executable) is not None

    def _create_background_task(self, coro: Any, *, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)

        def _done_callback(finished: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(finished)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                LOGGER.warning(
                    "Task em background falhou (%s).",
                    name,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_done_callback)

    async def _probe_music_api(self) -> None:
        base_url = self._music_api_base_url()
        if not self._is_url(base_url):
            raise RuntimeError(f"MUSIC_API_BASE_URL inválida: `{base_url}`.")

        session = await self._http_client()
        timeout = aiohttp.ClientTimeout(total=MUSIC_API_PROBE_TIMEOUT_SECONDS)
        try:
            async with session.get(base_url, timeout=timeout) as response:
                if response.status >= 500:
                    raise RuntimeError(
                        f"API de música indisponível em `{base_url}` (status HTTP {response.status})."
                    )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"Timeout ao conectar na API de música `{base_url}`.") from exc
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"Não consegui conectar na API de música `{base_url}`.") from exc

    async def _dependency_issues(self, *, include_api_probe: bool = False) -> list[str]:
        issues: list[str] = []

        if nacl is None:
            issues.append("Dependência ausente: instale `PyNaCl` (`pip install PyNaCl`).")

        ffmpeg_path = self._ffmpeg_path()
        if not self._path_available(ffmpeg_path):
            issues.append(f"FFmpeg não encontrado em `{ffmpeg_path}`.")

        base_url = self._music_api_base_url()
        if not self._is_url(base_url):
            issues.append(f"MUSIC_API_BASE_URL inválida: `{base_url}`.")
        elif include_api_probe:
            try:
                await self._probe_music_api()
            except RuntimeError as exc:
                issues.append(str(exc))

        return issues

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

    def _build_track_embed(
        self,
        track: QueueTrack,
        *,
        header: str,
        queue_position: int | None = None,
    ) -> discord.Embed:
        uri = track.webpage_url.strip() if self._is_url(track.webpage_url) else None
        description = f"[{track.title}]({uri})" if uri else track.title
        embed = discord.Embed(title=header, description=description, color=discord.Color.blurple())

        embed.add_field(name="Duração", value=f"`{self._format_duration_ms(track.duration_ms)}`", inline=True)
        embed.add_field(name="Pedido por", value=f"<@{track.requester_id}>", inline=True)

        if queue_position is not None:
            embed.add_field(name="Posição", value=f"`{queue_position}`", inline=True)

        if track.author.strip():
            embed.add_field(name="Canal", value=track.author.strip(), inline=True)

        if track.source.strip():
            embed.add_field(name="Fonte", value=track.source.strip(), inline=True)

        if track.identifier.strip():
            embed.set_footer(text=f"track_id: {track.identifier.strip()}")

        if track.thumbnail_url and self._is_url(track.thumbnail_url):
            embed.set_thumbnail(url=track.thumbnail_url)

        return embed

    async def _disconnect_guild_voice_client(self, guild: discord.Guild) -> None:
        voice_client = guild.voice_client
        if voice_client is None:
            return
        try:
            await voice_client.disconnect(force=True)
        except Exception as exc:
            LOGGER.warning(
                "Falha ao limpar cliente de voz da guild %s.",
                guild.id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def _schedule_idle_disconnect(self, guild_id: int) -> None:
        state = self._get_state(guild_id)
        self._cancel_idle_task(state)

        async def idle_disconnect_task() -> None:
            try:
                await asyncio.sleep(IDLE_TIMEOUT_SECONDS)

                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    return
                voice_client = guild.voice_client
                if voice_client is None or not voice_client.is_connected():
                    return
                if voice_client.is_playing() or voice_client.is_paused():
                    return
                if state.queue:
                    return

                await self._send_music_message(
                    state.announce_channel_id,
                    "Fila vazia por 5 minutos. Saindo do canal de voz.",
                )
                await voice_client.disconnect(force=True)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                LOGGER.warning(
                    "Falha ao desconectar por inatividade na guild %s.",
                    guild_id,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        state.idle_task = asyncio.create_task(
            idle_disconnect_task(),
            name=f"music-idle-disconnect-{guild_id}",
        )

    @staticmethod
    def _truncate_for_log(value: str, limit: int = YTDLP_ERROR_SNIPPET_LIMIT) -> str:
        text = value.strip()
        if len(text) <= limit:
            return text
        return f"{text[:limit]}..."

    @staticmethod
    def _clone_track(track: QueueTrack) -> QueueTrack:
        return replace(track)

    def _normalize_lookup_key(self, query: str) -> str:
        normalized_query = " ".join(query.strip().split())
        if self._is_url(normalized_query):
            video_id = self._extract_youtube_video_id(normalized_query)
            if video_id:
                return f"yt:{video_id.lower()}"
            return f"url:{normalized_query}"
        return f"q:{normalized_query.lower()}"

    def _normalize_stream_url(self, value: str) -> str:
        raw = value.strip()
        if not raw:
            return ""
        if self._is_url(raw):
            return raw

        base_url = self._music_api_base_url()
        if raw.startswith("/"):
            return f"{base_url}{raw}"
        return urljoin(f"{base_url}/", raw)

    @staticmethod
    def _extract_stream_expires_at(stream_url: str) -> float:
        if not stream_url.strip():
            return 0.0

        try:
            parsed = urlparse(stream_url)
        except Exception:
            return 0.0

        exp_values = parse_qs(parsed.query).get("exp", [])
        if not exp_values:
            return 0.0

        raw_value = str(exp_values[0]).strip()
        try:
            exp = float(raw_value)
        except ValueError:
            return 0.0

        now = time.time()
        if exp > 10_000_000_000:  # epoch em ms
            exp = exp / 1000.0
        elif exp < 10_000_000:  # expiração relativa em segundos
            exp = now + exp

        return exp if exp > now else 0.0

    @staticmethod
    def _duration_to_ms(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            number = float(value)
            if number <= 0:
                return None
            # Duração em segundos na maioria dos payloads.
            return int(number * 1000)
        return None

    def _parse_resolve_payload(
        self,
        payload: dict[str, Any],
        *,
        query: str,
        requester_id: int,
    ) -> QueueTrack:
        root_payload = payload
        for wrapper_key in ("data", "dados", "result", "resultado", "payload"):
            wrapper_value = payload.get(wrapper_key)
            if isinstance(wrapper_value, dict):
                root_payload = wrapper_value
                break

        track_payload = root_payload.get("track")
        if not isinstance(track_payload, dict):
            track_payload = {}

        metadata = root_payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = track_payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        stream_url_candidate = ""
        for container in (root_payload, payload, track_payload, metadata):
            for key in ("stream_url", "streamUrl", "url"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    stream_url_candidate = value.strip()
                    break
            if stream_url_candidate:
                break

        stream_url = self._normalize_stream_url(stream_url_candidate)
        if not self._is_url(stream_url):
            raise RuntimeError("A API não retornou `stream_url` válida para reprodução.")

        identifier = ""
        for container in (root_payload, payload, track_payload, metadata):
            for key in ("track_id", "trackId", "id"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    identifier = value.strip()
                    break
            if identifier:
                break

        if not identifier:
            parsed = urlparse(stream_url)
            path_parts = [part for part in parsed.path.strip("/").split("/") if part]
            if len(path_parts) >= 2 and path_parts[-2] == "stream":
                identifier = path_parts[-1]

        title = "Faixa desconhecida"
        for container in (metadata, track_payload, root_payload, payload):
            for key in ("title", "name"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    title = value.strip()
                    break
            if title != "Faixa desconhecida":
                break

        author = "Desconhecido"
        for container in (metadata, track_payload, root_payload, payload):
            for key in ("author", "uploader", "channel", "artist"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    author = value.strip()
                    break
            if author != "Desconhecido":
                break

        duration_ms: int | None = None
        duration_ms_candidates = (
            metadata.get("duration_ms"),
            metadata.get("durationMs"),
            track_payload.get("duration_ms"),
            track_payload.get("durationMs"),
            root_payload.get("duration_ms"),
            root_payload.get("durationMs"),
            payload.get("duration_ms"),
            payload.get("durationMs"),
        )
        for candidate in duration_ms_candidates:
            if isinstance(candidate, (int, float)) and float(candidate) > 0:
                duration_ms = int(float(candidate))
                break

        if duration_ms is None:
            for candidate in (
                metadata.get("duration"),
                track_payload.get("duration"),
                root_payload.get("duration"),
                payload.get("duration"),
            ):
                duration_ms = self._duration_to_ms(candidate)
                if duration_ms is not None:
                    break

        thumbnail_url: str | None = None
        for container in (metadata, track_payload, root_payload, payload):
            for key in ("thumbnail", "thumbnail_url", "thumbnailUrl"):
                value = container.get(key)
                if isinstance(value, str) and self._is_url(value.strip()):
                    thumbnail_url = value.strip()
                    break
            if thumbnail_url:
                break

            thumbnails = container.get("thumbnails")
            if isinstance(thumbnails, list):
                for item in thumbnails:
                    if not isinstance(item, dict):
                        continue
                    thumb_value = item.get("url")
                    if isinstance(thumb_value, str) and self._is_url(thumb_value.strip()):
                        thumbnail_url = thumb_value.strip()
                        break
            if thumbnail_url:
                break

        webpage_url = ""
        for container in (metadata, track_payload, root_payload, payload):
            for key in ("webpage_url", "webpageUrl", "original_url", "originalUrl", "source_url", "sourceUrl"):
                value = container.get(key)
                if isinstance(value, str) and self._is_url(value.strip()):
                    webpage_url = value.strip()
                    break
            if webpage_url:
                break

        if not webpage_url:
            webpage_url = stream_url

        source = "music-api"
        for container in (metadata, track_payload, root_payload, payload):
            for key in ("source", "platform", "provider"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    source = value.strip()
                    break
            if source != "music-api":
                break

        normalized_query = query.strip()
        stream_expires_at = self._extract_stream_expires_at(stream_url)
        if stream_expires_at <= 0:
            stream_expires_at = time.time() + 180

        return QueueTrack(
            identifier=identifier,
            title=title,
            author=author,
            duration_ms=duration_ms,
            webpage_url=webpage_url,
            stream_url=stream_url,
            thumbnail_url=thumbnail_url,
            requester_id=requester_id,
            search_query=normalized_query,
            lookup_key=self._normalize_lookup_key(normalized_query),
            original_input=normalized_query,
            stream_expires_at=stream_expires_at,
            play_attempts=0,
            prefetch_state="idle",
            resolved_at=time.time(),
            source=source,
        )

    def _cache_expiration_for_track(self, track: QueueTrack) -> float:
        now = time.time()
        expires_at = now + LOCAL_RESOLVE_CACHE_TTL_SECONDS

        if track.stream_expires_at > 0:
            stream_refresh_cutoff = track.stream_expires_at - STREAM_URL_REFRESH_WINDOW_SECONDS
            if stream_refresh_cutoff > now:
                expires_at = min(expires_at, stream_refresh_cutoff)
            else:
                expires_at = now + 5

        return expires_at

    def _prune_resolve_cache(self) -> None:
        now = time.time()
        expired_keys = [key for key, entry in self._resolve_cache.items() if entry.expires_at <= now]
        for key in expired_keys:
            self._resolve_cache.pop(key, None)

        while len(self._resolve_cache) > LOCAL_RESOLVE_CACHE_MAX_ITEMS:
            oldest_key = next(iter(self._resolve_cache), None)
            if oldest_key is None:
                break
            self._resolve_cache.pop(oldest_key, None)

    def _cached_track_for_lookup(
        self,
        lookup_key: str,
        *,
        requester_id: int,
        original_query: str,
    ) -> QueueTrack | None:
        entry = self._resolve_cache.get(lookup_key)
        if entry is None:
            return None

        now = time.time()
        if entry.expires_at <= now:
            self._resolve_cache.pop(lookup_key, None)
            return None

        base = self._clone_track(entry.track)
        return replace(
            base,
            requester_id=requester_id,
            search_query=original_query.strip(),
            original_input=base.original_input.strip() or original_query.strip(),
            play_attempts=0,
            prefetch_state="idle",
        )

    async def _api_post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float = MUSIC_API_REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        url = f"{self._music_api_base_url()}{path}"
        session = await self._http_client()
        timeout = aiohttp.ClientTimeout(
            total=max(1.0, float(timeout_seconds)),
            connect=MUSIC_API_CONNECT_TIMEOUT_SECONDS,
        )

        try:
            async with session.post(url, json=payload, timeout=timeout) as response:
                raw_body = await response.text()
                parsed_body: Any = {}
                if raw_body.strip():
                    try:
                        parsed_body = json.loads(raw_body)
                    except json.JSONDecodeError:
                        parsed_body = {}

                if response.status >= 400:
                    detail = raw_body.strip() or f"HTTP {response.status}"
                    if isinstance(parsed_body, dict):
                        message_value = (
                            parsed_body.get("message")
                            or parsed_body.get("mensagem")
                            or parsed_body.get("error")
                        )
                        if isinstance(message_value, str) and message_value.strip():
                            detail = message_value.strip()
                    raise MusicApiRequestError(
                        f"API {path} retornou erro ({response.status}): {self._truncate_for_log(detail, 220)}",
                        status_code=response.status,
                    )

                if not isinstance(parsed_body, dict):
                    return {}
                return parsed_body
        except asyncio.TimeoutError as exc:
            raise MusicApiRequestError(f"Timeout ao chamar API {path}.") from exc
        except aiohttp.ClientError as exc:
            raise MusicApiRequestError(f"Erro de conexão ao chamar API {path}.") from exc

    async def _api_get_json(
        self,
        path: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float = MUSIC_API_REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        url = f"{self._music_api_base_url()}{path}"
        session = await self._http_client()
        timeout = aiohttp.ClientTimeout(
            total=max(1.0, float(timeout_seconds)),
            connect=MUSIC_API_CONNECT_TIMEOUT_SECONDS,
        )

        try:
            async with session.get(url, params=params, timeout=timeout) as response:
                raw_body = await response.text()
                parsed_body: Any = {}
                if raw_body.strip():
                    try:
                        parsed_body = json.loads(raw_body)
                    except json.JSONDecodeError:
                        parsed_body = {}

                if response.status >= 400:
                    detail = raw_body.strip() or f"HTTP {response.status}"
                    if isinstance(parsed_body, dict):
                        message_value = (
                            parsed_body.get("message")
                            or parsed_body.get("mensagem")
                            or parsed_body.get("error")
                        )
                        if isinstance(message_value, str) and message_value.strip():
                            detail = message_value.strip()
                    raise MusicApiRequestError(
                        f"API {path} retornou erro ({response.status}): {self._truncate_for_log(detail, 220)}",
                        status_code=response.status,
                    )

                if not isinstance(parsed_body, dict):
                    return {}
                return parsed_body
        except asyncio.TimeoutError as exc:
            raise MusicApiRequestError(f"Timeout ao chamar API {path}.") from exc
        except aiohttp.ClientError as exc:
            raise MusicApiRequestError(f"Erro de conexão ao chamar API {path}.") from exc

    async def _resolve_query_to_youtube_link(self, query: str) -> str:
        sanitized = query.strip()
        if self._is_url(sanitized):
            return sanitized

        LOGGER.info("Resolvendo busca por nome via /search (query=%s).", sanitized[:120])
        search_payload = await self._api_get_json(
            "/search",
            {"q": sanitized},
            timeout_seconds=YTDLP_SEARCH_TIMEOUT_SECONDS,
        )

        candidates: list[dict[str, Any]] = []
        resultado = search_payload.get("resultado")
        if isinstance(resultado, dict):
            candidates.append(resultado)
        elif isinstance(resultado, list):
            candidates.extend([item for item in resultado if isinstance(item, dict)])

        for key in ("results", "itens", "items"):
            possible = search_payload.get(key)
            if isinstance(possible, list):
                candidates.extend([item for item in possible if isinstance(item, dict)])

        for entry in candidates:
            entry_url = self._entry_candidate_url(entry)
            if entry_url:
                return entry_url

            for key in ("link", "webpage_url", "webpageUrl"):
                value = entry.get(key)
                if isinstance(value, str) and self._is_url(value.strip()):
                    return value.strip()

        total = search_payload.get("total")
        if isinstance(total, (int, float)) and int(total) <= 0:
            raise RuntimeError("Nenhum resultado encontrado para essa busca.")
        raise RuntimeError("Não encontrei um resultado válido no /search para essa busca.")

    async def _resolve_from_api(self, query: str, guild_id: int, requester_id: int) -> QueueTrack:
        resolved_link = await self._resolve_query_to_youtube_link(query)
        payloads = [
            {"link": resolved_link, "guild_id": str(guild_id), "requester_id": str(requester_id)},
            {"link": resolved_link, "guildId": str(guild_id), "requesterId": str(requester_id)},
            {"link": resolved_link},
        ]

        last_error: Exception | None = None
        for index, payload in enumerate(payloads):
            try:
                data = await self._api_post_json(
                    "/resolve",
                    payload,
                    timeout_seconds=PLAY_COMMAND_RESOLVE_TIMEOUT_SECONDS - PLAY_COMMAND_INTERNAL_SAFETY_SECONDS,
                )
                return self._parse_resolve_payload(data, query=resolved_link, requester_id=requester_id)
            except MusicApiRequestError as exc:
                last_error = exc
                should_retry_schema = exc.status_code in {400, 404, 422} and index < len(payloads) - 1
                if should_retry_schema:
                    continue
                break

        if last_error is None:
            raise RuntimeError("Não consegui resolver essa faixa na API local.")
        raise RuntimeError(str(last_error)) from last_error

    async def _prefetch_track(self, track: QueueTrack, guild_id: int, *, wait_for_warmup: bool) -> None:
        if track.prefetch_state in {"running", "done"}:
            return
        if not track.identifier.strip():
            return

        async def _run_prefetch() -> None:
            payloads = [
                {"track_ids": [track.identifier], "guild_id": str(guild_id)},
                {"track_ids": [track.identifier], "guildId": str(guild_id)},
                {"trackIds": [track.identifier], "guildId": str(guild_id)},
                {"track_id": track.identifier, "guild_id": str(guild_id)},
                {"trackId": track.identifier, "guildId": str(guild_id)},
                {"track_id": track.identifier},
                {"trackId": track.identifier},
            ]
            for index, payload in enumerate(payloads):
                try:
                    await self._api_post_json(
                        "/prefetch",
                        payload,
                        timeout_seconds=MUSIC_API_PREFETCH_TIMEOUT_SECONDS,
                    )
                    track.prefetch_state = "done"
                    return
                except MusicApiRequestError as exc:
                    should_retry_schema = exc.status_code in {400, 404, 422} and index < len(payloads) - 1
                    if should_retry_schema:
                        continue
                    raise

        track.prefetch_state = "running"
        if wait_for_warmup:
            try:
                await asyncio.wait_for(_run_prefetch(), timeout=MUSIC_API_PREFETCH_WAIT_SECONDS)
            except Exception as exc:
                track.prefetch_state = "failed"
                LOGGER.info(
                    "Prefetch falhou para track_id=%s (guild=%s): %s",
                    track.identifier,
                    guild_id,
                    self._truncate_for_log(str(exc), 180),
                )
            return

        try:
            await _run_prefetch()
        except Exception as exc:
            track.prefetch_state = "failed"
            LOGGER.info(
                "Prefetch em background falhou para track_id=%s (guild=%s): %s",
                track.identifier,
                guild_id,
                self._truncate_for_log(str(exc), 180),
            )

    def _schedule_prefetch_next(self, state: GuildMusicState) -> None:
        if not state.queue:
            return
        next_track = state.queue[0]
        if next_track.prefetch_state in {"running", "done"}:
            return
        self._create_background_task(
            self._prefetch_track(next_track, state.guild_id, wait_for_warmup=False),
            name=f"music-prefetch-next-{state.guild_id}",
        )

    def _is_stream_expired_or_near_expire(self, track: QueueTrack) -> bool:
        if track.stream_expires_at <= 0:
            return False
        return track.stream_expires_at <= (time.time() + STREAM_URL_REFRESH_WINDOW_SECONDS)

    def _is_likely_stream_retryable_error(self, exc: Exception | str) -> bool:
        text = str(exc).strip().lower()
        if not text:
            return False

        markers = (
            "401",
            "404",
            "403",
            "410",
            "429",
            "500",
            "502",
            "503",
            "504",
            "forbidden",
            "unauthorized",
            "expired",
            "signature",
            "bad gateway",
            "server error",
            "upstream",
            "i/o error",
            "input/output error",
            "http error 403",
            "http error 401",
            "http error 404",
            "http error 410",
            "http error 429",
            "http error 500",
            "http error 502",
            "http error 503",
            "http error 504",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _is_timeout_ytdlp_error(exc: Exception) -> bool:
        return "timeout ao consultar yt-dlp" in str(exc).strip().lower()

    @staticmethod
    def _is_requested_format_error(exc: Exception) -> bool:
        return "requested format is not available" in str(exc).strip().lower()

    def _build_ytdlp_context(
        self,
        *,
        identifier: str,
        playback: bool,
        format_selector: str | None,
        apply_format: bool,
    ) -> tuple[str, dict[str, Any]]:
        if yt_dlp is None:
            raise RuntimeError("yt-dlp (python) não está instalado.")

        is_search_identifier = identifier.strip().lower().startswith("ytsearch")
        args: list[str] = [
            "--ignore-config",
            "--js-runtimes",
            self._ytdlp_runtime(),
            "-q",
            "--no-warnings",
            "--skip-download",
        ]

        cookies_path = self._ytdlp_cookies_path()
        if cookies_path and os.path.exists(cookies_path):
            args.extend(["--cookies", cookies_path])

        if playback and apply_format:
            selector = (format_selector or YTDLP_PRIMARY_FORMAT_SELECTOR).strip()
            if selector:
                args.extend(["-f", selector])
            args.append("--no-playlist")

        if not playback:
            if is_search_identifier:
                args.append("--flat-playlist")
            else:
                args.extend(["--flat-playlist", "--no-playlist"])

        args.extend(["--", identifier])
        _, _, urls, ydl_options = yt_dlp.parse_options(args)
        if not urls:
            raise RuntimeError("yt-dlp não retornou URL/identificador para resolver.")

        # Keep API behavior deterministic and avoid filesystem cache side effects.
        ydl_options["cachedir"] = False
        ydl_options["socket_timeout"] = YTDLP_RESOLVE_TIMEOUT_SECONDS
        ydl_options["ignoreerrors"] = False
        return urls[0], ydl_options

    @staticmethod
    def _extract_info_with_ytdlp_sync(identifier: str, options: dict[str, Any]) -> dict[str, Any]:
        if yt_dlp is None:
            raise RuntimeError("yt-dlp (python) não está instalado.")

        with yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.extract_info(identifier, download=False)

        if not isinstance(result, dict):
            raise RuntimeError("yt-dlp retornou payload inválido.")
        return result

    async def _run_ytdlp_json(
        self,
        identifier: str,
        *,
        playback: bool,
        format_selector: str | None = None,
        apply_format: bool = True,
        command_timeout: float | None = None,
    ) -> dict[str, Any]:
        started_at = asyncio.get_running_loop().time()
        log_identifier = identifier[:120]
        is_search_identifier = identifier.strip().lower().startswith("ytsearch")

        if playback:
            LOGGER.info(
                "yt-dlp resolve iniciado (modo=playback, format=%s, id=%s).",
                (format_selector or ("auto" if not apply_format else YTDLP_PRIMARY_FORMAT_SELECTOR)),
                log_identifier,
            )
        elif is_search_identifier:
            LOGGER.info("yt-dlp resolve iniciado (modo=search, id=%s).", log_identifier)
        else:
            LOGGER.info("yt-dlp resolve iniciado (modo=metadata, id=%s).", log_identifier)

        target_identifier, options = self._build_ytdlp_context(
            identifier=identifier,
            playback=playback,
            format_selector=format_selector,
            apply_format=apply_format,
        )

        try:
            default_timeout = YTDLP_PLAYBACK_TIMEOUT_SECONDS if playback else YTDLP_SEARCH_TIMEOUT_SECONDS
            if not playback and not is_search_identifier:
                default_timeout = YTDLP_RESOLVE_TIMEOUT_SECONDS
            timeout_value = default_timeout
            if command_timeout is not None:
                timeout_value = max(1.0, min(float(command_timeout), default_timeout))
            data = await asyncio.wait_for(
                asyncio.to_thread(self._extract_info_with_ytdlp_sync, target_identifier, options),
                timeout=timeout_value,
            )
        except asyncio.TimeoutError:
            elapsed = asyncio.get_running_loop().time() - started_at
            LOGGER.warning("yt-dlp timeout após %.2fs (id=%s).", elapsed, log_identifier)
            raise RuntimeError("Timeout ao consultar yt-dlp.") from None
        except Exception as exc:
            detail = self._truncate_for_log(str(exc) or "sem detalhe")
            elapsed = asyncio.get_running_loop().time() - started_at
            LOGGER.warning(
                "yt-dlp falhou após %.2fs (id=%s).",
                elapsed,
                log_identifier,
            )
            raise RuntimeError(f"yt-dlp falhou ({detail}).") from exc

        elapsed = asyncio.get_running_loop().time() - started_at
        LOGGER.info("yt-dlp resolve concluído em %.2fs (id=%s).", elapsed, log_identifier)
        return data

    async def _run_ytdlp_json_with_retry(
        self,
        identifier: str,
        *,
        playback: bool,
        format_selector: str | None = None,
        apply_format: bool = True,
        deadline: float | None = None,
        retry_on_timeout: bool = True,
    ) -> dict[str, Any]:
        last_exception: Exception | None = None
        attempts = (YTDLP_PLAYBACK_RETRIES + 1) if (playback and retry_on_timeout) else 1
        log_identifier = identifier[:120]

        for attempt in range(1, attempts + 1):
            remaining: float | None = None
            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    message = "Sem tempo restante para novas tentativas de resolução."
                    LOGGER.warning("%s (id=%s).", message, log_identifier)
                    raise RuntimeError(message)

            if playback:
                LOGGER.info(
                    "Tentando resolve playback (tentativa %s/%s, id=%s).",
                    attempt,
                    attempts,
                    log_identifier,
                )

            try:
                return await self._run_ytdlp_json(
                    identifier,
                    playback=playback,
                    format_selector=format_selector,
                    apply_format=apply_format,
                    command_timeout=remaining,
                )
            except Exception as exc:
                last_exception = exc
                is_timeout_error = self._is_timeout_ytdlp_error(exc)
                LOGGER.warning(
                    "Tentativa de resolve falhou (tentativa %s/%s, id=%s, timeout=%s): %s",
                    attempt,
                    attempts,
                    log_identifier,
                    is_timeout_error,
                    self._truncate_for_log(str(exc), 220),
                )
                if attempt < attempts and is_timeout_error and retry_on_timeout:
                    await asyncio.sleep(0.2)
                    continue

        if last_exception is not None:
            raise last_exception
        raise RuntimeError("Falha inesperada ao consultar yt-dlp.")

    @staticmethod
    def _extract_entries_from_search(data: dict[str, Any]) -> list[dict[str, Any]]:
        item_type = str(data.get("_type", "")).strip().lower()
        if item_type == "playlist":
            entries = data.get("entries")
            if isinstance(entries, list):
                return [entry for entry in entries if isinstance(entry, dict)]
            return []

        return [data]

    def _deduplicate_search_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduplicated: list[dict[str, Any]] = []
        seen: set[str] = set()

        for entry in entries:
            entry_url = self._entry_candidate_url(entry)
            if not entry_url:
                continue

            video_id = self._extract_youtube_video_id(entry_url)
            key = video_id.lower() if video_id else entry_url.strip().lower()
            if key in seen:
                continue

            seen.add(key)
            deduplicated.append(entry)

        return deduplicated

    @staticmethod
    def _is_known_unrecoverable_ytdlp_error(exc: Exception) -> bool:
        error_text = str(exc).strip().lower()
        if not error_text:
            return False

        markers = (
            "sign in to confirm you",
            "this video requires login",
            "private video",
            "video unavailable",
            "members-only",
            "age-restricted",
            "only images are available for download",
            "n challenge solving failed",
            "nsig extraction failed",
        )
        return any(marker in error_text for marker in markers)

    def _entry_candidate_url(self, entry: dict[str, Any]) -> str:
        for key in ("webpage_url", "original_url", "url"):
            value = entry.get(key)
            if isinstance(value, str) and self._is_url(value):
                return value.strip()

        identifier = entry.get("id")
        if isinstance(identifier, str) and YOUTUBE_ID_RE.fullmatch(identifier.strip()):
            return f"https://www.youtube.com/watch?v={identifier.strip()}"

        return ""

    def _extract_stream_url_from_resolved(self, resolved: dict[str, Any]) -> str | None:
        direct_url = resolved.get("url")
        if isinstance(direct_url, str) and self._is_url(direct_url):
            return direct_url.strip()

        requested_downloads = resolved.get("requested_downloads")
        if isinstance(requested_downloads, list):
            for download in requested_downloads:
                if not isinstance(download, dict):
                    continue
                download_url = download.get("url")
                if isinstance(download_url, str) and self._is_url(download_url):
                    return download_url.strip()

                requested_formats = download.get("requested_formats")
                if isinstance(requested_formats, list):
                    for fmt in requested_formats:
                        if not isinstance(fmt, dict):
                            continue
                        fmt_url = fmt.get("url")
                        if isinstance(fmt_url, str) and self._is_url(fmt_url):
                            acodec = str(fmt.get("acodec") or "").lower()
                            if acodec and acodec != "none":
                                return fmt_url.strip()

        requested_formats = resolved.get("requested_formats")
        if isinstance(requested_formats, list):
            for fmt in requested_formats:
                if not isinstance(fmt, dict):
                    continue
                fmt_url = fmt.get("url")
                if isinstance(fmt_url, str) and self._is_url(fmt_url):
                    acodec = str(fmt.get("acodec") or "").lower()
                    if acodec and acodec != "none":
                        return fmt_url.strip()

        formats = resolved.get("formats")
        if not isinstance(formats, list):
            return None

        best_url: str | None = None
        best_score = float("-inf")
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue

            fmt_url = fmt.get("url")
            if not isinstance(fmt_url, str) or not self._is_url(fmt_url):
                continue

            acodec = str(fmt.get("acodec") or "").lower()
            if not acodec or acodec == "none":
                continue

            protocol = str(fmt.get("protocol") or "").lower()
            if protocol and not any(tag in protocol for tag in ("http", "https", "m3u8")):
                continue

            ext = str(fmt.get("ext") or "").lower()
            abr = float(fmt.get("abr")) if isinstance(fmt.get("abr"), (int, float)) else 0.0
            tbr = float(fmt.get("tbr")) if isinstance(fmt.get("tbr"), (int, float)) else 0.0

            ext_bonus = 5.0 if ext in {"m4a", "webm", "mp4"} else 0.0
            protocol_bonus = 2.0 if protocol.startswith(("http", "https")) else 1.0
            score = (abr * 10.0) + tbr + ext_bonus + protocol_bonus

            if score > best_score:
                best_score = score
                best_url = fmt_url.strip()

        return best_url

    def _build_queue_track(
        self,
        *,
        query: str,
        requester_id: int,
        entry: dict[str, Any],
        resolved: dict[str, Any],
        stream_url_override: str | None = None,
    ) -> QueueTrack:
        stream_url_raw = stream_url_override if stream_url_override is not None else resolved.get("url")
        stream_url = stream_url_raw.strip() if isinstance(stream_url_raw, str) else ""
        if not self._is_url(stream_url):
            raise RuntimeError("yt-dlp não retornou URL de stream válida.")

        entry_url = self._entry_candidate_url(entry)
        page_url_raw = resolved.get("webpage_url")
        if isinstance(page_url_raw, str) and self._is_url(page_url_raw):
            webpage_url = page_url_raw.strip()
        elif self._is_url(entry_url):
            webpage_url = entry_url
        else:
            webpage_url = stream_url

        identifier_raw = resolved.get("id")
        if isinstance(identifier_raw, str) and identifier_raw.strip():
            identifier = identifier_raw.strip()
        else:
            fallback_id = entry.get("id")
            identifier = fallback_id.strip() if isinstance(fallback_id, str) else ""

        title_candidates = [resolved.get("title"), entry.get("title"), "Faixa desconhecida"]
        title = next((str(value).strip() for value in title_candidates if isinstance(value, str) and str(value).strip()), "Faixa desconhecida")

        author_candidates = [
            resolved.get("uploader"),
            resolved.get("channel"),
            entry.get("uploader"),
            entry.get("channel"),
            "Desconhecido",
        ]
        author = next((str(value).strip() for value in author_candidates if isinstance(value, str) and str(value).strip()), "Desconhecido")

        duration_value = resolved.get("duration")
        if not isinstance(duration_value, (int, float)):
            duration_value = entry.get("duration")
        duration_ms: int | None
        if isinstance(duration_value, (int, float)):
            duration_ms = int(float(duration_value) * 1000)
        else:
            duration_ms = None

        thumb_candidates = [resolved.get("thumbnail"), entry.get("thumbnail")]
        thumbnail_url = next(
            (
                str(value).strip()
                for value in thumb_candidates
                if isinstance(value, str) and self._is_url(str(value).strip())
            ),
            None,
        )

        return QueueTrack(
            identifier=identifier,
            title=title,
            author=author,
            duration_ms=duration_ms,
            webpage_url=webpage_url,
            stream_url=stream_url,
            thumbnail_url=thumbnail_url,
            requester_id=requester_id,
            search_query=query.strip(),
            source="youtube",
        )

    async def _resolve_query_to_track(
        self,
        query: str,
        requester_id: int,
        guild_id: int,
        *,
        force_refresh: bool = False,
    ) -> QueueTrack:
        started_at = asyncio.get_running_loop().time()
        sanitized = query.strip()
        if not sanitized:
            raise RuntimeError("Informe uma URL ou termo de busca válido.")

        lookup_key = self._normalize_lookup_key(sanitized)
        if not force_refresh:
            cached = self._cached_track_for_lookup(
                lookup_key,
                requester_id=requester_id,
                original_query=sanitized,
            )
            if cached is not None:
                LOGGER.info("Resolve local cache hit (guild=%s, key=%s).", guild_id, lookup_key[:96])
                return cached

        LOGGER.info(
            "Resolve iniciado via API local (guild=%s, requester=%s, key=%s, force_refresh=%s).",
            guild_id,
            requester_id,
            lookup_key[:96],
            force_refresh,
        )

        created_task = False
        async with self._resolve_inflight_lock:
            task = self._resolve_inflight.get(lookup_key)
            if task is None:
                task = asyncio.create_task(
                    self._resolve_from_api(sanitized, guild_id, requester_id),
                    name=f"music-resolve-{guild_id}-{lookup_key[:32]}",
                )
                self._resolve_inflight[lookup_key] = task
                created_task = True

        try:
            base_track = await task
        finally:
            if created_task:
                async with self._resolve_inflight_lock:
                    current_task = self._resolve_inflight.get(lookup_key)
                    if current_task is task:
                        self._resolve_inflight.pop(lookup_key, None)

        if created_task:
            cached_track = replace(
                base_track,
                requester_id=0,
                search_query=sanitized,
                original_input=base_track.original_input.strip() or sanitized,
                lookup_key=lookup_key,
                play_attempts=0,
                prefetch_state="idle",
            )
            self._resolve_cache[lookup_key] = ResolveCacheEntry(
                track=cached_track,
                expires_at=self._cache_expiration_for_track(cached_track),
            )
            self._prune_resolve_cache()

        elapsed = asyncio.get_running_loop().time() - started_at
        LOGGER.info(
            "Resolve concluído via API local (guild=%s, key=%s, elapsed=%.2fs).",
            guild_id,
            lookup_key[:96],
            elapsed,
        )

        return replace(
            base_track,
            requester_id=requester_id,
            search_query=sanitized,
            original_input=base_track.original_input.strip() or sanitized,
            lookup_key=lookup_key,
            play_attempts=0,
            prefetch_state="idle",
        )

    async def _connect_to_member_channel(
        self,
        guild: discord.Guild,
        member: discord.Member,
        state: GuildMusicState,
    ) -> discord.VoiceClient:
        if member.voice is None or member.voice.channel is None:
            raise RuntimeError("Entre em um canal de voz antes de usar comandos de música.")

        target_channel = member.voice.channel
        LOGGER.info(
            "Conectando no canal de voz (guild=%s, user=%s, target_channel=%s).",
            guild.id,
            member.id,
            target_channel.id,
        )
        if isinstance(target_channel, discord.VoiceChannel):
            limit = target_channel.user_limit
            is_full = limit > 0 and len(target_channel.members) >= limit
            bot_already_inside = guild.me in target_channel.members if guild.me else False
            if is_full and not bot_already_inside:
                raise RuntimeError("O canal de voz está lotado. Libere uma vaga e tente novamente.")

        voice_client = guild.voice_client
        if voice_client and not isinstance(voice_client, discord.VoiceClient):
            raise RuntimeError("Já existe outro cliente de voz ativo neste servidor. Desconecte-o e tente novamente.")

        if voice_client and voice_client.is_connected():
            if voice_client.channel and voice_client.channel.id != target_channel.id:
                LOGGER.info(
                    "Movendo bot de canal de voz (guild=%s, from=%s, to=%s).",
                    guild.id,
                    voice_client.channel.id,
                    target_channel.id,
                )
                await voice_client.move_to(target_channel)
            client = voice_client
        else:
            try:
                LOGGER.info("Abrindo nova conexão de voz (guild=%s, channel=%s).", guild.id, target_channel.id)
                client = await target_channel.connect(self_deaf=True)
            except TypeError:
                client = await target_channel.connect()

        self._cancel_idle_task(state)
        LOGGER.info(
            "Conexão de voz pronta (guild=%s, channel=%s, connected=%s).",
            guild.id,
            target_channel.id,
            client.is_connected(),
        )
        return client

    @staticmethod
    def _can_control(member: discord.Member, voice_client: discord.VoiceClient) -> tuple[bool, str | None]:
        user_channel = member.voice.channel if member.voice else None
        bot_channel = voice_client.channel

        if user_channel is None:
            return False, "Entre no canal de voz do bot para controlar a fila."
        if bot_channel is None:
            return False, "Não estou conectado em nenhum canal de voz."
        if user_channel.id != bot_channel.id:
            return False, f"Você precisa estar em **{bot_channel.name}** para controlar a música."
        return True, None

    async def _refresh_track_if_needed(
        self,
        track: QueueTrack,
        guild_id: int,
        *,
        force_refresh: bool,
    ) -> QueueTrack:
        has_valid_url = self._is_url(track.stream_url)
        if not force_refresh and has_valid_url and not self._is_stream_expired_or_near_expire(track):
            return track

        refresh_input = track.original_input.strip() or track.search_query.strip() or track.title.strip()
        if not refresh_input:
            raise RuntimeError("Faixa sem referência para renovar stream.")

        LOGGER.info(
            "Renovando stream_url via API (guild=%s, id=%s, force=%s).",
            guild_id,
            (track.identifier or "")[:48],
            force_refresh,
        )
        refreshed = await self._resolve_query_to_track(
            refresh_input,
            track.requester_id,
            guild_id,
            force_refresh=True,
        )
        return replace(
            refreshed,
            play_attempts=track.play_attempts,
            prefetch_state="idle",
        )

    async def _play_next(self, guild_id: int) -> bool:
        state = self._get_state(guild_id)
        LOGGER.info(
            "_play_next acionado (guild=%s, queue_size=%s, has_current=%s).",
            guild_id,
            len(state.queue),
            state.current is not None,
        )

        async with state.playback_lock:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return False

            voice_client = guild.voice_client
            if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
                state.current = None
                state.current_source = None
                return False

            if voice_client.is_playing() or voice_client.is_paused():
                return True

            ffmpeg_path = self._ffmpeg_path()
            if not self._path_available(ffmpeg_path):
                await self._send_music_message(
                    state.announce_channel_id,
                    f"FFmpeg não encontrado em `{ffmpeg_path}`. Não consigo iniciar a reprodução.",
                )
                return False

            while state.queue:
                track = state.queue.popleft()
                LOGGER.info(
                    "Preparando faixa para tocar (guild=%s, id=%s, title=%s).",
                    guild_id,
                    (track.identifier or "")[:40],
                    track.title[:80],
                )

                try:
                    track = await self._refresh_track_if_needed(track, guild_id, force_refresh=False)
                except Exception as exc:
                    LOGGER.warning(
                        "Falha ao renovar track antes do playback (guild=%s, id=%s).",
                        guild_id,
                        (track.identifier or "")[:40],
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                    await self._send_music_message(
                        state.announce_channel_id,
                        "Não consegui renovar a URL dessa faixa. Tentando a próxima.",
                    )
                    continue

                await self._prefetch_track(track, guild_id, wait_for_warmup=True)

                try:
                    source = discord.FFmpegPCMAudio(
                        track.stream_url,
                        executable=ffmpeg_path,
                        before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                        options="-vn",
                    )
                    wrapped = discord.PCMVolumeTransformer(source, volume=state.volume_percent / 100)
                except Exception as exc:
                    LOGGER.warning(
                        "Falha ao preparar FFmpeg para a faixa %s na guild %s.",
                        track.identifier or "sem_id",
                        guild_id,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                    if self._is_likely_stream_retryable_error(exc) and track.play_attempts < 1:
                        retry_track = replace(track, play_attempts=track.play_attempts + 1, prefetch_state="idle")
                        try:
                            retry_track = await self._refresh_track_if_needed(
                                retry_track,
                                guild_id,
                                force_refresh=True,
                            )
                            state.queue.appendleft(retry_track)
                            await self._send_music_message(
                                state.announce_channel_id,
                                "Falha temporária no stream; renovando URL e tentando novamente.",
                            )
                            continue
                        except Exception:
                            pass
                    await self._send_music_message(
                        state.announce_channel_id,
                        "Falha ao preparar a faixa para reprodução. Tentando a próxima da fila.",
                    )
                    continue

                state.current = track
                state.current_source = wrapped
                self._cancel_idle_task(state)

                def _after_playback(
                    error: Exception | None,
                    finished_track: QueueTrack = track,
                    ffmpeg_source: discord.FFmpegPCMAudio = source,
                ) -> None:
                    final_error = error
                    if final_error is None:
                        process = getattr(ffmpeg_source, "_process", None)
                        return_code = getattr(process, "returncode", None)
                        if isinstance(return_code, int) and return_code != 0:
                            final_error = RuntimeError(f"ffmpeg terminou com código {return_code}")

                    self.bot.loop.call_soon_threadsafe(
                        asyncio.create_task,
                        self._on_playback_finished(guild_id, finished_track, final_error),
                    )

                try:
                    voice_client.play(wrapped, after=_after_playback)
                except Exception as exc:
                    LOGGER.warning(
                        "Falha ao iniciar playback na guild %s.",
                        guild_id,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                    state.current = None
                    state.current_source = None
                    if self._is_likely_stream_retryable_error(exc) and track.play_attempts < 1:
                        retry_track = replace(track, play_attempts=track.play_attempts + 1, prefetch_state="idle")
                        try:
                            retry_track = await self._refresh_track_if_needed(
                                retry_track,
                                guild_id,
                                force_refresh=True,
                            )
                            state.queue.appendleft(retry_track)
                            await self._send_music_message(
                                state.announce_channel_id,
                                "Falha temporária no stream; renovando URL e tentando novamente.",
                            )
                            continue
                        except Exception:
                            pass
                    await self._send_music_message(
                        state.announce_channel_id,
                        "Não consegui iniciar essa faixa agora. Tentando a próxima.",
                    )
                    continue

                await self._send_music_message(
                    state.announce_channel_id,
                    "Reprodução iniciada.",
                    embed=self._build_track_embed(track, header="Tocando Agora"),
                )
                LOGGER.info(
                    "Playback iniciado (guild=%s, id=%s, queue_remaining=%s).",
                    guild_id,
                    (track.identifier or "")[:40],
                    len(state.queue),
                )
                self._schedule_prefetch_next(state)
                return True

            state.current = None
            state.current_source = None
            self._schedule_idle_disconnect(guild_id)
            LOGGER.info("Fila vazia após _play_next; idle disconnect agendado (guild=%s).", guild_id)
            return False

    async def _on_playback_finished(
        self,
        guild_id: int,
        finished_track: QueueTrack | None,
        error: Exception | None,
    ) -> None:
        state = self._get_state(guild_id)

        if error is not None:
            LOGGER.warning(
                "Erro durante playback na guild %s.",
                guild_id,
                exc_info=(type(error), error, error.__traceback__),
            )
            if (
                finished_track is not None
                and self._is_likely_stream_retryable_error(error)
                and finished_track.play_attempts < 1
            ):
                retry_track = replace(
                    finished_track,
                    play_attempts=finished_track.play_attempts + 1,
                    prefetch_state="idle",
                )
                try:
                    refreshed = await self._refresh_track_if_needed(
                        retry_track,
                        guild_id,
                        force_refresh=True,
                    )
                    state.queue.appendleft(refreshed)
                    await self._send_music_message(
                        state.announce_channel_id,
                        "A stream falhou durante a reprodução. Atualizei a URL e vou tentar novamente.",
                    )
                except Exception as exc:
                    LOGGER.warning(
                        "Falha ao renovar stream após erro de playback (guild=%s, id=%s).",
                        guild_id,
                        (finished_track.identifier or "")[:40],
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                    await self._send_music_message(
                        state.announce_channel_id,
                        "Falha durante a reprodução da faixa atual. Tentando a próxima da fila.",
                    )
            else:
                await self._send_music_message(
                    state.announce_channel_id,
                    "Falha durante a reprodução da faixa atual. Tentando a próxima da fila.",
                )
        else:
            LOGGER.info(
                "Playback finalizado sem erro (guild=%s, id=%s, title=%s).",
                guild_id,
                (finished_track.identifier if finished_track else "")[:40],
                (finished_track.title if finished_track else "")[:80],
            )

        state.current = None
        state.current_source = None
        await self._play_next(guild_id)

    @music.command(
        name="setup",
        description="Diagnostica dependências de áudio local (FFmpeg, voz e API de música).",
    )
    async def music_setup(self, interaction: discord.Interaction) -> None:
        issues = await self._dependency_issues(include_api_probe=True)

        status_lines = [
            f"PyNaCl: {'OK' if nacl is not None else 'FALHOU'}",
            f"FFmpeg: {self._ffmpeg_path()}",
            f"Music API: {self._music_api_base_url()}",
        ]

        if issues:
            await self._respond(
                interaction,
                "Diagnóstico de música:\n"
                + "\n".join(f"- {line}" for line in status_lines)
                + "\n\nAjustes necessários:\n"
                + "\n".join(f"- {issue}" for issue in issues),
                ephemeral=True,
            )
            return

        await self._respond(
            interaction,
            "Diagnóstico de música:\n"
            + "\n".join(f"- {line}" for line in status_lines)
            + "\n\nTudo pronto para tocar áudio com a API local de streaming.",
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

        issues = await self._dependency_issues()
        if issues:
            await self._respond(interaction, "\n".join(f"- {issue}" for issue in issues), ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        state = self._get_state(guild.id)
        state.announce_channel_id = interaction.channel_id

        try:
            voice_client = await self._connect_to_member_channel(guild, member, state)
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except (discord.ConnectionClosed, discord.ClientException, discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning(
                "Falha ao conectar no canal de voz na guild %s.",
                guild.id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.followup.send(
                "Não consegui entrar no canal de voz. Verifique permissões de `Connect` e `Speak`.",
                ephemeral=True,
            )
            return

        channel_name = voice_client.channel.name if voice_client.channel else "canal desconhecido"
        await interaction.followup.send(f"Conectado em **{channel_name}**.")

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

        issues = await self._dependency_issues()
        if issues:
            await self._respond(interaction, "\n".join(f"- {issue}" for issue in issues), ephemeral=True)
            return

        query = busca_ou_url.strip()
        if not query:
            await self._respond(interaction, "Informe uma URL ou termo de busca válido.", ephemeral=True)
            return

        LOGGER.info(
            "/music play recebido (guild=%s, user=%s, query=%s).",
            guild.id,
            member.id,
            query[:120],
        )
        await interaction.response.defer(thinking=True)

        state = self._get_state(guild.id)
        state.announce_channel_id = interaction.channel_id

        try:
            voice_client = await self._connect_to_member_channel(guild, member, state)
        except RuntimeError as exc:
            LOGGER.info("/music play bloqueado por regra de voz (guild=%s): %s", guild.id, str(exc))
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except (discord.ConnectionClosed, discord.ClientException, discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning(
                "Falha ao conectar no canal de voz antes de tocar na guild %s.",
                guild.id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.followup.send(
                "Não consegui entrar no canal de voz. Verifique permissões de `Connect` e `Speak`.",
                ephemeral=True,
            )
            return

        LOGGER.info(
            "Canal de voz pronto para /music play (guild=%s, connected=%s, queue_size=%s).",
            guild.id,
            voice_client.is_connected(),
            len(state.queue),
        )
        if len(state.queue) >= MAX_QUEUE_ITEMS:
            await interaction.followup.send(
                f"A fila já atingiu o limite de {MAX_QUEUE_ITEMS} faixas.",
                ephemeral=True,
            )
            return

        progress_message: discord.WebhookMessage | None = None
        try:
            progress_message = await interaction.followup.send(
                "Buscando e preparando a faixa...",
                wait=True,
            )
            LOGGER.info("Mensagem de progresso enviada para /music play (guild=%s).", guild.id)
        except (discord.Forbidden, discord.HTTPException):
            progress_message = None
            LOGGER.info("Não consegui enviar mensagem de progresso para /music play (guild=%s).", guild.id)

        try:
            resolve_started_at = asyncio.get_running_loop().time()
            track = await asyncio.wait_for(
                self._resolve_query_to_track(query, member.id, guild.id),
                timeout=PLAY_COMMAND_RESOLVE_TIMEOUT_SECONDS,
            )
            resolve_elapsed = asyncio.get_running_loop().time() - resolve_started_at
            LOGGER.info(
                "Resolve concluído para /music play na guild %s em %.2fs (query=%s).",
                guild.id,
                resolve_elapsed,
                query[:80],
            )
        except asyncio.TimeoutError:
            timeout_message = (
                "A busca demorou demais para responder agora. "
                "Tente novamente em instantes."
            )
            LOGGER.warning(
                "Timeout em /music play após %.2fs (guild=%s, query=%s).",
                PLAY_COMMAND_RESOLVE_TIMEOUT_SECONDS,
                guild.id,
                query[:120],
            )
            if progress_message is not None:
                try:
                    await progress_message.edit(content=timeout_message, embed=None)
                except (discord.Forbidden, discord.HTTPException):
                    await interaction.followup.send(timeout_message, ephemeral=True)
            else:
                await interaction.followup.send(timeout_message, ephemeral=True)
            return
        except Exception as exc:
            LOGGER.warning(
                "Falha ao resolver faixa para /music play na guild %s.",
                guild.id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            detail = str(exc).strip()
            error_message = detail or "Não consegui carregar essa faixa. Tente outra URL ou termo de busca."
            if progress_message is not None:
                try:
                    await progress_message.edit(content=error_message, embed=None)
                except (discord.Forbidden, discord.HTTPException):
                    await interaction.followup.send(error_message, ephemeral=True)
            else:
                await interaction.followup.send(error_message, ephemeral=True)
            return

        async with state.playback_lock:
            was_idle = (
                not voice_client.is_playing()
                and not voice_client.is_paused()
                and state.current is None
                and not state.queue
            )
            state.queue.append(track)
            queue_size = len(state.queue)
            LOGGER.info(
                "Faixa enfileirada (guild=%s, id=%s, title=%s, queue_size=%s, was_idle=%s).",
                guild.id,
                (track.identifier or "")[:40],
                track.title[:80],
                queue_size,
                was_idle,
            )

        if was_idle:
            LOGGER.info("Bot estava idle; iniciando reprodução imediata (guild=%s).", guild.id)
            started = await self._play_next(guild.id)
            if not started:
                start_error = "Não consegui iniciar essa faixa agora. Tente novamente em instantes."
                LOGGER.warning("Falha ao iniciar reprodução imediata após enqueue (guild=%s).", guild.id)
                if progress_message is not None:
                    try:
                        await progress_message.edit(content=start_error, embed=None)
                    except (discord.Forbidden, discord.HTTPException):
                        await interaction.followup.send(start_error, ephemeral=True)
                else:
                    await interaction.followup.send(start_error, ephemeral=True)
                return
            header = "Preparando para tocar"
            queue_position = None
        else:
            header = "Adicionado na Fila"
            queue_position = queue_size + (1 if state.current else 0)

        result_embed = self._build_track_embed(
            track,
            header=header,
            queue_position=queue_position,
        )
        if progress_message is not None:
            try:
                await progress_message.edit(content=None, embed=result_embed)
                LOGGER.info("Resposta final de /music play enviada via edição da mensagem de progresso (guild=%s).", guild.id)
                return
            except (discord.Forbidden, discord.HTTPException):
                pass

        await interaction.followup.send(embed=result_embed)
        LOGGER.info("Resposta final de /music play enviada via followup padrão (guild=%s).", guild.id)

    @music.command(name="queue", description="Mostra a fila atual de reprodução.")
    @app_commands.guild_only()
    @app_commands.describe(limite="Quantidade de itens a mostrar (1 a 20).")
    async def music_queue(self, interaction: discord.Interaction, limite: app_commands.Range[int, 1, 20] = 10) -> None:
        guild = interaction.guild
        if guild is None:
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        state = self._states.get(guild.id)
        voice_client = guild.voice_client
        if state is None or voice_client is None or not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
            await self._respond(interaction, "Não há fila ativa nesta guilda.", ephemeral=True)
            return

        lines: list[str] = []

        if state.current:
            lines.append(
                f"Tocando agora: **{state.current.title}** (`{self._format_duration_ms(state.current.duration_ms)}`)"
            )
        else:
            lines.append("Tocando agora: nada")

        if not state.queue:
            lines.append("Próximas faixas: fila vazia.")
        else:
            lines.append("Próximas faixas:")
            for index, track in enumerate(list(state.queue)[:limite], start=1):
                lines.append(
                    f"{index}. **{track.title}** (`{self._format_duration_ms(track.duration_ms)}`)"
                )
            if len(state.queue) > limite:
                lines.append(f"... e mais {len(state.queue) - limite} faixas.")

        await self._respond(interaction, "\n".join(lines))

    @music.command(name="now", description="Mostra a faixa que está tocando agora.")
    @app_commands.guild_only()
    async def music_now(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        state = self._states.get(guild.id)
        voice_client = guild.voice_client
        if (
            state is None
            or state.current is None
            or voice_client is None
            or not isinstance(voice_client, discord.VoiceClient)
            or not voice_client.is_connected()
        ):
            await self._respond(interaction, "Não há música tocando agora.", ephemeral=True)
            return

        await self._respond_embed(
            interaction,
            embed=self._build_track_embed(state.current, header="Tocando Agora"),
        )

    @music.command(name="pause", description="Pausa a reprodução atual.")
    @app_commands.guild_only()
    async def music_pause(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        voice_client = guild.voice_client
        if voice_client is None or not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
            await self._respond(interaction, "Não há player ativo nesta guilda.", ephemeral=True)
            return

        allowed, reason = self._can_control(member, voice_client)
        if not allowed:
            await self._respond(interaction, reason or "Ação negada.", ephemeral=True)
            return

        if not voice_client.is_playing():
            await self._respond(interaction, "Não há música tocando neste momento.", ephemeral=True)
            return

        voice_client.pause()
        await self._respond(interaction, "Música pausada.")

    @music.command(name="resume", description="Retoma a reprodução pausada.")
    @app_commands.guild_only()
    async def music_resume(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        voice_client = guild.voice_client
        if voice_client is None or not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
            await self._respond(interaction, "Não há player ativo nesta guilda.", ephemeral=True)
            return

        allowed, reason = self._can_control(member, voice_client)
        if not allowed:
            await self._respond(interaction, reason or "Ação negada.", ephemeral=True)
            return

        if not voice_client.is_paused():
            await self._respond(interaction, "Nenhuma música pausada para retomar.", ephemeral=True)
            return

        voice_client.resume()
        await self._respond(interaction, "Reprodução retomada.")

    @music.command(name="skip", description="Pula para a próxima música da fila.")
    @app_commands.guild_only()
    async def music_skip(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        state = self._states.get(guild.id)
        voice_client = guild.voice_client
        if (
            state is None
            or voice_client is None
            or not isinstance(voice_client, discord.VoiceClient)
            or not voice_client.is_connected()
        ):
            await self._respond(interaction, "Não há player ativo nesta guilda.", ephemeral=True)
            return

        allowed, reason = self._can_control(member, voice_client)
        if not allowed:
            await self._respond(interaction, reason or "Ação negada.", ephemeral=True)
            return

        if not voice_client.is_playing() and not voice_client.is_paused():
            await self._respond(interaction, "Não há faixa para pular agora.", ephemeral=True)
            return

        current_title = state.current.title if state.current else "Faixa atual"
        voice_client.stop()
        await self._respond(interaction, f"Pulada: **{current_title}**")

    @music.command(name="stop", description="Para a música atual e limpa a fila.")
    @app_commands.guild_only()
    async def music_stop(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        state = self._states.get(guild.id)
        voice_client = guild.voice_client
        if (
            state is None
            or voice_client is None
            or not isinstance(voice_client, discord.VoiceClient)
            or not voice_client.is_connected()
        ):
            await self._respond(interaction, "Não há player ativo nesta guilda.", ephemeral=True)
            return

        allowed, reason = self._can_control(member, voice_client)
        if not allowed:
            await self._respond(interaction, reason or "Ação negada.", ephemeral=True)
            return

        removed = len(state.queue)
        state.queue.clear()
        state.current = None
        state.current_source = None

        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()

        await self._respond(interaction, f"Fila limpa e reprodução interrompida. Itens removidos: `{removed}`.")

    @music.command(name="leave", description="Desconecta o bot do canal de voz e limpa a fila.")
    @app_commands.guild_only()
    async def music_leave(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await self._respond(interaction, "Este comando só funciona em servidor.", ephemeral=True)
            return

        state = self._states.get(guild.id)
        voice_client = guild.voice_client
        if (
            state is None
            or voice_client is None
            or not isinstance(voice_client, discord.VoiceClient)
            or not voice_client.is_connected()
        ):
            await self._respond(interaction, "Não estou conectado em canal de voz nesta guilda.", ephemeral=True)
            return

        allowed, reason = self._can_control(member, voice_client)
        if not allowed:
            await self._respond(interaction, reason or "Ação negada.", ephemeral=True)
            return

        state.queue.clear()
        state.current = None
        state.current_source = None
        self._cancel_idle_task(state)

        await voice_client.disconnect(force=True)
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

        state = self._states.get(guild.id)
        voice_client = guild.voice_client
        if (
            state is None
            or voice_client is None
            or not isinstance(voice_client, discord.VoiceClient)
            or not voice_client.is_connected()
        ):
            await self._respond(interaction, "Não há player ativo nesta guilda.", ephemeral=True)
            return

        allowed, reason = self._can_control(member, voice_client)
        if not allowed:
            await self._respond(interaction, reason or "Ação negada.", ephemeral=True)
            return

        state.volume_percent = int(valor)
        if state.current_source is not None:
            state.current_source.volume = state.volume_percent / 100

        await self._respond(interaction, f"Volume ajustado para `{valor}%`.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
