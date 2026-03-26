import ctypes
import os
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time

from core.backend_base import BackendBase
from core.hyperv_manager import HyperVManager
from core.mirror_config import (
    APT_MIRROR_LINES,
    DOCKER_APT_MIRRORS,
    DOCKER_REGISTRY_MIRRORS,
    UBUNTU_CLOUD_IMAGE_URLS,
)
from core.runtime_image_fetcher import RuntimeImageFetcher
from core.ssh_transport import SSHTransport


class HyperVBackend(BackendBase):
    backend_key = "hyperv"
    display_name = "Hyper-V"

    def __init__(self, config=None, parent=None):
        super().__init__(config=config, parent=parent)
        self.vm_name = self.config.get("hyperv_vm_name")
        self.switch_name = self.config.get("hyperv_switch_name")
        self.nat_name = self.config.get("hyperv_nat_name")
        self.subnet = self.config.get("hyperv_subnet")
        self.gateway_ip = self.config.get("hyperv_gateway_ip")
        self.guest_ip = self.config.get("hyperv_guest_ip")
        self.ssh_port = self.config.get("hyperv_ssh_port") or 22
        self.username = self.config.get("hyperv_username") or "nekro"
        self.base_path = self._resolve_base_path()
        self._stop_event = threading.Event()
        self._pending_deploy_info = None
        self.manager = HyperVManager(self.vm_name, self.switch_name, self.nat_name, self.subnet)
        self.transport = SSHTransport(
            self.guest_ip,
            self.username,
            port=self.ssh_port,
            private_key=self.config.get("hyperv_ssh_key_path") or None,
        )

    def _emit_pull_progress(self, phase, message):
        self.progress_updated.emit(f"__pull_progress__|{phase}|{message}")

    def get_check_funcs(self):
        """返回 4 个检测步骤的 callable 列表，每个返回 (passed, detail)"""
        ctx = {}

        def check_hyperv():
            edition = self.manager.get_windows_edition() or "Unknown"
            is_home = self.manager.is_home_edition()
            self.log_received.emit(f"[环境检测] Windows 版本: {edition}", "info")

            hyperv_enabled = self.manager.is_hyperv_enabled()
            management_available = self.manager.is_hyperv_management_available()

            if hyperv_enabled:
                self.log_received.emit("[环境检测] ✓ Hyper-V 已启用", "info")
            elif is_home and self.manager.can_force_enable_on_home():
                self.log_received.emit("[环境检测] ✗ Hyper-V 未启用，检测到家庭版，可尝试强制启用", "warning")
            else:
                self.log_received.emit("[环境检测] ✗ Hyper-V 未启用", "error")

            if management_available:
                self.log_received.emit("[环境检测] ✓ Hyper-V 管理命令可用", "info")
            else:
                self.log_received.emit("[环境检测] ✗ Hyper-V 管理命令不可用", "warning")

            ok = hyperv_enabled and management_available
            ctx["hyperv"] = ok
            detail = ""
            if not ok and is_home and self.manager.can_force_enable_on_home():
                detail = "家庭版可尝试强制启用"
            return (ok, detail)

        def check_vm():
            cache_dir = self._runtime_cache_dir()
            image_path = os.path.join(cache_dir, "ubuntu-hyperv.vhdx")
            if os.path.exists(image_path):
                self.log_received.emit("[环境检测] ✓ 已发现本地基础镜像缓存", "info")
            else:
                self.log_received.emit("[环境检测] 未发现本地基础镜像缓存，创建时会尝试下载", "warning")

            vm_exists = self.manager.vm_exists()
            if vm_exists:
                self.log_received.emit(f"[环境检测] ✓ 虚拟机 {self.vm_name} 已存在", "info")
                ctx["vm"] = True
                return (True, self.vm_name)
            self.log_received.emit(f"[环境检测] ✗ 虚拟机 {self.vm_name} 不存在", "warning")
            return (False, "未创建")

        def check_docker():
            if not ctx.get("hyperv") or not ctx.get("vm"):
                return (False, "")

            key_ready = self._ssh_key_ready()
            if key_ready:
                self.log_received.emit("[环境检测] ✓ SSH 密钥已准备", "info")
            else:
                self.log_received.emit("[环境检测] ✗ SSH 密钥未准备", "warning")
                return (False, "SSH 未就绪")

            ssh_ready = self.wait_for_ssh_ready(timeout=12)
            if not ssh_ready:
                self.log_received.emit("[环境检测] ✗ SSH 尚未就绪", "warning")
                return (False, "SSH 未就绪")

            self.log_received.emit("[环境检测] ✓ SSH 初始化已完成", "info")
            docker_ready = self._guest_command_ok("docker info", timeout=30)
            if docker_ready:
                self.log_received.emit("[环境检测] ✓ Docker 可用", "info")
                ctx["docker"] = True
                return (True, "SSH 已就绪")
            self.log_received.emit("[环境检测] ✗ Docker 不可用", "warning")
            return (False, "Docker 不可用")

        def check_compose():
            if not ctx.get("docker"):
                return (False, "")
            compose_ready = self._guest_command_ok("docker compose version", timeout=20)
            if compose_ready:
                self.log_received.emit("[环境检测] ✓ Docker Compose 可用", "info")
                return (True, "")
            self.log_received.emit("[环境检测] ✗ Docker Compose 不可用", "warning")
            return (False, "")

        return [check_hyperv, check_vm, check_docker, check_compose]

    def check_environment(self):
        funcs = self.get_check_funcs()
        results = [func() for func in funcs]

        all_ok = all(r[0] for r in results)
        if all_ok:
            self.log_received.emit("[环境检测] ✓ Hyper-V 运行环境已就绪", "info")
        else:
            self.log_received.emit("[环境检测] ✗ Hyper-V 运行环境未完成初始化", "warning")

        return {
            "wsl_installed": results[0][0],
            "distro": results[1][1] if results[1][0] else "",
            "docker_available": results[2][0],
            "compose_available": results[3][0],
        }

    def get_default_install_dir(self):
        configured = self.config.get("hyperv_install_dir")
        if configured:
            return configured
        return os.path.join(os.path.expanduser("~"), "NekroAgent", "hyperv")

    def create_runtime(self, install_dir):
        self.progress_updated.emit("准备创建 Hyper-V 运行环境...")
        self.log_received.emit("[Hyper-V] 开始创建运行环境", "info")

        if not self.manager.is_hyperv_enabled():
            self.log_received.emit("[Hyper-V] Hyper-V 未启用，无法继续", "error")
            return False
        if not self.manager.is_hyperv_management_available():
            self.log_received.emit("[Hyper-V] Hyper-V 管理命令不可用，请先重启系统后重试", "error")
            return False
        if not self._command_available("ssh") or not self._command_available("scp") or not self._command_available("ssh-keygen"):
            self.log_received.emit("[Hyper-V] 系统缺少 OpenSSH 客户端组件，请先启用 ssh/scp/ssh-keygen", "error")
            return False

        try:
            os.makedirs(install_dir, exist_ok=True)
        except Exception as exc:
            self.log_received.emit(f"[Hyper-V] 创建目录失败: {exc}", "error")
            return False

        base_image = os.path.join(self._runtime_cache_dir(), "ubuntu-hyperv.vhd")
        base_vhdx = base_image.replace(".vhd", ".vhdx")
        if not os.path.exists(base_image) and not os.path.exists(base_vhdx):
            # 下载 VHD tar.gz 并解压
            tar_path = base_image + ".tar.gz"
            fetcher = RuntimeImageFetcher(
                UBUNTU_CLOUD_IMAGE_URLS,
                log=self.log_received.emit,
                progress=self.progress_updated.emit,
            )
            if not fetcher.download(tar_path):
                self.log_received.emit("[Hyper-V] 基础镜像下载失败", "error")
                return False

            self.progress_updated.emit("正在解压基础镜像...")
            try:
                import tarfile
                with tarfile.open(tar_path, "r:gz") as tar:
                    for member in tar.getmembers():
                        if member.name.endswith(".vhd"):
                            member.name = os.path.basename(base_image)
                            tar.extract(member, path=os.path.dirname(base_image))
                            break
                    else:
                        self.log_received.emit("[Hyper-V] tar.gz 中未找到 .vhd 文件", "error")
                        return False
                os.remove(tar_path)
            except Exception as exc:
                self.log_received.emit(f"[Hyper-V] 解压失败: {exc}", "error")
                return False
            self.log_received.emit("[Hyper-V] 基础镜像准备完成", "info")

        self.progress_updated.emit("准备 SSH 密钥...")
        key_path = self._ensure_ssh_keypair(install_dir)
        if not key_path:
            return False

        self.progress_updated.emit("配置虚拟交换机和 NAT...")
        if not self.manager.ensure_switch():
            self.log_received.emit("[Hyper-V] 创建或复用虚拟交换机失败", "error")
            return False
        if not self.manager.ensure_nat(self.gateway_ip):
            self.log_received.emit("[Hyper-V] 创建或复用 NAT 失败", "error")
            return False

        self.progress_updated.emit("创建虚拟机...")
        ok, vm_vhdx = self.manager.create_vm(install_dir, base_image)
        if not ok:
            self.log_received.emit("[Hyper-V] 创建虚拟机失败", "error")
            return False
        self.log_received.emit(f"[Hyper-V] 虚拟磁盘路径: {vm_vhdx}", "info")

        mac_address = self.manager.get_vm_mac_address()
        if not mac_address:
            self.log_received.emit("[Hyper-V] 读取虚拟机网卡 MAC 地址失败", "error")
            return False

        self.progress_updated.emit("写入 cloud-init 引导盘...")
        seed_disk = self._build_cloud_init_seed(install_dir, mac_address, key_path)
        if not seed_disk:
            return False
        if not self.manager.attach_seed_disk(seed_disk):
            self.log_received.emit("[Hyper-V] 挂载 cloud-init 引导盘失败", "error")
            return False

        self.progress_updated.emit("启动虚拟机...")
        if not self.manager.start_vm():
            self.log_received.emit("[Hyper-V] 启动虚拟机失败", "error")
            return False

        self._configure_portproxy()
        self.progress_updated.emit("等待虚拟机完成 SSH 初始化...")
        if not self.wait_for_ssh_ready(timeout=240):
            self.log_received.emit("[Hyper-V] SSH 初始化超时，请检查 cloud-init 执行结果", "error")
            return False

        self.progress_updated.emit("通过 SSH 安装 Docker...")
        if not self._install_docker_sync():
            self.log_received.emit("[Hyper-V] Docker 安装失败", "error")
            return False

        self.config.set("hyperv_install_dir", install_dir)
        self.config.set("wsl_install_dir", install_dir)
        self.log_received.emit("[Hyper-V] 运行环境创建完成", "info")
        self.progress_updated.emit("运行环境创建成功！")
        return True

    def install_wsl(self):
        edition = self.manager.get_windows_edition() or "Unknown"
        is_home = self.manager.is_home_edition()
        self.log_received.emit(f"正在请求管理员权限启用 Hyper-V，当前 Windows 版本: {edition}", "info")

        system_root = os.environ.get("SystemRoot", r"C:\\Windows")
        packages_path = os.path.join(system_root, "servicing", "Packages")

        if is_home:
            if not self.manager.can_force_enable_on_home():
                self.log_received.emit("家庭版系统未找到 Hyper-V 组件包，无法执行强制启用。", "error")
                return False

            command = (
                '/c "pushd %SystemRoot%\\servicing\\Packages '
                '& for /f %%i in (\'dir /b *Hyper-V*.mum\') do dism /online /norestart /add-package:\\"%SystemRoot%\\servicing\\Packages\\%%i\\" '
                '& dism /online /enable-feature /featurename:Microsoft-Hyper-V -All /NoRestart '
                '& dism /online /enable-feature /featurename:VirtualMachinePlatform -All /NoRestart '
                '& bcdedit /set hypervisorlaunchtype auto '
                '& shutdown /r /t 60 /c \\"Hyper-V 组件启用完成，系统将在 60 秒后重启\\""' 
            )
            return self._launch_elevated("cmd.exe", command, packages_path, "已启动家庭版 Hyper-V 强制启用流程，完成后将在 60 秒后自动重启。")

        command = (
            '/k powershell -NoProfile -ExecutionPolicy Bypass -Command "'
            'Write-Host \'正在启用 Hyper-V，请稍候...\' -ForegroundColor Cyan;'
            'Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All -NoRestart;'
            'Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All -NoRestart;'
            'bcdedit /set hypervisorlaunchtype auto;'
            'if ($?) {'
            '  Write-Host \'Hyper-V 启用完成，系统将在 60 秒后重启...\' -ForegroundColor Green;'
            '  shutdown /r /t 60 /c \'Hyper-V 启用完成，系统将在 60 秒后重启\';'
            '} else {'
            '  Write-Host \'启用过程中出现错误，请检查上方输出\' -ForegroundColor Red;'
            '}'
            '"'
        )
        return self._launch_elevated("cmd.exe", command, None, "已启动 Hyper-V 启用流程，完成后将在 60 秒后自动重启。")

    def install_docker(self):
        if not self._ssh_key_ready():
            self.log_received.emit("[Hyper-V] SSH 密钥未准备，请先创建运行环境", "error")
            self.progress_updated.emit("__docker_fail__")
            return False
        if not self.wait_for_ssh_ready(timeout=20):
            self.log_received.emit("[Hyper-V] SSH 尚未就绪，无法安装 Docker", "error")
            self.progress_updated.emit("__docker_fail__")
            return False

        self.log_received.emit("[Hyper-V] 开始通过 SSH 安装 Docker...", "info")

        def _do_install():
            success = self._install_docker_sync()
            self.progress_updated.emit("__docker_done__" if success else "__docker_fail__")

        threading.Thread(target=_do_install, daemon=True).start()
        return True

    def start_services(self, deploy_mode):
        if self.is_running:
            return True

        self.status_changed.emit("启动中...")
        self._stop_event.clear()

        def _start():
            if not self.wait_for_ssh_ready(timeout=20):
                self.log_received.emit("[Hyper-V] SSH 未就绪，无法部署服务", "error")
                self.status_changed.emit("启动失败")
                return

            if deploy_mode == "napcat":
                compose_file = "docker-compose_with_napcat.yml"
            else:
                compose_file = "docker-compose_withnot_napcat.yml"

            compose_src = os.path.join(self.base_path, compose_file)
            env_src = os.path.join(self.base_path, "env")
            if not os.path.exists(compose_src):
                self.log_received.emit(f"Compose 文件不存在: {compose_src}", "error")
                self.status_changed.emit("启动失败")
                return

            deploy_dir = f"/home/{self.username}/nekro_agent"
            data_dir = self.config.get("data_dir") or f"/home/{self.username}/nekro_agent_data"

            try:
                self._guest_exec(f"mkdir -p {deploy_dir} {data_dir}")
                env_exists = self._guest_exec(f"test -f {deploy_dir}/.env && echo yes").strip() == "yes"

                if not env_exists:
                    self.log_received.emit("[Hyper-V] 首次部署，上传 Compose 配置", "info")
                    env_content = self._prepare_env(env_src, data_dir)
                    if not self._copy_to_guest(compose_src, f"{deploy_dir}/docker-compose.yml"):
                        self.status_changed.emit("启动失败")
                        return
                    if not self._write_to_guest(env_content, f"{deploy_dir}/.env"):
                        self.status_changed.emit("启动失败")
                        return
                else:
                    self.log_received.emit("[Hyper-V] 检测到已有部署配置，复用现有 .env", "info")
                    env_content = self._guest_exec(f"cat {deploy_dir}/.env", timeout=20)

                self.log_received.emit("[Hyper-V] 拉取 Docker 镜像...", "info")
                self._emit_pull_progress("start", "准备拉取 Nekro Agent 镜像")
                if not self._stream_guest_command(
                    f"cd {deploy_dir} && docker compose -f docker-compose.yml --env-file .env pull",
                    prefix="[镜像拉取]",
                    progress_prefix="拉取中",
                ):
                    self._emit_pull_progress("error", "Nekro Agent 镜像拉取失败")
                    self.status_changed.emit("启动失败")
                    return
                self._emit_pull_progress("stage", "Nekro Agent 镜像拉取完成")

                self._emit_pull_progress("stage", "准备拉取沙盒镜像")
                if not self._stream_guest_command(
                    "docker pull kromiose/nekro-agent-sandbox",
                    prefix="[沙盒镜像]",
                    progress_prefix="拉取中",
                ):
                    self._emit_pull_progress("error", "沙盒镜像拉取失败")
                    self.status_changed.emit("启动失败")
                    return
                self._emit_pull_progress("done", "镜像拉取完成")

                self.log_received.emit("[Hyper-V] 启动 Compose 服务...", "info")
                code, stdout, stderr = self.transport.exec(
                    f"cd {deploy_dir} && docker compose -f docker-compose.yml --env-file .env up -d",
                    timeout=180,
                )
                if code != 0:
                    self.log_received.emit(stdout or stderr or "Compose 启动失败", "error")
                    self.status_changed.emit("启动失败")
                    return

                self.is_running = True
                if not env_exists:
                    self._pending_deploy_info = (env_content, deploy_mode)
                    self._show_deploy_info(env_content, deploy_mode)
                    self._pending_deploy_info = None
                self.status_changed.emit("运行中")
            except Exception as exc:
                self.log_received.emit(f"[Hyper-V] 启动异常: {exc}", "error")
                self.status_changed.emit("启动失败")

        threading.Thread(target=_start, daemon=True).start()
        return True

    def stop_services(self):
        self._stop_event.set()
        was_running = self.is_running
        self.is_running = False

        if not was_running:
            self.status_changed.emit("已停止")
            return

        def _stop():
            deploy_dir = f"/home/{self.username}/nekro_agent"
            try:
                self._guest_exec(
                    f"cd {deploy_dir} && docker compose -f docker-compose.yml down",
                    timeout=120,
                )
                self.log_received.emit("[Hyper-V] 服务已停止", "info")
                self.status_changed.emit("已停止")
            except Exception as exc:
                self.log_received.emit(f"[Hyper-V] 停止服务失败: {exc}", "error")
                self.status_changed.emit("停止失败")

        threading.Thread(target=_stop, daemon=True).start()

    def update_services(self):
        self.log_received.emit("[Hyper-V] 开始更新服务...", "info")
        self.status_changed.emit("更新中...")

        def _update():
            deploy_dir = f"/home/{self.username}/nekro_agent"
            try:
                if not self._stream_guest_command(
                    f"cd {deploy_dir} && docker compose -f docker-compose.yml pull nekro_agent",
                    prefix="[镜像拉取]",
                    progress_prefix="拉取中",
                ):
                    self._emit_pull_progress("error", "Nekro Agent 镜像更新失败")
                    self.status_changed.emit("更新失败")
                    return

                self._emit_pull_progress("stage", "准备更新沙盒镜像")
                if not self._stream_guest_command(
                    "docker pull kromiose/nekro-agent-sandbox",
                    prefix="[沙盒镜像]",
                    progress_prefix="拉取中",
                ):
                    self._emit_pull_progress("error", "沙盒镜像更新失败")
                    self.status_changed.emit("更新失败")
                    return
                self._emit_pull_progress("done", "镜像更新完成")

                code, stdout, stderr = self.transport.exec(
                    f"cd {deploy_dir} && docker compose -f docker-compose.yml --env-file .env up -d",
                    timeout=180,
                )
                if code != 0:
                    self.log_received.emit(stdout or stderr or "服务重启失败", "error")
                    self.status_changed.emit("更新失败")
                    return

                self.log_received.emit("[Hyper-V] 服务更新完成", "info")
                self.status_changed.emit("运行中")
            except Exception as exc:
                self.log_received.emit(f"[Hyper-V] 更新异常: {exc}", "error")
                self.status_changed.emit("更新失败")

        threading.Thread(target=_update, daemon=True).start()

    def uninstall_environment(self):
        def _uninstall():
            self.status_changed.emit("卸载中...")
            install_dir = self.get_default_install_dir()
            deploy_dir = f"/home/{self.username}/nekro_agent"
            try:
                if self.wait_for_ssh_ready(timeout=10):
                    self._guest_exec(
                        f"cd {deploy_dir} && docker compose -f docker-compose.yml down -v 2>/dev/null; "
                        "docker system prune -af 2>/dev/null; "
                        f"rm -rf {deploy_dir}",
                        timeout=180,
                    )
            except Exception:
                pass

            self.manager.remove_vm()
            shutil.rmtree(install_dir, ignore_errors=True)

            if self.config:
                self.config.set("first_run", True)
                self.config.set("deploy_mode", "")
                self.config.set("data_dir", "")
                self.config.set("deploy_info", None)
                self.config.set("hyperv_install_dir", "")
                self.config.set("hyperv_ssh_key_path", "")
                self.config.set("hyperv_seed_disk", "")
                self.config.set("wsl_install_dir", "")

            self.status_changed.emit("已卸载")

        threading.Thread(target=_uninstall, daemon=True).start()

    def get_runtime_name(self):
        return self.vm_name

    def get_host_access_path(self, guest_path):
        return ""

    def _runtime_cache_dir(self):
        configured = self.config.get("runtime_image_cache") or "runtime_cache"
        if os.path.isabs(configured):
            return configured
        return os.path.join(self.base_path, configured)

    def _configure_portproxy(self):
        self.manager.ensure_portproxy(8021, self.guest_ip, 8021)
        self.manager.ensure_portproxy(6099, self.guest_ip, 6099)

    def wait_for_ssh_ready(self, timeout=180):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                code, stdout, _ = self.transport.exec("echo ok", timeout=10)
                if code == 0 and stdout.strip() == "ok":
                    return True
            except Exception:
                pass
            time.sleep(5)
        return False

    def _resolve_base_path(self):
        if getattr(sys, "frozen", False):
            return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        base = os.path.dirname(os.path.abspath(__file__))
        if base.endswith("core"):
            base = os.path.dirname(base)
        return base

    def _launch_elevated(self, executable, parameters, directory, success_message):
        try:
            result = ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                executable,
                parameters,
                directory,
                1,
            )
            if result <= 32:
                self.log_received.emit(f"管理员命令启动失败，返回码: {result}", "error")
                return False
            self.log_received.emit(success_message, "info")
            return True
        except Exception as exc:
            self.log_received.emit(f"管理员命令启动失败: {exc}", "error")
            return False

    def _command_available(self, command):
        return shutil.which(command) is not None

    def _ssh_key_ready(self):
        key_path = self.config.get("hyperv_ssh_key_path")
        pub_path = f"{key_path}.pub" if key_path else ""
        return bool(key_path and pub_path and os.path.exists(key_path) and os.path.exists(pub_path))

    def _ensure_ssh_keypair(self, install_dir):
        key_path = self.config.get("hyperv_ssh_key_path")
        if key_path and os.path.exists(key_path) and os.path.exists(f"{key_path}.pub"):
            self.transport.private_key = key_path
            return key_path

        key_dir = os.path.join(install_dir, "ssh")
        os.makedirs(key_dir, exist_ok=True)
        key_path = os.path.join(key_dir, "hyperv_id_ed25519")

        if not os.path.exists(key_path) or not os.path.exists(f"{key_path}.pub"):
            proc = subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if proc.returncode != 0:
                self.log_received.emit(proc.stderr or "ssh-keygen 执行失败", "error")
                return ""

        self.config.set("hyperv_ssh_key_path", key_path)
        self.transport.private_key = key_path
        return key_path

    def _build_cloud_init_seed(self, install_dir, raw_mac, key_path):
        seed_dir = os.path.join(install_dir, "cloud-init")
        os.makedirs(seed_dir, exist_ok=True)

        try:
            with open(f"{key_path}.pub", "r", encoding="utf-8") as fh:
                public_key = fh.read().strip()
        except OSError as exc:
            self.log_received.emit(f"[Hyper-V] 读取 SSH 公钥失败: {exc}", "error")
            return ""

        mac = ":".join(raw_mac[i:i + 2] for i in range(0, len(raw_mac), 2)).lower()
        prefix_length = self.subnet.split("/")[1]

        user_data = f"""#cloud-config
users:
  - name: {self.username}
    gecos: Nekro Agent
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: [sudo]
    lock_passwd: true
    ssh_authorized_keys:
      - {public_key}
package_update: true
packages:
  - openssh-server
write_files:
  - path: /etc/ssh/sshd_config.d/99-nekro-agent.conf
    permissions: '0644'
    content: |
      PasswordAuthentication no
      PubkeyAuthentication yes
      PermitRootLogin no
runcmd:
  - systemctl enable ssh
  - systemctl restart ssh
"""
        meta_data = f"instance-id: {self.vm_name}\nlocal-hostname: {self.vm_name}\n"
        network_config = f"""version: 2
ethernets:
  eth0:
    match:
      macaddress: "{mac}"
    set-name: eth0
    dhcp4: false
    addresses:
      - {self.guest_ip}/{prefix_length}
    routes:
      - to: default
        via: {self.gateway_ip}
    nameservers:
      addresses:
        - 223.5.5.5
        - 1.1.1.1
"""

        for name, content in {
            "user-data": user_data,
            "meta-data": meta_data,
            "network-config": network_config,
        }.items():
            with open(os.path.join(seed_dir, name), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)

        seed_disk = os.path.join(install_dir, "cloud-init-seed.iso")
        if not self.manager.create_seed_disk(seed_disk, seed_dir):
            self.log_received.emit("[Hyper-V] 生成 cloud-init 引导盘失败", "error")
            return ""

        self.config.set("hyperv_seed_disk", seed_disk)
        return seed_disk

    def _guest_command_ok(self, command, timeout=60):
        try:
            code, _, _ = self.transport.exec(command, timeout=timeout)
            return code == 0
        except Exception:
            return False

    def _guest_exec(self, command, timeout=120):
        code, stdout, stderr = self.transport.exec(command, timeout=timeout)
        if code != 0:
            raise RuntimeError(stderr or stdout or "远程命令执行失败")
        return stdout.strip()

    def _write_to_guest(self, content, remote_path):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            temp_path = fh.name
        try:
            return self._copy_to_guest(temp_path, remote_path)
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    def _copy_to_guest(self, local_path, remote_path):
        code, _, stderr = self.transport.copy_to_guest(local_path, remote_path, timeout=120)
        if code != 0:
            self.log_received.emit(stderr or f"上传文件失败: {local_path}", "error")
            return False
        return True

    def _install_docker_sync(self):
        self.log_received.emit("[Hyper-V] 开始安装 Docker...", "info")
        apt_sources = "\n".join(APT_MIRROR_LINES)
        steps = [
            (
                "写入 Ubuntu 镜像源",
                "sudo tee /etc/apt/sources.list >/dev/null <<'EOF'\n"
                + apt_sources
                + "\nEOF",
            ),
            (
                "安装前置依赖",
                "sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg lsb-release",
            ),
        ]

        for desc, cmd in steps:
            self.progress_updated.emit(desc + "...")
            if not self._run_guest_step(cmd, desc):
                return False

        installed = False
        for mirror_name, docker_mirror in DOCKER_APT_MIRRORS:
            self.progress_updated.emit(f"配置 Docker 源 ({mirror_name})...")
            repo_cmd = (
                "sudo install -m 0755 -d /etc/apt/keyrings && "
                f"curl -fsSL {docker_mirror}/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg && "
                "sudo chmod a+r /etc/apt/keyrings/docker.gpg && "
                f"echo \"deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] {docker_mirror}/linux/ubuntu "
                "$( . /etc/os-release && echo $VERSION_CODENAME ) stable\" | "
                "sudo tee /etc/apt/sources.list.d/docker.list >/dev/null && "
                "sudo apt-get update && "
                "sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
            )
            if self._run_guest_step(repo_cmd, f"Docker 安装 ({mirror_name})", timeout=600):
                installed = True
                break

        if not installed:
            self.log_received.emit("[Hyper-V] Docker 安装失败，所有镜像源均不可用", "error")
            return False

        mirrors = ",".join(f'"{item}"' for item in DOCKER_REGISTRY_MIRRORS)
        daemon_json = '{"registry-mirrors":[' + mirrors + '],"features":{"buildkit":true}}'
        self.progress_updated.emit("配置 Docker 镜像加速...")
        if not self._run_guest_step(
            f"sudo mkdir -p /etc/docker && sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'\n{daemon_json}\nEOF",
            "配置 Docker 镜像加速",
        ):
            return False

        self.progress_updated.emit("启动 Docker 服务...")
        if not self._run_guest_step(
            f"sudo usermod -aG docker {self.username} && sudo systemctl enable docker && sudo systemctl restart docker",
            "启动 Docker 服务",
            timeout=120,
        ):
            return False

        if not self._guest_command_ok("docker info", timeout=30):
            self.log_received.emit("[Hyper-V] Docker daemon 启动后仍不可用", "error")
            return False
        if not self._guest_command_ok("docker compose version", timeout=20):
            self.log_received.emit("[Hyper-V] Docker Compose 安装后仍不可用", "error")
            return False

        self.log_received.emit("[Hyper-V] Docker 安装完成", "info")
        return True

    def _run_guest_step(self, command, desc, timeout=180):
        try:
            code, stdout, stderr = self.transport.exec(command, timeout=timeout)
        except Exception as exc:
            self.log_received.emit(f"[Hyper-V] {desc}异常: {exc}", "error")
            return False

        if code != 0:
            self.log_received.emit(f"[Hyper-V] {desc}失败", "error")
            if stdout:
                self.log_received.emit(stdout, "debug")
            if stderr:
                self.log_received.emit(stderr, "debug")
            return False
        self.log_received.emit(f"[Hyper-V] ✓ {desc}", "info")
        return True

    def _stream_guest_command(self, command, prefix="", timeout=600, progress_prefix=""):
        args = [
            "ssh",
            *self.transport._base_args(),
            f"{self.username}@{self.guest_ip}",
            command,
        ]
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            started = time.time()
            while True:
                if proc.stdout is None:
                    break
                line = proc.stdout.readline()
                if line:
                    clean_line = line.strip()
                    if progress_prefix:
                        self._emit_pull_progress("update", clean_line)
                    else:
                        self.log_received.emit(f"{prefix} {clean_line}".strip(), "info")
                if proc.poll() is not None:
                    break
                if time.time() - started > timeout:
                    proc.kill()
                    self.log_received.emit(f"{prefix} 命令执行超时", "error")
                    return False
            return proc.wait() == 0
        except Exception as exc:
            self.log_received.emit(f"{prefix} 命令执行异常: {exc}", "error")
            return False

    def _show_deploy_info(self, env_content, deploy_mode):
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

        if self.config:
            self.config.set("deploy_info", info)

        self.log_received.emit("=== 部署完成！===", "info")
        self.log_received.emit(f"管理员账号: admin | 密码: {info['admin_password']}", "info")
        self.log_received.emit(f"Web 访问地址: http://127.0.0.1:{info['port']}", "info")
        self.deploy_info_ready.emit(info)

    def _prepare_env(self, env_template_path, data_dir):
        content = ""
        if os.path.exists(env_template_path):
            with open(env_template_path, "r", encoding="utf-8") as fh:
                content = fh.read()

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
            else:
                new_lines.append(line)

        return "\n".join(new_lines) + "\n"

    @staticmethod
    def _random_token(length=32):
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(length))
