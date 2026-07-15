import subprocess
import unittest
from unittest import mock

from core.wsl.constants import DISTRO_NAME
from core.wsl.discovery import WSLDiscoveryMixin
from core.wsl.shell import WSLShellMixin


class _Signal:
    def __init__(self):
        self.messages = []

    def emit(self, *args):
        self.messages.append(args)


class _StopFailureTakeoverDummy(WSLDiscoveryMixin):
    def __init__(self):
        self.log_received = _Signal()
        self.progress_updated = _Signal()
        self.continued_after_stop = False

    def _distro_exists(self):
        return True

    def _stop_source_instance(self, _distro, _deploy_dir):
        return False

    def _migrate_images(self, _src_distro):
        self.continued_after_stop = True
        return True


class _RestoreDummy(WSLDiscoveryMixin, WSLShellMixin):
    def __init__(self, *, docker_active=True, restore_returncode=0, start_returncode=0):
        self.log_received = _Signal()
        self.commands = []
        self.docker_active = docker_active
        self.restore_returncode = restore_returncode
        self.start_returncode = start_returncode

    def _wsl_run(self, distro, cmd, timeout=60, user=None):
        self.commands.append((distro, cmd, timeout, user))
        if cmd == "systemctl is-active docker":
            return subprocess.CompletedProcess(
                [],
                0 if self.docker_active else 3,
                stdout=b"active\n" if self.docker_active else b"inactive\n",
                stderr=b"",
            )
        if cmd == "systemctl start docker":
            return subprocess.CompletedProcess(
                [],
                self.start_returncode,
                stdout=b"",
                stderr=b"start failed\n" if self.start_returncode else b"",
            )
        if cmd.startswith("tar -xzf "):
            return subprocess.CompletedProcess(
                [],
                self.restore_returncode,
                stdout=b"",
                stderr=b"archive corrupt\n" if self.restore_returncode else b"",
            )
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")


class WSLDiscoveryMigrationTests(unittest.TestCase):
    def test_running_source_stop_failure_aborts_before_copying_data(self):
        backend = _StopFailureTakeoverDummy()
        instance = {
            "distro": "Ubuntu",
            "deploy_dir": "/root/nekro_agent",
            "data_dir": "/root/nekro_agent_data",
            "status": "running",
        }

        self.assertFalse(backend._takeover_foreign(instance))

        self.assertFalse(backend.continued_after_stop)
        self.assertTrue(
            any(
                "已中止迁移" in message and level == "error"
                for message, level in backend.log_received.messages
            )
        )

    def test_stop_source_failure_log_includes_command_context(self):
        backend = _RestoreDummy()
        failed = subprocess.CompletedProcess(
            [],
            17,
            stdout=b"",
            stderr=b"compose stop failed\n",
        )

        with mock.patch("core.wsl.discovery.subprocess.run", return_value=failed):
            self.assertFalse(
                backend._stop_source_instance("Ubuntu-Test", "/root/nekro_agent")
            )

        message, level = backend.log_received.messages[-1]
        self.assertEqual(level, "error")
        self.assertIn("发行版: Ubuntu-Test", message)
        self.assertIn("返回码: 17", message)
        self.assertIn("docker compose", message)
        self.assertIn("compose stop failed", message)

    @mock.patch("core.wsl.discovery.time.sleep")
    def test_restore_failure_restarts_docker_when_it_was_running(self, _sleep):
        backend = _RestoreDummy(docker_active=True, restore_returncode=2)

        self.assertFalse(backend._restore_data("/mnt/wsl/na_migrate.tar.gz"))

        commands = [cmd for _distro, cmd, _timeout, _user in backend.commands]
        self.assertEqual(
            commands,
            [
                "systemctl is-active docker",
                "systemctl stop docker",
                "tar -xzf /mnt/wsl/na_migrate.tar.gz -C /",
                "systemctl start docker",
            ],
        )
        message, level = backend.log_received.messages[-1]
        self.assertEqual(level, "error")
        self.assertIn(f"发行版: {DISTRO_NAME}", message)
        self.assertIn("返回码: 2", message)
        self.assertIn("archive corrupt", message)

    def test_restore_keeps_docker_stopped_when_it_was_inactive(self):
        backend = _RestoreDummy(docker_active=False)

        self.assertTrue(backend._restore_data("/mnt/wsl/na_migrate.tar.gz"))

        commands = [cmd for _distro, cmd, _timeout, _user in backend.commands]
        self.assertEqual(
            commands,
            [
                "systemctl is-active docker",
                "tar -xzf /mnt/wsl/na_migrate.tar.gz -C /",
            ],
        )

    @mock.patch("core.wsl.discovery.time.sleep")
    def test_restore_reports_failure_when_original_docker_state_cannot_be_restored(
        self,
        _sleep,
    ):
        backend = _RestoreDummy(docker_active=True, start_returncode=1)

        self.assertFalse(backend._restore_data("/mnt/wsl/na_migrate.tar.gz"))

        message, level = backend.log_received.messages[-1]
        self.assertEqual(level, "error")
        self.assertIn("恢复目标 Docker 原运行状态失败", message)
        self.assertIn("返回码: 1", message)
        self.assertIn("start failed", message)


if __name__ == "__main__":
    unittest.main()
