import unittest
from unittest.mock import patch

from core import app_updater


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
                    "browser_download_url": "https://example.test/NekroAgent-Setup.exe",
                    "size": 123,
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


if __name__ == "__main__":
    unittest.main()
