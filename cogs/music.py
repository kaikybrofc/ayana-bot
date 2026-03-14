from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

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
YTDLP_RESOLVE_TIMEOUT_SECONDS = 25
YTDLP_ERROR_SNIPPET_LIMIT = 600
MAX_SEARCH_CANDIDATES = 8
YTDLP_PLAYBACK_RETRIES = 2
YTDLP_RETRY_DELAY_SECONDS = 0.6
YTDLP_YOUTUBE_EXTRACTOR_ARGS = "youtube:player_client=tv_downgraded,android,web"
YTDLP_PLAYBACK_FORMATS = (
    "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
    "bestaudio/best",
    "best",
)


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
    source: str = "youtube"


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

    def cog_unload(self) -> None:
        for state in self._states.values():
            self._cancel_idle_task(state)

        for voice_client in list(self.bot.voice_clients):
            if isinstance(voice_client, discord.VoiceClient) and voice_client.is_connected():
                asyncio.create_task(voice_client.disconnect(force=True))

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

    async def _dependency_issues(self) -> list[str]:
        issues: list[str] = []

        if nacl is None:
            issues.append("Dependência ausente: instale `PyNaCl` (`pip install PyNaCl`).")

        ffmpeg_path = self._ffmpeg_path()
        if not self._path_available(ffmpeg_path):
            issues.append(f"FFmpeg não encontrado em `{ffmpeg_path}`.")

        ytdlp_path = self._ytdlp_path()
        if not self._path_available(ytdlp_path):
            issues.append(f"yt-dlp não encontrado em `{ytdlp_path}`.")

        cookies_path = self._ytdlp_cookies_path()
        if not cookies_path:
            issues.append("Cookies não configurados: defina `MUSIC_YTDLP_COOKIES_PATH`.")
        elif not os.path.exists(cookies_path):
            issues.append(f"Arquivo de cookies não encontrado: `{cookies_path}`.")

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

    async def _run_ytdlp_json(
        self,
        identifier: str,
        *,
        playback: bool,
        format_selector: str | None = None,
        apply_format: bool = True,
    ) -> dict[str, Any]:
        executable = self._ytdlp_path()
        if not self._path_available(executable):
            raise RuntimeError(f"yt-dlp não encontrado em `{executable}`.")

        args = [
            executable,
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

        if playback:
            args.extend(["--extractor-args", YTDLP_YOUTUBE_EXTRACTOR_ARGS])
            if apply_format:
                selector = (format_selector or "bestaudio/best").strip()
                if selector:
                    args.extend(["-f", selector])
        elif identifier.startswith("ytsearch"):
            args.extend(["--flat-playlist"])
        else:
            args.extend(["--flat-playlist", "--no-playlist"])

        args.extend(["-J", identifier])

        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                process.communicate(),
                timeout=YTDLP_RESOLVE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError("Timeout ao consultar yt-dlp.") from None

        stdout_text = stdout_data.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
        output_text = "\n".join(part for part in (stdout_text, stderr_text) if part).strip()

        if process.returncode != 0:
            detail = self._truncate_for_log(output_text or "sem detalhe")
            raise RuntimeError(f"yt-dlp falhou ({detail}).")

        if not output_text:
            raise RuntimeError("yt-dlp retornou saída vazia.")

        json_start = output_text.find("{")
        json_end = output_text.rfind("}")
        payload = output_text
        if json_start != -1 and json_end > json_start:
            payload = output_text[json_start : json_end + 1]

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            detail = self._truncate_for_log(output_text)
            raise RuntimeError(f"Não consegui interpretar a resposta do yt-dlp ({detail}).") from exc

        if not isinstance(data, dict):
            raise RuntimeError("yt-dlp retornou payload inválido.")

        return data

    async def _run_ytdlp_json_with_retry(
        self,
        identifier: str,
        *,
        playback: bool,
        format_selector: str | None = None,
        apply_format: bool = True,
    ) -> dict[str, Any]:
        last_exception: Exception | None = None
        attempts = YTDLP_PLAYBACK_RETRIES if playback else 1

        for attempt in range(1, attempts + 1):
            try:
                return await self._run_ytdlp_json(
                    identifier,
                    playback=playback,
                    format_selector=format_selector,
                    apply_format=apply_format,
                )
            except Exception as exc:
                last_exception = exc
                if attempt >= attempts:
                    break
                await asyncio.sleep(YTDLP_RETRY_DELAY_SECONDS * attempt)

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

    async def _resolve_query_to_track(self, query: str, requester_id: int) -> QueueTrack:
        sanitized = query.strip()
        if not sanitized:
            raise RuntimeError("Informe uma URL ou termo de busca válido.")

        is_url = self._is_url(sanitized)
        identifier = sanitized if is_url else f"ytsearch10:{sanitized}"

        search_payload = await self._run_ytdlp_json(identifier, playback=False)
        candidates = self._extract_entries_from_search(search_payload)
        if not candidates:
            raise RuntimeError("Nenhum resultado encontrado para a busca.")

        last_exception: Exception | None = None
        for entry in candidates[:MAX_SEARCH_CANDIDATES]:
            entry_url = self._entry_candidate_url(entry)
            if not entry_url:
                continue

            try:
                resolved_payload = await self._run_ytdlp_json_with_retry(
                    entry_url,
                    playback=True,
                    apply_format=False,
                )
                extracted_url = self._extract_stream_url_from_resolved(resolved_payload)
                if extracted_url and self._is_url(extracted_url):
                    return self._build_queue_track(
                        query=sanitized,
                        requester_id=requester_id,
                        entry=entry,
                        resolved=resolved_payload,
                        stream_url_override=extracted_url,
                    )
            except Exception as exc:
                last_exception = exc
                LOGGER.debug(
                    "Falha em resolve sem seletor para '%s' (url=%s).",
                    sanitized,
                    entry_url,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

            for format_selector in YTDLP_PLAYBACK_FORMATS:
                try:
                    resolved_payload = await self._run_ytdlp_json_with_retry(
                        entry_url,
                        playback=True,
                        format_selector=format_selector,
                    )
                    extracted_url = self._extract_stream_url_from_resolved(resolved_payload)
                    if not extracted_url:
                        raise RuntimeError("yt-dlp não retornou URL de stream válida.")
                    return self._build_queue_track(
                        query=sanitized,
                        requester_id=requester_id,
                        entry=entry,
                        resolved=resolved_payload,
                        stream_url_override=extracted_url,
                    )
                except Exception as exc:
                    last_exception = exc
                    LOGGER.debug(
                        "Falha ao preparar candidato para '%s' (url=%s, format=%s).",
                        sanitized,
                        entry_url,
                        format_selector,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )

        if last_exception is not None:
            raise RuntimeError("Não consegui preparar essa faixa para reprodução agora.") from last_exception

        raise RuntimeError("Não consegui preparar nenhuma faixa reproduzível para essa busca.")

    async def _connect_to_member_channel(
        self,
        guild: discord.Guild,
        member: discord.Member,
        state: GuildMusicState,
    ) -> discord.VoiceClient:
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
        if voice_client and not isinstance(voice_client, discord.VoiceClient):
            raise RuntimeError("Já existe outro cliente de voz ativo neste servidor. Desconecte-o e tente novamente.")

        if voice_client and voice_client.is_connected():
            if voice_client.channel and voice_client.channel.id != target_channel.id:
                await voice_client.move_to(target_channel)
            client = voice_client
        else:
            try:
                client = await target_channel.connect(self_deaf=True)
            except TypeError:
                client = await target_channel.connect()

        self._cancel_idle_task(state)
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

    async def _play_next(self, guild_id: int) -> bool:
        state = self._get_state(guild_id)

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
                    await self._send_music_message(
                        state.announce_channel_id,
                        "Falha ao preparar a faixa para reprodução. Tentando a próxima da fila.",
                    )
                    continue

                state.current = track
                state.current_source = wrapped
                self._cancel_idle_task(state)

                def _after_playback(error: Exception | None) -> None:
                    self.bot.loop.call_soon_threadsafe(
                        asyncio.create_task,
                        self._on_playback_finished(guild_id, error),
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
                return True

            state.current = None
            state.current_source = None
            self._schedule_idle_disconnect(guild_id)
            return False

    async def _on_playback_finished(self, guild_id: int, error: Exception | None) -> None:
        state = self._get_state(guild_id)

        if error is not None:
            LOGGER.warning(
                "Erro durante playback na guild %s.",
                guild_id,
                exc_info=(type(error), error, error.__traceback__),
            )
            await self._send_music_message(
                state.announce_channel_id,
                "Falha durante a reprodução da faixa atual. Tentando a próxima da fila.",
            )

        state.current = None
        state.current_source = None
        await self._play_next(guild_id)

    @music.command(
        name="setup",
        description="Diagnostica dependências de áudio local (FFmpeg, yt-dlp e cookies).",
    )
    async def music_setup(self, interaction: discord.Interaction) -> None:
        issues = await self._dependency_issues()

        cookies_path = self._ytdlp_cookies_path()
        cookies_exists = bool(cookies_path and os.path.exists(cookies_path))

        status_lines = [
            f"PyNaCl: {'OK' if nacl is not None else 'FALHOU'}",
            f"FFmpeg: {self._ffmpeg_path()}",
            f"yt-dlp: {self._ytdlp_path()}",
            f"JS runtime: {self._ytdlp_runtime()}",
            (
                f"Cookies: OK ({cookies_path})"
                if cookies_exists
                else (
                    f"Cookies: arquivo não encontrado ({cookies_path})"
                    if cookies_path
                    else "Cookies: não configurado (defina `MUSIC_YTDLP_COOKIES_PATH`)"
                )
            ),
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
            + "\n\nTudo pronto para tocar áudio com Python + yt-dlp.",
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
                "Falha ao conectar no canal de voz antes de tocar na guild %s.",
                guild.id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.followup.send(
                "Não consegui entrar no canal de voz. Verifique permissões de `Connect` e `Speak`.",
                ephemeral=True,
            )
            return

        if len(state.queue) >= MAX_QUEUE_ITEMS:
            await interaction.followup.send(
                f"A fila já atingiu o limite de {MAX_QUEUE_ITEMS} faixas.",
                ephemeral=True,
            )
            return

        try:
            track = await self._resolve_query_to_track(query, member.id)
        except Exception as exc:
            LOGGER.warning(
                "Falha ao resolver faixa para /music play na guild %s.",
                guild.id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            detail = str(exc).strip()
            await interaction.followup.send(
                detail or "Não consegui carregar essa faixa. Tente outra URL ou termo de busca.",
                ephemeral=True,
            )
            return

        was_idle = not voice_client.is_playing() and not voice_client.is_paused() and state.current is None and not state.queue

        state.queue.append(track)

        if was_idle:
            started = await self._play_next(guild.id)
            if not started:
                await interaction.followup.send(
                    "Não consegui iniciar essa faixa agora. Tente novamente em instantes.",
                    ephemeral=True,
                )
                return
            header = "Preparando para tocar"
            queue_position = None
        else:
            header = "Adicionado na Fila"
            queue_position = len(state.queue) + (1 if state.current else 0)

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
