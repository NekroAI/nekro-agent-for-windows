import gzip
import os
import secrets
import shlex
import subprocess
import tarfile
import tempfile
import threading
import time
import zlib
from urllib.parse import urlparse

import requests

from core.wsl.constants import DISTRO_NAME, ROOTFS_URLS
from core.wsl.prerequisites import (
    is_wsl_virtualization_error,
    wsl_virtualization_guidance,
)


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

        if not self._ensure_wsl2_components():
            return False

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
                    [
                        "wsl",
                        "--import",
                        DISTRO_NAME,
                        install_dir,
                        rootfs_path,
                        "--version",
                        "2",
                    ],
                    capture_output=True,
                    timeout=300,
                    creationflags=self._creation_flags(),
                )
                if proc.returncode != 0:
                    command_output = self._safe_decode(proc.stdout) + self._safe_decode(
                        proc.stderr
                    )
                    detail = self._format_command_failure(
                        "[发行版创建] WSL 导入失败",
                        args=[
                            "wsl",
                            "--import",
                            DISTRO_NAME,
                            install_dir,
                            rootfs_path,
                            "--version",
                            "2",
                        ],
                        timeout=300,
                        returncode=proc.returncode,
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                    )
                    if is_wsl_virtualization_error(command_output):
                        detail += wsl_virtualization_guidance()
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
                    args=[
                        "wsl",
                        "--import",
                        DISTRO_NAME,
                        install_dir,
                        rootfs_path,
                        "--version",
                        "2",
                    ],
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
                    args=[
                        "wsl",
                        "--import",
                        DISTRO_NAME,
                        install_dir,
                        rootfs_path,
                        "--version",
                        "2",
                    ],
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
        failures = []
        for url in ROOTFS_URLS:
            source = urlparse(url).netloc or url
            try:
                self.progress_updated.emit(f"正在从 {source} 下载 Ubuntu rootfs...")
                self.log_received.emit(
                    f"[发行版创建] 尝试 rootfs 下载源: {url}", "info"
                )
                with requests.get(
                    url,
                    headers={
                        "User-Agent": "NekroAgent/1.0",
                        # requests 会自动解压 Content-Encoding。优先请求原始字节，
                        # 避免把传输层长度误当成写入磁盘后的长度。
                        "Accept-Encoding": "identity",
                    },
                    stream=True,
                    timeout=(15, 60),
                ) as resp:
                    resp.raise_for_status()
                    content_encoding = (
                        resp.headers.get("content-encoding", "").strip().lower()
                    )
                    if content_encoding in {"", "identity"}:
                        try:
                            total = int(resp.headers.get("content-length", 0)) or None
                        except (TypeError, ValueError):
                            total = None
                    else:
                        total = None
                    downloaded = 0
                    chunk_size = 256 * 1024

                    with open(dest_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=chunk_size):
                            if not chunk:
                                continue
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

                    if downloaded <= 0:
                        raise requests.RequestException("下载源未返回任何数据")
                    if total is not None and downloaded != total:
                        raise requests.RequestException(
                            f"下载不完整，预期 {total} 字节，实际 {downloaded} 字节"
                        )
                    self.progress_updated.emit("正在校验镜像完整性...")
                    self._validate_rootfs_archive(dest_path)

                self.progress_updated.emit("下载完成")
                self.log_received.emit(
                    f"[发行版创建] rootfs 下载成功（{source}）", "info"
                )
                return True
            except requests.RequestException as e:
                detail = f"{source}: {self._describe_download_error(e)}"
                failures.append(detail)
                self._remove_quietly(dest_path)
                self.log_received.emit(
                    f"[发行版创建] rootfs 下载源失败: {detail}", "warn"
                )
                self.install_error.emit(f"{detail}，尝试下一个源...")
            except ValueError as e:
                detail = f"{source}: 下载内容校验失败（{e}）"
                failures.append(detail)
                # 校验没过的残档必须立刻删掉，否则会被后续步骤当成可用 rootfs
                self._remove_quietly(dest_path)
                self.log_received.emit(
                    f"[发行版创建] rootfs 下载源失败: {detail}", "warn"
                )
                self.install_error.emit(f"{detail}，尝试下一个源...")
            except OSError as e:
                detail = f"磁盘写入失败: {e}；目标文件: {dest_path}"
                self._remove_quietly(dest_path)
                self.install_error.emit(detail)
                return False

        failure_summary = "\n".join(f"- {failure}" for failure in failures)
        self.install_error.emit(
            "所有下载源均失败，已依次尝试：\n" + failure_summary
        )
        self._remove_quietly(dest_path)
        self.progress_updated.emit("所有下载源均失败")
        return False

    @staticmethod
    def _describe_download_error(e):
        """把 requests 异常翻成可操作的中文原因，并截断底层 urllib3 堆栈文本。"""
        if isinstance(e, requests.exceptions.ConnectTimeout):
            cause = "连接超时"
        elif isinstance(e, requests.exceptions.ReadTimeout):
            cause = "读取超时，下载源响应过慢"
        elif isinstance(e, requests.exceptions.SSLError):
            cause = "TLS 握手失败"
        elif isinstance(e, requests.exceptions.ProxyError):
            cause = "代理连接失败，请检查系统代理设置"
        elif isinstance(e, requests.exceptions.HTTPError):
            status = getattr(getattr(e, "response", None), "status_code", "")
            cause = f"下载源返回 HTTP {status}".strip()
        elif isinstance(e, requests.exceptions.ConnectionError):
            lowered = str(e).lower()
            if "getaddrinfo" in lowered or "name or service not known" in lowered:
                cause = "DNS 解析失败，请检查网络连接"
            elif "refused" in lowered:
                cause = "连接被拒绝"
            elif "timed out" in lowered or "timeout" in lowered:
                cause = "连接超时"
            else:
                cause = "网络连接失败"
        else:
            cause = "下载失败"
        text = " ".join(str(e).split())
        if len(text) > 160:
            text = f"{text[:160]}..."
        return f"{cause}（{text}）" if text else cause

    @staticmethod
    def _validate_rootfs_archive(path):
        """拒绝镜像站返回的 HTML/错误页，并校验 gzip tar 归档完整可读。

        必须读满整个 gzip 流，而不是读到 tar 的 EOF 块就停：gzip 的 CRC32 和
        长度校验位在流尾部，只迭代 tar 成员根本够不到，单比特翻转的损坏镜像
        会被直接放行。校验放在成员检查之前，损坏镜像可以更早失败。

        任何读取或解压异常都归一成 ValueError，交给调用方切换下一个下载源：
        损坏的 deflate 流抛 zlib.error，gzip.BadGzipFile 又是 OSError 子类，
        两者都不是 TarError；漏接会被误判成"磁盘写入失败"而放弃剩余镜像，
        或者干脆逃出下载循环、结束整个创建线程。
        """
        try:
            with open(path, "rb") as archive_file:
                if archive_file.read(2) != b"\x1f\x8b":
                    raise ValueError("返回内容不是 gzip 归档，可能是镜像站错误页")

            # tar 的 EOF 块之后仍有 gzip 尾部，必须单独读满整个流，
            # CRC32/长度校验位才会被检查到（只迭代 tar 成员够不到）
            with gzip.open(path, "rb") as stream:
                while stream.read(1024 * 1024):
                    pass

            member_count = 0
            with tarfile.open(path, mode="r:gz") as archive:
                for _member in archive:
                    member_count += 1
            if member_count == 0:
                raise ValueError("gzip tar 归档为空")
        except (tarfile.TarError, EOFError, zlib.error, OSError) as e:
            raise ValueError(
                f"gzip tar 归档已损坏或不完整: {type(e).__name__}: {e}"
            ) from e

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
        "需要重新启动",
        "需要重启",
        "重启电脑",
        "重启后",
        "重新启动系统前",
        "reboot required",
        "reboot is required",
        "restart required",
        "restart is required",
        "reboot your computer",
        "restart your computer",
        "changes will not be effective until",
        "restart needed",
        "enable pending",
        "启用挂起",
    )
    _WSL_INSTALL_NO_REBOOT_MARKERS = (
        "无需重启",
        "无需重新启动",
        "no restart required",
        "restart required: no",
    )
    _WSL_INSTALL_ALREADY_READY_MARKERS = (
        "windows subsystem for linux is already installed",
        "wsl is already installed",
        "适用于 linux 的 windows 子系统已安装",
        "适用于linux的windows子系统已安装",
    )

    @staticmethod
    def _remove_quietly(path):
        """删除临时文件，失败时静默忽略（清理失败不应影响安装结果判定）。"""
        try:
            os.remove(path)
        except OSError:
            pass

    @staticmethod
    def _decode_wsl_install_output(data):
        """解码 DISM/WSL 重定向日志，兼容无 BOM 的 UTF-16LE 中文输出。"""
        if isinstance(data, str):
            return data
        if not isinstance(data, bytes):
            return str(data or "")
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16", errors="replace")

        markers = (
            "重新启动",
            "重启",
            "操作成功",
            "已安装",
            "错误",
            "部署映像",
            "restart",
            "reboot",
            "operation completed",
            "already installed",
            "windows subsystem",
            "virtualmachineplatform",
            "error",
            "dism",
        )
        candidates = []
        for encoding in ("utf-8-sig", "utf-16-le", "gbk"):
            try:
                text = data.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
            lowered = text.lower()
            marker_score = sum(1 for marker in markers if marker in lowered)
            printable = sum(1 for char in text if char.isprintable() or char.isspace())
            printable_ratio = printable / len(text) if text else 0
            score = marker_score * 100 + printable_ratio * 10
            if encoding == "utf-16-le" and b"\x00" in data:
                score += 5
            score -= text.count("\ufffd") * 20
            candidates.append((score, text))
        if candidates:
            return max(candidates, key=lambda item: item[0])[1]
        return data.decode("latin1", errors="replace")

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
        if exit_token in {"1641", "3010"}:
            return "reboot"
        lowered = (output or "").lower()
        reboot_marker = (
            not any(marker in lowered for marker in cls._WSL_INSTALL_NO_REBOOT_MARKERS)
            and any(marker in lowered for marker in cls._WSL_INSTALL_REBOOT_MARKERS)
        )
        already_ready = any(
            marker in lowered for marker in cls._WSL_INSTALL_ALREADY_READY_MARKERS
        )
        if exit_token == "0":
            return "reboot" if reboot_marker else "done"
        if already_ready and not reboot_marker:
            return "done"
        return "reboot" if reboot_marker else "fail"

    # 分支逻辑写进临时批处理再提权执行，而不是拼一条单行 cmd 串：
    # 单行串在 PowerShell -ArgumentList 的引号嵌套下会被割裂，dism 的
    # 3010/1641（需重启）退出码会因此丢失，被误判成"安装完成"。
    # 正文保持纯 ASCII，日志路径由脚本按 %~dpn0 自行推导。
    _ELEVATED_INSTALL_SCRIPT = "\n".join(
        (
            "@echo off",
            "setlocal EnableExtensions",
            'call :na_main > "%~dpn0.log" 2>&1',
            "exit /b %ERRORLEVEL%",
            "",
            ":na_main",
            "dism.exe /online /enable-feature "
            "/featurename:Microsoft-Windows-Subsystem-Linux /all /norestart",
            'set "NA_WSL_RC=%ERRORLEVEL%"',
            "dism.exe /online /enable-feature "
            "/featurename:VirtualMachinePlatform /all /norestart",
            'set "NA_VMP_RC=%ERRORLEVEL%"',
            "call :na_accepts_rc %NA_WSL_RC%",
            "if errorlevel 1 exit /b %NA_WSL_RC%",
            "call :na_accepts_rc %NA_VMP_RC%",
            "if errorlevel 1 exit /b %NA_VMP_RC%",
            'if "%NA_WSL_RC%"=="3010" exit /b 3010',
            'if "%NA_WSL_RC%"=="1641" exit /b 1641',
            'if "%NA_VMP_RC%"=="3010" exit /b 3010',
            'if "%NA_VMP_RC%"=="1641" exit /b 1641',
            "wsl.exe --install --no-distribution",
            "exit /b %ERRORLEVEL%",
            "",
            "rem Only 0/3010/1641 may continue; string compare also"
            " rejects negative dism codes.",
            ":na_accepts_rc",
            'if "%~1"=="0" exit /b 0',
            'if "%~1"=="3010" exit /b 0',
            'if "%~1"=="1641" exit /b 0',
            "exit /b 1",
            "",
        )
    )

    def _run_elevated_wsl_install(self):
        """提权执行 WSL 必需组件安装，返回 (outcome, output, detail)。"""
        # 提权进程的输出无法直接通过管道回传，由批处理重定向到日志文件再读回；
        # dism/wsl.exe 输出编码不一，交由 _decode_wsl_install_output 识别。
        token = secrets.token_hex(8)
        base_path = os.path.join(
            tempfile.gettempdir(), f"na_wsl_install_{os.getpid()}_{token}"
        )
        script_path = f"{base_path}.cmd"
        log_path = f"{base_path}.log"
        try:
            with open(script_path, "w", encoding="ascii", newline="\r\n") as f:
                f.write(self._ELEVATED_INSTALL_SCRIPT)
        except (OSError, UnicodeEncodeError) as e:
            detail = self._format_command_failure(
                "无法写入 WSL2 组件安装脚本",
                cmd=script_path,
                exception=e,
            )
            return "fail", "", detail

        ps_target = script_path.replace("'", "''")
        ps_script = (
            "try { "
            f"$p = Start-Process -FilePath '{ps_target}' "
            "-Verb RunAs -Wait -PassThru; "
            "Write-Output ('NA_EXIT=' + $p.ExitCode) "
            "} catch { "
            "$msg = $_.Exception.Message; "
            "Write-Output ('NA_ERROR=' + $msg); "
            "if ($msg -match 'cancel|取消|1223') { "
            "Write-Output 'NA_EXIT=DENIED' "
            "} else { Write-Output 'NA_EXIT=LAUNCH_ERROR' } "
            "}"
        )
        exit_token = ""
        launch_error = ""
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
                elif line.startswith("NA_ERROR="):
                    launch_error = line.split("=", 1)[1].strip()
            if not exit_token:
                outer_error = self._safe_decode(proc.stderr).strip()
                launch_error = outer_error or (
                    f"PowerShell 未返回安装进程退出码（退出码 {proc.returncode}）"
                )
                exit_token = "LAUNCH_ERROR"
        except Exception as e:
            detail = self._format_command_failure(
                "WSL2 安装进程执行异常",
                cmd=(
                    "启用 Microsoft-Windows-Subsystem-Linux、VirtualMachinePlatform "
                    "并执行 wsl --install --no-distribution"
                ),
                timeout=1800,
                exception=e,
            )
            self._remove_quietly(script_path)
            self._remove_quietly(log_path)
            return "fail", "", detail

        raw_output = ""
        try:
            with open(log_path, "rb") as f:
                raw_output = self._decode_wsl_install_output(f.read())
        except OSError:
            pass
        finally:
            self._remove_quietly(log_path)
            self._remove_quietly(script_path)

        outcome = self._parse_wsl_install_outcome(exit_token, raw_output)
        output = raw_output.replace("\x00", "").strip()
        detail = (
            "用户拒绝了管理员权限请求"
            if exit_token == "DENIED"
            else (
                f"管理员安装进程启动失败: {launch_error or '未知错误'}"
                if exit_token == "LAUNCH_ERROR"
                else f"安装进程退出码: {exit_token or '未知'}"
            )
        )
        return outcome, output, detail

    def _wsl2_components_ready(self):
        """不提权地判断 WSL2 组件是否已就绪。

        用于跳过多余的 UAC：组件早就装好的机器不该每次创建运行环境都被提权
        打断。判定保守——只有在 `wsl --status` 明确成功且输出里没有虚拟化前置
        错误时才认为就绪，任何存疑情况都退回提权路径。
        """
        try:
            proc = subprocess.run(
                ["wsl", "--status"],
                capture_output=True,
                timeout=30,
                creationflags=self._creation_flags(),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if proc.returncode != 0:
            return False
        output = self._safe_decode(proc.stdout) + self._safe_decode(proc.stderr)
        return not is_wsl_virtualization_error(output)

    def _ensure_wsl2_components(self):
        """在下载 rootfs 前确保 WSL 与虚拟机平台组件已启用。"""
        self.progress_updated.emit("正在检查 WSL2 必需组件...")
        if self._wsl2_components_ready():
            self.log_received.emit(
                "[发行版创建] ✓ WSL2 必需组件已就绪，跳过管理员预检", "info"
            )
            return True

        self.progress_updated.emit("正在检查并启用 WSL2 必需组件...")
        self.log_received.emit(
            "[发行版创建] 预检 WSL 子系统与虚拟机平台（需要管理员权限）...",
            "info",
        )
        outcome, output, detail = self._run_elevated_wsl_install()
        if output:
            self.log_received.emit(f"[WSL 安装] 安装输出:\n{output}", "info")
        if outcome == "done":
            self.log_received.emit("[发行版创建] ✓ WSL2 必需组件已就绪", "info")
            return True
        if outcome == "reboot":
            message = (
                "已启用 WSL 子系统和“虚拟机平台”，需要重启电脑后才能创建运行环境。\n"
                "请重启电脑，再重新打开启动器继续。"
            )
        else:
            message = (
                "无法启用 WSL2 必需组件。\n"
                f"{detail}\n"
                "请确认管理员授权，并检查 Windows 版本是否支持 WSL2。"
            )
        self.log_received.emit(f"[发行版创建] {message}", "error")
        self.install_error.emit(message)
        self.progress_updated.emit("WSL2 必需组件尚未就绪")
        return False

    def install_wsl(self):
        """以管理员权限安装 WSL2，等待安装进程结束并回收结果（后台线程）。"""
        self.log_received.emit("正在请求管理员权限安装 WSL2...", "info")

        def _do_install():
            outcome, output, detail = self._run_elevated_wsl_install()
            if output:
                self.log_received.emit(f"[WSL 安装] 安装输出:\n{output}", "info")
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
