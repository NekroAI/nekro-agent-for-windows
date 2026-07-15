import subprocess
import unittest
from unittest import mock

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

    def _get_running_source_services(self, _distro, _deploy_dir):
        return ["nekro_agent", "nekro_postgres"]

    def _stop_source_instance(self, _distro, _deploy_dir):
        return False

    def _migrate_images(self, _src_distro):
        self.continued_after_stop = True
        return True


class _MigrationFlowDummy(WSLDiscoveryMixin):
    def __init__(self, *, running_services=None, pack_ok=True):
        self.log_received = _Signal()
        self.progress_updated = _Signal()
        self.running_services = list(running_services or [])
        self.pack_ok = pack_ok
        self.stop_calls = []
        self.start_calls = []

    def _distro_exists(self):
        return True

    def _get_running_source_services(self, distro, deploy_dir):
        self.probed = (distro, deploy_dir)
        return self.running_services

    def _stop_source_instance(self, distro, deploy_dir):
        self.stop_calls.append((distro, deploy_dir))
        return True

    def _start_source_instance(self, distro, deploy_dir, services):
        self.start_calls.append((distro, deploy_dir, services))
        return True

    def _migrate_images(self, _src_distro):
        return True

    def _pack_source_data(self, *_args):
        return self.pack_ok

    def _pack_via_windows_temp(self, *_args):
        return ""

    def _restore_data(self, _archive_path):
        return True

    def _relocate_dir(self, _src, _dest, timeout=120):
        return True

    def _cleanup_archive(self, _src_distro, _archive_path):
        return None

    def _sync_config_from_env(self, *_args):
        return None


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
    def test_stale_stopped_status_still_stops_actually_running_source(self):
        backend = _MigrationFlowDummy(running_services=["nekro_agent"])
        instance = {
            "distro": "Ubuntu",
            "deploy_dir": "/root/nekro_agent",
            "data_dir": "/root/nekro_agent_data",
            "status": "stopped",
            "env": {},
            "deploy_mode": "lite",
        }

        self.assertTrue(backend._takeover_foreign(instance))

        self.assertEqual(backend.probed, ("Ubuntu", "/root/nekro_agent"))
        self.assertEqual(backend.stop_calls, [("Ubuntu", "/root/nekro_agent")])
        self.assertEqual(backend.start_calls, [])

    def test_failure_after_source_stop_restores_only_previously_running_services(self):
        backend = _MigrationFlowDummy(
            running_services=["nekro_agent", "nekro_postgres"],
            pack_ok=False,
        )
        instance = {
            "distro": "Ubuntu",
            "deploy_dir": "/root/nekro_agent",
            "data_dir": "/root/nekro_agent_data",
            "status": "running",
            "env": {},
            "deploy_mode": "lite",
        }

        self.assertFalse(backend._takeover_foreign(instance))

        self.assertEqual(
            backend.start_calls,
            [
                (
                    "Ubuntu",
                    "/root/nekro_agent",
                    ["nekro_agent", "nekro_postgres"],
                )
            ],
        )

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

    def test_restore_failure_keeps_docker_stopped_when_it_was_running(self):
        backend = _RestoreDummy(docker_active=True, restore_returncode=2)

        self.assertFalse(backend._restore_data("/mnt/wsl/na_migrate.tar.gz"))

        commands = [cmd for _distro, cmd, _timeout, _user in backend.commands]
        self.assertEqual(
            commands,
            [
                "systemctl is-active docker",
                "systemctl stop docker",
                "tar -xzf /mnt/wsl/na_migrate.tar.gz -C /",
            ],
        )
        message, level = backend.log_received.messages[-1]
        self.assertEqual(level, "error")
        self.assertIn("目标 Docker 已保持停止", message)

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
