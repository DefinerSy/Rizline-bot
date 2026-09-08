from __future__ import annotations

import sys
import os
import tempfile
import time
import unittest
from pathlib import Path

from tools.local_rizline_login import LocalLoginManager


class LocalRizlineLoginTests(unittest.TestCase):
    @staticmethod
    def _wait_for_result(session) -> None:
        deadline = time.monotonic() + 5
        while session.state not in {"complete", "failed"} and time.monotonic() < deadline:
            time.sleep(0.02)

    def test_imports_only_the_export_and_issues_a_binding_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            tool_dir = root / "tool"
            tool_dir.mkdir()
            (tool_dir / "sibling.py").write_text("NAME = 'Alice'\n", encoding="utf-8")
            (tool_dir / "getUser.py").write_text(
                "from pathlib import Path\n"
                "from sibling import NAME\n"
                "input('phone:')\n"
                "input('password:')\n"
                "Path('gameData.json').write_text('{\\\"username\\\":\\\"' + NAME + '\\\",\\\"myBest\\\":[],\\\"levelsRks\\\":[]}')\n",
                encoding="utf-8",
            )
            relative_tool_dir = Path(os.path.relpath(tool_dir, Path.cwd()))
            manager = LocalLoginManager(
                tool_dir=relative_tool_dir,
                tool_python=sys.executable,
                save_dir=root / "saves",
                binding_db=root / "bindings.json",
            )
            _session_id, session = manager.create_session()
            manager.start(session, phone="13800138000", password="local-only", alias="alice", replace=False)

            self._wait_for_result(session)

            self.assertEqual(session.state, "complete")
            self.assertTrue((root / "saves" / "alice.json").is_file())
            self.assertIsNotNone(session.binding_code)
            assert session.binding_code is not None
            self.assertEqual(manager._bindings.redeem(session.binding_code, "qq-openid").alias, "alice")
            manager.stop()

    def test_preserves_a_virtual_environment_interpreter_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            tool_dir = root / "tool"
            tool_dir.mkdir()
            venv_python = root / "venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.symlink_to(sys.executable)
            manager = LocalLoginManager(
                tool_dir=tool_dir,
                tool_python=venv_python,
                save_dir=root / "saves",
                binding_db=root / "bindings.json",
            )

            self.assertEqual(manager._tool_python, os.path.abspath(venv_python))

    def test_imports_a_fresh_export_even_if_optional_upstream_work_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            tool_dir = root / "tool"
            tool_dir.mkdir()
            (tool_dir / "getUser.py").write_text(
                "from pathlib import Path\n"
                "input('phone:')\n"
                "input('password:')\n"
                "Path('gameData.json').write_text('{\\\"myBest\\\":[],\\\"levelsRks\\\":[]}')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            manager = LocalLoginManager(
                tool_dir=tool_dir,
                tool_python=sys.executable,
                save_dir=root / "saves",
                binding_db=root / "bindings.json",
            )
            _session_id, session = manager.create_session()
            manager.start(session, phone="13800138000", password="local-only", alias="alice", replace=False)

            self._wait_for_result(session)

            self.assertEqual(session.state, "complete")
            self.assertTrue((root / "saves" / "alice.json").is_file())
            manager.stop()

    def test_rejects_a_stale_export_when_the_login_tool_makes_no_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            tool_dir = root / "tool"
            tool_dir.mkdir()
            (tool_dir / "gameData.json").write_text('{"myBest":[],"levelsRks":[]}', encoding="utf-8")
            (tool_dir / "getUser.py").write_text(
                "input('phone:')\ninput('password:')\n",
                encoding="utf-8",
            )
            manager = LocalLoginManager(
                tool_dir=tool_dir,
                tool_python=sys.executable,
                save_dir=root / "saves",
                binding_db=root / "bindings.json",
            )
            _session_id, session = manager.create_session()
            manager.start(session, phone="13800138000", password="local-only", alias="alice", replace=False)

            self._wait_for_result(session)

            self.assertEqual(session.state, "failed")
            self.assertIn("没有生成新存档", session.message)
            self.assertFalse((root / "saves" / "alice.json").exists())
            manager.stop()

    def test_reports_a_safe_category_when_the_login_input_flow_exits_early(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            tool_dir = root / "tool"
            tool_dir.mkdir()
            (tool_dir / "getUser.py").write_text(
                "input('phone:')\ninput('password:')\nraise EOFError('sensitive upstream detail')\n",
                encoding="utf-8",
            )
            manager = LocalLoginManager(
                tool_dir=tool_dir,
                tool_python=sys.executable,
                save_dir=root / "saves",
                binding_db=root / "bindings.json",
            )
            _session_id, session = manager.create_session()
            manager.start(session, phone="13800138000", password="local-only", alias="alice", replace=False)

            self._wait_for_result(session)

            self.assertEqual(session.state, "failed")
            self.assertEqual(session.runner_failure, "INPUT")
            self.assertIn("登录输入流程", session.message)
            manager.stop()
