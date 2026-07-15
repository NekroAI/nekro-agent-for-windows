import subprocess
import threading
import unittest
from unittest.mock import patch

from core.wsl.deploy import WSLDeployMixin
from core.wsl.monitor import WSLMonitorMixin


class _Signal:
    def __init__(self, callback=None):
        self.items = []
        self.callback = callback

    def emit(self, *args):
        self.items.append(args)
        if self.callback:
            self.callback(*args)


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
        self._pending_deploy_info = None

    @staticmethod
    def _creation_flags():
        return 0


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
        self.progress_updated = _Signal()
        self.deploy_info_ready = _Signal()
        self.commands = []
        self.health_invalidations = 0

    def _invalidate_health_checks(self):
        self.health_invalidations += 1

    def _log_reader(self, *args):
        return None

    @staticmethod
    def _format_command_failure(action, **kwargs):
        return action

    @staticmethod
    def _creation_flags():
        return 0

    @staticmethod
    def _get_active_deploy_paths():
        return "/root/nekro_agent", "/root/nekro_agent_data", ""

    def _run_wsl_checked(self, distro, cmd, **kwargs):
        self.commands.append((distro, cmd, kwargs))


class WSLMonitorDeployTests(unittest.TestCase):
    def test_token_save_failure_does_not_mutate_config_reference(self):
        class Config:
            def __init__(self):
                self.deploy_info = {"napcat_token": "old"}
                self.last_save_error = "配置目录只读"

            def get(self, key):
                return self.deploy_info if key == "deploy_info" else None

        class Stdout:
            def __init__(self):
                self.lines = iter([b"WebUi started token=newtoken\n", b""])

            def readline(self):
                return next(self.lines)

        class Proc:
            def __init__(self):
                self.stdout = Stdout()

            @staticmethod
            def poll():
                return 0

        backend = _MonitorDummy()
        backend.config = Config()
        backend._save_deploy_info = lambda *_args, **_kwargs: False

        with patch("core.wsl.monitor.subprocess.Popen", return_value=Proc()):
            backend._log_reader("NekroAgent", "/root/nekro_agent")

        self.assertEqual(backend.config.deploy_info, {"napcat_token": "old"})
        self.assertIn("保存配置失败", backend.log_received.items[-1][0])

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
        self.assertEqual(backend.health_invalidations, 1)

    def test_health_ready_does_not_emit_after_stop_requested(self):
        backend = _MonitorDummy()
        backend.log_received = _Signal(lambda *_args: backend._stop_event.set())

        class _Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with patch("core.wsl.monitor.urlopen", return_value=_Response()):
            backend._health_check(8021)

        self.assertEqual(backend.progress_updated.items, [])
        self.assertEqual(backend.boot_finished.items, [])
        self.assertEqual(backend.status_changed.items, [])

    def test_stop_services_reports_failure_when_compose_precheck_fails(self):
        backend = _DeployDummy()
        backend.is_running = True
        check_failed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=b"wsl unavailable"
        )

        with (
            patch("core.wsl.deploy.threading.Thread", _ImmediateThread),
            patch("core.wsl.deploy.subprocess.run", return_value=check_failed),
        ):
            backend.stop_services()

        self.assertEqual(backend.commands, [])
        self.assertTrue(backend.is_running)
        self.assertEqual(backend.status_changed.items[-1], ("停止失败",))

    def test_start_services_does_not_start_thread_when_instance_save_fails(self):
        class FailingConfig:
            def __init__(self):
                self.last_save_error = "配置目录只读"
                self.calls = []

            @staticmethod
            def get_active_instance_id():
                return ""

            @staticmethod
            def next_instance_id():
                return "default"

            @staticmethod
            def get(key):
                values = {
                    "nekro_port": 8021,
                    "napcat_port": 6099,
                    "release_channel": "stable",
                }
                return values.get(key)

            @staticmethod
            def get_default_instance_id():
                return ""

            def update_instance_with_globals(
                self,
                inst_id,
                instance_updates=None,
                global_updates=None,
            ):
                self.calls.append((inst_id, instance_updates, global_updates))
                return False

        backend = _DeployDummy()
        backend.config = FailingConfig()
        backend._deploying = False
        backend._distro_exists = lambda: True

        with patch("core.wsl.deploy.threading.Thread") as thread_cls:
            started = backend.start_services("lite")

        self.assertFalse(started)
        thread_cls.assert_not_called()
        self.assertEqual(len(backend.config.calls), 1)
        inst_id, instance_updates, global_updates = backend.config.calls[0]
        self.assertEqual(inst_id, "default")
        self.assertEqual(instance_updates["deploy_mode"], "lite")
        self.assertEqual(global_updates["active_instance"], "default")
        self.assertEqual(global_updates["default_instance"], "default")
        self.assertFalse(backend._deploying)
        self.assertEqual(backend.status_changed.items[-1], ("启动失败",))
        self.assertIn(backend.config.last_save_error, backend.log_received.items[-1][0])

    def test_running_service_reports_deploy_info_save_failure(self):
        class FailingConfig:
            last_save_error = "磁盘已满"

            @staticmethod
            def get_active_instance_id():
                return "default"

            @staticmethod
            def get(key):
                if key == "deploy_info":
                    return {"napcat_token": "saved-token"}
                return None

            @staticmethod
            def update_instance_with_globals(
                _inst_id,
                instance_updates=None,
                global_updates=None,
            ):
                return False

        backend = _DeployDummy()
        backend.config = FailingConfig()
        backend._pending_deploy_info = None
        backend._launcher_data_path = lambda name: name
        backend._prepare_compose_content = lambda *_args, **_kwargs: "new-compose"
        backend._prepare_env = (
            lambda *_args, **_kwargs: "NEKRO_EXPOSE_PORT=8021\n"
            "NEKRO_ADMIN_PASSWORD=secret\n"
            "ONEBOT_ACCESS_TOKEN=token\n"
        )
        backend._write_to_wsl = lambda *_args, **_kwargs: None
        backend._clean_command_output = lambda output: str(output or "")
        backend._get_missing_images = lambda *_args, **_kwargs: []
        backend._instance_release_channel = lambda _inst: "stable"

        def _wsl_exec(_distro, command, **_kwargs):
            if command.startswith("test -f"):
                return "yes"
            if command.startswith("cat ") and command.endswith("/.env"):
                return "NEKRO_EXPOSE_PORT=8021\n"
            if command.startswith("cat "):
                return "old-compose"
            return ""

        backend._wsl_exec = _wsl_exec
        backend._run_wsl_checked = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b"",
            stderr=b"",
        )
        inst = {
            "deploy_mode": "lite",
            "deploy_dir": "/root/nekro_agent",
            "data_dir": "/root/nekro_agent_data",
            "nekro_port": 8021,
        }

        with patch("core.wsl.deploy.os.path.exists", return_value=True):
            started = backend._start_instance_sync("default", inst)

        self.assertFalse(started)
        self.assertTrue(backend.is_running)
        self.assertEqual(backend.status_changed.items[-1], ("启动失败",))
        messages = [item[0] for item in backend.log_received.items]
        self.assertTrue(any("服务已启动但配置未保存" in msg for msg in messages))
        self.assertFalse(any("部署完成" in msg for msg in messages))
        self.assertEqual(backend.deploy_info_ready.items, [])

    def test_show_deploy_info_does_not_emit_credentials_when_save_fails(self):
        backend = _DeployDummy()
        backend.config.last_save_error = "配置目录只读"
        backend._save_deploy_info = lambda *_args, **_kwargs: False

        shown = backend._show_deploy_info({"port": "8021"}, inst_id="default")

        self.assertFalse(shown)
        self.assertEqual(backend.deploy_info_ready.items, [])
        self.assertEqual(backend.status_changed.items[-1], ("启动失败",))
        self.assertFalse(
            any("部署完成" in item[0] for item in backend.log_received.items)
        )


if __name__ == "__main__":
    unittest.main()
