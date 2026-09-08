"""AH5+B35 calculation and image rendering for locally exported RizLine saves.

The selection rules are independently implemented from the publicly available
algorithm described by REDDRAGON-HL/rizline_b40_tool (Apache-2.0).  This module
uses only locally cached chart metadata and never contacts a score service at
query time.
"""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
import struct
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


LOGGER = logging.getLogger("qq_official_bot.rizline_b40")
EPSILON = 1e-6
TRACK_PREFIX = "track."
FONT_REGULAR_PATHS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
FONT_BOLD_PATHS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
DIFFICULTY_COLORS = {
    "EZ": "#48CBB4",
    "HD": "#F1AB4A",
    "IN": "#EC715A",
    "AT": "#9B5FA8",
    "SP": "#5A8FEA",
}
ARTWORK_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
MAX_RESOURCE_IMAGE_PIXELS = 36_000_000
MAX_LOCALIZATION_BYTES = 4 * 1024 * 1024
UI_RESOURCE_PATHS = {
    "font": "ui/Base/Font/Source Han Sans CN&Comfortaa Hybrid.ttf",
    "card": "ui/Base/Textures/空白卡.png",
    "avatar": "ui/Base/Textures/默认头像.png",
}


@dataclass(frozen=True)
class ChartMetadata:
    title: str
    chart_const: float
    max_hit: int
    riz_hit: int


@dataclass(frozen=True)
class B40Entry:
    record: Any
    source_rank: int
    title: str
    chart: ChartMetadata | None
    ah_status: int = 0  # 0 unknown/no, 1 confirmed, 2 compatible candidate

    @property
    def rks(self) -> float:
        return float(self.record.rks)

    @property
    def difficulty(self) -> str:
        return str(self.record.difficulty)


@dataclass(frozen=True)
class B40Report:
    special5: tuple[B40Entry, ...]
    ordinary35: tuple[B40Entry, ...]
    overflow: tuple[B40Entry, ...]
    exact_ah5_b35: bool
    missing_metadata_count: int

    @property
    def selected(self) -> tuple[B40Entry, ...]:
        return self.special5 + self.ordinary35

    @property
    def rating(self) -> float:
        # The reference tool keeps the denominator at 40 even for incomplete saves.
        return sum(entry.rks for entry in self.selected) / 40.0

    @property
    def mode_label(self) -> str:
        return "AH5 + B35" if self.exact_ah5_b35 else "RKS TOP 40（定数数据不完整）"


@dataclass(frozen=True)
class RizlinePlayerCard:
    """The non-sensitive display choices from a player's current RizLine card."""

    avatar_id: str = ""
    background_id: str = ""
    layout_id: str = ""
    bio_id1: str = ""
    bio_id2: str = ""
    avatar_x: float = 0.5
    avatar_y: float = 0.5


class RizlineSongCatalog:
    """Read a locally cached chart catalog without a runtime web dependency.

    Expected JSON shape::

        {
          "tracks": {
            "track.Example.artist.0": {
              "title": "Example",
              "charts": {
                "IN": {"const": 14.5, "hit": 1000, "riz_hit": 220}
              }
            }
          }
        }
    """

    def __init__(self, path: Path | str | None) -> None:
        self._path = Path(path) if path else None
        self._stamp: tuple[int, int] | None = None
        self._charts: dict[tuple[str, str], ChartMetadata] = {}

    def lookup(self, track_id: str, difficulty: str) -> ChartMetadata | None:
        self._refresh_if_needed()
        difficulty = difficulty.upper()
        for variant in _track_variants(track_id):
            chart = self._charts.get((variant, difficulty))
            if chart:
                return chart
        return None

    def _refresh_if_needed(self) -> None:
        if not self._path:
            self._charts = {}
            return
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            self._charts = {}
            self._stamp = None
            return
        except OSError:
            LOGGER.exception("Unable to inspect RizLine chart catalog")
            self._charts = {}
            self._stamp = None
            return

        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._stamp == stamp:
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            tracks = raw.get("tracks", {}) if isinstance(raw, dict) else {}
            if not isinstance(tracks, dict):
                raise ValueError("tracks must be an object")
            charts: dict[tuple[str, str], ChartMetadata] = {}
            for track_id, payload in tracks.items():
                if not isinstance(payload, dict):
                    continue
                title = str(payload.get("title") or _title_from_track(str(track_id)))
                raw_charts = payload.get("charts", {})
                if not isinstance(raw_charts, dict):
                    continue
                for difficulty, chart_payload in raw_charts.items():
                    if not isinstance(chart_payload, dict):
                        continue
                    chart_const = _number(chart_payload.get("const"))
                    max_hit = _integer(chart_payload.get("hit"))
                    riz_hit = _integer(chart_payload.get("riz_hit", chart_payload.get("rizHit")))
                    if chart_const is None or max_hit is None or riz_hit is None:
                        continue
                    chart = ChartMetadata(title, chart_const, max_hit, riz_hit)
                    for variant in _track_variants(str(track_id)):
                        charts[(variant, str(difficulty).upper())] = chart
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            LOGGER.exception("Unable to parse RizLine chart catalog")
            self._charts = {}
            self._stamp = stamp
            return

        self._charts = charts
        self._stamp = stamp


class RizlineArtworkCatalog:
    """Look up locally exported song artwork without querying a game service.

    ``rizline-assets-get`` writes its exported images below an ``output``
    directory and stores the chart-to-illustration relation in
    ``output/default.json``.  This reader intentionally consumes only those
    already exported files; it neither invokes that project nor downloads game
    assets while a QQ user is waiting for a reply.
    """

    def __init__(self, path: Path | str | None) -> None:
        self._path = Path(path) if path else None
        self._loaded = False
        self._artwork_by_track: dict[str, Path] = {}

    def lookup(self, track_id: str) -> Path | None:
        self._load_once()
        for key in _artwork_track_keys(track_id):
            artwork = self._artwork_by_track.get(key)
            if artwork:
                return artwork
        return None

    def _load_once(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path:
            return

        try:
            root = self._path.resolve(strict=True)
        except OSError:
            LOGGER.info("RizLine artwork directory is not available: %s", self._path)
            return
        if not root.is_dir():
            LOGGER.warning("RizLine artwork path is not a directory: %s", root)
            return

        image_index = self._build_image_index(root)
        if not image_index:
            LOGGER.info("No usable RizLine artwork found in %s", root)
            return

        metadata_path = root / "default.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            LOGGER.warning("RizLine artwork metadata is unavailable: %s", metadata_path)
            return
        if not isinstance(metadata, dict):
            LOGGER.warning("RizLine artwork metadata must be a JSON object: %s", metadata_path)
            return

        levels: list[Any] = []
        for collection_name in ("levels", "discOLevels"):
            collection = metadata.get(collection_name)
            if isinstance(collection, list):
                levels.extend(collection)
        for level in levels:
            if not isinstance(level, dict):
                continue
            track_id = level.get("id")
            illustration_id = level.get("illustrationId")
            if not isinstance(track_id, str) or not isinstance(illustration_id, str):
                continue
            artwork = self._best_artwork(image_index, illustration_id)
            if artwork:
                for key in _artwork_track_keys(track_id):
                    self._artwork_by_track.setdefault(key, artwork)

        # Most current resource dumps use matching ``track.`` and
        # ``illustration.`` identifiers.  Keep a best-effort fallback for a
        # partial dump whose ``default.json`` was not exported.
        for filename, artwork in image_index.items():
            stem = filename.rsplit(".", maxsplit=1)[0]
            if stem.startswith("illustration."):
                track_id = "track." + stem.removeprefix("illustration.")
                for key in _artwork_track_keys(track_id):
                    self._artwork_by_track.setdefault(key, artwork)

        LOGGER.info("Indexed %d RizLine song artwork entries from %s", len(self._artwork_by_track), root)

    @staticmethod
    def _build_image_index(root: Path) -> dict[str, Path]:
        index: dict[str, Path] = {}
        for directory_name in ("illustrations", "alt_illustrations"):
            directory = root / directory_name
            if not directory.is_dir():
                continue
            try:
                candidates = directory.rglob("*")
                for candidate in candidates:
                    if not candidate.is_file() or candidate.suffix.casefold() not in ARTWORK_IMAGE_SUFFIXES:
                        continue
                    resolved = candidate.resolve(strict=True)
                    try:
                        resolved.relative_to(root)
                    except ValueError:
                        LOGGER.warning("Skipping artwork outside configured directory: %s", candidate)
                        continue
                    index.setdefault(candidate.name.casefold(), resolved)
            except OSError:
                LOGGER.exception("Unable to scan RizLine artwork directory %s", directory)
        return index

    @staticmethod
    def _best_artwork(image_index: dict[str, Path], illustration_id: str) -> Path | None:
        base = illustration_id.strip()
        if not base:
            return None
        for candidate in (f"{base}.HiRes", f"{base}.cn.HiRes", base, f"{base}.cn"):
            filename = f"{_asset_safe_filename(candidate)}.png".casefold()
            artwork = image_index.get(filename)
            if artwork:
                return artwork
        return None


class RizlineCardAssetCatalog:
    """Read card artwork and local Chinese title strings from an export root.

    All paths are discovered by scanning a configured local export directory;
    identifiers from a save are used only as dictionary keys and never become
    filesystem paths.  This preserves the renderer's local-only boundary.
    """

    def __init__(self, path: Path | str | None) -> None:
        self._path = Path(path) if path else None
        self._loaded = False
        self._illustrations: dict[str, Path] = {}
        self._avatars: dict[str, Path] = {}
        self._layouts: dict[str, Path] = {}
        self._title_by_id: dict[str, str] = {}

    def avatar(self, asset_id: str) -> Path | None:
        self._load_once()
        # Player card avatars are normally illustration IDs.  ``avatars/`` is
        # only a fallback for the small NPC avatar collection in the export.
        return self._find_image(asset_id, self._illustrations) or self._find_image(asset_id, self._avatars)

    def background(self, asset_id: str) -> Path | None:
        self._load_once()
        return self._find_image(asset_id, self._illustrations) or self._find_image(asset_id, self._layouts)

    def layout(self, asset_id: str) -> Path | None:
        self._load_once()
        return self._find_image(asset_id, self._layouts)

    def title(self, title_id: str) -> str | None:
        self._load_once()
        return self._title_by_id.get(title_id)

    def ui_asset(self, name: str) -> Path | None:
        relative_path = UI_RESOURCE_PATHS.get(name)
        if not self._path or not relative_path:
            return None
        try:
            root = self._path.resolve(strict=True)
            candidate = (root / relative_path).resolve(strict=True)
            candidate.relative_to(root)
            return candidate if candidate.is_file() else None
        except (OSError, ValueError):
            return None

    def _load_once(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path:
            return
        try:
            root = self._path.resolve(strict=True)
        except OSError:
            return
        if not root.is_dir():
            return

        self._illustrations = self._index_images(root, ("illustrations", "alt_illustrations"))
        self._avatars = self._index_images(root, ("avatars",))
        self._layouts = self._index_images(root, ("layouts",))
        self._title_by_id = self._read_title_map(root)

    @staticmethod
    def _index_images(root: Path, directory_names: tuple[str, ...]) -> dict[str, Path]:
        index: dict[str, Path] = {}
        for directory_name in directory_names:
            directory = root / directory_name
            if not directory.is_dir():
                continue
            try:
                for candidate in directory.rglob("*"):
                    if not candidate.is_file() or candidate.suffix.casefold() not in ARTWORK_IMAGE_SUFFIXES:
                        continue
                    resolved = candidate.resolve(strict=True)
                    try:
                        resolved.relative_to(root)
                    except ValueError:
                        LOGGER.warning("Skipping card asset outside configured directory: %s", candidate)
                        continue
                    index.setdefault(candidate.name.casefold(), resolved)
            except OSError:
                LOGGER.exception("Unable to scan RizLine player card assets in %s", directory)
        return index

    @staticmethod
    def _find_image(asset_id: str, index: dict[str, Path]) -> Path | None:
        base = asset_id.strip()
        if not base or len(base) > 256:
            return None
        for candidate in (f"{base}.HiRes", f"{base}.cn.HiRes", base, f"{base}.cn"):
            artwork = index.get(f"{_asset_safe_filename(candidate)}.png".casefold())
            if artwork:
                return artwork
        return None

    @staticmethod
    def _read_title_map(root: Path) -> dict[str, str]:
        titles: dict[str, str] = {}
        localization = root / "localization"
        for name in ("local.zh-Hans.bio.txt", "local.zh-Hans.achievement.txt"):
            path = localization / name
            try:
                if path.stat().st_size > MAX_LOCALIZATION_BYTES:
                    continue
                lines = path.read_text(encoding="utf-8-sig").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line in lines:
                key, separator, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if not separator or not key or not value or len(key) > 160 or len(value) > 160:
                    continue
                titles.setdefault(key, value)
        return titles


def build_b40(records: Iterable[Any], catalog: RizlineSongCatalog) -> B40Report:
    """Build an AH5+B35 report when all ranked charts have metadata.

    If chart metadata is missing, the function deliberately falls back to a
    plainly labelled RKS Top 40 report rather than presenting an inaccurate
    AH5+B35 result.
    """

    ranked_records = sorted(
        (record for record in records if getattr(record, "rks", None) is not None),
        key=lambda record: (float(record.rks), int(getattr(record, "score", 0))),
        reverse=True,
    )
    entries: list[B40Entry] = []
    for source_rank, record in enumerate(ranked_records, start=1):
        chart = catalog.lookup(str(record.track_id), str(record.difficulty))
        title = chart.title if chart else str(getattr(record, "title", record.track_id))
        entries.append(
            B40Entry(
                record=record,
                source_rank=source_rank,
                title=title,
                chart=chart,
                ah_status=_ah_status(record, chart),
            )
        )

    missing_metadata_count = sum(entry.chart is None for entry in entries)
    if missing_metadata_count:
        return B40Report(
            special5=(),
            ordinary35=tuple(entries[:40]),
            overflow=tuple(entries[40:50]),
            exact_ah5_b35=False,
            missing_metadata_count=missing_metadata_count,
        )

    confirmed = [entry for entry in entries if entry.ah_status == 1]
    possible = [entry for entry in entries if entry.ah_status == 2]
    special: list[B40Entry] = []
    selected_keys: set[str] = set()
    for candidate in confirmed + possible:
        if len(special) >= 5:
            break
        key = _entry_key(candidate)
        if key not in selected_keys:
            special.append(candidate)
            selected_keys.add(key)
    special.sort(key=lambda entry: entry.rks, reverse=True)

    ordinary: list[B40Entry] = []
    overflow: list[B40Entry] = []
    for entry in entries:
        if _entry_key(entry) in selected_keys:
            continue
        if len(ordinary) < 35:
            ordinary.append(entry)
        elif len(overflow) < 10:
            overflow.append(entry)
        else:
            break

    return B40Report(
        special5=tuple(special),
        ordinary35=tuple(ordinary),
        overflow=tuple(overflow),
        exact_ah5_b35=True,
        missing_metadata_count=0,
    )


class B40ImageRenderer:
    """Render a PNG using optional, locally exported song artwork."""

    def __init__(
        self,
        output_dir: Path | str | None,
        *,
        artwork_dir: Path | str | None = None,
    ) -> None:
        self._output_dir = Path(output_dir) if output_dir else None
        self._artwork_catalog = RizlineArtworkCatalog(artwork_dir)
        self._card_assets = RizlineCardAssetCatalog(artwork_dir)
        self._font_path = self._card_assets.ui_asset("font")
        self._fonts: dict[tuple[int, bool], ImageFont.ImageFont] = {}

    def _font(self, size: int, *, bold: bool = False) -> ImageFont.ImageFont:
        key = (size, bold)
        if key not in self._fonts:
            try:
                if not self._font_path:
                    raise OSError("No local game font")
                self._fonts[key] = ImageFont.truetype(str(self._font_path), size=size)
            except (OSError, ValueError):
                self._fonts[key] = _load_font(size, bold=bold)
        return self._fonts[key]

    def render(
        self,
        username: str,
        alias: str,
        report: B40Report,
        total_rks: float | None = None,
        player_card: RizlinePlayerCard | None = None,
    ) -> Path | None:
        if not self._output_dir:
            return None
        self._output_dir.mkdir(parents=True, exist_ok=True)

        width = 1650
        margin = 39
        gap = 18
        columns = 5
        card_width = (width - 2 * margin - gap * (columns - 1)) // columns
        header_height = 320
        section_height = 60
        card_height = 174
        if report.exact_ah5_b35:
            sections = (("AH5", "ALL HIT / 优先谱面", report.special5, 5),
                        ("B35", "BEST PERFORMANCE / 最佳成绩", report.ordinary35, 35))
        else:
            sections = (("TOP 40", "RKS RANKING / 定数数据不完整", report.ordinary35, 40),)
        height = header_height + 114 + (len(sections) - 1) * 28
        for _, _, _, slots in sections:
            rows = math.ceil(slots / columns)
            height += section_height + rows * card_height + (rows - 1) * gap

        image = Image.new("RGBA", (width, height), "#EEF7F8")
        card = player_card or RizlinePlayerCard()
        self._draw_background(image, width, height, card)
        draw = ImageDraw.Draw(image)
        small = self._font(16)
        self._draw_player_pill(image, draw, width, username or alias, report, total_rks, card)

        y = header_height
        for title, subtitle, entries, slots in sections:
            self._draw_section_header(
                draw, margin, y, width - margin * 2, title, subtitle, len(entries), slots
            )
            y += section_height
            for index in range(slots):
                row, column = divmod(index, columns)
                x = margin + column * (card_width + gap)
                card_y = y + row * (card_height + gap)
                entry = entries[index] if index < len(entries) else None
                self._draw_card(image, draw, x, card_y, card_width, card_height, entry, index + 1)
            rows = math.ceil(slots / columns)
            y += rows * card_height + (rows - 1) * gap + 28

        footer = "Rizline  /  BEST 40     ·     LOCAL SAVE     ·     QQ BOT"
        footer_width = _text_width(draw, footer, small)
        draw.text(((width - footer_width) // 2, height - 42), footer, fill="#637C85", font=small, anchor="lt")
        if report.missing_metadata_count:
            note = f"{report.missing_metadata_count} 张谱面缺少定数资料 · 当前为 RKS TOP 40，并非精确 AH5 + B35"
        else:
            note = "AH = 已确认    ?AH = 兼容候选    FC = Full Combo    /    卡片下栏：分数 · 完成率"
        note_width = _text_width(draw, note, small)
        draw.text(((width - note_width) // 2, height - 73), note, fill="#57737D", font=small, anchor="lt")
        filename = f"rizline_b40_{_safe_filename(alias)}_{int(time.time())}_{secrets.token_hex(6)}.png"
        target = self._output_dir / filename
        temporary = self._output_dir / f".{filename}.tmp"
        image.convert("RGB").save(temporary, format="PNG", optimize=True)
        temporary.replace(target)
        return target

    def _draw_background(
        self,
        image: Image.Image,
        width: int,
        height: int,
        player_card: RizlinePlayerCard,
    ) -> None:
        texture_path = self._card_assets.ui_asset("card")
        if texture_path:
            texture = self._open_fitted_image(texture_path, image.size, centering=(0.5, 0.5))
            if texture:
                image.alpha_composite(texture)
        image.alpha_composite(Image.new("RGBA", image.size, (219, 243, 245, 90)))
        background = self._card_assets.background(player_card.background_id)
        if background:
            backdrop = self._open_fitted_image(background, (width, height), centering=(0.5, 0.5))
            if backdrop:
                backdrop = backdrop.filter(ImageFilter.GaussianBlur(6))
                backdrop.putalpha(backdrop.getchannel("A").point(lambda value: int(value * 0.14)))
                image.alpha_composite(backdrop)

        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        for center_x, center_y, radius in ((width - 80, 120, 360), (10, height - 100, 310)):
            overlay_draw.ellipse(
                (center_x - radius, center_y - radius, center_x + radius, center_y + radius),
                outline=(70, 185, 207, 30), width=42,
            )
            overlay_draw.ellipse(
                (center_x - radius + 62, center_y - radius + 62,
                 center_x + radius - 62, center_y + radius - 62),
                outline=(255, 255, 255, 125), width=3,
            )
        for dot_y in range(22, height, 28):
            for dot_x in range(22 + (14 if (dot_y // 28) % 2 else 0), width, 28):
                overlay_draw.ellipse((dot_x, dot_y, dot_x + 3, dot_y + 3), fill=(46, 133, 157, 24))
        image.alpha_composite(overlay)

    def _draw_player_pill(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        image_width: int,
        username: str,
        report: B40Report,
        total_rks: float | None,
        player_card: RizlinePlayerCard,
    ) -> None:
        title_font = self._font(40, bold=True)
        small_font = self._font(17)
        draw.text((40, 32), "Rizline", fill="#203B46", font=self._font(42, bold=True), anchor="lt")
        draw.line((229, 35, 229, 72), fill="#91C8D3", width=2)
        draw.text((251, 37), "律动轨迹 / BEST 40", fill="#376673", font=self._font(21), anchor="lt")
        draw.text((252, 67), "YOUR RHYTHM, YOUR RECORD.", fill="#6D8B94", font=self._font(12), anchor="lt")
        badge_width = 181
        badge_x = image_width - 39 - badge_width
        draw.rounded_rectangle((badge_x, 35, image_width - 39, 78), radius=21, fill="#239FB6")
        draw.text((badge_x + 25, 47), "成绩记录 / B40", fill="#FFFFFF", font=small_font, anchor="lt")

        x, y = 39, 109
        pill_width = image_width - 78
        pill_height = 174
        draw.rounded_rectangle((x, y + 6, x + pill_width, y + pill_height + 6), radius=48, fill="#C3D9DF")
        panel = Image.new("RGBA", (pill_width, pill_height), "#FFFFFF")
        background = self._card_assets.background(player_card.background_id)
        if background:
            backdrop = self._open_fitted_image(background, panel.size, centering=(0.5, 0.4))
            if backdrop:
                panel.alpha_composite(backdrop)
                panel.alpha_composite(Image.new("RGBA", panel.size, (255, 255, 255, 202)))
        layout = self._card_assets.layout(player_card.layout_id)
        if layout:
            layout_texture = self._open_fitted_image(layout, panel.size, centering=(0.5, 0.5))
            if layout_texture:
                layout_texture.putalpha(layout_texture.getchannel("A").point(lambda value: int(value * 0.12)))
                panel.alpha_composite(layout_texture)
        panel_mask = Image.new("L", panel.size, 0)
        ImageDraw.Draw(panel_mask).rounded_rectangle((0, 0, pill_width - 1, pill_height - 1), radius=48, fill=255)
        image.paste(panel, (x, y), panel_mask)
        draw.rounded_rectangle((x, y, x + pill_width, y + pill_height), radius=48, outline="#FFFFFF", width=3)

        avatar_x = x + 25
        avatar_y = y + 17
        avatar_size = 140
        avatar_path = self._card_assets.avatar(player_card.avatar_id) or self._card_assets.ui_asset("avatar")
        if not self._draw_player_avatar(image, avatar_x, avatar_y, avatar_size, avatar_path, player_card):
            draw.ellipse(
                (avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size),
                fill="#DFF2F5", outline="#FFFFFF", width=6,
            )
            initial = (username.strip() or "R")[0].upper()
            initial_font = self._font(48, bold=True)
            initial_width = _text_width(draw, initial, initial_font)
            draw.text(
                (avatar_x + (avatar_size - initial_width) // 2, avatar_y + 46),
                initial, fill="#239FB6", font=initial_font, anchor="lt",
            )

        content_left = x + 190
        draw.text(
            (content_left, y + 27), _truncate(draw, username, title_font, 730),
            fill="#203640", font=title_font, anchor="lt",
        )
        title_labels = [
            label
            for label in (
                self._card_assets.title(player_card.bio_id1),
                self._card_assets.title(player_card.bio_id2),
            )
            if label
        ]
        if title_labels:
            title_text = _truncate(draw, " · ".join(title_labels), small_font, 720)
            title_width = _text_width(draw, title_text, small_font)
            draw.rounded_rectangle(
                (content_left, y + 84, content_left + title_width + 28, y + 114),
                radius=15, fill="#E2F3F5",
            )
            draw.text((content_left + 14, y + 90), title_text, fill="#237B8D", font=small_font, anchor="lt")
        mode = "AH5 + B35" if report.exact_ah5_b35 else "RKS TOP 40"
        draw.text(
            (content_left, y + 133), f"{mode}    /    {len(report.selected):02d} RECORDS",
            fill="#506F7A", font=small_font, anchor="lt",
        )
        rating_left = x + pill_width - 421
        draw.rounded_rectangle(
            (rating_left, y + 17, x + pill_width - 18, y + pill_height - 17),
            radius=35, fill="#E8F6F7", outline="#CEE9EC", width=2,
        )
        rating_label = "B40 RATING" if report.exact_ah5_b35 else "TOP 40 RATING"
        draw.text((rating_left + 29, y + 34), rating_label, fill="#2B7D8E", font=small_font, anchor="lt")
        rating_text = f"{report.rating:.4f}"
        rating_font = self._font(51, bold=True)
        if _text_width(draw, rating_text, rating_font) > 345:
            rating_font = self._font(36, bold=True)
        draw.text((rating_left + 27, y + 65), rating_text, fill="#156E84", font=rating_font, anchor="lt")
        total_label = f"TOTAL RKS  {total_rks:.4f}" if total_rks is not None else "SUM / 40  ·  LOCAL SAVE"
        draw.text((rating_left + 29, y + 127), total_label, fill="#557D86", font=self._font(14), anchor="lt")

    @staticmethod
    def _open_fitted_image(
        path: Path,
        size: tuple[int, int],
        *,
        centering: tuple[float, float],
    ) -> Image.Image | None:
        try:
            with Image.open(path) as source:
                source.load()
                if (
                    source.width <= 0
                    or source.height <= 0
                    or source.width * source.height > MAX_RESOURCE_IMAGE_PIXELS
                ):
                    return None
                return ImageOps.fit(
                    source.convert("RGBA"),
                    size,
                    method=Image.Resampling.LANCZOS,
                    centering=centering,
                )
        except (OSError, ValueError):
            LOGGER.warning("Unable to open RizLine player card asset %s", path)
            return None

    def _draw_player_avatar(
        self,
        image: Image.Image,
        x: int,
        y: int,
        size: int,
        avatar_path: Path | None,
        player_card: RizlinePlayerCard,
    ) -> bool:
        if avatar_path is None:
            return False
        try:
            with Image.open(avatar_path) as source:
                source.load()
                if (
                    source.width <= 0
                    or source.height <= 0
                    or source.width * source.height > MAX_RESOURCE_IMAGE_PIXELS
                ):
                    return False
                inner_size = size - 14
                # ``avatarPos`` is a game UI transform, not a portable crop
                # rectangle.  The old fixed 32% crop can land on a tiny
                # decorative section when applied to exported HiRes art.
                # Containing the original art keeps the selected character
                # visible for every aspect ratio instead of over-zooming it.
                contained = ImageOps.contain(
                    source.convert("RGBA"),
                    (inner_size, inner_size),
                    method=Image.Resampling.LANCZOS,
                )
                avatar = Image.new("RGBA", (inner_size, inner_size), "#1E2836")
                avatar.alpha_composite(
                    contained,
                    ((inner_size - contained.width) // 2, (inner_size - contained.height) // 2),
                )
        except (OSError, ValueError):
            LOGGER.warning("Unable to open RizLine player avatar %s", avatar_path)
            return False

        draw = ImageDraw.Draw(image)
        draw.ellipse((x, y, x + size, y + size), fill="#FFFFFF", outline="#47D0DF", width=4)
        mask = Image.new("L", (inner_size, inner_size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, inner_size - 1, inner_size - 1), fill=255)
        image.paste(avatar, (x + 7, y + 7), mask)
        draw.ellipse((x, y, x + size, y + size), outline="#47D0DF", width=4)
        return True

    def _draw_section_header(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        width: int,
        title: str,
        subtitle: str,
        count: int,
        slots: int,
    ) -> None:
        title_font = self._font(26, bold=True)
        small_font = self._font(15)
        pill_width = _text_width(draw, title, title_font) + 49
        draw.rounded_rectangle((x, y, x + pill_width, y + 38), radius=19, fill="#229FB7")
        draw.text((x + 24, y + 6), title, fill="#FFFFFF", font=title_font, anchor="lt")
        subtitle_x = x + pill_width + 21
        draw.text((subtitle_x, y + 12), subtitle, fill="#39616F", font=small_font, anchor="lt")
        line_start = subtitle_x + _text_width(draw, subtitle, small_font) + 24
        line_end = x + width - 113
        draw.line((line_start, y + 20, line_end, y + 20), fill="#98BFC8", width=2)
        draw.ellipse((line_end - 4, y + 16, line_end + 4, y + 24), fill="#229FB7")
        count_text = f"{count:02d} / {slots:02d}"
        draw.text((x + width - _text_width(draw, count_text, small_font), y + 12),
                  count_text, fill="#39616F", font=small_font, anchor="lt")

    def _draw_card(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        width: int,
        height: int,
        entry: B40Entry | None,
        slot: int,
    ) -> None:
        title_font = self._font(20, bold=True)
        body_font = self._font(16)
        label_font = self._font(12)
        radius = 30
        if entry is not None:
            draw.rounded_rectangle((x, y + 4, x + width, y + height + 4), radius=radius, fill="#C6DADF")
        draw.rounded_rectangle(
            (x, y, x + width, y + height), radius=radius,
            fill="#FFFFFF" if entry else "#EAF1F3", outline="#FFFFFF", width=2,
        )
        rank_text = f"{slot:02d}"
        draw.text((x + width - 40, y + 18), rank_text, fill="#809CA6", font=body_font, anchor="lt")
        circle_x = x + 17
        circle_y = y + 52
        circle_size = 86
        if entry is None:
            draw.ellipse((circle_x, circle_y, circle_x + circle_size, circle_y + circle_size),
                         fill="#E0EBEE", outline="#FFFFFF", width=3)
            draw.ellipse((circle_x + 30, circle_y + 30, circle_x + 56, circle_y + 56),
                         outline="#B8CFD6", width=3)
            draw.text((x + 124, y + 76), "NO RECORD", fill="#809CA6", font=label_font, anchor="lt")
            draw.text((x + 124, y + 101), "等待新成绩", fill="#809CA6", font=body_font, anchor="lt")
            return

        color = DIFFICULTY_COLORS.get(entry.difficulty, "#6F82B1")
        title = _truncate(draw, entry.title, title_font, width - 78)
        draw.text((x + 19, y + 16), title, fill="#203B46", font=title_font, anchor="lt")
        artwork = self._artwork_catalog.lookup(str(entry.record.track_id))
        has_artwork = self._draw_artwork_circle(image, circle_x, circle_y, circle_size, artwork)
        if not has_artwork:
            draw.ellipse(
                (circle_x, circle_y, circle_x + circle_size, circle_y + circle_size),
                fill=color, outline="#EDF4F6", width=4,
            )
            for inset in (17, 28):
                draw.arc(
                    (circle_x + inset, circle_y + inset, circle_x + circle_size - inset, circle_y + circle_size - inset),
                    35, 295, fill="#FFFFFF", width=3,
                )
        info_x = x + 118
        const_text = f"{entry.chart.chart_const:.1f}" if entry.chart else "—"
        difficulty_text = f"{entry.difficulty}  {const_text}"
        badge_width = _text_width(draw, difficulty_text, body_font) + 22
        draw.rounded_rectangle((info_x, y + 51, info_x + badge_width, y + 76), radius=12, fill=color)
        draw.text((info_x + 11, y + 56), difficulty_text, fill="#FFFFFF", font=body_font, anchor="lt")
        rks_text = f"{entry.rks:.3f}"
        rks_font = self._font(29, bold=True)
        if _text_width(draw, rks_text, rks_font) > width - 137:
            rks_font = self._font(23, bold=True)
        draw.text((info_x, y + 88), rks_text, fill="#203B46", font=rks_font, anchor="lt")
        draw.text((info_x + 1, y + 122), "RKS", fill="#78949E", font=label_font, anchor="lt")
        status = "AH" if entry.ah_status == 1 else "?AH" if entry.ah_status == 2 else "FC" if entry.record.full_combo else ""
        if status:
            status_width = _text_width(draw, status, label_font) + 14
            status_x = x + width - status_width - 18
            status_color = "#A56B24" if entry.ah_status else "#298596"
            status_bg = "#FFF1D4" if entry.ah_status else "#E5F5F6"
            draw.rounded_rectangle((status_x, y + 118, x + width - 18, y + 139), radius=10, fill=status_bg)
            draw.text((status_x + 7, y + 122), status, fill=status_color, font=label_font, anchor="lt")
        draw.line((x + 18, y + 143, x + width - 18, y + 143), fill="#E1ECEF", width=1)
        draw.text((x + 18, y + 150), f"{int(entry.record.score):,}", fill="#426773", font=body_font, anchor="lt")
        completion = f"{entry.record.complete_rate:.2f}%"
        draw.text(
            (x + width - 18 - _text_width(draw, completion, body_font), y + 150),
            completion, fill="#568492", font=body_font, anchor="lt",
        )

    @staticmethod
    def _draw_artwork_circle(
        image: Image.Image,
        x: int,
        y: int,
        size: int,
        artwork_path: Path | None,
    ) -> bool:
        if artwork_path is None:
            return False
        try:
            with Image.open(artwork_path) as source:
                source.load()
                if source.width <= 0 or source.height <= 0 or source.width * source.height > 36_000_000:
                    return False
                cover_size = size - 8
                cover = ImageOps.fit(
                    source.convert("RGB"),
                    (cover_size, cover_size),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
        except (OSError, ValueError):
            LOGGER.warning("Unable to open RizLine artwork %s", artwork_path)
            return False

        draw = ImageDraw.Draw(image)
        draw.ellipse((x, y, x + size, y + size), fill="#FFFFFF")
        mask = Image.new("L", (cover_size, cover_size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, cover_size - 1, cover_size - 1), fill=255)
        image.paste(cover, (x + 4, y + 4), mask)
        draw.ellipse((x, y, x + size, y + size), outline="#FFFFFF", width=4)
        return True


def _ah_status(record: Any, chart: ChartMetadata | None) -> int:
    if chart is None:
        return 0
    confirmed = chart.max_hit > 0 and chart.riz_hit == chart.max_hit
    if confirmed:
        return 1
    if _hit_full_compatible(record, chart):
        return 2
    return 0


def _hit_full_compatible(record: Any, chart: ChartMetadata) -> bool:
    chart_const = chart.chart_const
    hit_count = _compute_h(chart.riz_hit, str(record.difficulty))
    if chart_const <= 0 or hit_count <= 0:
        return False
    acc = _acc_rks(float(record.complete_rate))
    observed_rks = float(record.rks)
    x = hit_count * (11 * chart_const + acc - observed_rks) / chart_const
    nearest = math.floor(x + 0.5)
    integral = abs(x - nearest) <= EPSILON and -EPSILON <= x <= hit_count + EPSILON
    perfect = max(0, math.floor((int(record.score) - 1_000_000) / 100 + 0.5))
    expected = 10 * chart_const + acc + chart_const * min(perfect, hit_count) / hit_count
    return integral or _float32_equal(expected, observed_rks)


def _compute_h(riz_hit: int, difficulty: str) -> int:
    if riz_hit <= 0:
        return 0
    factor = {"EZ": 0.5, "HD": 0.9}.get(difficulty.upper(), 0.98)
    if (1 - factor) * riz_hit > 5:
        return math.ceil(factor * riz_hit - EPSILON)
    return riz_hit - 5


def _acc_rks(complete_rate: float) -> float:
    for threshold, value in (
        (119.9999, 10.5),
        (118, 10.2),
        (116, 9.9),
        (114, 9.6),
        (112, 9.3),
        (110, 9.0),
        (105, 8.25),
        (100, 7.5),
        (95, 6.75),
        (90, 6.0),
        (80, 4.5),
        (70, 3.0),
        (60, 1.5),
    ):
        if complete_rate >= threshold:
            return value
    return 0.0


def _float32_equal(left: float, right: float) -> bool:
    return struct.pack("!f", left) == struct.pack("!f", right)


def _track_variants(track_id: str) -> tuple[str, ...]:
    clean = track_id.strip()
    decoded = unquote(unescape(clean))
    variants: list[str] = []
    for value in (clean, decoded):
        without_prefix = value.removeprefix(TRACK_PREFIX)
        with_prefix = value if value.startswith(TRACK_PREFIX) else f"{TRACK_PREFIX}{value}"
        for candidate in (value, without_prefix, with_prefix):
            if candidate and candidate not in variants:
                variants.append(candidate)
    return tuple(variants)


def _artwork_track_keys(track_id: str) -> tuple[str, ...]:
    """Return compatible keys for score records and level metadata."""
    keys: list[str] = []
    for variant in _track_variants(track_id):
        shortened = re.sub(r"\.\d+$", "", variant)
        for candidate in (variant, shortened):
            if candidate and candidate not in keys:
                keys.append(candidate)
    return tuple(keys)


def _asset_safe_filename(value: str) -> str:
    """Match the filename normalization used by the external exporter."""
    return value.replace("/", "_").replace("\\", "_")


def _entry_key(entry: B40Entry) -> str:
    return f"{entry.record.track_id}|{entry.difficulty}"


def _title_from_track(track_id: str) -> str:
    value = track_id.removeprefix(TRACK_PREFIX)
    value = re.sub(r"\.\d+$", "", value)
    return value.split(".", maxsplit=1)[0] or track_id


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").rstrip("+"))
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return round(number) if number is not None else None


def _load_font(size: int, *, bold: bool) -> ImageFont.ImageFont:
    for candidate in FONT_BOLD_PATHS if bold else FONT_REGULAR_PATHS:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    return int(draw.textlength(text, font=font))


def _truncate(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if _text_width(draw, text, font) <= max_width:
        return text
    suffix = "…"
    output = text
    while output and _text_width(draw, output + suffix, font) > max_width:
        output = output[:-1]
    return output + suffix if output else suffix


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value)[:40] or "player"
