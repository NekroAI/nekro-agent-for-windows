from abc import abstractmethod

from PyQt6.QtCore import QObject, pyqtSignal


class BackendBase(QObject):
    log_received = pyqtSignal(str, str)
    status_changed = pyqtSignal(str)
    boot_finished = pyqtSignal()
    progress_updated = pyqtSignal(str)
    deploy_info_ready = pyqtSignal(dict)
    install_error = pyqtSignal(str)  # 安装过程中的具体错误信息
    update_check_result = pyqtSignal(bool, str)  # (has_update, message)
    image_status_result = pyqtSignal(list)  # list of {image, name, local, remote, has_update, error}
    image_pull_result = pyqtSignal(str, bool, str)  # (image_ref, success, message)

    backend_key = ""
    display_name = ""

    def __init__(self, config=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.is_running = False

    @abstractmethod
    def check_environment(self):
        raise NotImplementedError

    @abstractmethod
    def get_check_funcs(self):
        """返回 4 个 callable 的列表，每个返回 (passed: bool, detail: str)。
        按顺序对应: 运行时平台、运行环境、Docker、Docker Compose。"""
        raise NotImplementedError

    @abstractmethod
    def get_default_install_dir(self):
        raise NotImplementedError

    def create_distro(self, install_dir):
        return self.create_runtime(install_dir)

    @abstractmethod
    def create_runtime(self, install_dir):
        raise NotImplementedError

    def prepare_runtime(self):
        return True

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
    def stop_services(self):
        raise NotImplementedError

    @abstractmethod
    def update_services(self):
        raise NotImplementedError

    @abstractmethod
    def uninstall_environment(self):
        raise NotImplementedError

    @abstractmethod
    def get_runtime_name(self):
        raise NotImplementedError

    @abstractmethod
    def get_host_access_path(self, guest_path):
        raise NotImplementedError
