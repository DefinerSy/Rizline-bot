#!/usr/bin/env python3
"""Create a one-time QQ binding code for an already imported RizLine save."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rizline_bindings import RizlineBindingStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a one-time RizLine QQ binding code")
    parser.add_argument("alias", help="已导入到机器人的玩家别名")
    parser.add_argument("--db", type=Path, default=Path("data/rizline_bindings.json"))
    parser.add_argument("--save-dir", type=Path, default=Path("data/rizline_saves"))
    parser.add_argument("--expires-minutes", type=int, default=15)
    arguments = parser.parse_args(argv)

    if not (arguments.save_dir / f"{arguments.alias}.json").is_file():
        print(f"创建失败：找不到玩家“{arguments.alias}”的本地存档。", file=sys.stderr)
        return 1
    try:
        code = RizlineBindingStore(arguments.db).issue_code(
            arguments.alias,
            expires_seconds=arguments.expires_minutes * 60,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"创建失败：{exc}", file=sys.stderr)
        return 1

    print(f"一次性绑定码（{arguments.expires_minutes} 分钟内有效，仅可使用一次）：\n{code}")
    print("请仅将此码私下发给对应 QQ 用户；对方在 C2C 私聊中发送：/riz bind <绑定码>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
