import subprocess
import threading
import unittest
from unittest.mock import patch

from core.wsl.deploy import WSLDeployMixin
from core.wsl.monitor import WSLMonitorMixin


class _Signal:
    def __init__(self):
        self.items = []

    def emit(self, *args):
        self.items.append(args)


class _Config:
    @staticmethod
    def get_active_instance_id():
        return "default"


class _MonitorDummy(WSLMonitorMixin):
    def __init__(self):
        self._stop_event = threading.Event()
        self.is_running = True
        self.config = None
        self.log_received = _Signal()
        self.progress_updated = _Signal()
        self.boot_finished = _Signal()
        self.status_changed = _Signal()


class _ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class _DeployDummy(WSLDeployMixin):
    def __init__(self):
        self._stop_event = threading.Event()
        self._log_process = None
        self.is_running = False
        self.config = _Config()
        self.log_received = _Signal()
        self.status_changed = _Signal()
        self.commands = []

    @staticmethod
    def _creation_flags():
        return 0

    @staticmethod
    def _get_active_deploy_paths():
        return "/root/nekro_agent", "/root/nekro_agent_data", ""

    def _run_wsl_checked(self, distro, cmd, **kwargs):
        self.commands.append((distro, cmd, kwargs))


class WSLMonitorDeployTests(unittest.TestCase):
    def test_health_timeout_keeps_compose_running_state(self):
        backend = _MonitorDummy()

        with patch("core.wsl.monitor.time.time", side_effect=[0, 301]):
            backend._health_check(8021)

        self.assertTrue(backend.is_running)
        self.assertEqual(backend.status_changed.items[-1], ("启动超时",))
        self.assertIn("Compose 服务可能仍在运行", backend.log_received.items[-1][0])

    def test_stop_services_runs_compose_stop_when_cached_state_is_false(self):
        backend = _DeployDummy()
        compose_exists = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"yes\n", stderr=b""
        )

        with (
            patch("core.wsl.deploy.threading.Thread", _ImmediateThread),
            patch("core.wsl.deploy.subprocess.run", return_value=compose_exists),
        ):
            backend.stop_services()

        self.assertEqual(len(backend.commands), 1)
        self.assertEqual(
            backend.commands[0][1],
            "docker compose -f docker-compose.yml stop",
        )
        self.assertFalse(backend.is_running)
        self.assertEqual(backend.status_changed.items[-1], ("已停止",))


if __name__ == "__main__":
    unittest.main()
