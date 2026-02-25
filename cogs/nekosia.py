import asyncio
import logging
import re
import time
from typing import Any
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

LOGGER = logging.getLogger("ayana.nekosia")
NEKOSIA_API_BASE = "https://api.nekosia.cat/api/v1"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_IMAGES_PER_REQUEST = 5
TAG_CACHE_TTL_SECONDS = 1800

MAIN_CATEGORIES = (
    "catgirl",
    "foxgirl",
    "wolfgirl",
    "maid",
    "feet",
    "coffee",
    "food",
    "random",
)

NO_RESULTS_MESSAGE_PREFIX = "No images matching the specified criteria were found."
BLACKLIST_MESSAGE_PREFIX = "That tag is on the blacklist."
AGE_RESTRICTED_HINTS = (
    "ero",
    "ecchi",
    "hentai",
    "nsfw",
    "lewd",
    "explicit",
    "r18",
    "18+",
    "smut",
)
AGE_RESTRICTED_RATINGS = {"suggestive", "nsfw", "explicit", "r18"}


class NekosiaRequestError(RuntimeError):
    """Raised when NekoSia API request fails."""


def _clean_csv(value: str | None) -> str | None:
    if value is None:
        return None
    parts = [item.strip() for item in value.split(",") if item.strip()]
    if not parts:
        return None
    deduplicated = list(dict.fromkeys(parts))
    return ",".join(deduplicated)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _read_list_of_strings(payload: dict[str, Any], key: str) -> list[str]:
    raw_values = payload.get(key)
    if not isinstance(raw_values, list):
        return []
    return [value.strip() for value in raw_values if isinstance(value, str) and value.strip()]


def _rating_value(image_payload: dict[str, Any]) -> str:
    rating = image_payload.get("rating")
    if isinstance(rating, str):
        return rating
    if isinstance(rating, dict):
        raw_value = rating.get("rating")
        if isinstance(raw_value, str) and raw_value:
            return raw_value
    return "unknown"


def _hex_to_discord_color(value: Any) -> discord.Color:
    if not isinstance(value, str):
        return discord.Color.blurple()

    cleaned = value.strip().removeprefix("#")
    if len(cleaned) != 6:
        return discord.Color.blurple()

    try:
        return discord.Color(int(cleaned, 16))
    except ValueError:
        return discord.Color.blurple()


def _resolve_image_url(image_payload: dict[str, Any]) -> str | None:
    image_block = image_payload.get("image")
    if not isinstance(image_block, dict):
        return None

    for variant in ("compressed", "original"):
        item = image_block.get(variant)
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if isinstance(url, str) and url:
            return url
    return None


def _is_expected_filter_error(message: str) -> bool:
    normalized = message.strip()
    return normalized.startswith(NO_RESULTS_MESSAGE_PREFIX) or normalized.startswith(
        BLACKLIST_MESSAGE_PREFIX
    )


def _is_age_restricted_rating(value: str) -> bool:
    return value.strip().lower() in AGE_RESTRICTED_RATINGS


def _contains_age_restricted_hint(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    if not normalized:
        return False
    compact = re.sub(r"[\s_\-]+", "", normalized)
    for hint in AGE_RESTRICTED_HINTS:
        token = hint.lower()
        if token.isalpha():
            if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", normalized):
                return True
        elif token in normalized:
            return True
        compact_token = token.replace("+", "")
        if compact_token and compact_token in compact:
            return True
    return False


class NekosiaCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._tags_cache: dict[str, list[str]] | None = None
        self._tags_cache_at = 0.0

    async def _api_get(
        self,
        path: str,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        url = f"{NEKOSIA_API_BASE}{path}"

        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.get(url, params=params) as response:
                    data = await response.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise NekosiaRequestError("Tempo de resposta da API excedido.") from exc
        except aiohttp.ClientError as exc:
            raise NekosiaRequestError("Falha ao conectar na API da NekoSia.") from exc
        except ValueError as exc:
            raise NekosiaRequestError("A API retornou JSON invalido.") from exc

        if not isinstance(data, dict):
            raise NekosiaRequestError("Resposta inesperada da API.")

        success = data.get("success")
        status = data.get("status", response.status)
        if response.status >= 400 or success is False:
            message = data.get("message")
            if not isinstance(message, str) or not message.strip():
                message = f"Erro da API (status {status})."
            raise NekosiaRequestError(message)

        return data

    @staticmethod
    def _extract_images(payload: dict[str, Any]) -> list[dict[str, Any]]:
        images = payload.get("images")
        if isinstance(images, list):
            return [item for item in images if isinstance(item, dict)]
        if "image" in payload and "id" in payload:
            return [payload]
        return []

    @staticmethod
    def _tags_preview(tags: list[str], max_items: int = 10) -> str:
        if not tags:
            return "N/A"
        selected = tags[:max_items]
        rendered = ", ".join(selected)
        if len(tags) > max_items:
            rendered += f" +{len(tags) - max_items}"
        if len(rendered) > 900:
            rendered = rendered[:897] + "..."
        return rendered

    def _build_image_embed(
        self,
        image_payload: dict[str, Any],
        index: int,
        total: int,
    ) -> discord.Embed:
        category = image_payload.get("category")
        category_label = category if isinstance(category, str) and category else "unknown"
        rating = _rating_value(image_payload)
        image_id = image_payload.get("id")
        image_id_label = image_id if isinstance(image_id, str) else "N/A"
        tags = _read_list_of_strings(image_payload, "tags")

        color = _hex_to_discord_color(
            image_payload.get("colors", {}).get("main")
            if isinstance(image_payload.get("colors"), dict)
            else None
        )
        embed = discord.Embed(
            title=f"NekoSia - {category_label}",
            description=f"Tags: `{self._tags_preview(tags)}`",
            color=color,
        )
        embed.add_field(name="ID", value=f"`{image_id_label}`", inline=True)
        embed.add_field(name="Rating", value=f"`{rating}`", inline=True)
        embed.add_field(name="Categoria", value=f"`{category_label}`", inline=True)

        source = image_payload.get("source")
        if isinstance(source, dict):
            source_url = source.get("url")
            if isinstance(source_url, str) and source_url:
                embed.add_field(name="Fonte", value=source_url, inline=False)

        attribution = image_payload.get("attribution")
        if isinstance(attribution, dict):
            artist = attribution.get("artist")
            if isinstance(artist, dict):
                username = artist.get("username")
                profile = artist.get("profile")
                if isinstance(username, str) and username:
                    if isinstance(profile, str) and profile:
                        embed.add_field(name="Artista", value=f"[{username}]({profile})", inline=True)
                    else:
                        embed.add_field(name="Artista", value=username, inline=True)

        image_url = _resolve_image_url(image_payload)
        if image_url:
            embed.set_image(url=image_url)

        embed.set_footer(text=f"Imagem {index}/{total}")
        return embed

    async def _get_tags_catalog(self) -> dict[str, list[str]]:
        now = time.monotonic()
        if self._tags_cache and (now - self._tags_cache_at) < TAG_CACHE_TTL_SECONDS:
            return self._tags_cache

        payload = await self._api_get("/tags")
        catalog = {
            "tags": _read_list_of_strings(payload, "tags"),
            "anime": _read_list_of_strings(payload, "anime"),
            "characters": _read_list_of_strings(payload, "characters"),
        }
        self._tags_cache = catalog
        self._tags_cache_at = now
        return catalog

    @staticmethod
    def _is_age_restricted_context(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        channel = interaction.channel
        if channel is None:
            return False

        checker = getattr(channel, "is_nsfw", None)
        if callable(checker):
            try:
                if checker():
                    return True
            except TypeError:
                pass

        parent = getattr(channel, "parent", None)
        parent_checker = getattr(parent, "is_nsfw", None)
        if callable(parent_checker):
            try:
                return bool(parent_checker())
            except TypeError:
                return False
        return False

    def _requires_age_restricted_channel(
        self,
        *,
        category: str,
        additional_tags: str | None,
        rating: str,
    ) -> bool:
        if _is_age_restricted_rating(rating):
            return True
        if _contains_age_restricted_hint(category):
            return True
        return any(_contains_age_restricted_hint(tag) for tag in _split_csv(additional_tags))

    @app_commands.command(name="nekosia", description="Busca imagens na API NekoSia.")
    @app_commands.choices(
        rating=[
            app_commands.Choice(name="safe", value="safe"),
            app_commands.Choice(name="suggestive", value="suggestive"),
        ]
    )
    @app_commands.describe(
        category="Categoria principal (ex.: catgirl, maid) ou uma tag (ex.: ero).",
        count="Quantidade de imagens (1 a 5).",
        additional_tags="Tags extras separadas por virgula.",
        blacklisted_tags="Tags para excluir separadas por virgula.",
        rating="Filtro de classificacao de conteudo (suggestive exige canal +18).",
    )
    async def nekosia(
        self,
        interaction: discord.Interaction,
        category: str = "catgirl",
        count: app_commands.Range[int, 1, MAX_IMAGES_PER_REQUEST] = 1,
        additional_tags: str | None = None,
        blacklisted_tags: str | None = None,
        rating: str | None = None,
    ) -> None:
        requested_category = category.strip()
        if not requested_category:
            await interaction.response.send_message(
                "Informe uma categoria valida para consulta.",
                ephemeral=True,
            )
            return

        normalized_additional = _clean_csv(additional_tags)
        normalized_blacklist = _clean_csv(blacklisted_tags)
        normalized_rating = (rating or "safe").strip().lower()
        if normalized_rating not in {"safe", "suggestive"}:
            await interaction.response.send_message(
                "Rating invalido. Use `safe` ou `suggestive`.",
                ephemeral=True,
            )
            return

        if self._requires_age_restricted_channel(
            category=requested_category,
            additional_tags=normalized_additional,
            rating=normalized_rating,
        ) and not self._is_age_restricted_context(interaction):
            await interaction.response.send_message(
                "Conteudo suggestive/NSFW so pode ser consultado em canal marcado como +18.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        query_params: dict[str, str | int] = {
            "count": int(count),
            "session": "id",
            "id": str(interaction.user.id),
            "rating": normalized_rating,
        }
        if normalized_additional:
            query_params["additionalTags"] = normalized_additional
        if normalized_blacklist:
            query_params["blacklistedTags"] = normalized_blacklist

        endpoint = f"/images/{quote(requested_category, safe='')}"
        fallback_used = False
        try:
            payload = await self._api_get(endpoint, params=query_params)
        except NekosiaRequestError as exc:
            error_message = str(exc)
            should_fallback_to_tags = requested_category not in MAIN_CATEGORIES and error_message.startswith(
                NO_RESULTS_MESSAGE_PREFIX
            )

            if should_fallback_to_tags:
                fallback_params = dict(query_params)
                merged_tags = [requested_category, *_split_csv(normalized_additional)]
                fallback_params["additionalTags"] = ",".join(dict.fromkeys(merged_tags))
                fallback_endpoint = "/images/nothing"
                try:
                    payload = await self._api_get(fallback_endpoint, params=fallback_params)
                    fallback_used = True
                except NekosiaRequestError as fallback_exc:
                    error_message = str(fallback_exc)
                    if _is_expected_filter_error(error_message):
                        LOGGER.info(
                            "Consulta NekoSia sem resultados (categoria '%s' com fallback em tags): %s",
                            requested_category,
                            error_message,
                        )
                    else:
                        LOGGER.warning(
                            "Falha na consulta NekoSia em categoria '%s' com fallback em tags: %s",
                            requested_category,
                            error_message,
                        )
                    await interaction.followup.send(
                        f"Falha ao consultar NekoSia: {error_message}",
                        ephemeral=True,
                    )
                    return
            else:
                if _is_expected_filter_error(error_message):
                    LOGGER.info(
                        "Consulta NekoSia sem resultados (categoria '%s'): %s",
                        requested_category,
                        error_message,
                    )
                else:
                    LOGGER.warning("Falha na consulta NekoSia em categoria '%s': %s", requested_category, error_message)
                await interaction.followup.send(
                    f"Falha ao consultar NekoSia: {error_message}",
                    ephemeral=True,
                )
                return

        images = self._extract_images(payload)
        if not images:
            await interaction.followup.send(
                "A API respondeu, mas nao retornou imagens para os filtros informados.",
                ephemeral=True,
            )
            return

        if not self._is_age_restricted_context(interaction):
            safe_images = [image for image in images if not _is_age_restricted_rating(_rating_value(image))]
            if not safe_images:
                await interaction.followup.send(
                    "A resposta continha apenas conteudo +18 e foi bloqueada neste canal.",
                    ephemeral=True,
                )
                return
            images = safe_images

        embeds = [
            self._build_image_embed(image, index + 1, len(images))
            for index, image in enumerate(images[:MAX_IMAGES_PER_REQUEST])
        ]
        if fallback_used:
            for embed in embeds:
                embed.set_footer(
                    text=f"{embed.footer.text} | fallback: category=nothing + additionalTags"
                )
        await interaction.followup.send(embeds=embeds)

    @nekosia.autocomplete("category")
    async def nekosia_category_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        del interaction
        normalized = current.lower().strip()
        if not normalized:
            choices = MAIN_CATEGORIES[:25]
        else:
            choices = [category for category in MAIN_CATEGORIES if normalized in category.lower()][:25]
        return [app_commands.Choice(name=category, value=category) for category in choices]

    @app_commands.command(name="nekosia_id", description="Busca uma imagem da NekoSia pelo ID.")
    @app_commands.describe(image_id="ID da imagem retornado pela API.")
    async def nekosia_id(
        self,
        interaction: discord.Interaction,
        image_id: str,
    ) -> None:
        normalized_id = image_id.strip()
        if not normalized_id:
            await interaction.response.send_message("Informe um ID valido.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        endpoint = f"/getImageById/{quote(normalized_id, safe='')}"
        try:
            payload = await self._api_get(endpoint)
        except NekosiaRequestError as exc:
            LOGGER.warning("Falha na consulta NekoSia por ID '%s': %s", normalized_id, exc)
            await interaction.followup.send(f"Falha ao consultar NekoSia: {exc}", ephemeral=True)
            return

        if not self._is_age_restricted_context(interaction):
            image_rating = _rating_value(payload)
            if _is_age_restricted_rating(image_rating):
                await interaction.followup.send(
                    "Esta imagem e classificada como +18 e nao pode ser exibida neste canal.",
                    ephemeral=True,
                )
                return

        embeds = [self._build_image_embed(payload, 1, 1)]
        await interaction.followup.send(embeds=embeds)

    @app_commands.command(name="nekosia_tags", description="Lista tags, animes ou personagens da NekoSia.")
    @app_commands.choices(
        tipo=[
            app_commands.Choice(name="tags", value="tags"),
            app_commands.Choice(name="anime", value="anime"),
            app_commands.Choice(name="characters", value="characters"),
        ]
    )
    @app_commands.describe(
        tipo="Conjunto para busca (tags, anime, characters).",
        termo="Filtro opcional para encontrar itens especificos.",
    )
    async def nekosia_tags(
        self,
        interaction: discord.Interaction,
        tipo: str = "tags",
        termo: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            catalog = await self._get_tags_catalog()
        except NekosiaRequestError as exc:
            LOGGER.warning("Falha ao consultar catalogo de tags NekoSia: %s", exc)
            await interaction.followup.send(f"Falha ao consultar NekoSia: {exc}", ephemeral=True)
            return

        dataset = catalog.get(tipo, [])
        if not dataset:
            await interaction.followup.send(
                "Nao encontrei resultados para esse tipo de listagem.",
                ephemeral=True,
            )
            return

        filtered = dataset
        if termo:
            normalized = termo.lower().strip()
            filtered = [item for item in dataset if normalized in item.lower()]

        if not filtered:
            await interaction.followup.send(
                "Nenhum item corresponde ao filtro informado.",
                ephemeral=True,
            )
            return

        shown = filtered[:25]
        embed = discord.Embed(
            title=f"NekoSia - {tipo}",
            description="\n".join(f"- `{item}`" for item in shown),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Exibindo {len(shown)} de {len(filtered)} resultado(s).")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NekosiaCog(bot))
