import subprocess
import threading
import os
import time
import sys
import secrets
import string
import tempfile
import re
import shlex
from urllib.request import urlopen, Request
from urllib.error import URLError
from core.backend_base import BackendBase


# 专用 WSL 发行版名称
DISTRO_NAME = "NekroAgent"

# 各部署模式需要的镜像清单
REQUIRED_IMAGES_BASE = {
    "napcat": [
        "postgres:14",
        "qdrant/qdrant:v1.17.1",
        "kromiose/nekro-agent:latest",
        "mlikiowa/napcat-docker",
        "kromiose/nekro-agent-sandbox",
        "kromiose/nekro-cc-sandbox",
    ],
    "lite": [
        "postgres:14",
        "qdrant/qdrant:v1.17.1",
        "kromiose/nekro-agent:latest",
        "kromiose/nekro-agent-sandbox",
        "kromiose/nekro-cc-sandbox",
    ],
}

# 镜像管理页面展示的镜像（image, 显示名, 描述, 支持的模式）
MANAGED_IMAGES_BASE = [
    ("kromiose/nekro-agent:latest",  "Nekro Agent 本体", "NA 核心服务镜像",         ["napcat", "lite"]),
    ("kromiose/nekro-agent-sandbox", "NA 沙盒",          "代码执行沙盒环境",         ["napcat", "lite"]),
    ("kromiose/nekro-cc-sandbox",    "NA CC 沙盒",       "CC 扩展沙盒环境",          ["napcat", "lite"]),
]

# Ubuntu 22.04 WSL rootfs 下载地址（按优先级排列）
ROOTFS_URLS = [
    "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-cloud-images/wsl/jammy/current/ubuntu-jammy-wsl-amd64-ubuntu22.04lts.rootfs.tar.gz",
    "https://mirror.sjtu.edu.cn/ubuntu-cloud-images/wsl/jammy/current/ubuntu-jammy-wsl-amd64-ubuntu22.04lts.rootfs.tar.gz",
    "https://cloud-images.ubuntu.com/wsl/jammy/current/ubuntu-jammy-wsl-amd64-ubuntu22.04lts.rootfs.tar.gz",
]

# 测试升级流程用，完成联调后改回 False
FORCE_MOCK_IMAGE_UPDATES = True

STABLE_IMAGE = "kromiose/nekro-agent:latest"
PREVIEW_IMAGE = "kromiose/nekro-agent:preview"
PREVIEW_COMPOSE_IMAGE = STABLE_IMAGE
PREVIEW_BACKUP_ARCHIVE_PATH = "/root/na_preview_backup.tar.gz"
NA_BACKUP_TARGETS = [
    "/var/lib/docker/volumes/nekro_postgres_data",
    "/var/lib/docker/volumes/nekro_qdrant_data",
    "/root/nekro_agent_data",
    "/root/nekro_agent",
]


class WSLManager(BackendBase):
    backend_key = "wsl"
    display_name = "WSL"

    def __init__(self, config=None, base_path=None):
        super().__init__(config=config)
        if base_path:
            self.base_path = os.path.abspath(base_path)
        else:
            if getattr(sys, 'frozen', False):
                # PyInstaller 打包后，数据文件在 _MEIPASS 或 exe 同级目录
                self.base_path = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
            else:
                self.base_path = os.path.dirname(os.path.abspath(__file__))
                if self.base_path.endswith('core'):
                    self.base_path = os.path.dirname(self.base_path)

        self.is_running = False
        self._log_process = None
        self._stop_event = threading.Event()

    def get_host_access_path(self, guest_path):
        normalized = guest_path or "/"
        return f"\\\\wsl$\\{DISTRO_NAME}{normalized}"

    @staticmethod
    def get_agent_image_ref(config=None):
        channel = "stable"
        if config is not None:
            try:
                channel = config.get("release_channel") or "stable"
            except Exception:
                channel = "stable"
        return PREVIEW_IMAGE if channel == "preview" else STABLE_IMAGE

    @classmethod
    def get_required_images(cls, deploy_mode, config=None):
        agent_ref = cls.get_agent_image_ref(config)
        base_images = REQUIRED_IMAGES_BASE.get(deploy_mode, REQUIRED_IMAGES_BASE["lite"])
        return [agent_ref if image == STABLE_IMAGE else image for image in base_images]

    @classmethod
    def get_managed_images(cls, config=None):
        agent_ref = cls.get_agent_image_ref(config)
        managed = []
        for image_ref, name, desc, modes in MANAGED_IMAGES_BASE:
            current_ref = agent_ref if image_ref == STABLE_IMAGE else image_ref
            managed.append((current_ref, name, desc, modes))
        return managed

    def get_backup_size_hint(self):
        distro = DISTRO_NAME
        existing_targets = self._existing_backup_targets(distro)
        if not existing_targets:
            return "未知"
        cmd = "du -shc " + " ".join(shlex.quote(target) for target in existing_targets) + " | tail -n1 | cut -f1"
        size = self._wsl_exec(distro, cmd, timeout=60).strip()
        return size or "未知"

    def preview_backup_exists(self):
        distro = DISTRO_NAME
        return self._wsl_exec(distro, f"test -f {shlex.quote(PREVIEW_BACKUP_ARCHIVE_PATH)} && echo yes").strip() == "yes"

    def create_runtime(self, install_dir):
        return self._create_distro(install_dir)

    def _emit_pull_progress(self, phase, message):
        self.progress_updated.emit(f"__pull_progress__|{phase}|{message}")

    def _get_local_images(self, distro):
        """获取 WSL 内已存在的 docker 镜像列表，返回 set of 'repo:tag'"""
        try:
            output = self._wsl_exec(distro, "docker images --format '{{.Repository}}:{{.Tag}}'", timeout=15)
            images = set()
            for line in output.strip().splitlines():
                line = line.strip().strip("'")
                if line and line != "<none>:<none>":
                    images.add(line)
            return images
        except Exception:
            return set()

    def _get_missing_images(self, distro, deploy_mode):
        """对比镜像清单，返回本地缺失的镜像列表"""
        required = self.get_required_images(deploy_mode, self.config)
        local = self._get_local_images(distro)

        missing = []
        for image in required:
            # 镜像名可能不带 tag，docker images 输出会带 :latest
            check_name = image if ":" in image else f"{image}:latest"
            if check_name not in local:
                missing.append(image)
        return missing

    def _pull_images(self, distro, images):
        """逐个拉取镜像列表，带进度反馈。返回 True 全部成功，False 有失败"""
        total = len(images)
        for idx, image in enumerate(images, 1):
            tag = image.split(":")[1] if ":" in image else "latest"
            image_name = image.split(":")[0]
            self._emit_pull_progress("stage", f"拉取镜像 ({idx}/{total}): {image}")
            self.log_received.emit(f"拉取镜像 ({idx}/{total}): {image}", "info")

            proc = subprocess.Popen(
                ["wsl", "-d", distro, "--", "docker", "pull", image],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=self._creation_flags(),
            )
            last_lines = []
            while True:
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                if line:
                    text = self._safe_decode(line).strip()
                    if text and not self._is_wsl_noise(text):
                        self._emit_pull_progress("update", text)
                        last_lines.append(text)
                        if len(last_lines) > 10:
                            last_lines.pop(0)

            proc.wait()
            if proc.returncode != 0:
                detail = "\n".join(last_lines[-5:]) if last_lines else ""
                err_msg = f"镜像拉取失败: {image}"
                if detail:
                    err_msg += f"\n{detail}"
                self._emit_pull_progress("error", err_msg)
                self.log_received.emit(err_msg, "error")
                return False
            self.log_received.emit(f"✓ {image} 拉取完成", "info")

        self._emit_pull_progress("done", "所有镜像拉取完成")
        self.log_received.emit("✓ 所有镜像拉取完成", "info")
        return True

    # ------------------------------------------------------------------ #
    #  环境检测
    # ------------------------------------------------------------------ #

    def get_check_funcs(self):
        """返回 4 个检测步骤的 callable 列表，每个返回 (passed, detail)"""
        ctx = {}

        def check_wsl():
            self.log_received.emit("[环境检测] 1/4 检测 WSL2...", "info")
            try:
                proc = subprocess.run(
                    ["wsl", "--status"],
                    capture_output=True, timeout=10,
                    creationflags=self._creation_flags(),
                )
                ok = (proc.returncode == 0)
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
                    ["wsl", "-d", DISTRO_NAME, "--", "bash", "-c",
                     "docker info"],
                    capture_output=True, timeout=30,
                    creationflags=self._creation_flags(),
                )
                ok = (proc.returncode == 0)
                ctx["docker"] = ok
                if ok:
                    self.log_received.emit("[环境检测] ✓ Docker 可用", "info")
                else:
                    self.log_received.emit("[环境检测] ✗ Docker 检测失败", "error")
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
                    ["wsl", "-d", DISTRO_NAME, "--", "bash", "-c",
                     "docker compose version"],
                    capture_output=True, timeout=10,
                    creationflags=self._creation_flags(),
                )
                ok = (proc.returncode == 0)
                if ok:
                    self.log_received.emit("[环境检测] ✓ Docker Compose 可用", "info")
                else:
                    self.log_received.emit("[环境检测] ✗ Docker Compose 检测失败", "error")
                return (ok, "")
            except Exception as e:
                self.log_received.emit(f"[环境检测] ✗ Docker Compose 检测异常: {e}", "error")
                return (False, "")

        return [check_wsl, check_distro, check_docker, check_compose]

    def _distro_exists(self):
        """检查 NekroAgent 专用发行版是否已存在"""
        try:
            proc = subprocess.run(
                ["wsl", "-l", "-q"],
                capture_output=True, timeout=10,
                creationflags=self._creation_flags(),
            )
            if proc.returncode != 0:
                self.log_received.emit(f"wsl -l 失败，返回码: {proc.returncode}", "debug")
                return False

            # 安全解码 wsl -l 输出
            output = self._safe_decode(proc.stdout)
            lines = [l.strip().strip('\x00') for l in output.splitlines()
                     if l.strip().strip('\x00')]
            self.log_received.emit(f"WSL 发行版列表: {lines}", "debug")
            exists = DISTRO_NAME in lines
            if exists:
                self.log_received.emit(f"找到 {DISTRO_NAME} 发行版", "debug")
            else:
                self.log_received.emit(f"未找到 {DISTRO_NAME} 发行版", "debug")
            return exists
        except Exception as e:
            self.log_received.emit(f"_distro_exists 异常: {e}", "debug")
            return False

    def _safe_decode(self, data):
        """安全解码字节数据，智能检测编码"""
        if isinstance(data, str):
            return data
        if isinstance(data, bytes):
            # 优先检测 BOM（wsl 带 BOM 的 UTF-16 输出）
            if data.startswith((b'\xff\xfe', b'\xfe\xff')):
                try:
                    return data.decode('utf-16')
                except Exception:
                    pass

            # 检测无 BOM 的 UTF-16-LE：特征是奇数字节位置有大量 0x00
            if len(data) >= 4:
                null_at_odd = sum(1 for i in range(1, len(data), 2) if data[i] == 0)
                total_odd = (len(data) + 1) // 2
                if total_odd > 0 and null_at_odd / total_odd > 0.7:
                    try:
                        return data.decode('utf-16-le')
                    except Exception:
                        pass

            # 尝试其他编码，不使用 errors='ignore' 以便能正确检测失败
            for encoding in ['utf-8', 'gbk', 'latin1']:
                try:
                    return data.decode(encoding)
                except (UnicodeDecodeError, LookupError):
                    continue

            # 最后的降级方案
            return data.decode('latin1', errors='replace')
        return str(data)

    def _is_wsl_noise(self, text):
        """判断是否为 WSL 系统噪音（如 hostname 解析警告的 UTF-16 乱码）"""
        # strip null bytes / BOM that may survive decoding
        stripped = text.strip().lstrip('\ufeff\x00')
        # 去掉残留 null 字节后再判断
        stripped_clean = stripped.replace('\x00', '')
        compact = re.sub(r"\s+", "", stripped_clean).lower()
        if compact.startswith("wsl:") or compact.startswith("wsl"):
            return True
        # 原文含大量 null 字节说明是 UTF-16 解码残留，直接过滤
        null_ratio = text.count('\x00') / len(text) if text else 0
        if null_ratio > 0.2:
            return True
        # known WSL warning substrings
        _WSL_NOISE_PATTERNS = (
            "localhost 代理配置",
            "localhost proxy",
            "NAT 模式下的 WSL",
            "NAT mode",
            "未镜像到 WSL",
        )
        lower = stripped_clean.lower()
        if any(p.lower() in lower for p in _WSL_NOISE_PATTERNS):
            return True
        # 非 ASCII 字符占比超过 30% 则认为是乱码
        non_ascii = sum(1 for c in text if ord(c) > 127)
        if len(text) > 0 and non_ascii / len(text) > 0.3:
            return True
        return False

    def _clean_stderr(self, data, max_len=500):
        """解码并清理 stderr，过滤 WSL 噪音"""
        text = self._safe_decode(data)
        lines = [l for l in text.splitlines() if l.strip() and not self._is_wsl_noise(l)]
        result = "\n".join(lines)
        return result[:max_len] if max_len else result

    def _clean_command_output(self, text, max_len=0):
        """清理命令输出，过滤空白行和 WSL 噪音。"""
        lines = []
        for line in self._safe_decode(text).splitlines():
            cleaned = line.strip()
            if not cleaned or self._is_wsl_noise(cleaned):
                continue
            lines.append(cleaned)
        result = "\n".join(lines)
        return result[:max_len] if max_len else result

    # ------------------------------------------------------------------ #
    #  专用发行版创建
    # ------------------------------------------------------------------ #

    def get_default_install_dir(self):
        """返回默认的 WSL 安装目录（优先非 C 盘）"""
        # 尝试找非 C 盘的盘符
        for drive in "DEFGH":
            if os.path.exists(f"{drive}:"):
                return f"{drive}:\\NekroAgent\\wsl"
        # 没有其他盘符，使用用户目录
        return os.path.join(os.path.expanduser("~"), "NekroAgent", "wsl")

    def _create_distro(self, install_dir):
        """下载 Ubuntu rootfs 并用 wsl --import 创建专用发行版（同步，在线程中调用）"""
        self.progress_updated.emit("准备创建 NekroAgent 运行环境...")
        self.log_received.emit("[发行版创建] 开始创建 NekroAgent 专用发行版...", "info")

        # 创建安装目录
        self.log_received.emit(f"[发行版创建] 1/4 创建安装目录: {install_dir}", "info")
        try:
            os.makedirs(install_dir, exist_ok=True)
            self.log_received.emit("[发行版创建] ✓ 安装目录已创建", "info")
        except Exception as e:
            self.log_received.emit(f"[发行版创建] ✗ 创建目录失败: {e}", "error")
            return False

        # 下载 rootfs
        self.log_received.emit("[发行版创建] 2/4 下载 Ubuntu rootfs...", "info")
        rootfs_path = os.path.join(install_dir, "rootfs.tar.gz")
        if not self._download_rootfs(rootfs_path):
            self.log_received.emit("[发行版创建] ✗ rootfs 下载失败", "error")
            return False
        self.log_received.emit("[发行版创建] ✓ rootfs 下载完成", "info")

        # wsl --import
        self.progress_updated.emit("正在导入 WSL 发行版...")
        self.log_received.emit("[发行版创建] 3/4 导入 WSL 发行版...", "info")
        try:
            proc = subprocess.run(
                ["wsl", "--import", DISTRO_NAME, install_dir, rootfs_path],
                capture_output=True, timeout=300,
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

        # 清理下载的 rootfs 文件
        try:
            os.remove(rootfs_path)
            self.log_received.emit("[发行版创建] ✓ 临时 rootfs 文件已清理", "info")
        except Exception:
            pass

        # 配置 WSL - 隔离 Windows 环境变量
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

            # 重启 WSL 发行版，使 systemd 配置生效
            self.log_received.emit("[发行版创建] 重启 WSL 发行版以启用 systemd...", "info")
            subprocess.run(
                ["wsl", "--terminate", DISTRO_NAME],
                capture_output=True, timeout=30,
                creationflags=self._creation_flags(),
            )
            time.sleep(2)
            self.log_received.emit("[发行版创建] ✓ WSL 发行版已重启", "info")
        except Exception as e:
            self.progress_updated.emit(f"配置 WSL 失败: {e}")
            self.log_received.emit(f"[发行版创建] ✗ 配置 WSL 失败: {e}", "error")
            return False

        # 保存配置
        if self.config:
            self.config.set("wsl_distro", DISTRO_NAME)
            self.config.set("wsl_install_dir", install_dir)
            self.log_received.emit("[发行版创建] ✓ 配置已保存", "info")

        self.progress_updated.emit("发行版创建成功！正在安装 Docker...")
        self.log_received.emit("[发行版创建] ✓ 发行版创建完成！开始安装 Docker...", "info")

        # 在新发行版内安装 Docker
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
                chunk_size = 256 * 1024  # 256KB

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
            """执行一个安装步骤，返回是否成功"""
            try:
                proc = subprocess.run(
                    ["wsl", "-d", distro, "--", "bash", "-c", cmd],
                    capture_output=True, timeout=timeout,
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

        # Docker 安装源（按优先级排列）
        docker_mirrors = [
            ("阿里云", "https://mirrors.aliyun.com/docker-ce"),
            ("清华大学", "https://mirrors.tuna.tsinghua.edu.cn/docker-ce"),
            ("官方源", "https://download.docker.com"),
        ]

        try:
            # 1/5 安装前置依赖
            self.progress_updated.emit("安装前置依赖...")
            self.log_received.emit("[Docker 安装] 1/5 安装前置依赖...", "info")
            if not _run_step(
                "apt-get update && apt-get install -y ca-certificates curl gnupg lsb-release",
                "前置依赖安装"
            ):
                return False
            self.log_received.emit("[Docker 安装] ✓ 前置依赖安装完成", "info")

            # 2/5 + 3/5 配置源并安装 Docker CE（多镜像源重试）
            installed = False
            for i, (mirror_name, docker_mirror) in enumerate(docker_mirrors):
                self.progress_updated.emit(f"配置 Docker 源 ({mirror_name})...")
                self.log_received.emit(
                    f"[Docker 安装] 2/5 添加 Docker GPG 密钥和源（{mirror_name}）"
                    f"{'  [重试]' if i > 0 else ''}...", "info"
                )

                # 清理旧的源配置和 apt 缓存
                if i > 0:
                    _run_step(
                        "rm -f /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.gpg && apt-get clean",
                        "清理旧源缓存"
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

            # 4/5 配置 Docker 镜像加速器
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
                "镜像加速器配置"
            ):
                self.log_received.emit("[Docker 安装] ⚠ 镜像加速器配置失败，将使用默认源", "warn")
            else:
                self.log_received.emit("[Docker 安装] ✓ 镜像加速器配置完成", "info")

            # 5/5 启动 Docker 服务（使用 systemctl，因为已启用 systemd）
            self.progress_updated.emit("启动 Docker 服务...")
            self.log_received.emit("[Docker 安装] 5/5 启动 Docker 服务...", "info")
            _run_step("systemctl daemon-reload && systemctl restart docker", "Docker 服务启动", timeout=60)
            self.log_received.emit("[Docker 安装] ✓ Docker 服务已启动", "info")

            # 等待 Docker daemon 就绪
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
                capture_output=True, timeout=30,
                creationflags=self._creation_flags(),
            )
            self.log_received.emit(f"已删除 WSL 发行版 {DISTRO_NAME}", "info")
        except Exception as e:
            self.log_received.emit(f"删除发行版失败: {e}", "error")

    # ------------------------------------------------------------------ #
    #  安装 WSL2（系统级）
    # ------------------------------------------------------------------ #

    def install_wsl(self):
        """以管理员权限安装 WSL2（通过 ShellExecute runas）"""
        self.log_received.emit("正在请求管理员权限安装 WSL2...", "info")
        try:
            import ctypes
            # --no-distribution: 仅启用 WSL 功能，不下载默认发行版（后续由程序自行导入）
            # 添加 /norestart 参数，稍后由程序控制重启
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "cmd", "/c wsl --install --no-distribution && shutdown /r /t 60 /c \"WSL 安装完成，60秒后自动重启\"", None, 1
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

    # ------------------------------------------------------------------ #
    #  服务部署
    # ------------------------------------------------------------------ #

    def start_services(self, deploy_mode):
        """部署 Docker Compose 服务"""
        if self.is_running:
            return True

        distro = DISTRO_NAME
        if not self._distro_exists():
            self.log_received.emit("NekroAgent 发行版不存在", "error")
            return False

        self._stop_event.clear()
        self.status_changed.emit("启动中...")

        # 选择 compose 文件
        if deploy_mode == "napcat":
            compose_file = "docker-compose_with_napcat.yml"
        else:
            compose_file = "docker-compose_withnot_napcat.yml"

        compose_src = os.path.join(self.base_path, "data", compose_file)
        env_src = os.path.join(self.base_path, "data", "env")

        if not os.path.exists(compose_src):
            self.log_received.emit(f"Compose 文件不存在: {compose_src}", "error")
            return False

        def _deploy():
            try:
                deploy_dir = "/root/nekro_agent"
                data_dir = "/root/nekro_agent_data"

                # 在 WSL 内创建部署目录
                self._wsl_exec(distro, f"mkdir -p {deploy_dir}")
                self._wsl_exec(distro, f"mkdir -p {data_dir}")

                # 检测是否为首次部署
                env_exists = self._wsl_exec(distro, f"test -f {deploy_dir}/.env && echo yes").strip()

                if env_exists == "yes":
                    self.log_received.emit("检测到已有部署配置，复用现有配置", "info")
                    env_content = self._wsl_exec(distro, f"cat {deploy_dir}/.env")
                else:
                    self.log_received.emit("首次部署，写入配置文件", "info")
                    self._copy_to_wsl(distro, compose_src, f"{deploy_dir}/docker-compose.yml")
                    env_content = self._prepare_env(env_src, data_dir)
                    self._write_to_wsl(distro, env_content, f"{deploy_dir}/.env")

                # 验证文件是否正确部署
                ls_output = self._wsl_exec(distro, f"ls -la {deploy_dir}")
                self.log_received.emit(f"部署目录内容:\n{ls_output}", "debug")

                self.log_received.emit("配置文件已部署到 WSL", "info")

                # 确保 Docker daemon 运行
                self.log_received.emit("确保 Docker 服务启动...", "info")
                self._wsl_exec(distro, "systemctl start docker", timeout=30)

                # 检查 docker 命令是否可用
                docker_version = self._wsl_exec(distro, "docker version")
                self.log_received.emit(f"Docker 版本:\n{docker_version}", "debug")

                # 检查 compose 命令是否可用
                compose_version = self._wsl_exec(distro, "docker compose version")
                self.log_received.emit(f"Docker Compose 版本: {compose_version}", "debug")

                # 检查并拉取缺失的镜像
                missing = self._get_missing_images(distro, deploy_mode)
                if missing:
                    self.log_received.emit(f"检测到 {len(missing)} 个镜像需要拉取...", "info")
                    self._emit_pull_progress("start", f"准备拉取 {len(missing)} 个镜像")
                    if not self._pull_images(distro, missing):
                        self.status_changed.emit("启动失败")
                        return
                else:
                    self.log_received.emit("所有镜像已就绪", "info")

                # docker compose up -d
                self.log_received.emit("启动 Docker Compose 服务...", "info")
                self.progress_updated.emit("启动 Compose 服务...")
                proc = subprocess.run(
                    ["wsl", "-d", distro, "--", "bash", "-c",
                     f"cd {deploy_dir} && docker compose -f docker-compose.yml --env-file .env up -d"],
                    capture_output=True, timeout=120,
                    creationflags=self._creation_flags(),
                )

                if proc.returncode != 0:
                    # 详细输出错误信息
                    self.log_received.emit(f"返回码: {proc.returncode}", "error")
                    self.log_received.emit(f"部署目录: {deploy_dir}", "error")
                    self.log_received.emit(f"STDOUT:\n{self._clean_stderr(proc.stdout, 0)}", "error")
                    self.log_received.emit(f"STDERR:\n{self._clean_stderr(proc.stderr, 0)}", "error")
                    self.log_received.emit("Compose 启动失败，详见上方日志", "error")
                    self.status_changed.emit("启动失败")
                    return

                self.is_running = True
                self.log_received.emit("Compose 服务已启动，等待就绪...", "info")

                # 从 .env 解析凭据
                is_first_deploy = env_exists != "yes"
                deploy_info = self._parse_deploy_info(env_content, deploy_mode)

                if is_first_deploy:
                    if deploy_mode == "napcat":
                        # napcat 模式：等 token 捕获后再弹窗
                        self._pending_deploy_info = deploy_info
                    else:
                        # lite 模式：直接弹窗
                        self._show_deploy_info(deploy_info)
                else:
                    self._refresh_deploy_info(deploy_info)

                threading.Thread(target=self._log_reader, args=(distro, deploy_dir), daemon=True).start()
                threading.Thread(target=self._health_check, daemon=True).start()

            except Exception as e:
                self.log_received.emit(f"部署失败: {e}", "error")
                self.status_changed.emit("启动失败")

        threading.Thread(target=_deploy, daemon=True).start()
        return True

    def stop_services(self):
        """停止 Docker Compose 服务"""
        self._stop_event.set()
        was_running = self.is_running
        self.is_running = False

        if self._log_process and self._log_process.poll() is None:
            try:
                self._log_process.terminate()
            except Exception:
                pass
            self._log_process = None

        if not was_running:
            self.status_changed.emit("已停止")
            return

        distro = DISTRO_NAME
        self.log_received.emit("正在停止服务...", "info")

        def _do_stop():
            try:
                wsl_home = self._wsl_exec(distro, "echo $HOME").strip()
                if not wsl_home:
                    wsl_home = "/root"
                deploy_dir = f"{wsl_home}/nekro_agent"

                subprocess.run(
                    ["wsl", "-d", distro, "--", "bash", "-c",
                     f"cd {deploy_dir} && docker compose -f docker-compose.yml stop"],
                    capture_output=True, timeout=60,
                    creationflags=self._creation_flags(),
                )
                self.log_received.emit("服务已停止", "info")

                # 关闭 NekroAgent 发行版
                self.log_received.emit(f"关闭 {distro} 发行版...", "info")
                subprocess.run(["wsl", "--terminate", distro], capture_output=True, timeout=30, creationflags=self._creation_flags())
                self.log_received.emit(f"{distro} 已关闭", "info")
            except subprocess.TimeoutExpired:
                self.log_received.emit("停止服务超时", "warn")
            except Exception as e:
                self.log_received.emit(f"停止服务异常: {e}", "error")
            finally:
                self.status_changed.emit("已停止")

        threading.Thread(target=_do_stop, daemon=True).start()

    def check_images_status(self, only_image=None, _mock_has_update=False):
        """检测 MANAGED_IMAGES 各镜像本地/远程状态，结果通过 image_status_result 信号发出
        only_image: 若传入则只检测该 image_ref
        _mock_has_update: 测试用，强制所有镜像返回有更新"""
        import json
        distro = DISTRO_NAME

        if _mock_has_update or FORCE_MOCK_IMAGE_UPDATES:
            results = [
                {"image": ref, "name": name, "modes": modes,
                 "local": "sha256:aabbccdd11", "remote": "sha256:eeff99887",
                 "has_update": True, "error": None}
                for ref, name, desc, modes in self.get_managed_images(self.config)
                if not only_image or ref == only_image
            ]
            self.image_status_result.emit(results)
            return
        import json
        distro = DISTRO_NAME

        def _do_check():
            results = []
            for image_ref, name, desc, modes in self.get_managed_images(self.config):
                if only_image and image_ref != only_image:
                    continue
                image = image_ref.split(":")[0]
                tag = image_ref.split(":")[1] if ":" in image_ref else "latest"
                entry = {"image": image_ref, "name": name, "modes": modes,
                         "local": None, "remote": None, "has_update": False, "error": None}
                try:
                    # 本地 digest
                    local_out = self._wsl_exec(
                        distro,
                        f"docker image inspect {image}:{tag} --format '{{{{index .RepoDigests 0}}}}' 2>/dev/null",
                        timeout=15,
                    ).strip().strip("'")
                    if local_out and "@" in local_out:
                        full_local = local_out.split("@")[-1]
                        entry["local"] = full_local[:19]
                    else:
                        full_local = ""
                        entry["local"] = None

                    # 远程 digest via Docker Hub
                    token_req = Request(
                        f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{image}:pull",
                        headers={"User-Agent": "NekroAgent/1.0"},
                    )
                    with urlopen(token_req, timeout=10) as resp:
                        token = json.loads(resp.read())["token"]
                    manifest_req = Request(
                        f"https://registry-1.docker.io/v2/{image}/manifests/{tag}",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/vnd.docker.distribution.manifest.v2+json",
                            "User-Agent": "NekroAgent/1.0",
                        },
                    )
                    with urlopen(manifest_req, timeout=10) as resp:
                        full_remote = resp.headers.get("Docker-Content-Digest", "")
                        entry["remote"] = full_remote[:19] if full_remote else ""

                    if full_remote:
                        entry["has_update"] = full_remote != full_local
                except Exception as e:
                    entry["error"] = str(e)
                results.append(entry)
            self.image_status_result.emit(results)

        threading.Thread(target=_do_check, daemon=True).start()


    def pull_single_image(self, image_ref):
        """拉取单个镜像，结果通过 image_pull_result 信号发出"""
        distro = DISTRO_NAME

        def _do_pull():
            image = image_ref.split(":")[0]
            tag = image_ref.split(":")[1] if ":" in image_ref else "latest"
            self._emit_pull_progress("start", f"拉取镜像: {image_ref}")
            ok = self._pull_images(distro, [f"{image}:{tag}"])
            if ok:
                self.image_pull_result.emit(image_ref, True, "拉取成功")
            else:
                self.image_pull_result.emit(image_ref, False, "拉取失败")

        threading.Thread(target=_do_pull, daemon=True).start()

    def run_remote_update(self):
        """从远端 JSON 拉取更新步骤并执行，optional 步骤通过信号询问用户确认"""
        from core.update_runner import parse_update_commands
        distro = DISTRO_NAME
        deploy_dir = "/root/nekro_agent"

        def _exec(cmd, timeout=300):
            """在 deploy_dir 下执行命令，返回 (returncode, output)"""
            proc = subprocess.run(
                ["wsl", "-d", distro, "--", "bash", "-c", f"cd {deploy_dir} && {cmd}"],
                capture_output=True, timeout=timeout,
                creationflags=self._creation_flags(),
            )
            out = self._clean_command_output(self._safe_decode(proc.stdout) + self._safe_decode(proc.stderr))
            return proc.returncode, out.strip()

        def _do_update():
            self._stop_event.clear()
            self.status_changed.emit("更新中...")
            steps = parse_update_commands(self.log_received.emit)
            if not steps:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "无法获取更新配置，请检查网络。")
                return

            for step in steps:
                step_type = step.get("type")
                label = step.get("label", "")
                cmd = step.get("_cmd")

                # notify 步骤只输出日志
                if step_type == "notify":
                    self.log_received.emit(step.get("message", label), "info")
                    continue

                # optional 步骤：先查大小再弹窗确认
                if step.get("optional"):
                    size_cmd = step.get("optional_size_cmd", "")
                    backup_size = "未知"
                    if size_cmd:
                        _, size_out = _exec(size_cmd, timeout=30)
                        if size_out:
                            backup_size = size_out.splitlines()[0].strip()
                    prompt = step.get("optional_prompt", f"是否执行：{label}？")
                    prompt = prompt.replace("{backup_size}", backup_size)

                    # 通过信号让 UI 弹窗，主线程会设置 _update_optional_reply
                    self._update_optional_reply = None
                    self.update_optional_confirm.emit(label, prompt)
                    # 等待 UI 回复（最多 120 秒）
                    import time as _time
                    deadline = _time.time() + 120
                    while self._update_optional_reply is None and _time.time() < deadline:
                        _time.sleep(0.1)
                    if self._update_optional_reply is None:
                        self.log_received.emit(f"[更新] 可选步骤确认超时，已跳过: {label}", "warning")
                        continue
                    if not self._update_optional_reply:
                        self.log_received.emit(f"[更新] 已跳过可选步骤: {label}", "info")
                        continue

                if cmd is None:
                    continue

                self.log_received.emit(f"[更新] 执行: {label}", "info")
                self._emit_pull_progress("stage", label)

                # pull 步骤用带进度的拉取
                if step_type == "pull":
                    image = step.get("image", "")
                    ok = self._pull_images(distro, [image])
                    if not ok:
                        self.status_changed.emit("更新失败")
                        self.update_finished.emit(False, f"镜像拉取失败: {image}")
                        return
                    continue

                # 其余命令直接执行
                try:
                    rc, out = _exec(cmd, timeout=300)
                except subprocess.TimeoutExpired:
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, f"步骤超时: {label}")
                    return
                except Exception as e:
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, f"步骤异常: {e}")
                    return

                if out:
                    self.log_received.emit(out, "info")
                if rc != 0:
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, f"步骤失败（返回码 {rc}）: {label}")
                    return

                self.log_received.emit(f"[更新] ✓ {label}", "info")

            self._emit_pull_progress("done", "更新完成")
            self.is_running = True
            if self._log_process is None or self._log_process.poll() is not None:
                threading.Thread(target=self._log_reader, args=(distro, deploy_dir), daemon=True).start()
            threading.Thread(target=self._health_check, daemon=True).start()
            self.update_finished.emit(True, "Nekro Agent 更新完成，正在等待服务重新就绪。")

        threading.Thread(target=_do_update, daemon=True).start()

    def switch_to_preview(self, create_backup=True):
        """备份数据与配置后，将 Nekro Agent 主容器切换到预览版镜像。"""
        distro = DISTRO_NAME
        deploy_dir = "/root/nekro_agent"

        def _exec(cmd, timeout=300):
            proc = subprocess.run(
                ["wsl", "-d", distro, "--", "bash", "-c", f"cd {deploy_dir} && {cmd}"],
                capture_output=True,
                timeout=timeout,
                creationflags=self._creation_flags(),
            )
            out = self._clean_command_output(self._safe_decode(proc.stdout) + self._safe_decode(proc.stderr))
            return proc.returncode, out.strip()

        def _emit_cmd_failure(prefix, rc, output):
            self.status_changed.emit("更新失败")
            message = f"{prefix}（返回码 {rc}）"
            if output:
                message += f"\n{output}"
            self.update_finished.emit(False, message)

        def _rewrite_compose_to_preview():
            compose_path = f"{deploy_dir}/docker-compose.yml"
            self._emit_pull_progress("stage", "写入预览版镜像配置")
            compose_content = self._wsl_exec(distro, f"cat {compose_path}")
            if PREVIEW_IMAGE in compose_content:
                return True
            if PREVIEW_COMPOSE_IMAGE not in compose_content:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "未找到稳定版镜像引用，无法切换到预览版。")
                return False

            updated_content = compose_content.replace(PREVIEW_COMPOSE_IMAGE, PREVIEW_IMAGE)
            self._write_to_wsl(distro, updated_content, compose_path)
            return True

        def _do_switch():
            self._stop_event.clear()
            self.status_changed.emit("更新中...")
            self.log_received.emit("[预览版] 开始切换到预览版 Nekro Agent", "info")

            try:
                if create_backup:
                    self._emit_pull_progress("stage", "备份数据与配置")
                    ok, backup_message = self._backup_nekro_archive(distro, PREVIEW_BACKUP_ARCHIVE_PATH)
                    if not ok:
                        self.status_changed.emit("更新失败")
                        self.update_finished.emit(False, backup_message)
                        return
                    self.log_received.emit(f"[预览版] {backup_message}", "info")
                else:
                    self.log_received.emit("[预览版] 已按用户选择跳过备份，后续将无法恢复到正式版。", "warning")

                self.log_received.emit("[预览版] 拉取预览版镜像", "info")
                self._emit_pull_progress("stage", "拉取预览版镜像")
                if not self._pull_images(distro, [PREVIEW_IMAGE]):
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, f"镜像拉取失败: {PREVIEW_IMAGE}")
                    return

                if not _rewrite_compose_to_preview():
                    return

                self._emit_pull_progress("stage", "重建 Nekro Agent 主容器")
                rc, out = _exec("docker compose -f docker-compose.yml --env-file .env up -d --no-deps --force-recreate nekro_agent")
                if rc != 0:
                    _emit_cmd_failure("重建 Nekro Agent 主容器失败", rc, out)
                    return
                if out:
                    self.log_received.emit(out, "info")

                self._emit_pull_progress("done", "预览版切换完成")
                self.is_running = True
                if self.config:
                    self.config.set("release_channel", "preview")
                    self.config.set("preview_backup_available", bool(create_backup))
                if self._log_process is None or self._log_process.poll() is not None:
                    threading.Thread(target=self._log_reader, args=(distro, deploy_dir), daemon=True).start()
                threading.Thread(target=self._health_check, daemon=True).start()
                self.update_finished.emit(True, "预览版切换完成，正在等待服务重新就绪。")
            except subprocess.TimeoutExpired:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "切换到预览版超时。")
            except Exception as e:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, f"切换到预览版异常: {e}")

        threading.Thread(target=_do_switch, daemon=True).start()

    def restore_stable_from_backup(self):
        """从预览版备份恢复正式版。"""
        distro = DISTRO_NAME
        deploy_dir = "/root/nekro_agent"

        def _exec(cmd, timeout=300):
            proc = subprocess.run(
                ["wsl", "-d", distro, "--", "bash", "-c", f"cd {deploy_dir} && {cmd}"],
                capture_output=True,
                timeout=timeout,
                creationflags=self._creation_flags(),
            )
            out = self._clean_command_output(self._safe_decode(proc.stdout) + self._safe_decode(proc.stderr))
            return proc.returncode, out.strip()

        def _rewrite_compose_to_stable():
            compose_path = f"{deploy_dir}/docker-compose.yml"
            self._emit_pull_progress("stage", "写回正式版镜像配置")
            compose_content = self._wsl_exec(distro, f"cat {compose_path}")
            if STABLE_IMAGE in compose_content and PREVIEW_IMAGE not in compose_content:
                return True
            if PREVIEW_IMAGE not in compose_content:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "未找到预览版镜像引用，无法恢复正式版。")
                return False
            updated_content = compose_content.replace(PREVIEW_IMAGE, STABLE_IMAGE)
            self._write_to_wsl(distro, updated_content, compose_path)
            return True

        def _do_restore():
            self._stop_event.clear()
            self.status_changed.emit("更新中...")
            self.log_received.emit("[恢复正式版] 开始从预览版备份恢复正式版", "info")

            if self.config and not self.config.get("preview_backup_available"):
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "当前预览版是在未备份的情况下切换的，无法恢复到正式版。")
                return

            if not self.preview_backup_exists():
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, f"未找到备份文件：{PREVIEW_BACKUP_ARCHIVE_PATH}")
                return

            try:
                self._emit_pull_progress("stage", "停止相关服务")
                rc, out = _exec("docker compose -f docker-compose.yml stop nekro_agent nekro_postgres nekro_qdrant nekro_napcat 2>/dev/null || true", timeout=120)
                if out:
                    self.log_received.emit(out, "info")

                self._emit_pull_progress("stage", "恢复备份数据")
                rc, out = _exec(f"tar -xzf {shlex.quote(PREVIEW_BACKUP_ARCHIVE_PATH)} -C /", timeout=600)
                if rc != 0:
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, f"恢复备份失败\n{out}" if out else "恢复备份失败")
                    return

                self.log_received.emit("[恢复正式版] 拉取正式版镜像", "info")
                self._emit_pull_progress("stage", "拉取正式版镜像")
                if not self._pull_images(distro, [STABLE_IMAGE]):
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, f"镜像拉取失败: {STABLE_IMAGE}")
                    return

                if not _rewrite_compose_to_stable():
                    return

                self._emit_pull_progress("stage", "重建并启动服务")
                rc, out = _exec("docker compose -f docker-compose.yml --env-file .env up -d", timeout=300)
                if rc != 0:
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, f"恢复正式版失败\n{out}" if out else "恢复正式版失败")
                    return
                if out:
                    self.log_received.emit(out, "info")

                self._emit_pull_progress("done", "正式版恢复完成")
                self.is_running = True
                if self.config:
                    self.config.set("release_channel", "stable")
                    self.config.set("preview_backup_available", False)
                if self._log_process is None or self._log_process.poll() is not None:
                    threading.Thread(target=self._log_reader, args=(distro, deploy_dir), daemon=True).start()
                threading.Thread(target=self._health_check, daemon=True).start()
                self.update_finished.emit(True, "正式版恢复完成，正在等待服务重新就绪。")
            except subprocess.TimeoutExpired:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "恢复正式版超时。")
            except Exception as e:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, f"恢复正式版异常: {e}")

        threading.Thread(target=_do_restore, daemon=True).start()

    def reply_update_optional(self, confirmed: bool):
        """UI 调用此方法回复 optional 步骤的用户选择"""
        self._update_optional_reply = confirmed

    def _existing_backup_targets(self, distro):
        existing = []
        for target in NA_BACKUP_TARGETS:
            if self._wsl_exec(distro, f"test -d {shlex.quote(target)} && echo yes").strip() == "yes":
                existing.append(target)
        return existing

    def _backup_nekro_archive(self, distro, archive_path):
        existing_targets = self._existing_backup_targets(distro)
        if not existing_targets:
            return False, "未找到可备份的目录。"

        target_args = " ".join(shlex.quote(target.lstrip("/")) for target in existing_targets)
        cmd = f"rm -f {shlex.quote(archive_path)} && tar -czf {shlex.quote(archive_path)} -C / {target_args}"
        rc = subprocess.run(
            ["wsl", "-d", distro, "--", "bash", "-c", cmd],
            capture_output=True,
            timeout=600,
            creationflags=self._creation_flags(),
        )
        out = self._safe_decode(rc.stdout) + self._safe_decode(rc.stderr)
        if rc.returncode != 0:
            message = "备份失败"
            cleaned = out.strip()
            if cleaned:
                message += f"\n{cleaned}"
            return False, message
        return True, f"已生成备份归档：{archive_path}"


    def uninstall_environment(self):
        """卸载：停止服务 → 删除容器/镜像 → 删除 WSL 发行版"""
        distro = DISTRO_NAME
        self.log_received.emit("开始卸载环境...", "info")
        self.status_changed.emit("卸载中...")

        # 先同步停止日志和状态
        self._stop_event.set()
        self.is_running = False
        if self._log_process and self._log_process.poll() is None:
            try:
                self._log_process.terminate()
            except Exception:
                pass
            self._log_process = None

        def _do_uninstall():
            try:
                wsl_home = self._wsl_exec(distro, "echo $HOME").strip()
                if not wsl_home:
                    wsl_home = "/root"
                deploy_dir = f"{wsl_home}/nekro_agent"

                # 1. 停止并删除容器
                self.log_received.emit("[卸载] 1/3 停止并删除容器...", "info")
                self._wsl_exec(distro,
                    f"cd {deploy_dir} && docker compose -f docker-compose.yml down -v 2>/dev/null; "
                    "docker system prune -af 2>/dev/null",
                    timeout=120)
                self.log_received.emit("[卸载] ✓ 容器已清除", "info")

                # 2. 删除部署目录
                self.log_received.emit("[卸载] 2/3 清理部署文件...", "info")
                self._wsl_exec(distro, f"rm -rf {deploy_dir}")
                self.log_received.emit("[卸载] ✓ 部署文件已清理", "info")

                # 3. 删除 WSL 发行版
                self.log_received.emit("[卸载] 3/3 删除 WSL 发行版...", "info")
                self.remove_distro()
                self.log_received.emit("[卸载] ✓ 环境卸载完成", "info")

                # 清除配置
                if self.config:
                    self.config.set("first_run", True)
                    self.config.set("deploy_mode", "")
                    self.config.set("wsl_distro", "")
                    self.config.set("wsl_install_dir", "")
                    self.config.set("deploy_info", None)

                self.status_changed.emit("已卸载")

            except Exception as e:
                self.log_received.emit(f"卸载异常: {e}", "error")
                self.status_changed.emit("卸载失败")

        threading.Thread(target=_do_uninstall, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  日志流
    # ------------------------------------------------------------------ #

    def _log_reader(self, distro, deploy_dir):
        """通过 docker compose logs -f 流式读取日志"""
        napcat_token_pattern = re.compile(r'WebUi.*token=([a-zA-Z0-9]+)')
        try:
            self._log_process = subprocess.Popen(
                ["wsl", "-d", distro, "--", "bash", "-c",
                 f"cd {deploy_dir} && docker compose -f docker-compose.yml logs -f --tail=50"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=self._creation_flags(),
            )

            for line in iter(self._log_process.stdout.readline, b''):
                if self._stop_event.is_set():
                    break
                text = line.decode("utf-8", errors="ignore").rstrip()
                if text:
                    self.log_received.emit(text, "vm")
                    # 捕获 NapCat WebUI token
                    m = napcat_token_pattern.search(text)
                    if m and self.config:
                        token = m.group(1)
                        info = self.config.get("deploy_info") or {}
                        if info.get("napcat_token") != token:
                            info["napcat_token"] = token
                            self.config.set("deploy_info", info)
                            self.log_received.emit(f"[NapCat] 已捕获 WebUI Token: {token}", "info")
                            # 首次部署等待 token 后弹窗
                            if hasattr(self, '_pending_deploy_info') and self._pending_deploy_info:
                                self._pending_deploy_info["napcat_token"] = token
                                self._show_deploy_info(self._pending_deploy_info)
                                self._pending_deploy_info = None

        except Exception as e:
            if not self._stop_event.is_set():
                self.log_received.emit(f"日志读取异常: {e}", "debug")
        finally:
            if self._log_process and self._log_process.poll() is None:
                try:
                    self._log_process.terminate()
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    #  健康检查
    # ------------------------------------------------------------------ #

    def _health_check(self):
        """轮询 Nekro Agent 服务直到返回 200"""
        nekro_port = self.config.get("nekro_port") or 8021
        timeout = 300
        start = time.time()
        interval = 2.0

        while time.time() - start < timeout and not self._stop_event.is_set():
            try:
                resp = urlopen(f"http://localhost:{nekro_port}", timeout=5)
                if resp.status == 200:
                    elapsed = time.time() - start
                    self.log_received.emit(f"服务已就绪！(耗时 {elapsed:.1f}s)", "info")
                    self.boot_finished.emit()
                    self.status_changed.emit("运行中")
                    return
            except Exception:
                pass

            time.sleep(interval)

        if not self._stop_event.is_set():
            self.log_received.emit("服务启动超时，请检查日志", "error")
            self.status_changed.emit("启动超时")

    # ------------------------------------------------------------------ #
    #  工具方法
    # ------------------------------------------------------------------ #

    def _wsl_exec(self, distro, cmd, timeout=60):
        """在 WSL 发行版中执行命令并返回 stdout"""
        try:
            proc = subprocess.run(
                ["wsl", "-d", distro, "--", "bash", "-c", cmd],
                capture_output=True, timeout=timeout,
                creationflags=self._creation_flags(),
            )
            return self._safe_decode(proc.stdout)
        except Exception:
            return ""

    def _copy_to_wsl(self, distro, local_path, wsl_path):
        """将 Windows 本地文件复制到 WSL 内"""
        win_path = os.path.abspath(local_path).replace("\\", "/")
        wsl_win_path = self._wsl_exec(distro, f'wslpath "{win_path}"').strip()
        if wsl_win_path:
            self._wsl_exec(distro, f'cp "{wsl_win_path}" "{wsl_path}"')
        else:
            drive = win_path[0].lower()
            mnt_path = f"/mnt/{drive}{win_path[2:]}"
            self._wsl_exec(distro, f'cp "{mnt_path}" "{wsl_path}"')

    def _write_to_wsl(self, distro, content, wsl_path):
        """将字符串内容写入 WSL 内文件"""
        import base64
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        self._wsl_exec(distro, f'echo "{encoded}" | base64 -d > "{wsl_path}"')

    def _show_deploy_info(self, info):
        """保存凭据并发送信号给 UI 弹窗"""
        if self.config:
            self.config.set("deploy_info", info)

        self.log_received.emit("=== 部署完成！===", "info")
        self.log_received.emit(f"管理员账号: admin | 密码: {info['admin_password']}", "info")
        self.log_received.emit(f"Web 访问地址: http://127.0.0.1:{info['port']}", "info")

        self.deploy_info_ready.emit(info)

    def _refresh_deploy_info(self, info):
        """非首次启动时静默刷新凭据（不弹窗），防止上次中途退出丢失"""
        if self.config:
            old_info = self.config.get("deploy_info") or {}
            if old_info.get("napcat_token"):
                info["napcat_token"] = old_info["napcat_token"]
            self.config.set("deploy_info", info)

    def _parse_deploy_info(self, env_content, deploy_mode):
        """从 .env 内容中解析部署凭据信息"""
        env_vars = {}
        for line in env_content.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env_vars[key.strip()] = value.strip()

        info = {
            "port": env_vars.get("NEKRO_EXPOSE_PORT", "8021"),
            "admin_password": env_vars.get("NEKRO_ADMIN_PASSWORD", ""),
            "onebot_token": env_vars.get("ONEBOT_ACCESS_TOKEN", ""),
            "deploy_mode": deploy_mode,
        }
        if deploy_mode == "napcat":
            info["napcat_port"] = env_vars.get("NAPCAT_EXPOSE_PORT", "6099")

        return info

    def _prepare_env(self, env_template_path, data_dir):
        """读取 env 模板文件，填充必要值，返回最终 .env 内容"""
        content = ""
        if os.path.exists(env_template_path):
            with open(env_template_path, "r", encoding="utf-8") as f:
                content = f.read()

        lines = content.splitlines()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                new_lines.append(line)
                continue

            key = stripped.split("=", 1)[0].strip()
            value = stripped.split("=", 1)[1].strip()

            if key == "NEKRO_DATA_DIR" and not value:
                new_lines.append(f"NEKRO_DATA_DIR={data_dir}")
            elif key == "QDRANT_API_KEY" and not value:
                new_lines.append(f"QDRANT_API_KEY={self._random_token(32)}")
            elif key == "ONEBOT_ACCESS_TOKEN" and not value:
                new_lines.append(f"ONEBOT_ACCESS_TOKEN={self._random_token(32)}")
            elif key == "NEKRO_ADMIN_PASSWORD" and not value:
                new_lines.append(f"NEKRO_ADMIN_PASSWORD={self._random_token(16)}")
            elif key == "NEKRO_EXPOSE_PORT":
                new_lines.append(f"NEKRO_EXPOSE_PORT={self.config.get('nekro_port') or 8021}")
            elif key == "NAPCAT_EXPOSE_PORT":
                new_lines.append(f"NAPCAT_EXPOSE_PORT={self.config.get('napcat_port') or 6099}")
            else:
                new_lines.append(line)

        return "\n".join(new_lines) + "\n"

    @staticmethod
    def _random_token(length=32):
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(length))

    @staticmethod
    def _creation_flags():
        """返回 Windows 平台的进程创建标志，打包后隐藏控制台窗口"""
        if sys.platform == "win32" and getattr(sys, 'frozen', False):
            return 0x08000000  # CREATE_NO_WINDOW
        return 0
