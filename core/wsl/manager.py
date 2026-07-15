import os
import sys
import threading

from core.backend_base import BackendBase
from core.launcher_daemon import LauncherDaemonFacade
from core.wsl.deploy import WSLDeployMixin
from core.wsl.discovery import WSLDiscoveryMixin
from core.wsl.environment import WSLEnvironmentMixin
from core.wsl.images import WSLImageMixin
from core.wsl.monitor import WSLMonitorMixin
from core.wsl.runtime import WSLRuntimeMixin
from core.wsl.shell import WSLShellMixin
from core.wsl.update import WSLUpdateMixin


class WSLManager(
    WSLRuntimeMixin,
    WSLImageMixin,
    WSLEnvironmentMixin,
    WSLDeployMixin,
    WSLUpdateMixin,
    WSLMonitorMixin,
    WSLDiscoveryMixin,
    WSLShellMixin,
    BackendBase,
):
    backend_key = "wsl"
    display_name = "WSL"

    def __init__(self, config=None, base_path=None):
        super().__init__(config=config)
        if base_path:
            self.base_path = os.path.abspath(base_path)
        else:
            if getattr(sys, "frozen", False):
                self.base_path = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
            else:
                self.base_path = os.path.dirname(os.path.abspath(__file__))
                if self.base_path.endswith("wsl"):
                    self.base_path = os.path.dirname(os.path.dirname(self.base_path))

        self.is_running = False
        self._deploying = False
        self._log_process = None
        self._stop_event = threading.Event()
        self._health_generation = 0
        self._health_lock = threading.Lock()
        self._pending_deploy_info = None
        self._update_optional_reply = None
        self._deploy_optional_reply = None
        self.launcher_daemon_start_error = ""
        self.launcher_daemon = LauncherDaemonFacade(self)
        try:
            self.launcher_daemon.start()
            self.log_received.emit("Windows 启动器 daemon facade 已启动", "debug")
        except Exception as e:
            self.launcher_daemon_start_error = (
                "Windows 启动器后台服务无法监听本机端口。"
                "可能已有另一个启动器进程正在运行，请关闭重复进程后重试。\n"
                f"错误: {type(e).__name__}: {e}"
            )
            self.log_received.emit(
                f"Windows 启动器 daemon facade 启动失败: {e}",
                "warn",
            )
