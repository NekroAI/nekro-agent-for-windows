import subprocess

from core.wsl.constants import DISTRO_NAME


class WSLEnvironmentMixin:
    def get_check_funcs(self):
        """返回 4 个检测步骤的 callable 列表，每个返回 (passed, detail)"""
        ctx = {}

        def check_wsl():
            self.log_received.emit("[环境检测] 1/4 检测 WSL2...", "info")
            try:
                proc = subprocess.run(
                    ["wsl", "--status"],
                    capture_output=True,
                    timeout=10,
                    creationflags=self._creation_flags(),
                )
                ok = proc.returncode == 0
                ctx["wsl"] = ok
                if ok:
                    self.log_received.emit("[环境检测] ✓ WSL2 已安装", "info")
                else:
                    self.log_received.emit("[环境检测] ✗ WSL2 未安装，返回码: " + str(proc.returncode), "error")
                return (ok, "")
            except FileNotFoundError:
                self.log_received.emit("[环境检测] ✗ wsl 命令未找到", "error")
                ctx["wsl"] = False
                return (False, "")
            except Exception as e:
                self.log_received.emit(f"[环境检测] ✗ WSL 检测异常: {e}", "error")
                ctx["wsl"] = False
                return (False, "")

        def check_distro():
            self.log_received.emit("[环境检测] 2/4 检测 NekroAgent 发行版...", "info")
            if not ctx.get("wsl"):
                return (False, "未创建")
            if self._distro_exists():
                ctx["distro"] = True
                self.log_received.emit(f"[环境检测] ✓ {DISTRO_NAME} 发行版已存在", "info")
                return (True, DISTRO_NAME)
            self.log_received.emit("[环境检测] ✗ NekroAgent 发行版不存在", "error")
            return (False, "未创建")

        def check_docker():
            self.log_received.emit("[环境检测] 3/4 检测 Docker...", "info")
            if not ctx.get("distro"):
                return (False, "")
            try:
                proc = subprocess.run(
                    ["wsl", "-d", DISTRO_NAME, "--", "bash", "-c", "docker --version"],
                    capture_output=True,
                    timeout=10,
                    creationflags=self._creation_flags(),
                )
                ok = proc.returncode == 0
                ctx["docker"] = ok
                if ok:
                    self.log_received.emit("[环境检测] ✓ Docker CLI 已安装", "info")
                else:
                    self.log_received.emit("[环境检测] ✗ Docker CLI 检测失败", "error")
                    self.log_received.emit(f"返回码: {proc.returncode}", "error")
                    self.log_received.emit(f"STDERR: {self._clean_stderr(proc.stderr, 300)}", "error")
                return (ok, "")
            except Exception as e:
                self.log_received.emit(f"[环境检测] ✗ Docker 检测异常: {e}", "error")
                return (False, "")

        def check_compose():
            self.log_received.emit("[环境检测] 4/4 检测 Docker Compose...", "info")
            if not ctx.get("docker"):
                return (False, "")
            try:
                proc = subprocess.run(
                    ["wsl", "-d", DISTRO_NAME, "--", "bash", "-c", "docker compose version"],
                    capture_output=True,
                    timeout=10,
                    creationflags=self._creation_flags(),
                )
                ok = proc.returncode == 0
                if ok:
                    self.log_received.emit("[环境检测] ✓ Docker Compose 可用", "info")
                else:
                    self.log_received.emit("[环境检测] ✗ Docker Compose 检测失败", "error")
                return (ok, "")
            except Exception as e:
                self.log_received.emit(f"[环境检测] ✗ Docker Compose 检测异常: {e}", "error")
                return (False, "")

        return [check_wsl, check_distro, check_docker, check_compose]
