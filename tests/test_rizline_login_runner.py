from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

from tools import rizline_login_runner


class RizlineLoginRunnerTests(unittest.TestCase):
    def test_reports_a_fixed_marker_for_the_local_decryptor_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            script = Path(temporary_directory) / "getUser.py"
            script.write_text(
                "raise ModuleNotFoundError('hidden detail', name='gameDataAes2Json')\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            original_argv = sys.argv[:]
            try:
                with contextlib.redirect_stdout(output):
                    result = rizline_login_runner.main([str(script)])
            finally:
                sys.argv = original_argv

            self.assertEqual(result, 1)
            self.assertEqual(output.getvalue(), "RIZLINE_LOCAL_FAILURE_DEPENDENCY_DECRYPTOR\n")
