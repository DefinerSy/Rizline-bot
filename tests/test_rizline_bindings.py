from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bot import ReplyService
from rizline import RizlineScoreService
from rizline_bindings import RizlineBindingStore


class RizlineBindingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.now = 1_000.0
        self.store = RizlineBindingStore(self.root / "bindings.json", now=lambda: self.now)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_code_is_one_time_and_not_stored_in_plaintext(self) -> None:
        code = self.store.issue_code("alice", expires_seconds=300)
        database = (self.root / "bindings.json").read_text(encoding="utf-8")
        self.assertNotIn(code, database)

        result = self.store.redeem(code, "qq-openid-1")

        self.assertEqual(result.status, "bound")
        self.assertEqual(result.alias, "alice")
        self.assertEqual(self.store.alias_for("qq-openid-1"), "alice")
        self.assertEqual(self.store.redeem(code, "qq-openid-2").status, "invalid")

    def test_expired_code_is_rejected_and_removed(self) -> None:
        code = self.store.issue_code("alice", expires_seconds=60)
        self.now += 61

        self.assertEqual(self.store.redeem(code, "qq-openid-1").status, "invalid")
        contents = json.loads((self.root / "bindings.json").read_text(encoding="utf-8"))
        self.assertEqual(contents["codes"], {})


class RizlineBindingReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_binding_uses_c2c_and_restricts_the_bound_player(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "alice.json").write_text(
                json.dumps({"username": "Alice", "myBest": [], "levelsRks": []}), encoding="utf-8"
            )
            store = RizlineBindingStore(root / "bindings.json")
            service = ReplyService(RizlineScoreService(root), bindings=store)
            code = store.issue_code("alice")

            group_result = await service.response_for(
                f"/riz bind {code}", source="group", user_openid="qq-openid-1"
            )
            bound_result = await service.response_for(
                f"/riz bind {code}", source="c2c", user_openid="qq-openid-1"
            )
            unbound_result = await service.response_for(
                "/riz b40", source="c2c", user_openid="qq-openid-2"
            )
            other_player_result = await service.response_for(
                "/riz b40 bob", source="c2c", user_openid="qq-openid-1"
            )

            self.assertIn("C2C 私聊", group_result.content)
            self.assertIn("已绑定", bound_result.content)
            self.assertIn("请先", unbound_result.content)
            self.assertIn("不能查询其他玩家", other_player_result.content)
