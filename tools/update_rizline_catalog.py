#!/usr/bin/env python3
"""Download and normalize the optional local RizLine chart catalog.

The bot never fetches this data while answering a QQ message.  Run this tool
manually when an administrator wants to refresh the local cache instead.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from urllib.request import Request, urlopen


DEFAULT_SONGS_URL = "https://dragonred.cn/rizb40/cache/songs_cargoquery.json"
DEFAULT_MAPPING_URL = "https://dragonred.cn/rizb40/cache/track_mapping.json"
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
DIFFICULTIES = ("EZ", "HD", "IN", "AT", "SP")
TRACK_PREFIX = "track."
TRACK_SUFFIX_RE = re.compile(r"\.\d+$")


def _decode_text(value: object) -> str:
    return unquote(unescape(str(value or ""))).strip()


def _normalise(value: object) -> str:
    return "".join(character for character in _decode_text(value).casefold() if character.isalnum())


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value).replace(",", "").strip().rstrip("+")
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def _integer(value: object) -> int | None:
    number = _number(value)
    return round(number) if number is not None else None


def _asset_title(asset_id: str) -> str:
    asset = TRACK_SUFFIX_RE.sub("", asset_id.removeprefix(TRACK_PREFIX))
    return asset.split(".", maxsplit=1)[0]


def _song_rows(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("songs"), dict):
        payload = payload["songs"]
    if not isinstance(payload, dict):
        return []
    rows = payload.get("cargoquery", payload.get("rows", []))
    if not isinstance(rows, list):
        return []
    output: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Raw MediaWiki Cargo replies wrap each row under ``title``.
        nested = row.get("title")
        output.append(nested if isinstance(nested, dict) else row)
    return output


def _mapping_items(payload: object) -> list[tuple[str, str]]:
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("mapping"), dict):
        payload = payload["mapping"]
    if not isinstance(payload, dict):
        return []
    items: list[tuple[str, str]] = []
    for key, value in payload.items():
        # The cached source is asset-id -> title.  Also accept the raw RizWiki
        # form, title -> {id: asset-id}, so the updater remains replaceable.
        if isinstance(value, str):
            asset_id, title = _decode_text(key), _decode_text(value)
        elif isinstance(value, dict) and value.get("id"):
            asset_id, title = _decode_text(value["id"]), _decode_text(key)
        else:
            continue
        asset_id = asset_id.removeprefix(TRACK_PREFIX)
        if asset_id and title:
            items.append((asset_id, title))
    return items


def build_catalog(songs_payload: object, mapping_payload: object) -> tuple[dict[str, Any], int]:
    """Return a bot catalog and the number of mappings that could not be matched."""
    songs_by_name: dict[str, dict[str, Any]] = {}
    for row in _song_rows(songs_payload):
        for key in (row.get("Page"), row.get("Title")):
            normalised = _normalise(key)
            if normalised:
                songs_by_name[normalised] = row

    tracks: dict[str, dict[str, Any]] = {}
    unmatched = 0
    for asset_id, mapped_title in _mapping_items(mapping_payload):
        song = songs_by_name.get(_normalise(mapped_title))
        if song is None:
            song = songs_by_name.get(_normalise(_asset_title(asset_id)))
        if song is None:
            unmatched += 1
            continue

        charts: dict[str, dict[str, float | int]] = {}
        for difficulty in DIFFICULTIES:
            chart_const = _number(song.get(f"{difficulty} Lv"))
            max_hit = _integer(song.get(f"{difficulty} Hit"))
            riz_hit = _integer(song.get(f"{difficulty} RizHit")) or 0
            max_score = _integer(song.get(f"{difficulty} Score")) or 0
            # Some rows omit RizHit even though the maximum score carries the
            # same information.  This follows the reference tool's fallback.
            if riz_hit == 0 and max_score >= 1_000_000:
                riz_hit = round((max_score - 1_000_000) / 100)
            if chart_const is None or chart_const <= 0 or not max_hit or riz_hit <= 0:
                continue
            charts[difficulty] = {
                "const": chart_const,
                "hit": max_hit,
                "riz_hit": riz_hit,
            }

        if charts:
            tracks[f"{TRACK_PREFIX}{asset_id}"] = {
                "title": _decode_text(song.get("Title")) or mapped_title,
                "artist": _decode_text(song.get("Artist")),
                "charts": charts,
            }

    catalog = {
        "format_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "description": "RizLine Chinese Wiki data, normalized from the rizline_b40_tool cache",
            "reference_project": "https://github.com/REDDRAGON-HL/rizline_b40_tool",
        },
        "tracks": tracks,
    }
    return catalog, unmatched


def download_json(url: str) -> object:
    request = Request(url, headers={"User-Agent": "qq-rizline-b40-catalog/1.0", "Accept": "application/json"})
    with urlopen(request, timeout=30) as response:
        payload = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"catalog response from {url} is larger than {MAX_DOWNLOAD_BYTES} bytes")
    return json.loads(payload.decode("utf-8"))


def write_catalog(target: Path, catalog: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh the local RizLine B40 chart catalog")
    parser.add_argument("--output", type=Path, default=Path("data/rizline_song_catalog.json"))
    parser.add_argument("--songs-url", default=DEFAULT_SONGS_URL)
    parser.add_argument("--mapping-url", default=DEFAULT_MAPPING_URL)
    arguments = parser.parse_args(argv)

    try:
        songs = download_json(arguments.songs_url)
        mapping = download_json(arguments.mapping_url)
        catalog, unmatched = build_catalog(songs, mapping)
        if not catalog["tracks"]:
            raise ValueError("the downloaded catalog contains no usable chart records")
        write_catalog(arguments.output, catalog)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Could not update RizLine catalog: {exc}", file=sys.stderr)
        return 1

    print(
        f"Wrote {len(catalog['tracks'])} tracks to {arguments.output} "
        f"({unmatched} mappings could not be matched)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
