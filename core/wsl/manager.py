import os
import sys
import threading

from core.backend_base import BackendBase
from core.wsl.deploy import WSLDeployMixin
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
        self._log_process = None
        self._stop_event = threading.Event()
        self._pending_deploy_info = None
        self._update_optional_reply = None
