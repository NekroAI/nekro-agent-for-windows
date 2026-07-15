import os
import secrets
import shlex
import subprocess
import tempfile
import threading
import time
from urllib.request import Request, urlopen

from core.wsl.constants import DISTRO_NAME, ROOTFS_URLS


class WSLRuntimeMixin:
    _RUNTIME_INSTALL_MARKER = ".nekroagent-runtime-installing"
    _RUNTIME_GUEST_MARKER = "/etc/nekroagent-launcher-install-id"

    def get_host_access_path(self, guest_path=None):
        if guest_path is None and hasattr(self, 'config') and self.config:
            guest_path = self.config.get_active_data_dir()
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

    def _runtime_install_marker_path(self, install_dir):
        return os.path.join(install_dir, self._RUNTIME_INSTALL_MARKER)

    def _read_runtime_install_marker(self, install_dir):
        marker_path = self._runtime_install_marker_path(install_dir)
        try:
            with open(marker_path, encoding="utf-8") as marker_file:
                return marker_file.read().strip()
        except FileNotFoundError:
            return ""
        except OSError as e:
            detail = (
                "[发行版创建] 读取恢复标记失败\n"
                f"文件: {marker_path}\n"
                f"异常: {type(e).__name__}: {e}"
            )
            self.log_received.emit(detail, "error")
            self.install_error.emit(detail)
            return ""

    def _write_runtime_install_marker(self, install_dir, token):
        marker_path = self._runtime_install_marker_path(install_dir)
        try:
            with open(marker_path, "w", encoding="utf-8", newline="") as marker_file:
                marker_file.write(f"{token}\n")
            return True
        except OSError as e:
            detail = (
                "[发行版创建] 写入恢复标记失败\n"
                f"文件: {marker_path}\n"
                f"异常: {type(e).__name__}: {e}"
            )
            self.log_received.emit(detail, "error")
            self.install_error.emit(detail)
            return False

    def _runtime_guest_marker_matches(self, token):
        marker = self._wsl_exec(
            DISTRO_NAME,
            f"cat {shlex.quote(self._RUNTIME_GUEST_MARKER)} 2>/dev/null",
            timeout=20,
            user="root",
        ).strip()
        return bool(token) and marker == token

    def _cleanup_runtime_install_markers(self, install_dir):
        try:
            self._wsl_run(
                DISTRO_NAME,
                f"rm -f -- {shlex.quote(self._RUNTIME_GUEST_MARKER)}",
                timeout=20,
                user="root",
            )
        except Exception:
            pass
        try:
            os.remove(self._runtime_install_marker_path(install_dir))
        except FileNotFoundError:
            pass
        except OSError as e:
            self.log_received.emit(f"[发行版创建] 清理恢复标记失败: {e}", "warn")

    def _discard_failed_runtime_import(self, install_token):
        """仅清理带有本次 token 的半导入发行版，避免误删既有环境。"""
        try:
            if not self._distro_exists():
                return True
            if not self._runtime_guest_marker_matches(install_token):
                detail = (
                    f"[发行版创建] 检测到 {DISTRO_NAME} 发行版，但其恢复标记与本次创建任务不匹配。\n"
                    "为避免删除既有或并发创建的运行环境，启动器不会自动注销该发行版。"
                )
                self.log_received.emit(detail, "error")
                self.install_error.emit(
                    detail
                    + "\n请确认该发行版来源；仅在确认它是无用半成品后再手动执行 "
                    "wsl --unregister NekroAgent。"
                )
                return False
            proc = subprocess.run(
                ["wsl", "--unregister", DISTRO_NAME],
                capture_output=True,
                timeout=120,
                creationflags=self._creation_flags(),
            )
            if proc.returncode == 0:
                self.log_received.emit(
                    f"[发行版创建] 已清理失败后残留的 {DISTRO_NAME} 半成品发行版",
                    "warn",
                )
                return True
            detail = self._format_command_failure(
                "[发行版创建] 清理半成品 WSL 发行版失败",
                args=["wsl", "--unregister", DISTRO_NAME],
                timeout=120,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        except Exception as e:
            detail = self._format_command_failure(
                "[发行版创建] 清理半成品 WSL 发行版异常",
                args=["wsl", "--unregister", DISTRO_NAME],
                timeout=120,
                exception=e,
            )
        self.log_received.emit(detail, "error")
        self.install_error.emit(
            detail
            + "\n请手动执行 wsl --unregister NekroAgent 清理半成品后再重试。"
        )
        return False

    def _create_distro(self, install_dir):
        """下载 Ubuntu rootfs 并用 wsl --import 创建专用发行版（同步，在线程中调用）"""
        self.progress_updated.emit("准备创建 NekroAgent 运行环境...")
        self.log_received.emit("[发行版创建] 开始创建 NekroAgent 专用发行版...", "info")

        self.log_received.emit(f"[发行版创建] 1/4 创建安装目录: {install_dir}", "info")
        try:
            os.makedirs(install_dir, exist_ok=True)
            self.log_received.emit("[发行版创建] ✓ 安装目录已创建", "info")
        except Exception as e:
            detail = (
                "[发行版创建] 创建安装目录失败\n"
                f"目录: {install_dir}\n"
                f"异常: {type(e).__name__}: {e}"
            )
            self.log_received.emit(detail, "error")
            self.install_error.emit(detail)
            return False

        install_token = self._read_runtime_install_marker(install_dir)
        distro_exists = self._distro_exists()
        resuming_import = False
        if distro_exists:
            if not install_token or not self._runtime_guest_marker_matches(install_token):
                detail = (
                    f"[发行版创建] {DISTRO_NAME} 发行版已存在，但它不属于本次未完成的创建任务。\n"
                    "为避免覆盖或误用现有运行环境，启动器不会再次导入或继续配置。\n"
                    "请在确认无需保留后通过启动器卸载现有运行环境，或选择原创建任务的安装目录重试。"
                )
                self.log_received.emit(detail, "error")
                self.install_error.emit(detail)
                return False
            resuming_import = True
            self.log_received.emit(
                f"[发行版创建] 检测到 {DISTRO_NAME} 的未完成安装，跳过重复导入并继续配置",
                "warn",
            )
        else:
            if not install_token:
                install_token = secrets.token_hex(24)
            if not self._write_runtime_install_marker(install_dir, install_token):
                return False

        rootfs_path = os.path.join(install_dir, "rootfs.tar.gz")
        if not resuming_import:
            self.log_received.emit("[发行版创建] 2/4 下载 Ubuntu rootfs...", "info")
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
                    detail = self._format_command_failure(
                        "[发行版创建] WSL 导入失败",
                        args=["wsl", "--import", DISTRO_NAME, install_dir, rootfs_path],
                        timeout=300,
                        returncode=proc.returncode,
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                    )
                    self.progress_updated.emit("导入失败")
                    self.log_received.emit(detail, "error")
                    self.install_error.emit(detail)
                    self._discard_failed_runtime_import(install_token)
                    return False
                self.log_received.emit(
                    f"[发行版创建] ✓ {DISTRO_NAME} 发行版导入完成", "info"
                )
                self._write_to_wsl(
                    DISTRO_NAME,
                    f"{install_token}\n",
                    self._RUNTIME_GUEST_MARKER,
                )
            except subprocess.TimeoutExpired as e:
                detail = self._format_command_failure(
                    "[发行版创建] WSL 导入超时",
                    args=["wsl", "--import", DISTRO_NAME, install_dir, rootfs_path],
                    timeout=300,
                    stdout=e.stdout,
                    stderr=e.stderr,
                    exception=e,
                )
                self.progress_updated.emit("导入超时")
                self.log_received.emit(detail, "error")
                self.install_error.emit(detail)
                self._discard_failed_runtime_import(install_token)
                return False
            except Exception as e:
                detail = self._format_command_failure(
                    "[发行版创建] WSL 导入后写入恢复标记失败",
                    args=["wsl", "--import", DISTRO_NAME, install_dir, rootfs_path],
                    timeout=300,
                    exception=e,
                )
                self.progress_updated.emit(f"导入异常: {e}")
                self.log_received.emit(detail, "error")
                self.install_error.emit(detail)
                self._discard_failed_runtime_import(install_token)
                return False

            try:
                os.remove(rootfs_path)
                self.log_received.emit("[发行版创建] ✓ 临时 rootfs 文件已清理", "info")
            except OSError:
                pass

        self.progress_updated.emit("正在配置 WSL 环境...")
        self.log_received.emit(
            "[发行版创建] 4/4 配置 WSL 环境（隔离 Windows PATH）...", "info"
        )
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

            self.log_received.emit(
                "[发行版创建] 重启 WSL 发行版以启用 systemd...", "info"
            )
            subprocess.run(
                ["wsl", "--terminate", DISTRO_NAME],
                capture_output=True,
                timeout=30,
                creationflags=self._creation_flags(),
            )
            time.sleep(2)
            self.log_received.emit("[发行版创建] ✓ WSL 发行版已重启", "info")
        except Exception as e:
            detail = (
                "[发行版创建] 配置 WSL 失败\n"
                f"发行版: {DISTRO_NAME}\n"
                f"配置文件: /etc/wsl.conf\n"
                f"异常: {type(e).__name__}: {e}"
            )
            self.progress_updated.emit(f"配置 WSL 失败: {e}")
            self.log_received.emit(detail, "error")
            self.install_error.emit(detail)
            return False

        if self.config:
            if not self.config.set("wsl_distro", DISTRO_NAME):
                detail = f"[发行版创建] 保存配置失败: {self.config.last_save_error}"
                self.log_received.emit(detail, "error")
                self.install_error.emit(detail)
                return False
            if not self.config.set("wsl_install_dir", install_dir):
                detail = f"[发行版创建] 保存安装目录失败: {self.config.last_save_error}"
                self.log_received.emit(detail, "error")
                self.install_error.emit(detail)
                return False
            self.log_received.emit("[发行版创建] ✓ 配置已保存", "info")

        self.progress_updated.emit("发行版创建成功！正在安装 Docker...")
        self.log_received.emit(
            "[发行版创建] ✓ 发行版创建完成！开始安装 Docker...", "info"
        )

        docker_ok = self._install_docker_sync()
        if docker_ok:
            self._cleanup_runtime_install_markers(install_dir)
        return docker_ok

    def _download_rootfs(self, dest_path):
        """下载 Ubuntu rootfs，返回是否成功"""
        import socket
        from urllib.error import HTTPError, URLError

        last_error = ""
        for url in ROOTFS_URLS:
            try:
                self.progress_updated.emit("正在下载 Ubuntu rootfs...")
                self.log_received.emit(f"[发行版创建] 下载 rootfs: {url}", "info")
                req = Request(url, headers={"User-Agent": "NekroAgent/1.0"})
                with urlopen(req, timeout=60) as resp:

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
                                self.progress_updated.emit(
                                    f"下载中... {mb_done:.1f} / {mb_total:.1f} MB ({pct}%)"
                                )
                            else:
                                mb_done = downloaded / (1024 * 1024)
                                self.progress_updated.emit(f"下载中... {mb_done:.1f} MB")

                self.progress_updated.emit("下载完成")
                return True
            except HTTPError as e:
                last_error = f"HTTP {e.code} {e.reason}（{url}）"
                self.install_error.emit(
                    f"下载源返回错误: {last_error}，尝试下一个源..."
                )
            except URLError as e:
                reason = str(e.reason)
                if (
                    isinstance(e.reason, socket.timeout)
                    or "timed out" in reason.lower()
                ):
                    last_error = f"连接超时（{url}）"
                elif "Name or service not known" in reason or "getaddrinfo" in reason:
                    last_error = f"DNS 解析失败，请检查网络连接（{url}）"
                elif "Connection refused" in reason:
                    last_error = f"连接被拒绝（{url}）"
                else:
                    last_error = f"网络错误: {reason}（{url}）"
                self.install_error.emit(f"{last_error}，尝试下一个源...")
            except OSError as e:
                last_error = f"磁盘写入失败: {e}；目标文件: {dest_path}"
                self.install_error.emit(last_error)
                return False
            except Exception as e:
                last_error = str(e)
                self.install_error.emit(f"下载异常: {last_error}，尝试下一个源...")

        self.install_error.emit(f"所有下载源均失败，最后错误: {last_error}")
        try:
            if os.path.exists(dest_path):
                os.remove(dest_path)
        except OSError:
            pass
        self.progress_updated.emit("所有下载源均失败")
        return False

    def _install_docker_sync(self):
        """在专用发行版内同步安装 Docker（通过 Docker 官方源，使用国内镜像）"""
        distro = DISTRO_NAME
        self.progress_updated.emit("正在安装 Docker...")
        self.log_received.emit("[Docker 安装] 开始安装 Docker...", "info")

        def _run_step(cmd, desc, timeout=300, emit_error=True, log_failure=True):
            try:
                proc = subprocess.run(
                    ["wsl", "-d", distro, "--", "bash", "-c", cmd],
                    capture_output=True,
                    timeout=timeout,
                    creationflags=self._creation_flags(),
                )
            except subprocess.TimeoutExpired as e:
                detail = self._format_command_failure(
                    f"[Docker 安装] {desc}超时",
                    cmd=cmd,
                    distro=distro,
                    timeout=timeout,
                    stdout=e.stdout,
                    stderr=e.stderr,
                    exception=e,
                )
                if log_failure:
                    self.log_received.emit(detail, "error")
                if emit_error:
                    self.install_error.emit(detail)
                return False
            if proc.returncode != 0:
                detail = self._format_command_failure(
                    f"[Docker 安装] {desc}失败",
                    cmd=cmd,
                    distro=distro,
                    timeout=timeout,
                    returncode=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                )
                if log_failure:
                    self.log_received.emit(detail, "error")
                if emit_error:
                    self.install_error.emit(detail)
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
                        emit_error=False,
                        log_failure=False,
                    )

                add_repo_cmd = (
                    "mkdir -p /etc/apt/keyrings && "
                    f"curl -fsSL {docker_mirror}/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && "
                    "chmod a+r /etc/apt/keyrings/docker.gpg && "
                    f'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] '
                    f'{docker_mirror}/linux/ubuntu $(lsb_release -cs) stable" '
                    "> /etc/apt/sources.list.d/docker.list"
                )
                if not _run_step(add_repo_cmd, "Docker 源配置", emit_error=False):
                    self.install_error.emit(
                        f"{mirror_name}源下载失败，正在尝试下一个源..."
                    )
                    self.log_received.emit(
                        f"[Docker 安装] ⚠ {mirror_name} 源配置失败，尝试下一个源...",
                        "warn",
                    )
                    continue
                self.log_received.emit(
                    f"[Docker 安装] ✓ Docker 源配置完成（{mirror_name}）", "info"
                )

                self.progress_updated.emit(f"安装 Docker CE ({mirror_name})...")
                self.log_received.emit(
                    "[Docker 安装] 3/5 安装 Docker CE + Compose 插件...", "info"
                )
                if _run_step(
                    "apt-get update && apt-get install -y "
                    "docker-ce docker-ce-cli containerd.io "
                    "docker-buildx-plugin docker-compose-plugin",
                    "Docker CE 安装",
                    timeout=600,
                    emit_error=False,
                ):
                    self.log_received.emit("[Docker 安装] ✓ Docker CE 安装完成", "info")
                    installed = True
                    break
                else:
                    self.install_error.emit(
                        f"{mirror_name}源安装失败，正在尝试下一个源..."
                    )
                    self.log_received.emit(
                        f"[Docker 安装] ⚠ {mirror_name} 安装失败，尝试下一个源...",
                        "warn",
                    )

            if not installed:
                detail = (
                    "所有 Docker 软件源均失败。\n"
                    "已尝试: "
                    + "、".join(name for name, _url in docker_mirrors)
                    + "\n请检查网络、DNS、代理或稍后重试。"
                )
                self.log_received.emit(detail, "error")
                self.install_error.emit(detail)
                return False

            self.progress_updated.emit("配置镜像加速器...")
            self.log_received.emit(
                "[Docker 安装] 4/5 配置 Docker 镜像加速器...", "info"
            )
            daemon_json = (
                '{"registry-mirrors":['
                '"https://docker.m.daocloud.io",'
                '"https://docker.1ms.run",'
                '"https://ccr.ccs.tencentyun.com",'
                '"https://docker.jiaxin.site",'
                '"https://docker.xuanyuan.me",'
                '"http://kubesre.xyz"'
                ']}'
            )
            if not _run_step(
                f"mkdir -p /etc/docker && echo '{daemon_json}' > /etc/docker/daemon.json",
                "镜像加速器配置",
            ):
                self.log_received.emit(
                    "[Docker 安装] ⚠ 镜像加速器配置失败，将使用默认源", "warn"
                )
            else:
                self.log_received.emit("[Docker 安装] ✓ 镜像加速器配置完成", "info")

            self.progress_updated.emit("启动 Docker 服务...")
            self.log_received.emit("[Docker 安装] 5/5 启动 Docker 服务...", "info")
            if not _run_step(
                "systemctl daemon-reload && systemctl restart docker",
                "Docker 服务启动",
                timeout=60,
            ):
                return False
            self.log_received.emit("[Docker 安装] ✓ Docker 服务已启动", "info")

            self.log_received.emit("[Docker 安装] 等待 Docker daemon 就绪...", "info")
            time.sleep(2)
            if not _run_step("docker info >/dev/null", "Docker daemon 就绪检查", timeout=30):
                return False

            self.progress_updated.emit("Docker 安装完成！")
            self.log_received.emit("[Docker 安装] ✓ Docker 安装完成！", "info")
            return True
        except subprocess.TimeoutExpired as e:
            detail = self._format_command_failure(
                "[Docker 安装] Docker 安装超时",
                distro=distro,
                exception=e,
            )
            self.progress_updated.emit("Docker 安装超时")
            self.log_received.emit(detail, "error")
            self.install_error.emit(detail)
            return False
        except Exception as e:
            detail = (
                "[Docker 安装] Docker 安装异常\n"
                f"发行版: {distro}\n"
                f"异常: {type(e).__name__}: {e}"
            )
            self.progress_updated.emit(f"Docker 安装异常: {e}")
            self.log_received.emit(detail, "error")
            self.install_error.emit(detail)
            return False

    def remove_distro(self):
        """删除专用 WSL 发行版"""
        try:
            proc = subprocess.run(
                ["wsl", "--unregister", DISTRO_NAME],
                capture_output=True,
                timeout=30,
                creationflags=self._creation_flags(),
            )
            if proc.returncode != 0:
                detail = self._format_command_failure(
                    "删除 WSL 发行版失败",
                    args=["wsl", "--unregister", DISTRO_NAME],
                    timeout=30,
                    returncode=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                )
                self.log_received.emit(detail, "error")
                return False
            self.log_received.emit(f"已删除 WSL 发行版 {DISTRO_NAME}", "info")
            return True
        except Exception as e:
            detail = self._format_command_failure(
                "删除 WSL 发行版异常",
                args=["wsl", "--unregister", DISTRO_NAME],
                timeout=30,
                exception=e,
            )
            self.log_received.emit(detail, "error")
            return False

    _WSL_INSTALL_REBOOT_MARKERS = (
        "重新启动",
        "重启",
        "reboot",
        "restart",
    )

    @classmethod
    def _parse_wsl_install_outcome(cls, exit_token, output):
        """根据提权安装进程的退出码与输出判定结果。

        返回 "done"（安装完成，可直接重新检测）、"reboot"（组件已启用，
        需要重启电脑后继续）或 "fail"。首次在全新机器上执行
        `wsl --install` 通常只启用 Windows 可选组件即要求重启，且可能以
        非零码退出；重启后需要再执行一次才能完成剩余安装。
        """
        if exit_token == "DENIED":
            return "fail"
        lowered = (output or "").lower()
        reboot_marker = any(marker in lowered for marker in cls._WSL_INSTALL_REBOOT_MARKERS)
        if exit_token == "0":
            return "reboot" if reboot_marker else "done"
        return "reboot" if reboot_marker else "fail"

    def install_wsl(self):
        """以管理员权限安装 WSL2，等待安装进程结束并回收结果（后台线程）。"""
        self.log_received.emit("正在请求管理员权限安装 WSL2...", "info")

        def _do_install():
            # 提权进程的输出无法直接通过管道回传，重定向到日志文件再读回；
            # wsl.exe 输出为 UTF-16，交由 _safe_decode 识别。
            log_path = os.path.join(
                tempfile.gettempdir(), f"na_wsl_install_{os.getpid()}.log"
            )
            inner_cmd = f'wsl --install --no-distribution > "{log_path}" 2>&1'
            ps_inner = inner_cmd.replace("'", "''")
            ps_script = (
                "try { "
                "$p = Start-Process -FilePath 'cmd.exe' "
                f"-ArgumentList '/d','/c','{ps_inner}' "
                "-Verb RunAs -Wait -PassThru; "
                "Write-Output ('NA_EXIT=' + $p.ExitCode) "
                "} catch { Write-Output 'NA_EXIT=DENIED' }"
            )
            exit_token = ""
            try:
                proc = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        ps_script,
                    ],
                    capture_output=True,
                    timeout=1800,
                    creationflags=self._creation_flags(),
                )
                for line in self._safe_decode(proc.stdout).splitlines():
                    line = line.strip().strip("\x00")
                    if line.startswith("NA_EXIT="):
                        exit_token = line.split("=", 1)[1].strip()
            except Exception as e:
                self.log_received.emit(
                    self._format_command_failure(
                        "WSL2 安装进程执行异常",
                        cmd="wsl --install --no-distribution",
                        timeout=1800,
                        exception=e,
                    ),
                    "error",
                )
                self.progress_updated.emit("__wsl_fail__")
                return

            output = ""
            try:
                with open(log_path, "rb") as f:
                    output = self._clean_command_output(self._safe_decode(f.read()))
            except OSError:
                pass
            finally:
                try:
                    os.remove(log_path)
                except OSError:
                    pass

            if output:
                self.log_received.emit(f"[WSL 安装] 安装输出:\n{output}", "info")

            outcome = self._parse_wsl_install_outcome(exit_token, output)
            if outcome == "done":
                self.log_received.emit("[WSL 安装] 安装步骤执行完成，开始重新检测", "info")
                self.progress_updated.emit("__wsl_done__")
            elif outcome == "reboot":
                self.log_received.emit(
                    "[WSL 安装] Windows 组件已启用，需要重启电脑后生效。"
                    "重启后请重新打开启动器；若仍提示未安装，请再次点击安装完成剩余步骤。",
                    "warning",
                )
                self.progress_updated.emit("__wsl_reboot__")
            else:
                detail = "用户拒绝了管理员权限请求" if exit_token == "DENIED" else (
                    f"安装进程退出码: {exit_token or '未知'}"
                )
                self.log_received.emit(f"[WSL 安装] 安装失败（{detail}）", "error")
                self.progress_updated.emit("__wsl_fail__")

        threading.Thread(target=_do_install, daemon=True).start()
        return True

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
            self.progress_updated.emit(
                "__docker_done__" if success else "__docker_fail__"
            )

        threading.Thread(target=_do_install, daemon=True).start()
        return True
