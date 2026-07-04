import unittest

from core.wsl.runtime import WSLRuntimeMixin


class ParseWSLInstallOutcomeTests(unittest.TestCase):
    def test_exit_zero_without_reboot_marker_is_done(self):
        self.assertEqual(
            WSLRuntimeMixin._parse_wsl_install_outcome("0", "正在安装: 适用于 Linux 的 Windows 子系统\n操作成功完成。"),
            "done",
        )

    def test_exit_zero_with_chinese_reboot_marker_requires_reboot(self):
        self.assertEqual(
            WSLRuntimeMixin._parse_wsl_install_outcome("0", "请求的操作成功。直到重新启动系统前更改将不会生效。"),
            "reboot",
        )

    def test_nonzero_with_english_reboot_marker_requires_reboot(self):
        self.assertEqual(
            WSLRuntimeMixin._parse_wsl_install_outcome(
                "1", "No action was taken as a system reboot is required."
            ),
            "reboot",
        )
        self.assertEqual(
            WSLRuntimeMixin._parse_wsl_install_outcome(
                "4294967295", "Please restart your computer to complete installation."
            ),
            "reboot",
        )

    def test_denied_is_fail_even_with_marker(self):
        self.assertEqual(
            WSLRuntimeMixin._parse_wsl_install_outcome("DENIED", "please reboot"),
            "fail",
        )

    def test_nonzero_without_marker_is_fail(self):
        self.assertEqual(
            WSLRuntimeMixin._parse_wsl_install_outcome("4294967295", "未知错误"),
            "fail",
        )

    def test_missing_exit_token_is_fail(self):
        self.assertEqual(WSLRuntimeMixin._parse_wsl_install_outcome("", ""), "fail")


if __name__ == "__main__":
    unittest.main()
