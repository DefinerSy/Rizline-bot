"""Inspect or explicitly sync QQ's official menu and command panels."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import QQDnsFallback, Settings


PUBLIC_COMMANDS = (
    ("/riz b40", "生成我的B40成绩图"),
    ("/riz profile", "查看我的玩家资料"),
    ("/riz top", "查看我的最佳成绩"),
    ("/riz song", "查询单曲，后接曲名"),
    ("/riz binding", "查看当前绑定状态"),
    ("/riz help", "查看使用帮助"),
    ("/riz update", "使用已存令牌更新存档"),
)
PRIVATE_COMMANDS = (
    ("/riz login", "自助短信登录绑定"),
    ("/riz resend", "重发登录短信"),
    ("/riz confirm", "核对账号后确认绑定"),
    ("/riz cancel", "取消本次登录"),
    ("/riz unbind", "解除当前绑定"),
    ("/riz bind", "手动绑定，后接绑定码"),
)


def message_item(name: str, command: str) -> dict[str, str]:
    return {"type": "send_message", "name": name, "send_message": command}


def desired_menu() -> dict[str, Any]:
    return {"items": [
        message_item("B40", "/riz b40"),
        {"type": "menu", "name": "查分", "sub_menu_items": [
            message_item("玩家资料", "/riz profile"),
            message_item("最佳成绩", "/riz top"),
            message_item("单曲查询", "/riz song "),
            message_item("使用帮助", "/riz help"),
        ]},
        {"type": "menu", "name": "账号", "sub_menu_items": [
            message_item("登录绑定", "/riz login"),
            message_item("更新存档", "/riz update"),
            message_item("绑定状态", "/riz binding"),
            message_item("手动绑码", "/riz bind "),
            message_item("解除绑定", "/riz unbind"),
        ]},
        {"type": "menu", "name": "登录操作", "sub_menu_items": [
            message_item("重发短信", "/riz resend"),
            message_item("确认绑定", "/riz confirm"),
            message_item("取消登录", "/riz cancel"),
        ]},
    ]}


def desired_panel(scope: str) -> dict[str, Any]:
    if scope not in {"c2c", "group"}:
        raise ValueError("Unsupported command panel scope")
    commands = PUBLIC_COMMANDS + (PRIVATE_COMMANDS if scope == "c2c" else ())
    return {"remark": f"RizLine score bot / {scope}", "items": [
        {"type": "command", "name": name.removeprefix("/"), "desc": description, "only_admin": False}
        for name, description in commands
    ]}


def contains_expected(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            contains_expected(actual.get(key, False), value) if key == "only_admin"
            else key in actual and contains_expected(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            contains_expected(found, wanted) for found, wanted in zip(actual, expected)
        )
    return actual == expected


def merge_menu(current: dict[str, Any] | None) -> dict[str, Any]:
    result = copy.deepcopy(current or {"items": []})
    existing = result.setdefault("items", [])
    for wanted in desired_menu()["items"]:
        matches = [item for item in existing if item.get("name") == wanted["name"]]
        if matches:
            if len(matches) != 1 or not contains_expected(matches[0], wanted):
                raise RuntimeError("Menu name conflict; refusing to overwrite existing configuration")
        else:
            existing.append(wanted)
    if len(existing) > 10:
        raise RuntimeError("Merged menu would exceed QQ's item limit")
    result.pop("version", None)
    return result


def owned_panel(records: list[dict], scope: str) -> dict | None:
    remark = desired_panel(scope)["remark"]
    matches = [record for record in records if record.get("target_type") == "all"
               and record.get("scope") == scope and record.get("panel", {}).get("remark") == remark]
    if len(matches) > 1:
        raise RuntimeError("Multiple managed panels found; refusing ambiguous update")
    if matches:
        return matches[0]
    if any(record.get("target_type") == "all" for record in records):
        raise RuntimeError("An existing global panel needs review before adding another")
    return None


class MenuApi:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self.settings = settings
        self.session = session
        self.headers: dict[str, str] = {}
        self.host = "https://sandbox.api.sgroup.qq.com" if settings.is_sandbox else "https://api.sgroup.qq.com"

    async def authenticate(self) -> None:
        async with self.session.post(
            "https://bots.qq.com/app/getAppAccessToken",
            json={"appId": self.settings.app_id, "clientSecret": self.settings.app_secret},
            allow_redirects=False,
        ) as response:
            data = await response.json(content_type=None)
            if response.status != 200 or not isinstance(data, dict) or not data.get("access_token"):
                raise RuntimeError("QQ authentication failed; credential details suppressed")
        self.headers = {"Authorization": "QQBot " + data["access_token"], "X-Union-Appid": self.settings.app_id}

    async def request(self, method: str, path: str, **kwargs) -> dict:
        if method not in {"GET", "PUT", "POST"} or not re.fullmatch(r"/v2/(menu|panels(?:/[A-Za-z0-9_-]+)?)", path):
            raise RuntimeError("Unexpected menu API request")
        async with self.session.request(method, self.host + path, headers=self.headers,
                                        allow_redirects=False, **kwargs) as response:
            data = await response.json(content_type=None)
            if not 200 <= response.status < 300:
                code = data.get("code") if isinstance(data, dict) else None
                code = code if isinstance(code, int) else "unknown"
                raise RuntimeError(f"QQ menu API HTTP {response.status}, code={code}; response details suppressed")
            if not isinstance(data, dict):
                raise RuntimeError("QQ menu API returned an unexpected response format")
            return data

    async def panels(self, scope: str) -> list[dict]:
        records: list[dict] = []
        cursor = ""
        for _ in range(5):
            params = {"scope": scope, "limit": "50"}
            if cursor:
                params["cursor"] = cursor
            page = await self.request("GET", "/v2/panels", params=params)
            if "records" not in page and page.get("is_end") is True and not page.get("next_cursor"):
                return records
            if not isinstance(page.get("records"), list):
                raise RuntimeError("QQ panel list has no valid records field")
            records.extend(page["records"])
            if page.get("is_end") is True:
                return records
            next_cursor = page.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise RuntimeError("QQ panel pagination did not complete")
            cursor = next_cursor
        raise RuntimeError("QQ panel pagination exceeded safety limit")


async def sync(api: MenuApi, *, apply: bool, backup_dir: Path) -> None:
    menu_before = await api.request("GET", "/v2/menu")
    before = {scope: await api.panels(scope) for scope in ("c2c", "group")}
    menu = merge_menu(menu_before.get("menu"))
    owned = {scope: owned_panel(before[scope], scope) for scope in before}
    menu_changed = not contains_expected(menu_before.get("menu"), menu)
    changed_scopes = [scope for scope, record in owned.items()
                      if record is None or not contains_expected(record.get("panel"), desired_panel(scope))]
    print(json.dumps({"mode": "apply" if apply else "inspect", "menu_update": menu_changed,
                      "panel_updates": changed_scopes, "menu_entries": len(menu["items"]),
                      "c2c_commands": len(desired_panel("c2c")["items"]),
                      "group_commands": len(desired_panel("group")["items"])}, ensure_ascii=False))
    if not apply or (not menu_changed and not changed_scopes):
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix="qq-menu-before-", suffix=".json", dir=backup_dir)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump({"app_id": api.settings.app_id, "sandbox": api.settings.is_sandbox,
                   "menu": menu_before, "panels": before}, output, ensure_ascii=False, indent=2)
    print("Configuration backup:", filename)
    if menu_changed:
        latest_menu = await api.request("GET", "/v2/menu")
        if latest_menu != menu_before:
            raise RuntimeError("Menu changed during inspection; nothing overwritten")
        await api.request("PUT", "/v2/menu", json={"menu": menu})
        verified_menu = await api.request("GET", "/v2/menu")
        if not contains_expected(verified_menu.get("menu"), menu):
            raise RuntimeError("Menu write was not verified; inspect before retrying")
        print("Verified: private-chat menu")
    for scope in changed_scopes:
        latest = await api.panels(scope)
        if latest != before[scope]:
            raise RuntimeError("Panel changed during inspection; not overwritten")
        payload = desired_panel(scope)
        record = owned[scope]
        if record:
            panel_id = record["panel_id"]
            await api.request("PUT", "/v2/panels/" + quote(panel_id, safe=""), json={"panel": payload})
        else:
            created = await api.request("POST", "/v2/panels",
                                        json={"scope": scope, "target_type": "all", "panel": payload})
            panel_id = created.get("panel_id")
        if not isinstance(panel_id, str) or not panel_id:
            raise RuntimeError("Panel write returned no ID; inspect before retrying")
        verified = await api.request("GET", "/v2/panels/" + quote(panel_id, safe=""))
        if verified.get("scope") != scope or verified.get("target_type") != "all" or not contains_expected(verified.get("panel"), payload):
            raise RuntimeError("Panel write was not verified; inspect before retrying")
        print(f"Verified: {scope} command panel ({len(payload['items'])} commands)")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Explicitly write menu and panels after inspection")
    parser.add_argument("--expect-app-id", required=True, help="Require the intended bot AppID to match local settings")
    arguments = parser.parse_args()
    logging.disable(logging.CRITICAL)
    settings = Settings.from_environment()
    if settings.app_id != arguments.expect_app_id:
        raise RuntimeError("Configured bot does not match the intended AppID")
    QQDnsFallback(settings.dns_fallback).install()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as session:
        api = MenuApi(settings, session)
        await api.authenticate()
        await sync(api, apply=arguments.apply, backup_dir=Path("data/qq_menu_backups"))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as error:
        print(f"Menu sync stopped: {type(error).__name__}; private details suppressed. Inspect before retrying.", file=sys.stderr)
        raise SystemExit(1) from None
