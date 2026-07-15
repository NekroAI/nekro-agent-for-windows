import hashlib
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core import app_updater
from ui import update_dialog


class AppUpdaterTests(unittest.TestCase):
    def test_check_update_returns_available_installer(self):
        release = {
            "tag_name": "v9.9.9",
            "name": "Release v9.9.9",
            "body": "Changes",
            "published_at": "2026-06-08T00:00:00Z",
            "assets": [
                {"name": "source.zip", "browser_download_url": "https://example.test/source.zip"},
                {
                    "name": "NekroAgent-Setup-v9.9.9.exe",
                    "browser_download_url": (
                        "https://github.com/NekroAI/nekro-agent-for-windows/"
                        "releases/download/v9.9.9/NekroAgent-Setup.exe"
                    ),
                    "size": 123,
                    "digest": "sha256:" + "a" * 64,
                },
            ],
        }

        with patch.object(app_updater, "_try_github_api", return_value=(release, [])):
            result = app_updater.check_update()

        self.assertEqual(result.status, "available")
        self.assertIsNotNone(result.update_info)
        assert result.update_info is not None
        self.assertEqual(result.update_info["tag"], "v9.9.9")
        self.assertEqual(result.update_info["file_name"], "NekroAgent-Setup-v9.9.9.exe")
        self.assertEqual(result.update_info["file_sha256"], "a" * 64)

    def test_check_update_ignores_installer_sidecar_files(self):
        release = {
            "tag_name": "v9.9.9",
            "assets": [
                {
                    "name": "NekroAgent-Setup-v9.9.9.exe.sha256",
                    "browser_download_url": (
                        "https://github.com/NekroAI/nekro-agent-for-windows/"
                        "releases/download/v9.9.9/NekroAgent-Setup.exe.sha256"
                    ),
                    "size": 64,
                },
                {
                    "name": "NekroAgent-Setup-v9.9.9.exe",
                    "browser_download_url": (
                        "https://github.com/NekroAI/nekro-agent-for-windows/"
                        "releases/download/v9.9.9/NekroAgent-Setup.exe"
                    ),
                    "size": 123,
                },
            ],
        }

        with patch.object(app_updater, "_try_github_api", return_value=(release, [])):
            result = app_updater.check_update()

        self.assertEqual(result.status, "available")
        assert result.update_info is not None
        self.assertEqual(result.update_info["file_name"], "NekroAgent-Setup-v9.9.9.exe")

    def test_check_update_reports_latest_separately_from_failure(self):
        release = {"tag_name": app_updater.APP_VERSION, "assets": []}

        with patch.object(app_updater, "_try_github_api", return_value=(release, [])):
            result = app_updater.check_update()

        self.assertEqual(result.status, "latest")
        self.assertEqual(result.message, "")

    def test_check_update_reports_network_failure(self):
        failures = ["api.github.com: ConnectTimeout: timed out"]

        with patch.object(app_updater, "_try_github_api", return_value=(None, failures)):
            result = app_updater.check_update()

        self.assertEqual(result.status, "failed")
        self.assertIn("启动器更新检查失败", result.message)
        self.assertIn("ConnectTimeout", result.detail)

    def test_check_update_reports_missing_installer_asset(self):
        release = {
            "tag_name": "v9.9.9",
            "assets": [
                {"name": "NekroAgent-v9.9.9.zip", "browser_download_url": "https://example.test/a.zip"}
            ],
        }

        with patch.object(app_updater, "_try_github_api", return_value=(release, [])):
            result = app_updater.check_update()

        self.assertEqual(result.status, "failed")
        self.assertIn("没有找到 Windows 安装包", result.message)

    def test_check_update_rejects_non_github_installer_url(self):
        release = {
            "tag_name": "v9.9.9",
            "assets": [
                {
                    "name": "NekroAgent-Setup-v9.9.9.exe",
                    "browser_download_url": "https://example.test/setup.exe",
                    "size": 123,
                }
            ],
        }

        with patch.object(app_updater, "_try_github_api", return_value=(release, [])):
            result = app_updater.check_update()

        self.assertEqual(result.status, "failed")
        self.assertIn("地址不可信", result.message)

    def test_format_download_failure_names_launcher_update_package(self):
        message = app_updater.format_download_failure(
            [
                "https://ghfast.top/https://github.com/NekroAI/file.exe",
                "https://github.com/NekroAI/file.exe",
            ],
            ["ghfast.top: HTTP 502", "github.com: ConnectTimeout: timed out"],
        )

        self.assertIn("启动器更新安装包下载失败", message)
        self.assertIn("已尝试下载源：ghfast.top、github.com", message)
        self.assertIn("HTTP 502", message)

    def test_download_rejects_size_mismatch(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.headers = {"content-length": "4"}
        response.iter_content.return_value = [b"MZxx"]

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app_updater,
            "_accelerated_download_url",
            return_value=[
                "https://github.com/NekroAI/nekro-agent-for-windows/"
                "releases/download/v9.9.9/a.exe"
            ],
        ), patch("core.config_manager.get_app_data_dir", return_value=temp_dir), patch.object(
            app_updater.requests, "get", return_value=response
        ):
            worker = app_updater.DownloadWorker(
                "https://github.com/a.exe", "a.exe", expected_size=5
            )
            results = []
            worker.finished.connect(lambda success, value: results.append((success, value)))
            worker.run()

        self.assertFalse(results[0][0])
        self.assertIn("Release 记录", results[0][1])

    def test_download_rejects_html_with_expected_size(self):
        payload = b"<html>bad</html>"
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.headers = {"content-length": str(len(payload))}
        response.iter_content.return_value = [payload]

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app_updater,
            "_accelerated_download_url",
            return_value=[
                "https://github.com/NekroAI/nekro-agent-for-windows/"
                "releases/download/v9.9.9/a.exe"
            ],
        ), patch("core.config_manager.get_app_data_dir", return_value=temp_dir), patch.object(
            app_updater.requests, "get", return_value=response
        ):
            worker = app_updater.DownloadWorker(
                "https://github.com/a.exe", "a.exe", expected_size=len(payload)
            )
            results = []
            worker.finished.connect(lambda success, value: results.append((success, value)))
            worker.run()

        self.assertFalse(results[0][0])
        self.assertIn("Windows PE", results[0][1])

    def test_mirror_requires_and_accepts_official_sha256(self):
        payload = b"MZinstaller"
        expected_sha256 = hashlib.sha256(payload).hexdigest()

        def response():
            value = MagicMock()
            value.__enter__.return_value = value
            value.status_code = 200
            value.headers = {"content-length": str(len(payload))}
            value.iter_content.return_value = [payload]
            return value

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app_updater,
            "_accelerated_download_url",
            return_value=[
                "https://mirror.test/a.exe",
                "https://github.com/NekroAI/nekro-agent-for-windows/"
                "releases/download/v9.9.9/a.exe",
            ],
        ), patch("core.config_manager.get_app_data_dir", return_value=temp_dir), patch.object(
            app_updater.requests, "get", side_effect=[response(), response()]
        ), patch.object(
            app_updater, "_authenticode_status", return_value=("unsigned", "NotSigned")
        ):
            worker = app_updater.DownloadWorker(
                "https://github.com/NekroAI/nekro-agent-for-windows/"
                "releases/download/v9.9.9/a.exe",
                "a.exe",
                expected_size=len(payload),
                expected_sha256=expected_sha256,
            )
            results = []
            worker.finished.connect(lambda success, value: results.append((success, value)))
            worker.run()

            self.assertTrue(results[0][0])
            self.assertTrue(os.path.isfile(results[0][1]))

    def test_unsigned_download_without_digest_uses_only_official_source(self):
        official_url = (
            "https://github.com/NekroAI/nekro-agent-for-windows/"
            "releases/download/v9.9.9/a.exe"
        )
        worker = app_updater.DownloadWorker(
            official_url,
            "a.exe",
            expected_size=10,
        )

        with patch.object(
            app_updater,
            "_accelerated_download_url",
            wraps=app_updater._accelerated_download_url,
        ) as accelerated, patch.object(
            app_updater.requests,
            "get",
            side_effect=app_updater.requests.RequestException("stop"),
        ):
            worker.run()

        accelerated.assert_not_called()

    def test_app_shutdown_terminates_stuck_download_thread(self):
        worker = MagicMock()
        thread = MagicMock()
        thread.isRunning.return_value = True
        thread.wait.return_value = False

        with patch.object(update_dialog, "_DETACHED_DOWNLOADS", [(worker, thread)]):
            update_dialog._shutdown_downloads(wait_ms=1)

        worker.cancel.assert_called_once_with()
        thread.requestInterruption.assert_called_once_with()
        thread.quit.assert_called_once_with()
        thread.terminate.assert_called_once_with()
        self.assertEqual(thread.wait.call_count, 2)


if __name__ == "__main__":
    unittest.main()
