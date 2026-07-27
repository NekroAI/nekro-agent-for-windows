import os
import sys
import tempfile
import unittest
from unittest import mock

import main


class _Backend:
    display_name = "WSL"
    backend_key = "wsl"

    @staticmethod
    def get_check_funcs():
        return [
            lambda: (True, "ok"),
            lambda: (False, "missing"),
        ]


class BackendCheckReportTests(unittest.TestCase):
    def _run(self, output_path):
        with mock.patch("core.config_manager.ConfigManager"), mock.patch(
            "core.backend_factory.BackendFactory.create", return_value=_Backend()
        ):
            return main._run_backend_check(output_path)

    def test_writes_report_file_and_returns_failure_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "report.txt")
            self.assertEqual(self._run(path), 1)
            with open(path, encoding="utf-8") as report_file:
                report = report_file.read()

        self.assertIn("[PASS] WSL", report)
        self.assertIn("[FAIL] Nekro Agent runtime: missing", report)
        self.assertIn("Result: FAIL", report)

    def test_report_survives_windowed_build_without_stdout(self):
        """打包后的 windowed EXE 里 sys.stdout 为 None，print 会被静默丢弃。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "report.txt")
            original_stdout = sys.stdout
            sys.stdout = None
            try:
                code = self._run(path)
            finally:
                sys.stdout = original_stdout
            with open(path, encoding="utf-8") as report_file:
                report = report_file.read()

        self.assertEqual(code, 1)
        self.assertIn("Result: FAIL", report)

    def test_unwritable_output_path_does_not_break_exit_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # 目录路径不可当作文件写入，写报告失败不应改变检查结论
            self.assertEqual(self._run(temp_dir), 1)

    def test_all_checks_passing_returns_zero(self):
        backend = _Backend()
        backend.get_check_funcs = lambda: [lambda: (True, "")]

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "core.config_manager.ConfigManager"
        ), mock.patch(
            "core.backend_factory.BackendFactory.create", return_value=backend
        ):
            path = os.path.join(temp_dir, "report.txt")
            self.assertEqual(main._run_backend_check(path), 0)
            with open(path, encoding="utf-8") as report_file:
                self.assertIn("Result: PASS", report_file.read())


if __name__ == "__main__":
    unittest.main()
