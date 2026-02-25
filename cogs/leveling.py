import io
import logging
import math
import os
import random
import time
import unicodedata
from contextlib import nullcontext
from typing import Any

import aiohttp
import discord
import regex as regex_lib
from discord import app_commands
from discord.ext import commands

from warn_store import total_xp_for_level, xp_for_next_level

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:
    Image = None
    ImageDraw = None
    ImageFilter = None
    ImageFont = None

try:
    from pilmoji import Pilmoji
    from pilmoji.source import Twemoji
except ImportError:
    Pilmoji = None
    Twemoji = None

LOGGER = logging.getLogger("ayana.cogs.leveling")


class LevelingCog(commands.Cog):
    CARD_WIDTH = 1200
    RANK_CARD_HEIGHT = 500
    RANK_RENDER_SCALE = 2
    BOARD_WIDTH = 1200
    BOARD_HEIGHT = 500
    BOARD_ROW_HEIGHT = 54
    BOARD_MAX_ROWS = 5
    XP_COOLDOWN_SECONDS = 30.0

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._rng = random.Random()
        self._xp_cooldowns: dict[tuple[int, int], float] = {}

    def _warn_store(self):
        warn_store = getattr(self.bot, "warn_store", None)
        if warn_store is None:
            raise RuntimeError("WarnStore nao inicializado.")
        return warn_store

    @staticmethod
    def _ensure_canvas_support() -> None:
        if Image is None or ImageDraw is None or ImageFont is None:
            raise RuntimeError(
                "Canvas indisponivel: instale `Pillow` (rode `pip install -r requirements.txt`)."
            )

    @staticmethod
    def _resample_filter() -> int:
        if Image is None:
            return 1
        resampling = getattr(Image, "Resampling", None)
        if resampling is not None:
            return int(resampling.LANCZOS)
        return int(Image.LANCZOS)

    @staticmethod
    def _discord_asset_size(
        target: int,
        *,
        min_size: int = 16,
        max_size: int = 4096,
    ) -> int:
        safe_min = max(16, int(min_size))
        safe_max = min(4096, int(max_size))
        if safe_min > safe_max:
            safe_min, safe_max = 16, 4096

        # Discord exige potência de 2 no intervalo [16, 4096].
        clamped = max(safe_min, min(safe_max, int(target)))
        size = safe_min
        while size < clamped and size < safe_max:
            size <<= 1
        return min(size, safe_max)

    @staticmethod
    def _load_font(size: int, *, bold: bool = False):
        if ImageFont is None:
            return None

        font_names = (
            (
                "NotoSans-Bold.ttf",
                "NotoSansDisplay-Bold.ttf",
                "NotoSansCJK-Bold.ttc",
                "NotoSansJP-Bold.otf",
                "NotoSansKR-Bold.otf",
                "NotoSansSC-Bold.otf",
                "NotoSansTC-Bold.otf",
                "DejaVuSans-Bold.ttf",
                "LiberationSans-Bold.ttf",
                "FreeSansBold.otf",
                "Arial Unicode.ttf",
                "Arial Unicode MS.ttf",
                "NotoSansSymbols2-Regular.ttf",
                "NotoEmoji-Regular.ttf",
                "NotoColorEmoji.ttf",
                "Symbola.ttf",
                "Segoe UI Emoji.ttf",
            )
            if bold
            else (
                "NotoSans-Regular.ttf",
                "NotoSansDisplay-Regular.ttf",
                "NotoSansCJK-Regular.ttc",
                "NotoSansJP-Regular.otf",
                "NotoSansKR-Regular.otf",
                "NotoSansSC-Regular.otf",
                "NotoSansTC-Regular.otf",
                "DejaVuSans.ttf",
                "LiberationSans-Regular.ttf",
                "FreeSans.otf",
                "Arial Unicode.ttf",
                "Arial Unicode MS.ttf",
                "NotoSansSymbols2-Regular.ttf",
                "NotoEmoji-Regular.ttf",
                "NotoColorEmoji.ttf",
                "Symbola.ttf",
                "Segoe UI Emoji.ttf",
            )
        )
        search_roots = (
            "",
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.expanduser("~/.fonts"),
            os.path.expanduser("~/.local/share/fonts"),
        )
        search_subdirs = (
            "",
            "truetype",
            "truetype/noto",
            "truetype/dejavu",
            "liberation",
            "gnu-free",
            "noto",
            "opentype/noto",
        )
        candidates: list[str] = list(font_names)
        for root in search_roots:
            if not root:
                continue
            for subdir in search_subdirs:
                base = os.path.join(root, subdir) if subdir else root
                for font_name in font_names:
                    candidates.append(os.path.join(base, font_name))

        if "PIL" in globals():
            pil_dir = os.path.dirname(__import__("PIL").__file__)
            pil_fonts = os.path.join(pil_dir, "fonts")
            candidates.extend(
                (
                    os.path.join(pil_fonts, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
                    os.path.join(pil_fonts, "DejaVuSans.ttf"),
                )
            )

        seen: set[str] = set()
        for name in candidates:
            if name in seen:
                continue
            seen.add(name)
            try:
                return ImageFont.truetype(name, size=size)
            except OSError:
                continue
        return ImageFont.load_default()

    @staticmethod
    def _normalize_text(text: str | None) -> str:
        raw = "" if text is None else str(text)
        return unicodedata.normalize("NFC", raw)

    @staticmethod
    def _grapheme_clusters(text: str) -> list[str]:
        if not text:
            return []
        normalized = unicodedata.normalize("NFC", text)
        if not normalized:
            return []
        return regex_lib.findall(r"\X", normalized)

    def _first_grapheme(self, text: str | None, default: str = "?") -> str:
        clusters = self._grapheme_clusters(self._normalize_text(text))
        return clusters[0] if clusters else default

    def _pick_display_name(self, *values: str | None, fallback: str = "Usuario") -> str:
        for value in values:
            normalized = self._normalize_text(value).strip()
            if normalized:
                return normalized
        return self._normalize_text(fallback).strip() or "Usuario"

    @staticmethod
    def _center_text_y(draw, *, top: int, bottom: int, font, sample: str = "Ag") -> int:
        bbox = draw.textbbox((0, 0), sample, font=font)
        text_height = max(1, bbox[3] - bbox[1])
        return top + ((bottom - top - text_height) // 2) - bbox[1]

    @staticmethod
    def _is_emoji_cluster(cluster: str) -> bool:
        if not cluster:
            return False
        return bool(regex_lib.search(r"\p{Extended_Pictographic}", cluster))

    def _strip_emoji_clusters(self, text: str) -> str:
        if not text:
            return ""
        clean: list[str] = []
        for cluster in self._grapheme_clusters(text):
            if self._is_emoji_cluster(cluster):
                continue
            clean.append(cluster)
        return "".join(clean)

    def _text_for_renderer(self, text: str, emoji_draw=None) -> str:
        normalized = self._normalize_text(text)
        if emoji_draw is None:
            return self._strip_emoji_clusters(normalized)
        return normalized

    def _text_width(self, draw, text: str, font, *, emoji_draw=None) -> int:
        candidate = self._text_for_renderer(text, emoji_draw=emoji_draw)
        if emoji_draw is not None:
            try:
                width, _ = emoji_draw.getsize(candidate, font=font)
                return int(width)
            except Exception:
                candidate = self._strip_emoji_clusters(candidate)
        return int(draw.textlength(candidate, font=font))

    @staticmethod
    def _emoji_renderer(image, draw):
        if Pilmoji is None or Twemoji is None:
            return nullcontext(None)
        try:
            return Pilmoji(
                image,
                draw=draw,
                source=Twemoji,
                cache=True,
                render_discord_emoji=True,
            )
        except Exception:
            return nullcontext(None)

    def _draw_text(self, draw, xy: tuple[int, int], text: str, *, font, fill, emoji_draw=None) -> None:
        candidate = self._text_for_renderer(text, emoji_draw=emoji_draw)
        if emoji_draw is not None:
            try:
                emoji_draw.text(xy, candidate, font=font, fill=fill)
                return
            except Exception:
                candidate = self._strip_emoji_clusters(candidate)
        draw.text(xy, candidate, font=font, fill=fill)

    @staticmethod
    def _format_int(value: int) -> str:
        return f"{int(value):,}".replace(",", ".")

    @staticmethod
    def _progress_bar(current: int, total: int, width: int = 12) -> str:
        if total <= 0:
            return "-" * width
        safe_current = max(0, min(current, total))
        filled = round((safe_current / total) * width)
        return ("#" * filled) + ("-" * (width - filled))

    @staticmethod
    def _draw_vertical_gradient(
        draw,
        *,
        width: int,
        height: int,
        top_color: tuple[int, int, int],
        bottom_color: tuple[int, int, int],
    ) -> None:
        steps = max(height - 1, 1)
        for y in range(height):
            ratio = y / steps
            color = (
                int(top_color[0] + (bottom_color[0] - top_color[0]) * ratio),
                int(top_color[1] + (bottom_color[1] - top_color[1]) * ratio),
                int(top_color[2] + (bottom_color[2] - top_color[2]) * ratio),
                255,
            )
            draw.line((0, y, width, y), fill=color)

    def _truncate_text(self, draw, text: str, font, max_width: int, *, emoji_draw=None) -> str:
        normalized = self._text_for_renderer(text, emoji_draw=emoji_draw)
        if max_width <= 0:
            return ""
        if self._text_width(draw, normalized, font, emoji_draw=emoji_draw) <= max_width:
            return normalized

        suffix = "…"
        suffix_width = self._text_width(draw, suffix, font, emoji_draw=emoji_draw)
        if suffix_width > max_width:
            return suffix

        graphemes = self._grapheme_clusters(normalized)
        if not graphemes:
            return suffix

        low = 0
        high = len(graphemes)
        while low < high:
            mid = (low + high + 1) // 2
            candidate = "".join(graphemes[:mid]) + suffix
            if self._text_width(draw, candidate, font, emoji_draw=emoji_draw) <= max_width:
                low = mid
            else:
                high = mid - 1

        if low <= 0:
            return suffix
        return "".join(graphemes[:low]) + suffix

    @staticmethod
    def _draw_soft_glow(
        draw,
        *,
        left: int,
        top: int,
        right: int,
        bottom: int,
        radius: int,
        color: tuple[int, int, int, int],
        steps: int = 3,
        spread: int = 3,
    ) -> None:
        del steps
        red, green, blue, alpha = color
        expand = max(1, int(spread))
        glow_alpha = max(10, int(alpha * 0.22))
        draw.rounded_rectangle(
            (left - expand, top - expand, right + expand, bottom + expand),
            radius=radius + expand,
            outline=(red, green, blue, glow_alpha),
            width=max(1, expand // 2),
        )

    @staticmethod
    def _draw_inner_shadow(
        draw,
        *,
        left: int,
        top: int,
        right: int,
        bottom: int,
        radius: int,
        strength: int = 32,
        inset_steps: int = 3,
    ) -> None:
        for step in range(inset_steps):
            inset = step + 1
            alpha = max(8, int(strength / (step + 1.5)))
            draw.rounded_rectangle(
                (left + inset, top + inset, right - inset, bottom - inset),
                radius=max(1, radius - inset),
                outline=(0, 0, 0, alpha),
                width=1,
            )

    @staticmethod
    def _blur_region(image, box: tuple[int, int, int, int], radius: int = 3) -> None:
        if ImageFilter is None:
            return
        left, top, right, bottom = box
        if right <= left or bottom <= top:
            return
        region = image.crop(box).filter(ImageFilter.GaussianBlur(radius=max(1, radius)))
        image.paste(region, box)

    def _draw_particles(
        self,
        draw,
        *,
        width: int,
        height: int,
        seed: int,
    ) -> None:
        rng = random.Random(seed)
        for _ in range(70):
            x = rng.randint(24, width - 24)
            y = rng.randint(24, height - 24)
            size = rng.randint(1, 4)
            alpha = rng.randint(18, 65)
            if rng.random() > 0.7:
                color = (166, 122, 255, alpha)
            else:
                color = (126, 181, 255, alpha)
            draw.ellipse((x, y, x + size, y + size), fill=color)

    @staticmethod
    def _draw_stat_icon(
        draw,
        *,
        kind: str,
        x: int,
        y: int,
        size: int,
        color: tuple[int, int, int, int],
    ) -> None:
        cx = x + (size // 2)
        cy = y + (size // 2)
        if kind == "level":
            points: list[tuple[int, int]] = []
            outer = size * 0.48
            inner = size * 0.22
            for idx in range(10):
                angle = (-math.pi / 2) + (idx * (math.pi / 5))
                radius = outer if idx % 2 == 0 else inner
                points.append((int(cx + (math.cos(angle) * radius)), int(cy + (math.sin(angle) * radius))))
            draw.polygon(points, fill=color)
            return
        if kind == "xp":
            bolt = [
                (x + int(size * 0.56), y + int(size * 0.02)),
                (x + int(size * 0.30), y + int(size * 0.48)),
                (x + int(size * 0.50), y + int(size * 0.48)),
                (x + int(size * 0.34), y + int(size * 0.98)),
                (x + int(size * 0.70), y + int(size * 0.43)),
                (x + int(size * 0.50), y + int(size * 0.43)),
            ]
            draw.polygon(bolt, fill=color)
            return
        if kind == "position":
            cup_left = x + int(size * 0.18)
            cup_top = y + int(size * 0.16)
            cup_right = x + int(size * 0.82)
            cup_bottom = y + int(size * 0.56)
            draw.rounded_rectangle((cup_left, cup_top, cup_right, cup_bottom), radius=max(1, int(size * 0.1)), outline=color, width=max(1, size // 10))
            draw.rectangle((x + int(size * 0.42), y + int(size * 0.56), x + int(size * 0.58), y + int(size * 0.78)), fill=color)
            draw.rounded_rectangle((x + int(size * 0.28), y + int(size * 0.78), x + int(size * 0.72), y + int(size * 0.92)), radius=max(1, int(size * 0.07)), fill=color)
            return
        if kind == "messages":
            bubble = (x + int(size * 0.08), y + int(size * 0.12), x + int(size * 0.92), y + int(size * 0.76))
            draw.rounded_rectangle(bubble, radius=max(1, int(size * 0.2)), outline=color, width=max(1, size // 9))
            tail = [
                (x + int(size * 0.34), y + int(size * 0.74)),
                (x + int(size * 0.27), y + int(size * 0.96)),
                (x + int(size * 0.50), y + int(size * 0.76)),
            ]
            draw.polygon(tail, fill=color)

    def _avatar_fallback_from_name(self, name: str | None, size: int):
        self._ensure_canvas_support()
        fallback = Image.new("RGBA", (size, size), (53, 71, 120, 255))
        fallback_draw = ImageDraw.Draw(fallback)
        source_name = self._normalize_text(name)
        initials = self._first_grapheme(source_name, default="?").upper()
        font = self._load_font(max(24, int(size * 0.42)), bold=True)
        bbox = fallback_draw.textbbox((0, 0), initials, font=font)
        text_width = max(1, bbox[2] - bbox[0])
        text_height = max(1, bbox[3] - bbox[1])
        text_x = ((size - text_width) // 2) - bbox[0]
        text_y = ((size - text_height) // 2) - bbox[1]
        fallback_draw.text(
            (text_x, text_y),
            initials,
            font=font,
            fill=(232, 240, 255, 245),
        )
        return fallback

    def _avatar_fallback(self, user: discord.abc.User, size: int):
        source_name = self._pick_display_name(
            getattr(user, "display_name", None),
            getattr(user, "global_name", None),
            getattr(user, "name", None),
            fallback="Usuario",
        )
        return self._avatar_fallback_from_name(source_name, size)

    def _resize_cover(self, image, size: int):
        self._ensure_canvas_support()
        width, height = image.size
        if width <= 0 or height <= 0:
            raise ValueError("Imagem invalida para resize cover.")
        # object-fit: cover + object-position: center
        scale = max(size / width, size / height)
        resized = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            self._resample_filter(),
        )
        left = max(0, (resized.width - size) // 2)
        top = max(0, (resized.height - size) // 2)
        return resized.crop((left, top, left + size, top + size))

    @staticmethod
    def _circle_mask(size: int, supersample: int = 4):
        if Image is None:
            return None
        aa = max(1, supersample)
        hi_size = size * aa
        hi = Image.new("L", (hi_size, hi_size), 0)
        hi_draw = ImageDraw.Draw(hi)
        margin = max(0, aa // 2)
        hi_draw.ellipse((margin, margin, hi_size - 1 - margin, hi_size - 1 - margin), fill=255)
        resampling = getattr(Image, "Resampling", None)
        lanczos = resampling.LANCZOS if resampling is not None else Image.LANCZOS
        return hi.resize((size, size), lanczos)

    async def _fetch_avatar_from_url(self, avatar_url: str, size: int):
        self._ensure_canvas_support()
        candidate = avatar_url.strip()
        if not candidate.lower().startswith(("http://", "https://")):
            return None
        timeout = aiohttp.ClientTimeout(total=8)
        headers = {"User-Agent": "AyanaBot/leveling-canvas"}
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(candidate, allow_redirects=True) as response:
                    if response.status != 200:
                        return None
                    raw = await response.read()
        except Exception:
            return None

        try:
            loaded = Image.open(io.BytesIO(raw)).convert("RGBA")
            return self._resize_cover(loaded, size)
        except Exception:
            return None

    @staticmethod
    def _draw_progress_bar(
        image,
        draw,
        *,
        left: int,
        top: int,
        right: int,
        bottom: int,
        ratio: float,
        start_color: tuple[int, int, int] = (63, 175, 255),
        middle_color: tuple[int, int, int] = (96, 165, 250),
        end_color: tuple[int, int, int] = (141, 236, 255),
    ) -> None:
        bar_height = max(1, bottom - top)
        corner_radius = max(10, bar_height // 2)
        border_width = max(1, bar_height // 18)
        inset = max(2, bar_height // 10)

        draw.rounded_rectangle(
            (left, top, right, bottom),
            radius=corner_radius,
            fill=(31, 40, 67, 250),
            outline=(89, 108, 166, 235),
            width=border_width,
        )
        safe_ratio = max(0.0, min(1.0, ratio))
        inner_left = left + inset
        inner_top = top + inset
        inner_bottom = bottom - inset
        fill_right = inner_left + int((right - left - (inset * 2)) * safe_ratio)
        if fill_right <= inner_left:
            return

        fill_width = fill_right - inner_left
        fill_height = max(1, inner_bottom - inner_top)
        fill_layer = Image.new("RGBA", (fill_width, fill_height), (0, 0, 0, 0))
        fill_draw = ImageDraw.Draw(fill_layer)
        max_step = max(fill_width - 1, 1)

        for x in range(fill_width):
            grad_ratio = x / max_step
            if grad_ratio <= 0.5:
                local = grad_ratio / 0.5
                color = (
                    int(start_color[0] + (middle_color[0] - start_color[0]) * local),
                    int(start_color[1] + (middle_color[1] - start_color[1]) * local),
                    int(start_color[2] + (middle_color[2] - start_color[2]) * local),
                    255,
                )
            else:
                local = (grad_ratio - 0.5) / 0.5
                color = (
                    int(middle_color[0] + (end_color[0] - middle_color[0]) * local),
                    int(middle_color[1] + (end_color[1] - middle_color[1]) * local),
                    int(middle_color[2] + (end_color[2] - middle_color[2]) * local),
                    255,
                )
            fill_draw.line((x, 0, x, fill_height), fill=color)

        fill_radius = max(8, fill_height // 2)
        fill_mask = Image.new("L", (fill_width, fill_height), 0)
        mask_draw = ImageDraw.Draw(fill_mask)
        mask_draw.rounded_rectangle((0, 0, fill_width - 1, fill_height - 1), radius=fill_radius, fill=255)

        image.paste(fill_layer, (inner_left, inner_top), fill_mask)
        draw.rounded_rectangle(
            (inner_left, inner_top, fill_right, inner_bottom),
            radius=fill_radius,
            outline=(209, 248, 255, 138),
            width=max(1, border_width - 1),
        )
        highlight_bottom = inner_top + max(1, fill_height // 4)
        draw.rounded_rectangle(
            (inner_left + 1, inner_top + 1, fill_right - 1, highlight_bottom),
            radius=max(4, fill_radius - 2),
            fill=(255, 255, 255, 38),
        )
        glow_extra = max(1, border_width // 2)
        draw.rounded_rectangle(
            (
                inner_left - glow_extra,
                inner_top - glow_extra,
                fill_right + glow_extra,
                inner_bottom + glow_extra,
            ),
            radius=fill_radius + glow_extra,
            outline=(59, 130, 246, 112),
            width=1,
        )

    async def _fetch_avatar_image(
        self,
        user: discord.abc.User | None,
        size: int,
        avatar_url: str | None = None,
        fallback_name: str | None = None,
    ):
        self._ensure_canvas_support()
        safe_fallback = self._pick_display_name(
            fallback_name,
            getattr(user, "display_name", None) if user is not None else None,
            getattr(user, "global_name", None) if user is not None else None,
            getattr(user, "name", None) if user is not None else None,
            fallback="Usuario",
        )
        requested_url = (avatar_url or "").strip()
        if requested_url:
            from_url = await self._fetch_avatar_from_url(requested_url, size)
            if from_url is not None:
                return from_url
            return self._avatar_fallback_from_name(safe_fallback, size)

        if user is None:
            return self._avatar_fallback_from_name(safe_fallback, size)

        try:
            avatar_asset = user.display_avatar.replace(
                format="png",
                size=self._discord_asset_size(size * 2, min_size=64, max_size=1024),
            )
            raw = await avatar_asset.read()
            loaded = Image.open(io.BytesIO(raw)).convert("RGBA")
            return self._resize_cover(loaded, size)
        except Exception:
            return self._avatar_fallback_from_name(safe_fallback, size)

    async def _fetch_avatar_circle(
        self,
        user: discord.abc.User | None,
        size: int,
        avatar_url: str | None = None,
        fallback_name: str | None = None,
    ):
        avatar = await self._fetch_avatar_image(
            user,
            size,
            avatar_url=avatar_url,
            fallback_name=fallback_name,
        )
        mask = self._circle_mask(size, supersample=4)
        if mask is None:
            return avatar

        output = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        output.paste(avatar, (0, 0), mask)
        return output

    def _build_rank_embed(
        self,
        *,
        member: discord.Member,
        level: int,
        total_xp: int,
        rank_position: int,
        message_count: int,
        level_progress: int,
        level_total_needed: int,
    ) -> discord.Embed:
        progress_bar = self._progress_bar(level_progress, level_total_needed)
        embed = discord.Embed(
            title=f"Rank de {member}",
            color=member.color if member.color.value else discord.Color.blurple(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Nivel", value=f"`{level}`", inline=True)
        embed.add_field(name="XP total", value=f"`{total_xp}`", inline=True)
        embed.add_field(name="Posicao", value=f"`#{rank_position}`", inline=True)
        embed.add_field(name="Mensagens com XP", value=f"`{message_count}`", inline=True)
        embed.add_field(
            name="Progresso do nivel",
            value=f"`[{progress_bar}] {level_progress}/{level_total_needed}`",
            inline=False,
        )
        return embed

    async def _resolve_leaderboard_identity(
        self,
        guild: discord.Guild,
        user_id: int,
        *,
        avatar_size: int = 128,
    ) -> tuple[str, discord.abc.User | None, str | None]:
        member = guild.get_member(user_id)
        if member is not None:
            display_name = self._pick_display_name(
                member.display_name,
                member.global_name,
                member.name,
                fallback=f"Usuario {user_id}",
            )
            avatar_url = None
            if member.display_avatar:
                try:
                    avatar_url = member.display_avatar.replace(
                        format="png",
                        size=self._discord_asset_size(
                            avatar_size * 2,
                            min_size=64,
                            max_size=1024,
                        ),
                    ).url
                except Exception:
                    avatar_url = None
            return display_name, member, avatar_url

        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                user = None

        if user is not None:
            display_name = self._pick_display_name(
                getattr(user, "global_name", None),
                user.name,
                fallback=f"Usuario {user_id}",
            )
            avatar_url = None
            if user.display_avatar:
                try:
                    avatar_url = user.display_avatar.replace(
                        format="png",
                        size=self._discord_asset_size(
                            avatar_size * 2,
                            min_size=64,
                            max_size=1024,
                        ),
                    ).url
                except Exception:
                    avatar_url = None
            return display_name, user, avatar_url

        fallback_name = self._normalize_text(f"Usuario {user_id}")
        return fallback_name, None, None

    def _build_leaderboard_embed(
        self,
        guild: discord.Guild,
        rows: list[dict[str, Any]],
    ) -> discord.Embed:
        lines: list[str] = []
        for position, row in enumerate(rows, start=1):
            lines.append(
                (
                    f"{position}. <@{row['user_id']}> - "
                    f"Nivel `{row['level']}` | XP `{row['total_xp']}` | "
                    f"Msgs `{row['message_count']}`"
                )
            )
        embed = discord.Embed(
            title=f"Leaderboard de niveis - {guild.name}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Top {len(rows)} usuarios")
        return embed

    async def _render_rank_canvas(
        self,
        *,
        guild: discord.Guild,
        member: discord.Member,
        level: int,
        total_xp: int,
        rank_position: int,
        message_count: int,
        level_progress: int,
        level_total_needed: int,
    ) -> io.BytesIO:
        self._ensure_canvas_support()
        scale = max(1, int(self.RANK_RENDER_SCALE))
        width = self.CARD_WIDTH * scale
        height = self.RANK_CARD_HEIGHT * scale
        s = lambda value: int(value * scale)

        image = Image.new("RGBA", (width, height), (0, 0, 0, 255))
        draw = ImageDraw.Draw(image)
        self._draw_vertical_gradient(
            draw,
            width=width,
            height=height,
            top_color=(5, 13, 34),
            bottom_color=(11, 24, 58),
        )
        draw.ellipse((s(700), s(-180), s(1290), s(290)), fill=(122, 73, 216, 68))
        draw.ellipse((s(520), s(210), s(1230), s(760)), fill=(40, 92, 198, 70))
        draw.rectangle((0, 0, width, height), fill=(7, 12, 24, 34))
        self._draw_particles(draw, width=width, height=height, seed=(member.id ^ guild.id))

        panel_left = s(30)
        panel_top = s(30)
        panel_right = width - s(30)
        panel_bottom = height - s(30)
        self._blur_region(image, (panel_left, panel_top, panel_right, panel_bottom), radius=max(2, s(2)))
        draw.rounded_rectangle(
            (panel_left + s(3), panel_top + s(10), panel_right + s(3), panel_bottom + s(10)),
            radius=s(30),
            fill=(1, 5, 14, 96),
        )
        self._draw_soft_glow(
            draw,
            left=panel_left,
            top=panel_top,
            right=panel_right,
            bottom=panel_bottom,
            radius=s(30),
            color=(91, 131, 235, 120),
            spread=max(1, s(1)),
        )
        draw.rounded_rectangle(
            (panel_left, panel_top, panel_right, panel_bottom),
            radius=s(30),
            fill=(8, 18, 42, 224),
            outline=(120, 150, 255, 64),
            width=max(1, s(1)),
        )
        self._draw_inner_shadow(
            draw,
            left=panel_left,
            top=panel_top,
            right=panel_right,
            bottom=panel_bottom,
            radius=s(30),
            strength=28,
        )

        left_panel = (s(58), s(68), s(356), s(432))
        self._blur_region(image, left_panel, radius=max(2, s(2)))
        draw.rounded_rectangle(
            (left_panel[0] + s(2), left_panel[1] + s(6), left_panel[2] + s(2), left_panel[3] + s(6)),
            radius=s(24),
            fill=(1, 5, 13, 82),
        )
        self._draw_soft_glow(
            draw,
            left=left_panel[0],
            top=left_panel[1],
            right=left_panel[2],
            bottom=left_panel[3],
            radius=s(24),
            color=(102, 148, 255, 96),
            spread=max(1, s(1)),
        )
        draw.rounded_rectangle(
            left_panel,
            radius=s(24),
            fill=(14, 29, 62, 200),
            outline=(120, 150, 255, 58),
            width=max(1, s(1)),
        )
        self._draw_inner_shadow(
            draw,
            left=left_panel[0],
            top=left_panel[1],
            right=left_panel[2],
            bottom=left_panel[3],
            radius=s(24),
            strength=24,
        )

        name_font = self._load_font(s(40), bold=True)
        handle_font = self._load_font(s(22), bold=False)
        server_font = self._load_font(s(16), bold=False)
        stat_label_font = self._load_font(s(16), bold=False)
        stat_value_font = self._load_font(s(36), bold=True)
        progress_font = self._load_font(s(20), bold=False)

        safe_guild_name = self._normalize_text(guild.name).strip() or "Servidor"
        safe_display_name = self._normalize_text(member.display_name).strip() or "Usuario"
        safe_handle_name = self._normalize_text(member.name).strip() or "usuario"

        with self._emoji_renderer(image, draw) as emoji_draw:
            display_name = self._truncate_text(
                draw,
                safe_display_name,
                name_font,
                s(252),
                emoji_draw=emoji_draw,
            )
            handle_text = self._truncate_text(
                draw,
                f"@{safe_handle_name}",
                handle_font,
                s(252),
                emoji_draw=emoji_draw,
            )
            server_line = self._truncate_text(
                draw,
                f"Servidor: {safe_guild_name}",
                server_font,
                s(252),
                emoji_draw=emoji_draw,
            )
            self._draw_text(
                draw,
                (s(78), s(90)),
                display_name,
                font=name_font,
                fill=(242, 247, 255, 255),
                emoji_draw=emoji_draw,
            )
            self._draw_text(
                draw,
                (s(80), s(146)),
                handle_text,
                font=handle_font,
                fill=(179, 198, 244, 210),
                emoji_draw=emoji_draw,
            )
            self._draw_text(
                draw,
                (s(80), s(182)),
                server_line,
                font=server_font,
                fill=(156, 176, 226, 180),
                emoji_draw=emoji_draw,
            )

        avatar_size = s(168)
        avatar_x = s(122)
        avatar_y = s(218)
        avatar_url = None
        if member.display_avatar:
            avatar_url = member.display_avatar.replace(
                format="png",
                size=self._discord_asset_size(avatar_size * 2, min_size=128, max_size=1024),
            ).url
        avatar = await self._fetch_avatar_circle(member, avatar_size, avatar_url=avatar_url)
        draw.ellipse(
            (avatar_x - s(10), avatar_y - s(10), avatar_x + avatar_size + s(10), avatar_y + avatar_size + s(10)),
            outline=(74, 170, 255, 126),
            width=max(1, s(2)),
        )
        draw.ellipse(
            (avatar_x - s(5), avatar_y - s(5), avatar_x + avatar_size + s(5), avatar_y + avatar_size + s(5)),
            outline=(19, 36, 74, 255),
            width=max(1, s(3)),
        )
        image.paste(avatar, (avatar_x, avatar_y), avatar)
        draw.ellipse(
            (avatar_x - s(2), avatar_y - s(2), avatar_x + avatar_size + s(2), avatar_y + avatar_size + s(2)),
            outline=(146, 227, 255, 188),
            width=max(1, s(1)),
        )
        status_radius = s(14)
        status_cx = avatar_x + avatar_size - s(8)
        status_cy = avatar_y + avatar_size - s(8)
        draw.ellipse(
            (status_cx - status_radius - s(3), status_cy - status_radius - s(3), status_cx + status_radius + s(3), status_cy + status_radius + s(3)),
            fill=(7, 18, 40, 255),
        )
        draw.ellipse(
            (status_cx - status_radius, status_cy - status_radius, status_cx + status_radius, status_cy + status_radius),
            fill=(39, 199, 113, 255),
            outline=(171, 255, 212, 210),
            width=max(1, s(1)),
        )

        stat_boxes = (
            ("level", "Nivel", str(level)),
            ("xp", "XP total", self._format_int(total_xp)),
            ("position", "Posicao", f"#{rank_position}"),
            ("messages", "Mensagens", self._format_int(message_count)),
        )
        box_left = s(420)
        box_top = s(108)
        box_width = s(156)
        box_height = s(140)
        box_gap = s(26)

        for index, (icon_kind, label, value) in enumerate(stat_boxes):
            left = box_left + ((box_width + box_gap) * index)
            right = left + box_width
            box_bottom = box_top + box_height
            self._blur_region(image, (left, box_top, right, box_bottom), radius=max(2, s(2)))
            draw.rounded_rectangle(
                (left + s(2), box_top + s(6), right + s(2), box_bottom + s(6)),
                radius=s(18),
                fill=(1, 5, 13, 82),
            )
            draw.rounded_rectangle(
                (left, box_top, right, box_bottom),
                radius=s(18),
                fill=(17, 31, 62, 196),
                outline=(120, 150, 255, 64),
                width=max(1, s(1)),
            )
            self._draw_inner_shadow(
                draw,
                left=left,
                top=box_top,
                right=right,
                bottom=box_bottom,
                radius=s(18),
                strength=24,
                inset_steps=2,
            )
            icon_size = s(18)
            icon_x = left + s(16)
            icon_y = box_top + s(22)
            self._draw_stat_icon(
                draw,
                kind=icon_kind,
                x=icon_x,
                y=icon_y,
                size=icon_size,
                color=(140, 172, 246, 180),
            )
            draw.text(
                (icon_x + icon_size + s(8), box_top + s(20)),
                label,
                font=stat_label_font,
                fill=(157, 176, 224, 178),
            )
            value_text = self._truncate_text(draw, value, stat_value_font, box_width - s(32))
            draw.text((left + s(16), box_top + s(70)), value_text, font=stat_value_font, fill=(243, 247, 255, 255))

        ratio = level_progress / max(level_total_needed, 1)
        progress_text = (
            f"Progress {self._format_int(level_progress)}/{self._format_int(level_total_needed)} "
            f"({(ratio * 100):.1f}%)"
        )
        missing = max(level_total_needed - level_progress, 0)
        progress_panel = (s(420), s(288), s(1112), s(432))
        self._blur_region(image, progress_panel, radius=max(2, s(2)))
        draw.rounded_rectangle(
            (
                progress_panel[0] + s(2),
                progress_panel[1] + s(6),
                progress_panel[2] + s(2),
                progress_panel[3] + s(6),
            ),
            radius=s(22),
            fill=(1, 5, 13, 86),
        )
        draw.rounded_rectangle(
            progress_panel,
            radius=s(22),
            fill=(14, 29, 62, 198),
            outline=(120, 150, 255, 54),
            width=max(1, s(1)),
        )
        self._draw_inner_shadow(
            draw,
            left=progress_panel[0],
            top=progress_panel[1],
            right=progress_panel[2],
            bottom=progress_panel[3],
            radius=s(22),
            strength=18,
            inset_steps=2,
        )
        text_y = progress_panel[1] + s(26)
        draw.text((s(446), text_y), progress_text, font=progress_font, fill=(195, 214, 255, 236))

        remaining_text = f"Remaining {self._format_int(missing)} XP"
        remaining_width = int(draw.textlength(remaining_text, font=progress_font))
        draw.text(
            (progress_panel[2] - s(26) - remaining_width, text_y),
            remaining_text,
            font=progress_font,
            fill=(184, 209, 255, 214),
        )
        self._draw_progress_bar(
            image,
            draw,
            left=s(446),
            top=progress_panel[1] + s(74),
            right=progress_panel[2] - s(24),
            bottom=progress_panel[1] + s(116),
            ratio=ratio,
            start_color=(59, 130, 246),
            middle_color=(96, 165, 250),
            end_color=(147, 197, 253),
        )

        output = io.BytesIO()
        image.convert("RGB").save(output, format="PNG", optimize=True)
        output.seek(0)
        return output

    async def _render_leaderboard_canvas(
        self,
        *,
        guild: discord.Guild,
        rows: list[dict[str, Any]],
    ) -> io.BytesIO:
        self._ensure_canvas_support()
        shown_rows = rows[: self.BOARD_MAX_ROWS]
        width = self.BOARD_WIDTH
        height = self.BOARD_HEIGHT
        row_height = self.BOARD_ROW_HEIGHT

        image = Image.new("RGBA", (width, height), (0, 0, 0, 255))
        draw = ImageDraw.Draw(image)
        self._draw_vertical_gradient(
            draw,
            width=width,
            height=height,
            top_color=(5, 13, 34),
            bottom_color=(14, 28, 62),
        )
        draw.ellipse((730, -180, 1290, 260), fill=(124, 74, 227, 78))
        draw.ellipse((500, 220, 1220, 760), fill=(44, 98, 211, 74))
        draw.rectangle((0, 0, width, height), fill=(8, 12, 26, 38))
        self._draw_particles(draw, width=width, height=height, seed=(guild.id ^ len(rows)))

        panel_left = 26
        panel_top = 26
        panel_right = width - 26
        panel_bottom = height - 26
        self._draw_soft_glow(
            draw,
            left=panel_left,
            top=panel_top,
            right=panel_right,
            bottom=panel_bottom,
            radius=30,
            color=(100, 132, 231, 168),
            steps=3,
            spread=3,
        )

        draw.rounded_rectangle(
            (panel_left, panel_top, panel_right, panel_bottom),
            radius=30,
            fill=(8, 17, 39, 236),
            outline=(83, 114, 194, 222),
            width=2,
        )

        title_font = self._load_font(50, bold=True)
        subtitle_font = self._load_font(22, bold=False)
        header_font = self._load_font(19, bold=True)
        row_user_font = self._load_font(24, bold=False)
        row_value_font = self._load_font(24, bold=True)
        row_pos_font = self._load_font(26, bold=True)

        with self._emoji_renderer(image, draw) as emoji_draw:
            draw.text((56, 54), "Leaderboard", font=title_font, fill=(244, 248, 255, 255))
            draw.text((58, 108), "Top users by XP", font=subtitle_font, fill=(171, 194, 246, 255))
            guild_label = self._truncate_text(
                draw,
                f"Guild: {self._normalize_text(guild.name)}",
                subtitle_font,
                350,
                emoji_draw=emoji_draw,
            )
            guild_label_width = self._text_width(
                draw,
                guild_label,
                subtitle_font,
                emoji_draw=emoji_draw,
            )
            self._draw_text(
                draw,
                (width - 58 - guild_label_width, 108),
                guild_label,
                font=subtitle_font,
                fill=(165, 186, 239, 255),
                emoji_draw=emoji_draw,
            )

        table_left = 66
        table_right = width - 66
        table_top = 166

        col_pos = 88
        col_user = 190
        col_level = 760
        col_xp = 892
        col_msgs = 1042
        row_avatar_size = 40
        row_avatar_x = col_user
        row_name_x = row_avatar_x + row_avatar_size + 14
        max_user_name_width = max(140, col_level - row_name_x - 24)

        header_y = table_top
        draw.text((col_pos, header_y), "POS", font=header_font, fill=(154, 176, 231, 255))
        draw.text((row_name_x, header_y), "USER", font=header_font, fill=(154, 176, 231, 255))
        draw.text((col_level, header_y), "LEVEL", font=header_font, fill=(154, 176, 231, 255))
        draw.text((col_xp, header_y), "XP", font=header_font, fill=(154, 176, 231, 255))
        draw.text((col_msgs, header_y), "MSGS", font=header_font, fill=(154, 176, 231, 255))
        draw.line((table_left, header_y + 33, table_right, header_y + 33), fill=(87, 115, 192, 132), width=1)

        medal_colors = {
            1: (243, 194, 74, 255),
            2: (190, 204, 219, 255),
            3: (202, 145, 95, 255),
        }

        rows_top = header_y + 46
        row_gap = 12
        resolved_rows: list[dict[str, Any]] = []
        for row in shown_rows:
            user_id = int(row["user_id"])
            display_name, user_ref, avatar_url = await self._resolve_leaderboard_identity(
                guild,
                user_id,
                avatar_size=row_avatar_size,
            )
            resolved_rows.append(
                {
                    "row": row,
                    "display_name": display_name,
                    "user_ref": user_ref,
                    "avatar_url": avatar_url,
                }
            )

        with self._emoji_renderer(image, draw) as emoji_draw:
            for index, resolved in enumerate(resolved_rows, start=1):
                row = resolved["row"]
                top = rows_top + ((index - 1) * (row_height + row_gap))
                bottom = top + row_height
                row_left = table_left
                row_right = table_right

                shadow_offset = 4
                draw.rounded_rectangle(
                    (row_left + 1, top + shadow_offset, row_right + 1, bottom + shadow_offset),
                    radius=18,
                    fill=(2, 6, 16, 110),
                )

                base_fill = (17, 30, 61, 232) if index % 2 else (15, 27, 57, 232)
                border_color = (88, 120, 205, 224)
                glow_color = (96, 156, 255, 148)
                if index == 1:
                    base_fill = (23, 37, 74, 236)
                    border_color = (114, 156, 255, 238)
                    glow_color = (125, 184, 255, 186)

                self._draw_soft_glow(
                    draw,
                    left=row_left,
                    top=top,
                    right=row_right,
                    bottom=bottom,
                    radius=18,
                    color=glow_color,
                    steps=2,
                    spread=2,
                )
                draw.rounded_rectangle(
                    (row_left, top, row_right, bottom),
                    radius=18,
                    fill=base_fill,
                    outline=border_color,
                    width=2,
                )
                draw.line(
                    (row_left + 16, top + 1, row_right - 16, top + 1),
                    fill=(166, 208, 255, 84),
                    width=1,
                )

                full_name = str(resolved["display_name"])
                name = self._truncate_text(
                    draw,
                    full_name,
                    row_user_font,
                    max_user_name_width,
                    emoji_draw=emoji_draw,
                )
                pos_color = medal_colors.get(index, (199, 213, 255, 255))

                avatar_y = top + ((row_height - row_avatar_size) // 2)
                avatar = await self._fetch_avatar_circle(
                    resolved["user_ref"],
                    row_avatar_size,
                    avatar_url=resolved["avatar_url"],
                    fallback_name=full_name,
                )
                draw.ellipse(
                    (
                        row_avatar_x - 3,
                        avatar_y - 3,
                        row_avatar_x + row_avatar_size + 3,
                        avatar_y + row_avatar_size + 3,
                    ),
                    fill=(6, 18, 46, 220),
                )
                draw.ellipse(
                    (
                        row_avatar_x - 2,
                        avatar_y - 2,
                        row_avatar_x + row_avatar_size + 2,
                        avatar_y + row_avatar_size + 2,
                    ),
                    outline=(89, 154, 255, 126),
                    width=1,
                )
                image.paste(avatar, (row_avatar_x, avatar_y), avatar)
                draw.ellipse(
                    (
                        row_avatar_x - 1,
                        avatar_y - 1,
                        row_avatar_x + row_avatar_size + 1,
                        avatar_y + row_avatar_size + 1,
                    ),
                    outline=(180, 232, 255, 146),
                    width=1,
                )

                pos_text_y = self._center_text_y(draw, top=top, bottom=bottom, font=row_pos_font, sample="#1")
                value_text_y = self._center_text_y(draw, top=top, bottom=bottom, font=row_value_font, sample="184")
                user_text_y = self._center_text_y(draw, top=top, bottom=bottom, font=row_user_font, sample="Ag")

                draw.text((col_pos, pos_text_y), f"#{index}", font=row_pos_font, fill=pos_color)
                self._draw_text(
                    draw,
                    (row_name_x, user_text_y),
                    name,
                    font=row_user_font,
                    fill=(238, 243, 255, 255),
                    emoji_draw=emoji_draw,
                )
                draw.text((col_level, value_text_y), str(int(row["level"])), font=row_value_font, fill=(229, 236, 255, 255))
                draw.text(
                    (col_xp, value_text_y),
                    self._format_int(int(row["total_xp"])),
                    font=row_value_font,
                    fill=(229, 236, 255, 255),
                )
                draw.text(
                    (col_msgs, value_text_y),
                    self._format_int(int(row["message_count"])),
                    font=row_value_font,
                    fill=(229, 236, 255, 255),
                )

        footer_text = f"Showing top {len(shown_rows)} users"
        footer_width = int(draw.textlength(footer_text, font=subtitle_font))
        draw.text((width - 58 - footer_width, height - 66), footer_text, font=subtitle_font, fill=(153, 176, 231, 240))

        output = io.BytesIO()
        image.convert("RGB").save(output, format="PNG", optimize=True)
        output.seek(0)
        return output

    def _is_eligible_message(self, message: discord.Message) -> bool:
        if message.guild is None or message.author.bot:
            return False
        if not isinstance(message.author, discord.Member):
            return False
        if message.webhook_id is not None:
            return False
        return True

    def _xp_gain_for_message(self, message: discord.Message) -> int:
        content_length = len((message.content or "").strip())
        length_bonus = min(content_length, 240) // 24
        attachment_bonus = min(len(message.attachments), 2) * 3
        sticker_bonus = min(len(message.stickers), 1) * 2
        random_bonus = self._rng.randint(5, 10)

        xp_gain = 8 + length_bonus + attachment_bonus + sticker_bonus + random_bonus
        return max(10, min(xp_gain, 40))

    def _is_xp_rate_limited(self, guild_id: int, user_id: int) -> bool:
        now = time.monotonic()
        key = (guild_id, user_id)
        last_award = self._xp_cooldowns.get(key)
        if last_award is not None and (now - last_award) < self.XP_COOLDOWN_SECONDS:
            return True

        self._xp_cooldowns[key] = now
        if len(self._xp_cooldowns) > 50_000:
            cutoff = now - (self.XP_COOLDOWN_SECONDS * 4)
            self._xp_cooldowns = {
                cache_key: ts
                for cache_key, ts in self._xp_cooldowns.items()
                if ts >= cutoff
            }
        return False

    async def _announce_level_up(self, message: discord.Message, payload: dict[str, Any]) -> None:
        level = int(payload["level"])
        total_xp = int(payload["total_xp"])
        missing_xp = xp_for_next_level(level)
        try:
            await message.channel.send(
                (
                    f"{message.author.display_name} subiu para o nivel `{level}`. "
                    f"XP total: `{total_xp}` | Proximo nivel em `{missing_xp}` XP."
                ),
                delete_after=12,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning(
                "Falha ao anunciar level up. guild=%s canal=%s usuario=%s",
                message.guild.id if message.guild else "N/A",
                message.channel.id,
                message.author.id,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self._is_eligible_message(message):
            return
        if message.guild is None or not isinstance(message.author, discord.Member):
            return
        if self._is_xp_rate_limited(message.guild.id, message.author.id):
            return

        xp_gain = self._xp_gain_for_message(message)
        try:
            result = await self._warn_store().add_level_xp(
                guild_id=message.guild.id,
                user_id=message.author.id,
                xp_gain=xp_gain,
            )
        except Exception as exc:
            LOGGER.error(
                "Falha ao registrar XP no MySQL.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return

        if result.get("leveled_up"):
            await self._announce_level_up(message, result)

    @app_commands.command(name="rank", description="Mostra nivel e XP de um membro.")
    @app_commands.guild_only()
    @app_commands.describe(member="Membro para consultar. Se vazio, usa voce.")
    async def rank(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        if member is None:
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    "Nao consegui identificar voce neste servidor.",
                    ephemeral=True,
                )
                return
            member = interaction.user

        await interaction.response.defer(thinking=True)

        try:
            profile = await self._warn_store().get_member_level(guild.id, member.id)
        except Exception as exc:
            LOGGER.error(
                "Falha ao consultar rank no MySQL.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.followup.send(
                "Falha ao consultar o rank agora. Tente novamente em instantes.",
                ephemeral=True,
            )
            return

        if profile is None:
            await interaction.followup.send(
                f"{member.mention} ainda nao possui XP registrado nesta guilda.",
                ephemeral=True,
            )
            return

        level = int(profile["level"])
        total_xp = int(profile["total_xp"])
        rank_position = int(profile["rank_position"])
        message_count = int(profile["message_count"])
        current_level_base = total_xp_for_level(level)
        next_level_total = total_xp_for_level(level + 1)
        level_progress = total_xp - current_level_base
        level_total_needed = max(1, next_level_total - current_level_base)

        try:
            card = await self._render_rank_canvas(
                guild=guild,
                member=member,
                level=level,
                total_xp=total_xp,
                rank_position=rank_position,
                message_count=message_count,
                level_progress=level_progress,
                level_total_needed=level_total_needed,
            )
            file = discord.File(card, filename=f"rank-{member.id}.png")
            await interaction.followup.send(file=file)
            return
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            LOGGER.error(
                "Falha ao renderizar canvas do /rank.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

        fallback_embed = self._build_rank_embed(
            member=member,
            level=level,
            total_xp=total_xp,
            rank_position=rank_position,
            message_count=message_count,
            level_progress=level_progress,
            level_total_needed=level_total_needed,
        )
        await interaction.followup.send(embed=fallback_embed)

    @app_commands.command(name="leaderboard", description="Mostra o ranking de niveis da guilda.")
    @app_commands.guild_only()
    @app_commands.describe(limit="Quantidade de usuarios no ranking (3 a 20).")
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 3, 20] = 10,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando so funciona em servidor.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            rows = await self._warn_store().get_level_leaderboard(guild.id, int(limit))
        except Exception as exc:
            LOGGER.error(
                "Falha ao consultar leaderboard no MySQL.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.followup.send(
                "Falha ao consultar o leaderboard agora. Tente novamente em instantes.",
                ephemeral=True,
            )
            return

        if not rows:
            await interaction.followup.send(
                "Ainda nao ha usuarios com XP registrado nesta guilda.",
                ephemeral=True,
            )
            return

        try:
            board = await self._render_leaderboard_canvas(guild=guild, rows=rows)
            file = discord.File(board, filename=f"leaderboard-{guild.id}.png")
            await interaction.followup.send(file=file)
            return
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            LOGGER.error(
                "Falha ao renderizar canvas do /leaderboard.",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

        fallback_embed = self._build_leaderboard_embed(guild, rows)
        await interaction.followup.send(embed=fallback_embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LevelingCog(bot))
