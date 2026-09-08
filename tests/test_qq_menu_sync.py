from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tools.sync_qq_commands import (
    PRIVATE_COMMANDS, PUBLIC_COMMANDS, contains_expected, desired_menu, desired_panel,
    MenuApi, merge_menu, owned_panel, sync,
)


class MenuDefinitionTests(unittest.TestCase):
    def test_lengths_and_menu_shapes_follow_official_limits(self) -> None:
        def length(text):
            return sum(1 if ord(character) < 128 else 2 for character in text)

        menu = desired_menu()
        self.assertLessEqual(len(menu["items"]), 10)
        for item in menu["items"]:
            self.assertLessEqual(length(item["name"]), 10)
            children = item.get("sub_menu_items", [])
            self.assertLessEqual(len(children), 5)
            for child in children:
                self.assertLessEqual(length(child["name"]), 14)
                self.assertEqual(child["type"], "send_message")
                self.assertNotIn("sub_menu_items", child)
        for scope in ("c2c", "group"):
            panel = desired_panel(scope)
            self.assertLessEqual(len(panel["items"]), 20)
            for item in panel["items"]:
                self.assertLessEqual(length(item["name"]), 14)
                self.assertLessEqual(length(item["desc"]), 30)

    def test_private_commands_are_absent_from_group_panel(self) -> None:
        group_commands = {item["name"] for item in desired_panel("group")["items"]}
        c2c_commands = {item["name"] for item in desired_panel("c2c")["items"]}
        self.assertEqual(group_commands, {name.removeprefix("/") for name, _ in PUBLIC_COMMANDS})
        self.assertFalse(group_commands & {name.removeprefix("/") for name, _ in PRIVATE_COMMANDS})
        self.assertEqual(len(c2c_commands), 13)
        self.assertEqual(len(group_commands), 7)
        self.assertIn("riz update", group_commands)

    def test_merge_keeps_unrelated_existing_items_and_is_idempotent(self) -> None:
        old_item = {"type": "send_message", "name": "其他", "send_message": "/ping"}
        before = {"items": [old_item]}
        merged = merge_menu(before)
        self.assertEqual(before, {"items": [old_item]})
        self.assertEqual(merged["items"][0], old_item)
        self.assertEqual(merge_menu(merged), merged)
        self.assertEqual(merge_menu(None), desired_menu())

    def test_name_conflicts_and_menu_capacity_stop_before_writing(self) -> None:
        with self.assertRaises(RuntimeError):
            merge_menu({"items": [{"name": "B40", "type": "link", "link": "https://example.com"}]})
        with self.assertRaises(RuntimeError):
            merge_menu({"items": [{"name": f"item{index}"} for index in range(7)]})

    def test_unknown_global_panel_is_not_replaced(self) -> None:
        record = {"panel_id": "existing", "scope": "c2c", "target_type": "all",
                  "panel": {"remark": "not-managed-by-this-tool", "items": []}}
        with self.assertRaises(RuntimeError):
            owned_panel([record], "c2c")
        record["panel"] = desired_panel("c2c")
        self.assertIs(owned_panel([record], "c2c"), record)

    def test_verification_ignores_extra_server_metadata_but_not_missing_commands(self) -> None:
        expected = desired_menu()
        actual = copy.deepcopy(expected)
        actual["version"] = 4
        self.assertTrue(contains_expected(actual, expected))
        actual["items"].pop()
        self.assertFalse(contains_expected(actual, expected))

    def test_panel_matches_qq_normalized_names_and_omitted_false_defaults(self) -> None:
        expected = desired_panel("c2c")
        actual = copy.deepcopy(expected)
        for item in actual["items"]:
            self.assertFalse(item["name"].startswith("/"))
            item.pop("only_admin")
        self.assertTrue(contains_expected(actual, expected))
        actual["items"][0]["only_admin"] = True
        self.assertFalse(contains_expected(actual, expected))


class FakeApi:
    def __init__(self):
        self.settings = SimpleNamespace(app_id="test-bot", is_sandbox=True)
        self.menu = {"menu": None, "version": 1}
        self.records = {}
        self.writes = []

    async def panels(self, scope):
        return copy.deepcopy([record for record in self.records.values() if record["scope"] == scope])

    async def request(self, method, path, **kwargs):
        if method == "GET" and path == "/v2/menu":
            return copy.deepcopy(self.menu)
        if method == "GET":
            return copy.deepcopy(self.records[path.rsplit("/", 1)[1]])
        self.writes.append((method, path))
        payload = kwargs["json"]
        if method == "PUT" and path == "/v2/menu":
            self.menu = {"menu": copy.deepcopy(payload["menu"]), "version": 2}
            return {"version": 2}
        if method == "POST":
            panel_id = f"panel_{len(self.records)}"
            self.records[panel_id] = dict(copy.deepcopy(payload), panel_id=panel_id)
            return {"panel_id": panel_id}
        raise AssertionError("Unexpected test API operation")


class MenuSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_api_list_may_omit_records_but_not_nonterminal_pages(self) -> None:
        api = object.__new__(MenuApi)
        api.request = AsyncMock(return_value={"is_end": True})
        self.assertEqual(await api.panels("c2c"), [])
        api.request = AsyncMock(return_value={"is_end": False})
        with self.assertRaises(RuntimeError):
            await api.panels("c2c")

    async def test_inspection_does_not_write_and_apply_is_backed_up_and_idempotent(self) -> None:
        api = FakeApi()
        with tempfile.TemporaryDirectory() as directory:
            backups = Path(directory) / "backups"
            await sync(api, apply=False, backup_dir=backups)
            self.assertEqual(api.writes, [])
            self.assertFalse(backups.exists())
            await sync(api, apply=True, backup_dir=backups)
            self.assertEqual(len(api.writes), 3)
            self.assertEqual(len(list(backups.iterdir())), 1)
            backup = next(backups.iterdir())
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            await sync(api, apply=True, backup_dir=backups)
            self.assertEqual(len(api.writes), 3)
            self.assertEqual(len(list(backups.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
