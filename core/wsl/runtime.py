import os
import subprocess
import threading
import time
from urllib.request import Request, urlopen

from core.wsl.constants import DISTRO_NAME, ROOTFS_URLS


class WSLRuntimeMixin:
    def get_host_access_path(self, guest_path):
        normalized = guest_path or "/"
        return f"\\\\wsl$\\{DISTRO_NAME}{normalized}"

    def get_default_install_dir(self):
        """返回默认的 WSL 安装目录（优先非 C 盘）"""
        for drive in "DEFGH":
            if os.path.exists(f"{drive}:"):
                return f"{drive}:\\NekroAgent\\wsl"
        return os.path.join(os.path.expanduser("~"), "NekroAgent", "wsl")

    def create_runtime(self, install_dir):
        return self._create_distro(install_dir)

    def _create_distro(self, install_dir):
        """下载 Ubuntu rootfs 并用 wsl --import 创建专用发行版（同步，在线程中调用）"""
        self.progress_updated.emit("准备创建 NekroAgent 运行环境...")
        self.log_received.emit("[发行版创建] 开始创建 NekroAgent 专用发行版...", "info")

        self.log_received.emit(f"[发行版创建] 1/4 创建安装目录: {install_dir}", "info")
        try:
            os.makedirs(install_dir, exist_ok=True)
            self.log_received.emit("[发行版创建] ✓ 安装目录已创建", "info")
        except Exception as e:
            self.log_received.emit(f"[发行版创建] ✗ 创建目录失败: {e}", "error")
            return False

        self.log_received.emit("[发行版创建] 2/4 下载 Ubuntu rootfs...", "info")
        rootfs_path = os.path.join(install_dir, "rootfs.tar.gz")
        if not self._download_rootfs(rootfs_path):
            self.log_received.emit("[发行版创建] ✗ rootfs 下载失败", "error")
            return False
        self.log_received.emit("[发行版创建] ✓ rootfs 下载完成", "info")

        self.progress_updated.emit("正在导入 WSL 发行版...")
        self.log_received.emit("[发行版创建] 3/4 导入 WSL 发行版...", "info")
        try:
            proc = subprocess.run(
                ["wsl", "--import", DISTRO_NAME, install_dir, rootfs_path],
                capture_output=True,
                timeout=300,
                creationflags=self._creation_flags(),
            )
            if proc.returncode != 0:
                stderr_text = self._clean_stderr(proc.stderr, 300)
                self.progress_updated.emit("导入失败")
                self.log_received.emit("[发行版创建] ✗ WSL 导入失败", "error")
                self.log_received.emit(f"返回码: {proc.returncode}", "error")
                self.log_received.emit(f"STDERR: {stderr_text}", "error")
                self.install_error.emit(f"WSL 导入失败（返回码 {proc.returncode}）：{stderr_text}")
                return False
            self.log_received.emit(f"[发行版创建] ✓ {DISTRO_NAME} 发行版导入完成", "info")
        except subprocess.TimeoutExpired:
            self.progress_updated.emit("导入超时")
            self.log_received.emit("[发行版创建] ✗ 导入超时", "error")
            return False
        except Exception as e:
            self.progress_updated.emit(f"导入异常: {e}")
            self.log_received.emit(f"[发行版创建] ✗ 导入异常: {e}", "error")
            return False

        try:
            os.remove(rootfs_path)
            self.log_received.emit("[发行版创建] ✓ 临时 rootfs 文件已清理", "info")
        except Exception:
            pass

        self.progress_updated.emit("正在配置 WSL 环境...")
        self.log_received.emit("[发行版创建] 4/4 配置 WSL 环境（隔离 Windows PATH）...", "info")
        try:
            wsl_conf_content = """[boot]
systemd = true

[interop]
appendWindowsPath = false

[user]
default = root
"""
            self._write_to_wsl(DISTRO_NAME, wsl_conf_content, "/etc/wsl.conf")
            self.log_received.emit("[发行版创建] ✓ WSL 配置完成", "info")

            self.log_received.emit("[发行版创建] 重启 WSL 发行版以启用 systemd...", "info")
            subprocess.run(
                ["wsl", "--terminate", DISTRO_NAME],
                capture_output=True,
                timeout=30,
                creationflags=self._creation_flags(),
            )
            time.sleep(2)
            self.log_received.emit("[发行版创建] ✓ WSL 发行版已重启", "info")
        except Exception as e:
            self.progress_updated.emit(f"配置 WSL 失败: {e}")
            self.log_received.emit(f"[发行版创建] ✗ 配置 WSL 失败: {e}", "error")
            return False

        if self.config:
            self.config.set("wsl_distro", DISTRO_NAME)
            self.config.set("wsl_install_dir", install_dir)
            self.log_received.emit("[发行版创建] ✓ 配置已保存", "info")

        self.progress_updated.emit("发行版创建成功！正在安装 Docker...")
        self.log_received.emit("[发行版创建] ✓ 发行版创建完成！开始安装 Docker...", "info")

        return self._install_docker_sync()

    def _download_rootfs(self, dest_path):
        """下载 Ubuntu rootfs，返回是否成功"""
        from urllib.error import HTTPError, URLError
        import socket

        last_error = ""
        for url in ROOTFS_URLS:
            try:
                self.progress_updated.emit("正在下载 Ubuntu rootfs...")
                req = Request(url, headers={"User-Agent": "NekroAgent/1.0"})
                resp = urlopen(req, timeout=60)

                total = resp.headers.get("Content-Length")
                total = int(total) if total else None
                downloaded = 0
                chunk_size = 256 * 1024

                with open(dest_path, "wb") as f:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = int(downloaded * 100 / total)
                            mb_done = downloaded / (1024 * 1024)
                            mb_total = total / (1024 * 1024)
                            self.progress_updated.emit(f"下载中... {mb_done:.1f} / {mb_total:.1f} MB ({pct}%)")
                        else:
                            mb_done = downloaded / (1024 * 1024)
                            self.progress_updated.emit(f"下载中... {mb_done:.1f} MB")

                self.progress_updated.emit("下载完成")
                return True
            except HTTPError as e:
                last_error = f"HTTP {e.code} {e.reason}（{url}）"
                self.install_error.emit(f"下载源返回错误: {last_error}，尝试下一个源...")
            except URLError as e:
                reason = str(e.reason)
                if isinstance(e.reason, socket.timeout) or "timed out" in reason.lower():
                    last_error = f"连接超时（{url}）"
                elif "Name or service not known" in reason or "getaddrinfo" in reason:
                    last_error = f"DNS 解析失败，请检查网络连接（{url}）"
                elif "Connection refused" in reason:
                    last_error = f"连接被拒绝（{url}）"
                else:
                    last_error = f"网络错误: {reason}（{url}）"
                self.install_error.emit(f"{last_error}，尝试下一个源...")
            except OSError as e:
                last_error = f"磁盘写入失败: {e}"
                self.install_error.emit(last_error)
                return False
            except Exception as e:
                last_error = str(e)
                self.install_error.emit(f"下载异常: {last_error}，尝试下一个源...")

        self.install_error.emit(f"所有下载源均失败，最后错误: {last_error}")
        self.progress_updated.emit("所有下载源均失败")
        return False

    def _install_docker_sync(self):
        """在专用发行版内同步安装 Docker（通过 Docker 官方源，使用国内镜像）"""
        distro = DISTRO_NAME
        self.progress_updated.emit("正在安装 Docker...")
        self.log_received.emit("[Docker 安装] 开始安装 Docker...", "info")

        def _run_step(cmd, desc, timeout=300):
            try:
                proc = subprocess.run(
                    ["wsl", "-d", distro, "--", "bash", "-c", cmd],
                    capture_output=True,
                    timeout=timeout,
                    creationflags=self._creation_flags(),
                )
            except subprocess.TimeoutExpired:
                self.install_error.emit(f"{desc} 超时（>{timeout}s），请检查网络或磁盘")
                return False
            if proc.returncode != 0:
                stderr = self._clean_stderr(proc.stderr)
                self.log_received.emit(f"[Docker 安装] ✗ {desc}失败", "error")
                self.log_received.emit(f"[DEBUG] 返回码: {proc.returncode}", "error")
                self.log_received.emit(f"[DEBUG] STDERR: {stderr}", "error")
                self.install_error.emit(f"{desc}失败（返回码 {proc.returncode}）: {stderr[:200]}")
                return False
            return True

        docker_mirrors = [
            ("阿里云", "https://mirrors.aliyun.com/docker-ce"),
            ("清华大学", "https://mirrors.tuna.tsinghua.edu.cn/docker-ce"),
            ("官方源", "https://download.docker.com"),
        ]

        try:
            self.progress_updated.emit("安装前置依赖...")
            self.log_received.emit("[Docker 安装] 1/5 安装前置依赖...", "info")
            if not _run_step(
                "apt-get update && apt-get install -y ca-certificates curl gnupg lsb-release",
                "前置依赖安装",
            ):
                return False
            self.log_received.emit("[Docker 安装] ✓ 前置依赖安装完成", "info")

            installed = False
            for i, (mirror_name, docker_mirror) in enumerate(docker_mirrors):
                self.progress_updated.emit(f"配置 Docker 源 ({mirror_name})...")
                self.log_received.emit(
                    f"[Docker 安装] 2/5 添加 Docker GPG 密钥和源（{mirror_name}）"
                    f"{'  [重试]' if i > 0 else ''}...",
                    "info",
                )

                if i > 0:
                    _run_step(
                        "rm -f /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.gpg && apt-get clean",
                        "清理旧源缓存",
                    )

                add_repo_cmd = (
                    "mkdir -p /etc/apt/keyrings && "
                    f"curl -fsSL {docker_mirror}/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && "
                    "chmod a+r /etc/apt/keyrings/docker.gpg && "
                    f'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] '
                    f'{docker_mirror}/linux/ubuntu $(lsb_release -cs) stable" '
                    "> /etc/apt/sources.list.d/docker.list"
                )
                if not _run_step(add_repo_cmd, "Docker 源配置"):
                    self.log_received.emit(f"[Docker 安装] ⚠ {mirror_name} 源配置失败，尝试下一个源...", "warn")
                    continue
                self.log_received.emit(f"[Docker 安装] ✓ Docker 源配置完成（{mirror_name}）", "info")

                self.progress_updated.emit(f"安装 Docker CE ({mirror_name})...")
                self.log_received.emit("[Docker 安装] 3/5 安装 Docker CE + Compose 插件...", "info")
                if _run_step(
                    "apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin",
                    "Docker CE 安装",
                    timeout=600,
                ):
                    self.log_received.emit("[Docker 安装] ✓ Docker CE 安装完成", "info")
                    installed = True
                    break
                else:
                    self.log_received.emit(f"[Docker 安装] ⚠ {mirror_name} 安装失败，尝试下一个源...", "warn")

            if not installed:
                self.log_received.emit("[Docker 安装] ✗ 所有镜像源均失败", "error")
                return False

            self.progress_updated.emit("配置镜像加速器...")
            self.log_received.emit("[Docker 安装] 4/5 配置 Docker 镜像加速器...", "info")
            daemon_json = (
                '{"registry-mirrors":['
                '"https://docker.m.daocloud.io",'
                '"https://docker.1ms.run",'
                '"https://ccr.ccs.tencentyun.com"'
                ']}'
            )
            if not _run_step(
                f"mkdir -p /etc/docker && echo '{daemon_json}' > /etc/docker/daemon.json",
                "镜像加速器配置",
            ):
                self.log_received.emit("[Docker 安装] ⚠ 镜像加速器配置失败，将使用默认源", "warn")
            else:
                self.log_received.emit("[Docker 安装] ✓ 镜像加速器配置完成", "info")

            self.progress_updated.emit("启动 Docker 服务...")
            self.log_received.emit("[Docker 安装] 5/5 启动 Docker 服务...", "info")
            _run_step("systemctl daemon-reload && systemctl restart docker", "Docker 服务启动", timeout=60)
            self.log_received.emit("[Docker 安装] ✓ Docker 服务已启动", "info")

            self.log_received.emit("[Docker 安装] 等待 Docker daemon 就绪...", "info")
            time.sleep(2)

            self.progress_updated.emit("Docker 安装完成！")
            self.log_received.emit("[Docker 安装] ✓ Docker 安装完成！", "info")
            return True
        except subprocess.TimeoutExpired:
            self.progress_updated.emit("Docker 安装超时")
            self.log_received.emit("[Docker 安装] ✗ Docker 安装超时", "error")
            return False
        except Exception as e:
            self.progress_updated.emit(f"Docker 安装异常: {e}")
            self.log_received.emit(f"[Docker 安装] ✗ Docker 安装异常: {e}", "error")
            return False

    def remove_distro(self):
        """删除专用 WSL 发行版"""
        try:
            subprocess.run(
                ["wsl", "--unregister", DISTRO_NAME],
                capture_output=True,
                timeout=30,
                creationflags=self._creation_flags(),
            )
            self.log_received.emit(f"已删除 WSL 发行版 {DISTRO_NAME}", "info")
        except Exception as e:
            self.log_received.emit(f"删除发行版失败: {e}", "error")

    def install_wsl(self):
        """以管理员权限安装 WSL2（通过 ShellExecute runas）"""
        self.log_received.emit("正在请求管理员权限安装 WSL2...", "info")
        try:
            import ctypes

            ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                "cmd",
                '/c wsl --install --no-distribution && shutdown /r /t 60 /c "WSL 安装完成，60秒后自动重启"',
                None,
                1,
            )
            self.log_received.emit("WSL2 安装已启动，安装完成后将在 60 秒后自动重启", "info")
            return True
        except Exception as e:
            self.log_received.emit(f"WSL2 安装启动失败: {e}", "error")
            return False

    def install_docker(self):
        """在专用发行版内异步安装 Docker"""
        if not self._distro_exists():
            self.log_received.emit("NekroAgent 发行版不存在，请先创建环境", "error")
            return False

        self.log_received.emit(f"正在 {DISTRO_NAME} 中安装 Docker...", "info")
        self.status_changed.emit("安装 Docker...")

        def _do_install():
            success = self._install_docker_sync()
            if success:
                self.log_received.emit("Docker 安装完成", "info")
            else:
                self.log_received.emit("Docker 安装失败", "error")
            self.progress_updated.emit("__docker_done__" if success else "__docker_fail__")

        threading.Thread(target=_do_install, daemon=True).start()
        return True
