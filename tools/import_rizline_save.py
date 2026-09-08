#!/usr/bin/env python3
"""Safely install an already exported, decrypted RizLine save for the bot.

This tool deliberately has no login, password, SMS, or token arguments.  Use
an administrator-controlled local exporter to obtain ``gameData.json`` first,
then import only that JSON into the bot's restricted save directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


MAX_SAVE_BYTES = 10 * 1024 * 1024
PLAYER_ALIAS_RE = re.compile(r"^[\w-]{1,40}$", re.UNICODE)


def validate_save(payload: bytes) -> None:
    """Reject malformed or unexpectedly large files before they reach the bot."""
    if not payload:
        raise ValueError("存档文件为空")
    if len(payload) > MAX_SAVE_BYTES:
        raise ValueError(f"存档文件超过 {MAX_SAVE_BYTES // (1024 * 1024)} MiB 限制")
    try:
        raw: Any = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("存档必须是 UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("存档根节点必须是 JSON 对象")
    data = raw.get("data") if isinstance(raw.get("data"), dict) and "myBest" not in raw else raw
    if not isinstance(data, dict) or not isinstance(data.get("myBest"), list):
        raise ValueError("存档未包含可用的 myBest 成绩数据")


def import_save(source: Path | str, save_dir: Path | str, alias: str, *, replace: bool = False) -> Path:
    """Copy a validated export atomically into the bot directory with mode 600."""
    if not PLAYER_ALIAS_RE.fullmatch(alias):
        raise ValueError("玩家别名只能包含汉字、字母、数字、下划线或连字符，且不超过 40 个字符")

    source_path = Path(source)
    try:
        payload = source_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"无法读取存档文件：{source_path}") from exc
    validate_save(payload)

    destination_dir = Path(save_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / f"{alias}.json"
    if target.exists() and not replace:
        raise ValueError(f"玩家“{alias}”已有本地存档；确认替换请加 --replace")

    temporary = destination_dir / f".{alias}.{os.getpid()}.tmp"
    try:
        temporary.write_bytes(payload)
        os.chmod(temporary, 0o600)
        temporary.replace(target)
        os.chmod(target, 0o600)
    except OSError as exc:
        raise ValueError("写入机器人存档目录失败") from exc
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import an already exported RizLine save into the QQ bot")
    parser.add_argument("source", type=Path, help="已导出的、解密后的 gameData.json")
    parser.add_argument("alias", help="机器人中使用的玩家别名")
    parser.add_argument("--save-dir", type=Path, default=Path("data/rizline_saves"))
    parser.add_argument("--replace", action="store_true", help="明确允许替换同别名的已有存档")
    arguments = parser.parse_args(argv)

    try:
        import_save(arguments.source, arguments.save_dir, arguments.alias, replace=arguments.replace)
    except ValueError as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1
    print(f"已导入玩家“{arguments.alias}”的本地 RizLine 成绩存档。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
