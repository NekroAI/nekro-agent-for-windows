from abc import abstractmethod
import threading

from PyQt6.QtCore import QObject, pyqtSignal


class BackendBase(QObject):
    log_received = pyqtSignal(str, str)
    status_changed = pyqtSignal(str)
    boot_finished = pyqtSignal()
    progress_updated = pyqtSignal(str)
    deploy_info_ready = pyqtSignal(dict)
    napcat_network_config_finished = pyqtSignal(dict)
    install_error = pyqtSignal(str)  # 安装过程中的具体错误信息
    image_status_result = pyqtSignal(list)  # list of {image, name, local, remote, has_update, error}
    image_pull_result = pyqtSignal(str, bool, str)  # (image_ref, success, message)
    update_optional_confirm = pyqtSignal(str, str)  # (step_label, prompt_with_size)
    deploy_optional_confirm = pyqtSignal(str, str)  # (step_label, prompt)
    update_finished = pyqtSignal(bool, str)  # (success, message)
    instance_removed = pyqtSignal(bool, str, bool)  # (success, inst_id, was_active)

    backend_key = ""
    display_name = ""

    def __init__(self, config=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.is_running = False
        self._exclusive_op_lock = threading.Lock()
        self._exclusive_op_name = ""

    def acquire_exclusive_operation(self, name):
        """尝试占用互斥操作槽（更新、切换预览、还原、daemon 任务等）。

        成功返回 True；已有互斥操作在执行时返回 False，调用方应放弃本次操作。
        """
        if self._exclusive_op_lock.acquire(blocking=False):
            self._exclusive_op_name = name
            return True
        return False

    def release_exclusive_operation(self):
        """释放互斥操作槽；未持有时静默忽略。"""
        self._exclusive_op_name = ""
        try:
            self._exclusive_op_lock.release()
        except RuntimeError:
            pass

    def exclusive_operation_name(self):
        """返回当前互斥操作名；空字符串表示空闲。"""
        return self._exclusive_op_name if self._exclusive_op_lock.locked() else ""

    @abstractmethod
    def get_check_funcs(self):
        """返回 4 个 callable 的列表，每个返回 (passed: bool, detail: str)。
        按顺序对应: 运行时平台、运行环境、Docker、Docker Compose。"""
        raise NotImplementedError

    @abstractmethod
    def get_default_install_dir(self):
        raise NotImplementedError

    @abstractmethod
    def create_runtime(self, install_dir):
        raise NotImplementedError

    @abstractmethod
    def install_wsl(self):
        raise NotImplementedError

    @abstractmethod
    def install_docker(self):
        raise NotImplementedError

    @abstractmethod
    def start_services(self, deploy_mode):
        raise NotImplementedError

    @abstractmethod
    def start_all_services(self, default_instance_id=None):
        raise NotImplementedError

    @abstractmethod
    def stop_services(self):
        raise NotImplementedError

    @abstractmethod
    def stop_all_services(self):
        raise NotImplementedError

    @abstractmethod
    def uninstall_environment(self):
        raise NotImplementedError

    @abstractmethod
    def get_host_access_path(self, guest_path):
        raise NotImplementedError
