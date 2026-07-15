import os
import subprocess
import tempfile
import unittest
from unittest import mock

from core.wsl.constants import DISTRO_NAME
from core.wsl.runtime import WSLRuntimeMixin


class _Signal:
    def __init__(self):
        self.messages = []

    def emit(self, *args):
        self.messages.append(args)


class _Config:
    def __init__(self, fail_once_key=None):
        self.fail_once_key = fail_once_key
        self.failed = False
        self.last_save_error = "simulated save failure"
        self.calls = []

    def set(self, key, value):
        self.calls.append((key, value))
        if key == self.fail_once_key and not self.failed:
            self.failed = True
            return False
        return True


class _RuntimeDummy(WSLRuntimeMixin):
    def __init__(self, *, distro_exists=False, config=None, docker_results=None):
        self.log_received = _Signal()
        self.progress_updated = _Signal()
        self.install_error = _Signal()
        self.config = config
        self.distro_exists = distro_exists
        self.guest_marker = ""
        self.fail_wsl_conf_once = False
        self.fail_guest_marker_once = False
        self.download_calls = 0
        self.docker_results = list(docker_results or [True])
        self.docker_calls = 0

    def _creation_flags(self):
        return 0

    def _distro_exists(self):
        return self.distro_exists

    def _download_rootfs(self, _dest_path):
        self.download_calls += 1
        return True

    def _write_to_wsl(self, _distro, content, wsl_path):
        if wsl_path == self._RUNTIME_GUEST_MARKER:
            if self.fail_guest_marker_once:
                self.fail_guest_marker_once = False
                raise RuntimeError("simulated guest marker failure")
            self.guest_marker = content.strip()
            return
        if wsl_path == "/etc/wsl.conf" and self.fail_wsl_conf_once:
            self.fail_wsl_conf_once = False
            raise RuntimeError("simulated wsl.conf failure")

    def _wsl_exec(self, _distro, cmd, timeout=60, user=None):
        if cmd.startswith("cat "):
            return self.guest_marker
        return ""

    def _wsl_run(self, _distro, cmd, timeout=60, user=None):
        if cmd.startswith("rm -f -- "):
            self.guest_marker = ""
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    def _install_docker_sync(self):
        result = self.docker_results[min(self.docker_calls, len(self.docker_results) - 1)]
        self.docker_calls += 1
        return result

    @staticmethod
    def _format_command_failure(action, **_kwargs):
        return action


def _successful_wsl_run(backend, calls):
    def _run(args, **_kwargs):
        calls.append(args)
        if args[:2] == ["wsl", "--import"]:
            backend.distro_exists = True
        elif args[:2] == ["wsl", "--unregister"]:
            backend.distro_exists = False
            backend.guest_marker = ""
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    return _run


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


class CreateRuntimeRecoveryTests(unittest.TestCase):
    @mock.patch("core.wsl.runtime.time.sleep")
    def test_retry_after_wsl_conf_failure_skips_second_import(self, _sleep):
        backend = _RuntimeDummy()
        backend.fail_wsl_conf_once = True
        subprocess_calls = []

        with tempfile.TemporaryDirectory() as install_dir, mock.patch(
            "core.wsl.runtime.subprocess.run",
            side_effect=_successful_wsl_run(backend, subprocess_calls),
        ):
            self.assertFalse(backend.create_runtime(install_dir))
            self.assertTrue(os.path.exists(backend._runtime_install_marker_path(install_dir)))

            self.assertTrue(backend.create_runtime(install_dir))
            self.assertFalse(os.path.exists(backend._runtime_install_marker_path(install_dir)))

        import_calls = [args for args in subprocess_calls if args[:2] == ["wsl", "--import"]]
        self.assertEqual(len(import_calls), 1)
        self.assertEqual(backend.download_calls, 1)
        self.assertEqual(backend.docker_calls, 1)

    @mock.patch("core.wsl.runtime.time.sleep")
    def test_retry_after_config_save_failure_continues_existing_partial_distro(self, _sleep):
        config = _Config(fail_once_key="wsl_install_dir")
        backend = _RuntimeDummy(config=config)
        subprocess_calls = []

        with tempfile.TemporaryDirectory() as install_dir, mock.patch(
            "core.wsl.runtime.subprocess.run",
            side_effect=_successful_wsl_run(backend, subprocess_calls),
        ):
            self.assertFalse(backend.create_runtime(install_dir))
            self.assertTrue(backend.create_runtime(install_dir))

        import_calls = [args for args in subprocess_calls if args[:2] == ["wsl", "--import"]]
        self.assertEqual(len(import_calls), 1)
        self.assertEqual(backend.download_calls, 1)
        self.assertEqual(backend.docker_calls, 1)
        self.assertGreaterEqual(config.calls.count(("wsl_install_dir", install_dir)), 2)

    @mock.patch("core.wsl.runtime.time.sleep")
    def test_retry_after_docker_failure_reuses_matching_partial_distro(self, _sleep):
        backend = _RuntimeDummy(docker_results=[False, True])
        subprocess_calls = []

        with tempfile.TemporaryDirectory() as install_dir, mock.patch(
            "core.wsl.runtime.subprocess.run",
            side_effect=_successful_wsl_run(backend, subprocess_calls),
        ):
            self.assertFalse(backend.create_runtime(install_dir))
            self.assertTrue(backend.create_runtime(install_dir))

        import_calls = [args for args in subprocess_calls if args[:2] == ["wsl", "--import"]]
        self.assertEqual(len(import_calls), 1)
        self.assertEqual(backend.docker_calls, 2)

    def test_existing_unmarked_distro_is_not_reused_or_reimported(self):
        backend = _RuntimeDummy(distro_exists=True)

        with tempfile.TemporaryDirectory() as install_dir, mock.patch(
            "core.wsl.runtime.subprocess.run"
        ) as run:
            self.assertFalse(backend.create_runtime(install_dir))

        run.assert_not_called()
        self.assertEqual(backend.download_calls, 0)
        self.assertEqual(backend.docker_calls, 0)
        message = backend.install_error.messages[-1][0]
        self.assertIn(DISTRO_NAME, message)
        self.assertIn("不会再次导入或继续配置", message)

    @mock.patch("core.wsl.runtime.time.sleep")
    def test_guest_marker_failure_does_not_unregister_unproven_distro(self, _sleep):
        backend = _RuntimeDummy()
        backend.fail_guest_marker_once = True
        subprocess_calls = []

        with tempfile.TemporaryDirectory() as install_dir, mock.patch(
            "core.wsl.runtime.subprocess.run",
            side_effect=_successful_wsl_run(backend, subprocess_calls),
        ):
            self.assertFalse(backend.create_runtime(install_dir))
            self.assertTrue(backend.distro_exists)

        import_calls = [args for args in subprocess_calls if args[:2] == ["wsl", "--import"]]
        unregister_calls = [
            args for args in subprocess_calls if args[:2] == ["wsl", "--unregister"]
        ]
        self.assertEqual(len(import_calls), 1)
        self.assertEqual(unregister_calls, [])
        self.assertTrue(
            any(
                "不会自动注销" in message[0]
                for message in backend.install_error.messages
            )
        )

    def test_import_false_negative_never_unregisters_existing_unmarked_distro(self):
        backend = _RuntimeDummy()
        import_failure = subprocess.CompletedProcess(
            [],
            1,
            stdout=b"",
            stderr=b"A distribution with the supplied name already exists.\n",
        )

        with tempfile.TemporaryDirectory() as install_dir, mock.patch.object(
            backend,
            "_distro_exists",
            side_effect=[False, True],
        ), mock.patch(
            "core.wsl.runtime.subprocess.run",
            return_value=import_failure,
        ) as run:
            self.assertFalse(backend.create_runtime(install_dir))

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len([args for args in commands if args[:2] == ["wsl", "--import"]]), 1)
        self.assertEqual([args for args in commands if args[:2] == ["wsl", "--unregister"]], [])

    def test_discard_refuses_mismatched_guest_marker(self):
        backend = _RuntimeDummy(distro_exists=True)
        backend.guest_marker = "another-install-token"

        with mock.patch("core.wsl.runtime.subprocess.run") as run:
            self.assertFalse(backend._discard_failed_runtime_import("current-install-token"))

        run.assert_not_called()
        message = backend.install_error.messages[-1][0]
        self.assertIn("恢复标记与本次创建任务不匹配", message)


if __name__ == "__main__":
    unittest.main()
