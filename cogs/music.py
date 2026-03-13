import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urljoin

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
    track_id: str | None = None
    stream_expires_at_ms: int | None = None
    uploader_name: str | None = None
    source_name: str | None = None
    view_count: int | None = None


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
    prefetched_track_ids: set[str] = field(default_factory=set)
    prefetch_task: asyncio.Task[None] | None = None


class MusicCog(commands.Cog):
    music = app_commands.Group(name="music", description="Comandos para reproduzir musica em canal de voz.")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._players: dict[int, GuildMusicPlayer] = {}

    def cog_unload(self) -> None:
        for player in list(self._players.values()):
            if player.loop_task and not player.loop_task.done():
                player.loop_task.cancel()
            if player.prefetch_task and not player.prefetch_task.done():
                player.prefetch_task.cancel()
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
    def _format_view_count(view_count: int | None) -> str:
        if view_count is None:
            return "n/d"
        return f"{view_count:,}"

    def _build_track_embed(
        self,
        track: MusicTrack,
        *,
        header: str,
        queue_position: int | None = None,
    ) -> discord.Embed:
        description = f"[{track.title}]({track.webpage_url})" if self._is_url(track.webpage_url) else track.title
        embed = discord.Embed(
            title=header,
            description=description,
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Duracao", value=f"`{self._format_duration(track.duration_seconds)}`", inline=True)
        embed.add_field(name="Pedido por", value=f"<@{track.requester_id}>", inline=True)
        if queue_position is not None:
            embed.add_field(name="Posicao", value=f"`{queue_position}`", inline=True)

        if track.uploader_name:
            embed.add_field(name="Canal", value=track.uploader_name, inline=True)
        if track.source_name:
            embed.add_field(name="Fonte", value=track.source_name, inline=True)
        if track.view_count is not None:
            embed.add_field(name="Views", value=f"`{self._format_view_count(track.view_count)}`", inline=True)

        if track.track_id:
            embed.set_footer(text=f"track_id: {track.track_id}")
        if track.thumbnail_url and self._is_url(track.thumbnail_url):
            embed.set_thumbnail(url=track.thumbnail_url)
        return embed

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
    def _prefetch_enabled() -> bool:
        raw = os.getenv("YTDLS_PREFETCH_ENABLED", "true").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    @staticmethod
    def _friendly_ytdls_error(status_code: int, payload: str) -> str:
        lowered = payload.lower()
        if "anti-bot" in lowered or "cookies" in lowered or "not a bot" in lowered:
            return (
                "A API de yt-dl recusou esta faixa por verificacao anti-bot/cookies. "
                "Renove os cookies da API e tente novamente."
            )
        if "expirada" in lowered or "expired" in lowered:
            return "A URL de stream expirou. Recarregue a faixa e tente novamente."
        if status_code == 401:
            return "A API de yt-dl retornou 401 (acesso ao stream invalido/expirado)."
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
        uploader_name = (
            str(info.get("uploader")).strip()
            if isinstance(info.get("uploader"), str) and str(info.get("uploader")).strip()
            else None
        ) or (
            str(info.get("channel")).strip()
            if isinstance(info.get("channel"), str) and str(info.get("channel")).strip()
            else None
        )
        source_name = MusicCog._normalize_source_name(
            str(info.get("extractor_key")).strip()
            if isinstance(info.get("extractor_key"), str) and str(info.get("extractor_key")).strip()
            else None
        )
        view_count = MusicCog._parse_positive_int(info.get("view_count"))

        return MusicTrack(
            title=title,
            stream_url=str(stream_url),
            webpage_url=webpage_url,
            requester_id=0,
            duration_seconds=duration_seconds,
            thumbnail_url=safe_thumbnail,
            uploader_name=uploader_name,
            source_name=source_name,
            view_count=view_count,
        )

    @staticmethod
    def _select_thumbnail(*sources: object) -> str | None:
        for source in sources:
            if not isinstance(source, dict):
                continue
            thumbnail = source.get("thumbnail")
            if isinstance(thumbnail, str) and thumbnail.strip():
                return thumbnail.strip()
            thumbnails = source.get("thumbnails")
            if not isinstance(thumbnails, list):
                continue
            for item in thumbnails:
                if isinstance(item, dict):
                    url = item.get("url")
                    if isinstance(url, str) and url.strip():
                        return url.strip()
                elif isinstance(item, str) and item.strip():
                    return item.strip()
        return None

    @staticmethod
    def _parse_duration_seconds(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            duration = int(value)
            return duration if duration > 0 else None
        if isinstance(value, str):
            raw = value.strip()
            if raw.isdigit():
                duration = int(raw)
                return duration if duration > 0 else None
        return None

    @staticmethod
    def _parse_positive_int(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, float):
            parsed = int(value)
            return parsed if parsed >= 0 else None
        if isinstance(value, str):
            raw = value.strip()
            if raw.isdigit():
                return int(raw)
        return None

    @staticmethod
    def _select_first_string(keys: tuple[str, ...], *sources: object) -> str | None:
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key in keys:
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @staticmethod
    def _normalize_source_name(value: str | None) -> str | None:
        if value is None:
            return None
        lowered = value.strip().lower()
        if not lowered:
            return None
        if lowered == "youtube":
            return "YouTube"
        if lowered == "soundcloud":
            return "SoundCloud"
        return value.strip()

    @staticmethod
    def _parse_expires_at_ms(value: object) -> int | None:
        if isinstance(value, bool):
            return None

        expires: int | None
        if isinstance(value, (int, float)):
            expires = int(value)
        elif isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            if "." in raw:
                try:
                    expires = int(float(raw))
                except ValueError:
                    return None
            elif raw.isdigit():
                expires = int(raw)
            else:
                return None
        else:
            return None

        if expires <= 0:
            return None
        if expires < 10_000_000_000:
            return expires * 1000
        return expires

    @staticmethod
    def _build_absolute_url(api_base_url: str, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        raw = value.strip()
        if not raw:
            return None
        if raw.startswith(("http://", "https://")):
            return raw
        return urljoin(f"{api_base_url}/", raw.lstrip("/"))

    @staticmethod
    def _extract_search_result(payload: object) -> dict[str, object] | None:
        if not isinstance(payload, dict):
            return None
        result = payload.get("resultado")
        if isinstance(result, dict):
            return result
        if isinstance(result, list):
            first = next((item for item in result if isinstance(item, dict)), None)
            if first is not None:
                return first
        return None

    @staticmethod
    def _extract_link_from_result(result: dict[str, object]) -> str:
        for key in ("url", "link", "webpage_url"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        video_id = result.get("id")
        if isinstance(video_id, str) and video_id.strip():
            return f"https://www.youtube.com/watch?v={video_id.strip()}"
        return ""

    async def _search_track_via_ytdls_api(
        self,
        session: aiohttp.ClientSession,
        api_base_url: str,
        query: str,
    ) -> dict[str, object]:
        async with session.get(f"{api_base_url}/search", params={"q": query}) as response:
            payload_text = await response.text()
            if response.status != 200:
                raise RuntimeError(
                    self._friendly_ytdls_error(
                        status_code=response.status,
                        payload=payload_text,
                    )
                )
            try:
                payload = await response.json(content_type=None)
            except Exception as exc:
                raise RuntimeError("A API de yt-dl retornou resposta invalida em /search.") from exc

        if isinstance(payload, dict) and payload.get("sucesso") is False:
            message = payload.get("mensagem")
            if isinstance(message, str) and message.strip():
                raise RuntimeError(message.strip())
        result = self._extract_search_result(payload)
        if result is None:
            raise RuntimeError("A API de yt-dl nao encontrou resultados para esta busca.")
        return result

    async def _resolve_track_via_ytdls_api(
        self,
        session: aiohttp.ClientSession,
        api_base_url: str,
        link: str,
    ) -> dict[str, object]:
        payload_attempts = ({"link": link}, {"url": link}, {"query": link})
        last_error: RuntimeError | None = None

        for index, payload in enumerate(payload_attempts):
            async with session.post(f"{api_base_url}/resolve", json=payload) as response:
                payload_text = await response.text()
                if response.status == 404:
                    last_error = RuntimeError("A API de yt-dl nao expoe o endpoint /resolve.")
                    continue
                if response.status != 200:
                    lowered = payload_text.lower()
                    is_missing_link_payload = (
                        response.status == 400
                        and "link" in lowered
                        and ("obrigatorio" in lowered or "required" in lowered)
                    )
                    if is_missing_link_payload and index + 1 < len(payload_attempts):
                        continue
                    raise RuntimeError(
                        self._friendly_ytdls_error(
                            status_code=response.status,
                            payload=payload_text,
                        )
                    )
                try:
                    payload_json = await response.json(content_type=None)
                except Exception as exc:
                    raise RuntimeError("A API de yt-dl retornou resposta invalida em /resolve.") from exc
                if not isinstance(payload_json, dict):
                    raise RuntimeError("A API de yt-dl retornou payload invalido em /resolve.")
                return payload_json

        if last_error is not None:
            raise last_error
        raise RuntimeError("A API de yt-dl nao retornou dados em /resolve.")

    def _track_from_resolve_payload(
        self,
        *,
        api_base_url: str,
        resolve_payload: dict[str, object],
        search_result: dict[str, object],
        link: str,
        requester_id: int,
    ) -> MusicTrack:
        if resolve_payload.get("sucesso") is False:
            message = resolve_payload.get("mensagem")
            if isinstance(message, str) and message.strip():
                raise RuntimeError(message.strip())
            raise RuntimeError("A API de yt-dl falhou ao resolver esta faixa.")

        resolve_data = resolve_payload.get("resultado")
        if isinstance(resolve_data, dict):
            payload_data = resolve_data
        else:
            payload_data = resolve_payload

        stream_url = self._build_absolute_url(api_base_url, payload_data.get("stream_url"))
        if not stream_url:
            stream_url = self._build_absolute_url(api_base_url, payload_data.get("url"))
        if not stream_url:
            raise RuntimeError("A API de yt-dl nao retornou `stream_url` valido em /resolve.")

        title = payload_data.get("title")
        if not isinstance(title, str) or not title.strip():
            title = search_result.get("title")
        safe_title = str(title).strip() if isinstance(title, str) and title.strip() else "Faixa desconhecida"

        track_id_value = payload_data.get("track_id")
        track_id = str(track_id_value).strip() if isinstance(track_id_value, str) and track_id_value.strip() else None

        duration_seconds = self._parse_duration_seconds(payload_data.get("duration"))
        if duration_seconds is None:
            duration_seconds = self._parse_duration_seconds(search_result.get("duration"))
        uploader_name = self._select_first_string(("uploader", "channel"), payload_data, search_result)
        source_name = self._normalize_source_name(
            self._select_first_string(("ie_key", "extractor_key", "source"), payload_data, search_result)
        )
        view_count = self._parse_positive_int(payload_data.get("view_count"))
        if view_count is None:
            view_count = self._parse_positive_int(search_result.get("view_count"))

        return MusicTrack(
            title=safe_title,
            stream_url=stream_url,
            webpage_url=link,
            requester_id=requester_id,
            duration_seconds=duration_seconds,
            thumbnail_url=self._select_thumbnail(payload_data, search_result),
            track_id=track_id,
            stream_expires_at_ms=self._parse_expires_at_ms(payload_data.get("expires_at")),
            uploader_name=uploader_name,
            source_name=source_name,
            view_count=view_count,
        )

    async def _download_track_via_legacy_api(
        self,
        *,
        session: aiohttp.ClientSession,
        api_base_url: str,
        search_result: dict[str, object],
        link: str,
        requester_id: int,
    ) -> MusicTrack:
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

        title = search_result.get("title")
        safe_title = str(title).strip() if isinstance(title, str) and title.strip() else "Faixa desconhecida"
        duration_seconds = self._parse_duration_seconds(search_result.get("duration"))
        uploader_name = self._select_first_string(("uploader", "channel"), search_result)
        source_name = self._normalize_source_name(
            self._select_first_string(("ie_key", "extractor_key", "source"), search_result)
        )
        view_count = self._parse_positive_int(search_result.get("view_count"))

        return MusicTrack(
            title=safe_title,
            stream_url=temp_path,
            webpage_url=link,
            requester_id=requester_id,
            duration_seconds=duration_seconds,
            thumbnail_url=self._select_thumbnail(search_result),
            cleanup_path=temp_path,
            uploader_name=uploader_name,
            source_name=source_name,
            view_count=view_count,
        )

    async def _extract_track_via_ytdls_api(self, api_base_url: str, query: str, requester_id: int) -> MusicTrack:
        timeout = aiohttp.ClientTimeout(total=240, connect=20, sock_read=240)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            search_result = await self._search_track_via_ytdls_api(session, api_base_url, query)
            link = self._extract_link_from_result(search_result)
            if not link:
                raise RuntimeError("A API de yt-dl nao retornou URL valida para esta faixa.")
            try:
                resolve_payload = await self._resolve_track_via_ytdls_api(session, api_base_url, link)
                return self._track_from_resolve_payload(
                    api_base_url=api_base_url,
                    resolve_payload=resolve_payload,
                    search_result=search_result,
                    link=link,
                    requester_id=requester_id,
                )
            except RuntimeError as exc:
                LOGGER.info(
                    "Fallback para /download na API yt-dl (%s): %s",
                    api_base_url,
                    exc,
                )

            return await self._download_track_via_legacy_api(
                session=session,
                api_base_url=api_base_url,
                search_result=search_result,
                link=link,
                requester_id=requester_id,
            )

    async def _extract_track(self, query: str, requester_id: int) -> MusicTrack:
        api_base_url = self._ytdls_api_base_url()
        if api_base_url:
            try:
                return await self._extract_track_via_ytdls_api(api_base_url, query, requester_id)
            except Exception as exc:
                LOGGER.warning(
                    "Falha ao extrair faixa via API yt-dl (%s).",
                    api_base_url,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
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

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    async def _refresh_stream_url_if_needed(self, track: MusicTrack, *, force: bool = False) -> bool:
        if track.cleanup_path is not None:
            return False
        if not force:
            if not track.stream_expires_at_ms:
                return False
            expires_at_ms = track.stream_expires_at_ms
            if expires_at_ms - self._now_ms() > 30_000:
                return False

        api_base_url = self._ytdls_api_base_url()
        if not api_base_url:
            return False
        if not self._is_url(track.webpage_url):
            return False

        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            resolve_payload = await self._resolve_track_via_ytdls_api(session, api_base_url, track.webpage_url)

        refreshed_track = self._track_from_resolve_payload(
            api_base_url=api_base_url,
            resolve_payload=resolve_payload,
            search_result={
                "title": track.title,
                "duration": track.duration_seconds,
                "thumbnail": track.thumbnail_url,
                "uploader": track.uploader_name,
                "source": track.source_name,
                "view_count": track.view_count,
            },
            link=track.webpage_url,
            requester_id=track.requester_id,
        )
        track.stream_url = refreshed_track.stream_url
        track.stream_expires_at_ms = refreshed_track.stream_expires_at_ms
        if refreshed_track.track_id:
            track.track_id = refreshed_track.track_id
        track.uploader_name = refreshed_track.uploader_name or track.uploader_name
        track.source_name = refreshed_track.source_name or track.source_name
        track.view_count = refreshed_track.view_count if refreshed_track.view_count is not None else track.view_count
        return True

    async def _probe_stream_url(self, stream_url: str) -> None:
        timeout = aiohttp.ClientTimeout(total=12, connect=5, sock_read=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(stream_url, headers={"Range": "bytes=0-1"}) as response:
                if response.status in (200, 206):
                    return
                body = (await response.text()).strip()
                compact_body = " ".join(body.split())
                if len(compact_body) > 180:
                    compact_body = compact_body[:180] + "..."
                detail = f" | {compact_body}" if compact_body else ""
                raise RuntimeError(f"HTTP {response.status} ao validar stream{detail}")

    async def _ensure_track_stream_ready(self, track: MusicTrack) -> None:
        if track.cleanup_path is not None:
            return
        if not self._ytdls_api_base_url():
            return

        try:
            await self._probe_stream_url(track.stream_url)
            return
        except Exception as initial_exc:
            LOGGER.info("Probe inicial falhou para `%s`: %s", track.title, initial_exc)

        # Se o track_id foi invalidado (404/not found) ou upstream retornou 403/502,
        # pedimos um /resolve novo para obter stream_url fresco e tentamos de novo.
        await self._refresh_stream_url_if_needed(track, force=True)
        await self._probe_stream_url(track.stream_url)

    async def _prefetch_track_ids(self, api_base_url: str, track_ids: list[str]) -> bool:
        if not track_ids:
            return True

        timeout = aiohttp.ClientTimeout(total=25, connect=8, sock_read=25)
        payload_variants = ({"track_ids": track_ids}, {"tracks": track_ids})

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for index, payload in enumerate(payload_variants):
                async with session.post(f"{api_base_url}/prefetch", json=payload) as response:
                    if response.status in (200, 202):
                        return True
                    body = await response.text()
                    if response.status == 404:
                        return False
                    lowered = body.lower()
                    if response.status == 400 and index + 1 < len(payload_variants):
                        if "track_ids" in lowered or "tracks" in lowered:
                            continue
                    LOGGER.info("Prefetch ignorado na API yt-dl (%s): HTTP %s", api_base_url, response.status)
                    return False
        return False

    async def _prefetch_queue_for_player(self, player: GuildMusicPlayer) -> None:
        api_base_url = self._ytdls_api_base_url()
        if not api_base_url:
            return

        async with player.queue_lock:
            track_ids: list[str] = []
            for queued_track in list(player.queue)[:5]:
                track_id = queued_track.track_id.strip() if isinstance(queued_track.track_id, str) else ""
                if not track_id or track_id in player.prefetched_track_ids:
                    continue
                track_ids.append(track_id)
                player.prefetched_track_ids.add(track_id)

        if not track_ids:
            return
        try:
            prefetched = await self._prefetch_track_ids(api_base_url, track_ids)
            if prefetched:
                return
            async with player.queue_lock:
                for track_id in track_ids:
                    player.prefetched_track_ids.discard(track_id)
        except Exception as exc:
            LOGGER.info("Falha no prefetch de faixas da guild %s: %s", player.guild_id, exc)
            async with player.queue_lock:
                for track_id in track_ids:
                    player.prefetched_track_ids.discard(track_id)

    def _schedule_prefetch(self, player: GuildMusicPlayer) -> None:
        if not self._prefetch_enabled():
            return
        if player.prefetch_task and not player.prefetch_task.done():
            return
        player.prefetch_task = asyncio.create_task(
            self._prefetch_queue_for_player(player),
            name=f"music-prefetch-{player.guild_id}",
        )

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
            player.prefetched_track_ids.clear()
        for track in queued_tracks:
            self._cleanup_track_file(track)
        return count

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
            LOGGER.warning("Nao consegui enviar mensagem de musica no canal %s.", channel_id)

    async def _disconnect_player(self, guild_id: int, *, from_loop: bool = False) -> None:
        player = self._players.pop(guild_id, None)
        if player is None:
            return

        if player.prefetch_task and not player.prefetch_task.done():
            player.prefetch_task.cancel()

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

            if isinstance(track.track_id, str) and track.track_id:
                player.prefetched_track_ids.discard(track.track_id)
            self._schedule_prefetch(player)

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
                await self._refresh_stream_url_if_needed(track)
                await self._ensure_track_stream_ready(track)
            except Exception as exc:
                LOGGER.warning(
                    "Stream indisponivel para `%s` na guild %s.",
                    track.title,
                    player.guild_id,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                await self._send_music_message(
                    player.announce_channel_id,
                    f"Falha ao validar stream de `{track.title}`. Pulando para a proxima.",
                )
                self._cleanup_track_file(track)
                player.current = None
                continue

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

            now_playing_embed = self._build_track_embed(track, header="Tocando Agora")
            await self._send_music_message(
                player.announce_channel_id,
                "Reproducao iniciada.",
                embed=now_playing_embed,
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
            self._schedule_prefetch(player)

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

        self._schedule_prefetch(player)
        self._start_loop_if_needed(player)

        if queued_position <= 1 and player.current is None:
            header = "Preparando para Tocar"
            queue_position = None
        else:
            header = "Adicionado na Fila"
            queue_position = queued_position

        await interaction.followup.send(
            embed=self._build_track_embed(
                track,
                header=header,
                queue_position=queue_position,
            )
        )

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
        await self._respond_embed(
            interaction,
            embed=self._build_track_embed(track, header="Tocando Agora"),
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
