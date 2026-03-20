from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import shutil
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

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
HTTP_CONNECT_TIMEOUT_SECONDS = 2.5
HTTP_REQUEST_TIMEOUT_SECONDS = 14
YTMP3_PROBE_TIMEOUT_SECONDS = 2.0
LOCAL_RESOLVE_CACHE_TTL_SECONDS = 900
LOCAL_RESOLVE_CACHE_MAX_ITEMS = 4000
STREAM_URL_REFRESH_WINDOW_SECONDS = 90
STREAM_TRANSIENT_RETRY_DELAYS_SECONDS = (0.25, 0.5, 1.0)
STREAM_429_JITTER_MAX_SECONDS = 0.25
STREAM_HTTP_STATUS_RE = re.compile(r"\b(401|404|410|429|500|502|503|504)\b")
LOG_ERROR_SNIPPET_LIMIT = 600
MAX_SEARCH_CANDIDATES = 5
PLAY_COMMAND_RESOLVE_TIMEOUT_SECONDS = 26
YTMP3_SEARCH_BASE_URL_DEFAULT = "https://yt-meta.ytconvert.org"
YTMP3_DOWNLOAD_API_URL_DEFAULT = "https://hub.ytconvert.org/api/download"
YTMP3_SEARCH_TIMEOUT_SECONDS = 8
YTMP3_DOWNLOAD_CREATE_TIMEOUT_SECONDS = 22
YTMP3_STATUS_TIMEOUT_SECONDS = 12
YTMP3_STATUS_MAX_POLLS = 16
YTMP3_STATUS_POLL_INTERVAL_SECONDS = 1.0


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
    resolved_at: float = 0.0
    source: str = "ytmp3.gg"


@dataclass(slots=True)
class ResolveCacheEntry:
    track: QueueTrack
    expires_at: float


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

    def cog_unload(self) -> None:
        for state in self._states.values():
            self._cancel_idle_task(state)

        for voice_client in list(self.bot.voice_clients):
            if isinstance(voice_client, discord.VoiceClient) and voice_client.is_connected():
                asyncio.create_task(voice_client.disconnect(force=True))

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
    def _ytmp3_search_base_url() -> str:
        configured = os.getenv("MUSIC_YTMP3_SEARCH_BASE_URL", YTMP3_SEARCH_BASE_URL_DEFAULT).strip()
        if not configured:
            return YTMP3_SEARCH_BASE_URL_DEFAULT
        return configured.rstrip("/")

    @staticmethod
    def _ytmp3_download_api_url() -> str:
        configured = os.getenv("MUSIC_YTMP3_DOWNLOAD_API_URL", YTMP3_DOWNLOAD_API_URL_DEFAULT).strip()
        if not configured:
            return YTMP3_DOWNLOAD_API_URL_DEFAULT
        return configured.rstrip("/")

    async def _http_client(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(
                total=HTTP_REQUEST_TIMEOUT_SECONDS,
                connect=HTTP_CONNECT_TIMEOUT_SECONDS,
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

    @staticmethod
    def _path_available(executable: str) -> bool:
        if os.path.sep in executable:
            return os.path.exists(executable)
        return shutil.which(executable) is not None

    async def _probe_ytmp3(self) -> None:
        search_base = self._ytmp3_search_base_url()
        if not self._is_url(search_base):
            raise RuntimeError(f"MUSIC_YTMP3_SEARCH_BASE_URL inválida: `{search_base}`.")

        session = await self._http_client()
        timeout = aiohttp.ClientTimeout(total=YTMP3_PROBE_TIMEOUT_SECONDS)
        try:
            async with session.get(
                f"{search_base}/search",
                params={"q": "music test"},
                timeout=timeout,
            ) as response:
                if response.status >= 500:
                    raise RuntimeError(
                        f"Endpoint de busca YTMP3 indisponível em `{search_base}` (status HTTP {response.status})."
                    )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"Timeout ao conectar no endpoint de busca YTMP3 `{search_base}`.") from exc
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"Não consegui conectar no endpoint de busca YTMP3 `{search_base}`.") from exc

    async def _dependency_issues(self, *, include_api_probe: bool = False) -> list[str]:
        issues: list[str] = []

        if nacl is None:
            issues.append("Dependência ausente: instale `PyNaCl` (`pip install PyNaCl`).")

        ffmpeg_path = self._ffmpeg_path()
        if not self._path_available(ffmpeg_path):
            issues.append(f"FFmpeg não encontrado em `{ffmpeg_path}`.")

        search_base = self._ytmp3_search_base_url()
        download_url = self._ytmp3_download_api_url()
        if not self._is_url(search_base):
            issues.append(f"MUSIC_YTMP3_SEARCH_BASE_URL inválida: `{search_base}`.")
        if not self._is_url(download_url):
            issues.append(f"MUSIC_YTMP3_DOWNLOAD_API_URL inválida: `{download_url}`.")

        if include_api_probe and not issues:
            try:
                await self._probe_ytmp3()
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
    def _truncate_for_log(value: str, limit: int = LOG_ERROR_SNIPPET_LIMIT) -> str:
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

    @staticmethod
    def _extract_stream_expires_at(stream_url: str) -> float:
        if not stream_url.strip():
            return 0.0

        try:
            parsed = urlparse(stream_url)
        except Exception:
            return 0.0

        query_values = parse_qs(parsed.query)
        exp_values = query_values.get("exp", []) or query_values.get("expires", [])
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
        )

    async def _ytmp3_get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        session = await self._http_client()
        timeout = aiohttp.ClientTimeout(
            total=max(1.0, float(timeout_seconds)),
            connect=HTTP_CONNECT_TIMEOUT_SECONDS,
        )
        try:
            async with session.get(
                url,
                params=params or None,
                timeout=timeout,
                headers={"Accept": "application/json"},
            ) as response:
                raw_body = await response.text()
                payload: Any = {}
                if raw_body.strip():
                    try:
                        payload = json.loads(raw_body)
                    except json.JSONDecodeError:
                        payload = {}

                if response.status >= 400:
                    detail = raw_body.strip() or f"HTTP {response.status}"
                    if isinstance(payload, dict):
                        message_value = payload.get("message") or payload.get("error") or payload.get("jobError")
                        if isinstance(message_value, str) and message_value.strip():
                            detail = message_value.strip()
                    raise RuntimeError(
                        f"YTMP3 retornou erro ({response.status}): {self._truncate_for_log(detail, 220)}"
                    )

                if isinstance(payload, dict):
                    return payload
                return {}
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Timeout ao consultar endpoint YTMP3.") from exc
        except aiohttp.ClientError as exc:
            raise RuntimeError("Erro de conexão ao consultar endpoint YTMP3.") from exc

    async def _ytmp3_post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        session = await self._http_client()
        timeout = aiohttp.ClientTimeout(
            total=max(1.0, float(timeout_seconds)),
            connect=HTTP_CONNECT_TIMEOUT_SECONDS,
        )
        try:
            async with session.post(
                url,
                json=payload,
                timeout=timeout,
                headers={"Accept": "application/json"},
            ) as response:
                raw_body = await response.text()
                data: Any = {}
                if raw_body.strip():
                    try:
                        data = json.loads(raw_body)
                    except json.JSONDecodeError:
                        data = {}

                if response.status >= 400:
                    detail = raw_body.strip() or f"HTTP {response.status}"
                    if isinstance(data, dict):
                        message_value = data.get("message") or data.get("error") or data.get("jobError")
                        if isinstance(message_value, str) and message_value.strip():
                            detail = message_value.strip()
                    raise RuntimeError(
                        f"YTMP3 retornou erro ({response.status}): {self._truncate_for_log(detail, 220)}"
                    )

                if isinstance(data, dict):
                    return data
                return {}
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Timeout ao criar job de stream no YTMP3.") from exc
        except aiohttp.ClientError as exc:
            raise RuntimeError("Erro de conexão ao criar job de stream no YTMP3.") from exc

    def _extract_search_candidate_links(self, search_payload: dict[str, Any]) -> list[str]:
        candidate_entries: list[dict[str, Any]] = []

        for key in ("resultado", "resultados", "results", "itens", "items", "entries"):
            value = search_payload.get(key)
            if isinstance(value, dict):
                candidate_entries.append(value)
            elif isinstance(value, list):
                candidate_entries.extend([item for item in value if isinstance(item, dict)])

        links: list[str] = []
        seen: set[str] = set()

        for entry in candidate_entries:
            entry_type = str(entry.get("type") or "").strip().lower()
            if entry_type and entry_type not in {"stream", "video"}:
                continue

            entry_url = self._entry_candidate_url(entry)
            if not entry_url:
                for key in ("id", "link", "url", "webpage_url", "webpageUrl", "original_url", "originalUrl"):
                    value = entry.get(key)
                    if isinstance(value, str) and self._is_url(value.strip()):
                        entry_url = value.strip()
                        break

            if not entry_url:
                continue

            dedupe_key = self._normalize_lookup_key(entry_url)
            if dedupe_key in seen:
                continue

            seen.add(dedupe_key)
            links.append(entry_url)
            if len(links) >= MAX_SEARCH_CANDIDATES:
                break

        return links

    async def _resolve_query_to_youtube_links(self, query: str, guild_id: int) -> list[str]:
        sanitized = query.strip()
        if self._is_url(sanitized):
            return [sanitized]

        LOGGER.info("Resolvendo busca por nome via YTMP3 (query=%s).", sanitized[:120])
        search_payload = await self._ytmp3_get_json(
            f"{self._ytmp3_search_base_url()}/search",
            {"q": sanitized},
            timeout_seconds=YTMP3_SEARCH_TIMEOUT_SECONDS,
        )

        candidate_links = self._extract_search_candidate_links(search_payload)
        if candidate_links:
            return candidate_links

        raise RuntimeError("Nenhum resultado encontrado para essa busca.")

    async def _resolve_from_ytmp3(self, query: str, guild_id: int, requester_id: int) -> QueueTrack:
        resolved_links = await self._resolve_query_to_youtube_links(query, guild_id)
        last_error: Exception | None = None
        for candidate_index, resolved_link in enumerate(resolved_links, start=1):
            LOGGER.info(
                "Tentando resolver stream via YTMP3 (%s/%s) (guild=%s, link=%s).",
                candidate_index,
                len(resolved_links),
                guild_id,
                resolved_link[:120],
            )
            try:
                create_payload = await self._ytmp3_post_json(
                    self._ytmp3_download_api_url(),
                    {
                        "url": resolved_link,
                        "os": "linux",
                        "output": {"type": "audio", "format": "mp3"},
                        "audio": {"bitrate": "128k", "trackId": "origin"},
                    },
                    timeout_seconds=YTMP3_DOWNLOAD_CREATE_TIMEOUT_SECONDS,
                )

                status_url_value = create_payload.get("statusUrl")
                status_url = status_url_value.strip() if isinstance(status_url_value, str) else ""
                if not self._is_url(status_url):
                    raise RuntimeError("YTMP3 não retornou `statusUrl` válida para acompanhamento.")

                status_payload: dict[str, Any] = {}
                status_text = "pending"
                for _ in range(YTMP3_STATUS_MAX_POLLS):
                    status_payload = await self._ytmp3_get_json(
                        status_url,
                        timeout_seconds=YTMP3_STATUS_TIMEOUT_SECONDS,
                    )
                    status_text = str(status_payload.get("status") or "").strip().lower()
                    if status_text == "completed":
                        break
                    if status_text in {"failed", "error", "not_found"}:
                        detail = (
                            status_payload.get("error")
                            or status_payload.get("jobError")
                            or status_payload.get("message")
                            or "Falha desconhecida no processamento do YTMP3."
                        )
                        raise RuntimeError(str(detail))
                    await asyncio.sleep(YTMP3_STATUS_POLL_INTERVAL_SECONDS)

                if status_text != "completed":
                    raise RuntimeError("O YTMP3 demorou demais para concluir a conversão desta faixa.")

                stream_candidate = status_payload.get("downloadUrl")
                if not isinstance(stream_candidate, str) or not self._is_url(stream_candidate.strip()):
                    raise RuntimeError("O YTMP3 concluiu a conversão, mas não retornou URL de download válida.")

                stream_url = stream_candidate.strip()
                title_value = status_payload.get("title") or create_payload.get("title")
                title = str(title_value).strip() if isinstance(title_value, str) and str(title_value).strip() else "Faixa desconhecida"

                thumb_value = create_payload.get("thumbnail") or create_payload.get("thumbnailUrl")
                thumbnail_url = thumb_value.strip() if isinstance(thumb_value, str) and self._is_url(thumb_value.strip()) else None

                duration_ms: int | None = None
                for candidate_duration in (status_payload.get("duration"), create_payload.get("duration")):
                    duration_ms = self._duration_to_ms(candidate_duration)
                    if duration_ms is not None:
                        break

                identifier = self._extract_youtube_video_id(resolved_link) or ""
                if not identifier:
                    try:
                        parsed_status = urlparse(status_url)
                        status_parts = [part for part in parsed_status.path.strip("/").split("/") if part]
                        if status_parts:
                            identifier = status_parts[-1]
                    except Exception:
                        identifier = ""

                stream_expires_at = self._extract_stream_expires_at(stream_url)
                if stream_expires_at <= 0:
                    stream_expires_at = time.time() + 180

                return QueueTrack(
                    identifier=identifier,
                    title=title,
                    author="YouTube",
                    duration_ms=duration_ms,
                    webpage_url=resolved_link,
                    stream_url=stream_url,
                    thumbnail_url=thumbnail_url,
                    requester_id=requester_id,
                    search_query=query.strip(),
                    lookup_key=self._normalize_lookup_key(query.strip()),
                    original_input=query.strip(),
                    stream_expires_at=stream_expires_at,
                    play_attempts=0,
                    resolved_at=time.time(),
                    source="ytmp3.gg",
                )
            except Exception as exc:
                last_error = exc
            if candidate_index < len(resolved_links):
                LOGGER.info(
                    "Resolve falhou para candidato atual; tentando próximo (guild=%s).",
                    guild_id,
                )

        if last_error is None:
            raise RuntimeError("Não consegui resolver essa faixa no YTMP3.")
        raise RuntimeError(str(last_error)) from last_error

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
    def _extract_stream_http_status(exc: Exception | str) -> int | None:
        match = STREAM_HTTP_STATUS_RE.search(str(exc))
        if match is None:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _max_stream_retry_attempts_for_status(status_code: int | None) -> int:
        if status_code == 404:
            return 0
        if status_code in {429, 502, 503}:
            return len(STREAM_TRANSIENT_RETRY_DELAYS_SECONDS)
        if status_code in {401, 410}:
            return 1
        return 1

    @staticmethod
    def _stream_retry_delay_seconds(status_code: int | None, next_attempt_number: int) -> float:
        if status_code not in {429, 502, 503}:
            return 0.0

        index = min(max(next_attempt_number - 1, 0), len(STREAM_TRANSIENT_RETRY_DELAYS_SECONDS) - 1)
        base_delay = STREAM_TRANSIENT_RETRY_DELAYS_SECONDS[index]
        if status_code == 429:
            return base_delay + random.uniform(0.0, STREAM_429_JITTER_MAX_SECONDS)
        return base_delay

    @staticmethod
    def _ffmpeg_before_options(guild_id: int) -> str:
        return (
            "-reconnect 1 "
            "-reconnect_streamed 1 "
            "-reconnect_delay_max 5 "
            f'-headers "x-guild-id: {guild_id}"'
        )

    def _entry_candidate_url(self, entry: dict[str, Any]) -> str:
        for key in ("webpage_url", "original_url", "url"):
            value = entry.get(key)
            if isinstance(value, str) and self._is_url(value):
                return value.strip()

        identifier = entry.get("id")
        if isinstance(identifier, str):
            normalized = identifier.strip()
            if self._is_url(normalized):
                return normalized
            if YOUTUBE_ID_RE.fullmatch(normalized):
                return f"https://www.youtube.com/watch?v={normalized}"

        return ""

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
            "Resolve iniciado via YTMP3 (guild=%s, requester=%s, key=%s, force_refresh=%s).",
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
                    self._resolve_from_ytmp3(sanitized, guild_id, requester_id),
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
            )
            self._resolve_cache[lookup_key] = ResolveCacheEntry(
                track=cached_track,
                expires_at=self._cache_expiration_for_track(cached_track),
            )
            self._prune_resolve_cache()

        elapsed = asyncio.get_running_loop().time() - started_at
        LOGGER.info(
            "Resolve concluído via YTMP3 (guild=%s, key=%s, elapsed=%.2fs).",
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

    async def _try_recover_stream_failure(
        self,
        *,
        state: GuildMusicState,
        track: QueueTrack,
        guild_id: int,
        error: Exception | str,
    ) -> bool:
        if not self._is_likely_stream_retryable_error(error):
            return False

        status_code = self._extract_stream_http_status(error)
        max_attempts = self._max_stream_retry_attempts_for_status(status_code)
        if max_attempts <= 0:
            LOGGER.info(
                "Falha de stream sem retry por política (guild=%s, track=%s, status=%s).",
                guild_id,
                (track.identifier or "")[:40],
                status_code,
            )
            return False

        if track.play_attempts >= max_attempts:
            LOGGER.info(
                "Falha de stream acima do limite de tentativas (guild=%s, track=%s, status=%s, attempts=%s/%s).",
                guild_id,
                (track.identifier or "")[:40],
                status_code,
                track.play_attempts,
                max_attempts,
            )
            return False

        next_attempt = track.play_attempts + 1
        retry_delay = self._stream_retry_delay_seconds(status_code, next_attempt)
        if retry_delay > 0:
            await asyncio.sleep(retry_delay)

        retry_track = replace(track, play_attempts=next_attempt)
        try:
            refreshed = await self._refresh_track_if_needed(
                retry_track,
                guild_id,
                force_refresh=True,
            )
        except Exception as exc:
            LOGGER.warning(
                "Falha ao renovar stream após erro (guild=%s, track=%s, status=%s).",
                guild_id,
                (track.identifier or "")[:40],
                status_code,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return False

        state.queue.appendleft(refreshed)
        await self._send_music_message(
            state.announce_channel_id,
            f"Falha temporária no stream (status {status_code or 'desconhecido'}). Tentando novamente.",
        )
        return True

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
            "Renovando stream_url via YTMP3 (guild=%s, id=%s, force=%s).",
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

                try:
                    source = discord.FFmpegPCMAudio(
                        track.stream_url,
                        executable=ffmpeg_path,
                        before_options=self._ffmpeg_before_options(guild_id),
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
                    recovered = await self._try_recover_stream_failure(
                        state=state,
                        track=track,
                        guild_id=guild_id,
                        error=exc,
                    )
                    if recovered:
                        continue
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
                    recovered = await self._try_recover_stream_failure(
                        state=state,
                        track=track,
                        guild_id=guild_id,
                        error=exc,
                    )
                    if recovered:
                        continue
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
            if finished_track is not None:
                recovered = await self._try_recover_stream_failure(
                    state=state,
                    track=finished_track,
                    guild_id=guild_id,
                    error=error,
                )
                if not recovered:
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
        description="Diagnostica dependências de áudio local (FFmpeg, voz e endpoints de streaming).",
    )
    async def music_setup(self, interaction: discord.Interaction) -> None:
        issues = await self._dependency_issues(include_api_probe=True)

        status_lines = [
            f"PyNaCl: {'OK' if nacl is not None else 'FALHOU'}",
            f"FFmpeg: {self._ffmpeg_path()}",
            f"YTMP3 Search: {self._ytmp3_search_base_url()}",
            f"YTMP3 Download API: {self._ytmp3_download_api_url()}",
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
            + "\n\nTudo pronto para tocar áudio com o fluxo de streaming YTMP3.",
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
