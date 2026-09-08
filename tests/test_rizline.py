from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rizline import RizlineScoreService


class RizlineScoreServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.save_dir = Path(self._temporary_directory.name)
        (self.save_dir / "alice.json").write_text(
            json.dumps(
                {
                    "username": "Alice",
                    "totalRks": 15.25,
                    "rizcard": {
                        "avatarId": "illustration.Sky%20City.composer.0",
                        "backgroundId": "illustration.Second.composer.0",
                        "layoutId": "layout.00022",
                        "bioId1": "bio.exbio00001.1",
                        "bioId2": "ach.demo.title1",
                        "avatarPos": {"x": 0.4, "y": 0.6},
                    },
                    "myBest": [
                        {
                            "trackAssetId": "track.Sky%20City.composer.0",
                            "difficultyClassName": "IN",
                            "score": 1_012_345,
                            "completeRate": 119.5,
                            "isFullCombo": True,
                            "isClear": True,
                        },
                        {
                            "trackAssetId": "track.Second.composer.0",
                            "difficultyClassName": "HD",
                            "score": 900_000,
                            "completeRate": 98.0,
                            "isFullCombo": False,
                            "isClear": True,
                        },
                    ],
                    "levelsRks": [
                        {
                            "trackId": "track.Sky%20City.composer.0",
                            "difficultyClassName": "IN",
                            "rks": 16.1,
                        },
                        {
                            "trackId": "track.Second.composer.0",
                            "difficultyClassName": "HD",
                            "rks": 12.2,
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.service = RizlineScoreService(self.save_dir, default_player="alice")

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_profile_uses_default_player(self) -> None:
        result = self.service.reply_for("/riz profile")
        self.assertIn("Alice", result)
        self.assertIn("总 RKS：15.2500", result)
        self.assertIn("已记录谱面：2", result)

    def test_top_sorts_by_rks_and_decodes_title(self) -> None:
        result = self.service.reply_for("/riz top alice 2")
        self.assertIn("1. Sky City [IN]", result)
        self.assertIn("RKS 16.1000", result)
        self.assertIn("2. Second [HD]", result)

    def test_song_query_and_b40_summary(self) -> None:
        self.assertIn("Sky City", self.service.reply_for("/riz song sky"))
        report = self.service.reply_for("/riz alice b40")
        self.assertIn("RKS TOP 40", report)
        self.assertIn("B40 RATING：0.7075", report)
        self.assertIn("不把它误称为 AH5+B35", report)

    def test_b40_response_generates_an_image_when_catalog_and_output_are_configured(self) -> None:
        catalog_path = self.save_dir / "catalog.json"
        catalog_path.write_text(
            json.dumps(
                {
                    "tracks": {
                        "track.Sky%20City.composer.0": {
                            "title": "Sky City",
                            "charts": {"IN": {"const": 14.0, "hit": 1000, "riz_hit": 220}},
                        },
                        "track.Second.composer.0": {
                            "title": "Second",
                            "charts": {"HD": {"const": 10.0, "hit": 600, "riz_hit": 120}},
                        },
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        service = RizlineScoreService(
            self.save_dir,
            default_player="alice",
            song_catalog_path=catalog_path,
            image_output_dir=self.save_dir / "exports",
        )

        result = service.response_for("/riz b40")

        self.assertIsNotNone(result.image_path)
        assert result.image_path is not None
        self.assertTrue(result.image_path.is_file())
        self.assertIn("AH5 + B35", result.content)
        self.assertIn("已生成 B40 成绩图", result.content)

    def test_player_alias_cannot_escape_save_directory(self) -> None:
        result = self.service.reply_for("/riz ../secret profile")
        self.assertIn("玩家别名只能", result)

    def test_profile_extracts_only_the_current_rizcard_display_fields(self) -> None:
        profile, error = self.service._load_player("alice")

        self.assertIsNone(error)
        assert profile is not None
        self.assertEqual(profile.card.avatar_id, "illustration.Sky%20City.composer.0")
        self.assertEqual(profile.card.background_id, "illustration.Second.composer.0")
        self.assertEqual(profile.card.layout_id, "layout.00022")
        self.assertEqual(profile.card.bio_id1, "bio.exbio00001.1")
        self.assertAlmostEqual(profile.card.avatar_x, 0.4)
        self.assertAlmostEqual(profile.card.avatar_y, 0.6)


if __name__ == "__main__":
    unittest.main()
