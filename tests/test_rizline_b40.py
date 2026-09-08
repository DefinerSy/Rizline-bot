from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from rizline import ScoreRecord
from rizline_b40 import (
    B40ImageRenderer,
    RizlineArtworkCatalog,
    RizlineCardAssetCatalog,
    RizlinePlayerCard,
    RizlineSongCatalog,
    build_b40,
    _load_font,
)


def make_record(index: int, rks: float, *, title: str | None = None) -> ScoreRecord:
    return ScoreRecord(
        track_id=f"track.Chart{index}.tester.0",
        title=title or f"Chart {index}",
        difficulty="IN",
        score=1_001_000,
        complete_rate=120.0,
        full_combo=True,
        cleared=True,
        rks=rks,
    )


def write_catalog(path: Path, records: list[ScoreRecord], *, possible_indices: set[int] = set()) -> None:
    tracks = {}
    for index, record in enumerate(records, start=1):
        if index in possible_indices:
            chart = {"const": 14.0, "hit": 100, "riz_hit": 50}
        else:
            chart = {"const": 14.0, "hit": 100, "riz_hit": 100}
        tracks[record.track_id] = {"title": f"Catalog Chart {index}", "charts": {"IN": chart}}
    path.write_text(json.dumps({"tracks": tracks}), encoding="utf-8")


class RizlineB40Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_confirmed_ah_are_selected_before_possible_candidates(self) -> None:
        # With IN, RizHit=50 gives h=45.  These values satisfy the reference
        # project's integrality check and therefore become ?AH candidates.
        possible_rks = [164.1888888888889, 163.87777777777777, 163.56666666666666]
        records = [
            make_record(1, 170.0),
            make_record(2, 169.0),
            make_record(3, 168.0),
            *(make_record(index + 4, rks) for index, rks in enumerate(possible_rks)),
        ]
        catalog_path = self.root / "catalog.json"
        write_catalog(catalog_path, records, possible_indices={4, 5, 6})

        report = build_b40(records, RizlineSongCatalog(catalog_path))

        self.assertTrue(report.exact_ah5_b35)
        self.assertEqual([entry.ah_status for entry in report.special5], [1, 1, 1, 2, 2])
        self.assertEqual(len(report.ordinary35), 1)
        self.assertEqual(report.ordinary35[0].ah_status, 2)
        self.assertAlmostEqual(report.rating, sum(record.rks or 0.0 for record in records) / 40.0)

    def test_missing_catalog_entry_uses_clearly_labelled_top40_fallback(self) -> None:
        records = [make_record(1, 170.0), make_record(2, 160.0)]
        catalog_path = self.root / "catalog.json"
        write_catalog(catalog_path, records[:1])

        report = build_b40(records, RizlineSongCatalog(catalog_path))

        self.assertFalse(report.exact_ah5_b35)
        self.assertEqual(report.missing_metadata_count, 1)
        self.assertEqual(len(report.special5), 0)
        self.assertEqual(len(report.ordinary35), 2)
        self.assertIn("定数数据不完整", report.mode_label)

    def test_renderer_writes_a_valid_png(self) -> None:
        records = [make_record(1, 170.0), make_record(2, 160.0)]
        catalog_path = self.root / "catalog.json"
        write_catalog(catalog_path, records)
        report = build_b40(records, RizlineSongCatalog(catalog_path))

        output_path = B40ImageRenderer(self.root / "exports").render("测试玩家", "alice", report)

        self.assertIsNotNone(output_path)
        assert output_path is not None
        self.assertTrue(output_path.is_file())
        with Image.open(output_path) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (1650, 2082))
            self.assertEqual(image.mode, "RGB")
            self.assertGreater(min(image.getpixel((10, 400))), 200)

    def test_renderer_uses_local_exported_artwork_when_available(self) -> None:
        records = [make_record(1, 170.0)]
        catalog_path = self.root / "catalog.json"
        write_catalog(catalog_path, records)
        report = build_b40(records, RizlineSongCatalog(catalog_path))

        artwork_root = self.root / "asset-output"
        illustration_id = "illustration.Chart1.tester.0"
        artwork_path = (
            artwork_root
            / "illustrations"
            / illustration_id
            / "HiRes"
            / f"{illustration_id}.HiRes.png"
        )
        artwork_path.parent.mkdir(parents=True)
        Image.new("RGB", (64, 96), "#e02020").save(artwork_path)
        (artwork_root / "default.json").write_text(
            json.dumps({"levels": [{"id": records[0].track_id, "illustrationId": illustration_id}]}),
            encoding="utf-8",
        )

        artwork_catalog = RizlineArtworkCatalog(artwork_root)
        self.assertEqual(artwork_catalog.lookup(records[0].track_id), artwork_path.resolve())
        output_path = B40ImageRenderer(self.root / "exports", artwork_dir=artwork_root).render(
            "测试玩家", "alice", report
        )

        self.assertIsNotNone(output_path)
        assert output_path is not None
        with Image.open(output_path) as image:
            red, green, blue = image.getpixel((99, 475))
            self.assertGreater(red, 180)
            self.assertLess(green, 100)
            self.assertLess(blue, 100)

    def test_renderer_uses_rizcard_background_avatar_and_localized_titles(self) -> None:
        records = [make_record(1, 170.0)]
        catalog_path = self.root / "catalog.json"
        write_catalog(catalog_path, records)
        report = build_b40(records, RizlineSongCatalog(catalog_path))
        asset_root = self.root / "asset-output"

        avatar_id = "illustration.avatar.tester.0"
        avatar_path = asset_root / "illustrations" / avatar_id / f"{avatar_id}.png"
        avatar_path.parent.mkdir(parents=True)
        Image.new("RGB", (100, 100), "#e02020").save(avatar_path)

        background_id = "illustration.background.tester.0"
        background_path = asset_root / "illustrations" / background_id / f"{background_id}.png"
        background_path.parent.mkdir(parents=True)
        Image.new("RGB", (100, 100), "#196cc0").save(background_path)

        layout_id = "layout.00022"
        layout_path = asset_root / "layouts" / layout_id / f"{layout_id}.png"
        layout_path.parent.mkdir(parents=True)
        Image.new("RGBA", (100, 100), "#40603080").save(layout_path)

        localization = asset_root / "localization"
        localization.mkdir(parents=True)
        (localization / "local.zh-Hans.bio.txt").write_text(
            "bio.first=第一称号\nbio.second=第二称号\n",
            encoding="utf-8",
        )

        player_card = RizlinePlayerCard(
            avatar_id=avatar_id,
            background_id=background_id,
            layout_id=layout_id,
            bio_id1="bio.first",
            bio_id2="bio.second",
            avatar_x=0.5,
            avatar_y=0.5,
        )
        card_assets = RizlineCardAssetCatalog(asset_root)
        self.assertEqual(card_assets.avatar(avatar_id), avatar_path.resolve())
        self.assertEqual(card_assets.background(background_id), background_path.resolve())
        self.assertEqual(card_assets.title("bio.first"), "第一称号")

        output_path = B40ImageRenderer(self.root / "exports", artwork_dir=asset_root).render(
            "测试玩家", "alice", report, player_card=player_card
        )

        self.assertIsNotNone(output_path)
        assert output_path is not None
        with Image.open(output_path) as image:
            background_pixel = image.getpixel((10, 220))
            self.assertGreater(background_pixel[2], background_pixel[0])
            avatar_pixel = image.getpixel((134, 196))
            self.assertGreater(avatar_pixel[0], 180)
            self.assertLess(avatar_pixel[1], 100)
            self.assertLess(avatar_pixel[2], 100)

    def test_top40_fallback_renders_all_forty_records_in_one_section(self) -> None:
        records = [make_record(index, 170.0 - index) for index in range(40)]
        report = build_b40(records, RizlineSongCatalog(None))
        renderer = B40ImageRenderer(self.root / "exports")

        with patch.object(renderer, "_draw_card", wraps=renderer._draw_card) as draw_card:
            with patch.object(renderer, "_draw_section_header", wraps=renderer._draw_section_header) as draw_section:
                output_path = renderer.render("测试玩家", "alice", report)

        self.assertEqual(draw_card.call_count, 40)
        self.assertEqual([call.args[6] for call in draw_card.call_args_list], list(report.ordinary35))
        self.assertEqual(draw_section.call_count, 1)
        self.assertEqual(draw_section.call_args.args[4], "TOP 40")
        assert output_path is not None
        with Image.open(output_path) as image:
            self.assertEqual(image.size, (1650, 2012))

    def test_renderer_uses_exported_font_and_falls_back_for_corrupt_font(self) -> None:
        source_font = _load_font(16, bold=False)
        if not getattr(source_font, "path", None):
            self.skipTest("No system TrueType font available")
        asset_root = self.root / "asset-output"
        font_path = asset_root / "ui/Base/Font/Source Han Sans CN&Comfortaa Hybrid.ttf"
        font_path.parent.mkdir(parents=True)
        shutil.copyfile(source_font.path, font_path)

        renderer = B40ImageRenderer(self.root / "exports", artwork_dir=asset_root)
        self.assertEqual(Path(renderer._font(20).path), font_path.resolve())
        self.assertIs(renderer._font(20), renderer._font(20))

        font_path.write_bytes(b"not a font")
        fallback_renderer = B40ImageRenderer(self.root / "exports", artwork_dir=asset_root)
        self.assertNotEqual(Path(fallback_renderer._font(20).path), font_path.resolve())

    def test_ui_resources_reject_unknown_names_and_escaping_symlinks(self) -> None:
        asset_root = self.root / "asset-output"
        ui_dir = asset_root / "ui/Base/Textures"
        ui_dir.mkdir(parents=True)
        outside_image = self.root / "outside.png"
        Image.new("RGB", (10, 10), "red").save(outside_image)
        (ui_dir / "默认头像.png").symlink_to(outside_image)
        assets = RizlineCardAssetCatalog(asset_root)

        self.assertIsNone(assets.ui_asset("avatar"))
        self.assertIsNone(assets.ui_asset("../../outside.png"))

    def test_renderer_uses_default_avatar_when_player_avatar_is_missing(self) -> None:
        asset_root = self.root / "asset-output"
        avatar_path = asset_root / "ui/Base/Textures/默认头像.png"
        avatar_path.parent.mkdir(parents=True)
        Image.new("RGB", (100, 100), "#e02020").save(avatar_path)
        report = build_b40([make_record(1, 100)], RizlineSongCatalog(None))

        output_path = B40ImageRenderer(self.root / "exports", artwork_dir=asset_root).render(
            "测试玩家", "alice", report, player_card=RizlinePlayerCard(avatar_id="missing")
        )

        assert output_path is not None
        with Image.open(output_path) as image:
            self.assertEqual(image.getpixel((134, 196)), (224, 32, 32))

    def test_avatar_keeps_wide_artwork_without_zooming_to_saved_position(self) -> None:
        avatar_path = self.root / "wide-avatar.png"
        avatar = Image.new("RGB", (400, 200), "#e02020")
        avatar.paste("#20e020", (100, 0, 300, 200))
        avatar.save(avatar_path)
        renderer = B40ImageRenderer(self.root / "exports")
        image = Image.new("RGBA", (160, 160), "white")

        self.assertTrue(renderer._draw_player_avatar(
            image, 0, 0, 140, avatar_path, RizlinePlayerCard(avatar_x=0, avatar_y=1)
        ))
        self.assertGreater(image.getpixel((20, 70))[0], 180)
        self.assertGreater(image.getpixel((70, 70))[1], 180)
        self.assertGreater(image.getpixel((120, 70))[0], 180)

    def test_long_titles_and_high_values_render_with_all_difficulties(self) -> None:
        records = [replace(make_record(index, 1234.5678),
                           title="很长的曲名 / A Very Long Song Title " * 5,
                           difficulty=difficulty, score=1_234_567, complete_rate=123.456)
                   for index, difficulty in enumerate(("EZ", "HD", "IN", "AT", "SP"))]
        report = build_b40(records, RizlineSongCatalog(None))
        output_path = B40ImageRenderer(self.root / "exports").render(
            "很长的玩家名称 Long Player Name " * 10, "alice", report, total_rks=1234.5678
        )
        assert output_path is not None
        with Image.open(output_path) as image:
            self.assertEqual(image.mode, "RGB")


if __name__ == "__main__":
    unittest.main()
