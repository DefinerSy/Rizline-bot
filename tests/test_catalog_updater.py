from __future__ import annotations

import unittest

from tools.update_rizline_catalog import build_catalog


class CatalogUpdaterTests(unittest.TestCase):
    def test_normalizes_cached_source_and_recovers_missing_riz_hit_from_score(self) -> None:
        songs = {
            "cargoquery": [
                {
                    "Page": "Test Song",
                    "Title": "Test Song",
                    "Artist": "Tester",
                    "IN Lv": "14.5",
                    "IN Hit": "1000",
                    "IN RizHit": "0",
                    "IN Score": "1022000",
                }
            ]
        }
        mapping = {"TestSong.Tester.0": "Test Song"}

        catalog, unmatched = build_catalog(songs, mapping)

        self.assertEqual(unmatched, 0)
        track = catalog["tracks"]["track.TestSong.Tester.0"]
        self.assertEqual(track["title"], "Test Song")
        self.assertEqual(track["charts"]["IN"], {"const": 14.5, "hit": 1000, "riz_hit": 220})


if __name__ == "__main__":
    unittest.main()
