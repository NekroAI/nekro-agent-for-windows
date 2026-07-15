import re
import shlex
import subprocess
import threading
import time
from urllib.request import urlopen

from core.port_utils import normalize_port
from core.wsl.constants import DISTRO_NAME


class WSLMonitorMixin:
    _health_generation: int = 0
    _health_lock = threading.Lock()

    def refresh_running_state(self):
        """后台探测当前 active 实例的 compose 服务是否仍在运行，校准 is_running。

        用于更新失败、移除实例等 is_running 可能失真的场景；
        探测完成后按真实状态发出「运行中」或「已停止」。
        """

        def _probe():
            deploy_dir, _, _ = self._get_active_deploy_paths()
            cmd = (
                f"test -f {shlex.quote(deploy_dir)}/docker-compose.yml "
                f"&& cd {shlex.quote(deploy_dir)} "
                "&& docker compose -f docker-compose.yml ps --quiet --status running"
            )
            try:
                output = self._wsl_exec(DISTRO_NAME, cmd, timeout=30)
            except Exception:
                return
            running = bool(self._clean_command_output(output).strip())
            self.is_running = running
            self.status_changed.emit("运行中" if running else "已停止")

        threading.Thread(target=_probe, daemon=True).start()

    def _log_reader(self, distro, deploy_dir, log_prefix="", inst_id=""):
        """通过 docker compose logs -f 流式读取日志"""
        import shlex
        napcat_token_pattern = re.compile(r"WebUi.*token=([a-zA-Z0-9]+)")
        proc = None
        try:
            quoted_dir = shlex.quote(deploy_dir)
            proc = subprocess.Popen(
                [
                    "wsl",
                    "-d",
                    distro,
                    "--",
                    "bash",
                    "-c",
                    f"cd {quoted_dir} && docker compose -f docker-compose.yml logs -f --tail=50",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=self._creation_flags(),
            )
            self._log_process = proc

            for line in iter(proc.stdout.readline, b""):
                if self._stop_event.is_set():
                    break
                text = line.decode("utf-8", errors="ignore").rstrip()
                if text:
                    self.log_received.emit(f"{log_prefix}{text}", "vm")
                    m = napcat_token_pattern.search(text)
                    if m and self.config:
                        token = m.group(1)
                        info = self.config.get("deploy_info") or {}
                        if info.get("napcat_token") != token:
                            info["napcat_token"] = token
                            self._save_deploy_info(info, inst_id=inst_id)
                            self.log_received.emit(
                                f"{log_prefix}[NapCat] 已捕获 WebUI Token，请在部署凭据窗口查看。",
                                "info",
                            )
                            if self._pending_deploy_info:
                                self._pending_deploy_info["napcat_token"] = token
                                self._show_deploy_info(
                                    self._pending_deploy_info,
                                    inst_id=inst_id,
                                )
                                self._pending_deploy_info = None
        except Exception as e:
            if not self._stop_event.is_set():
                self.log_received.emit(
                    f"{log_prefix}日志读取异常\n"
                    f"发行版: {distro}\n"
                    f"部署目录: {deploy_dir}\n"
                    "命令: docker compose -f docker-compose.yml logs -f --tail=50\n"
                    f"异常: {type(e).__name__}: {e}",
                    "debug",
                )
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            if self._log_process is proc:
                self._log_process = None

    def _health_check(self, nekro_port=None):
        """轮询 Nekro Agent 服务直到返回 200，自动取消前一轮检查"""
        with self._health_lock:
            self._health_generation += 1
            my_gen = self._health_generation

        if nekro_port is None:
            nekro_port = 8021
            if self.config:
                nekro_port = self.config.get("nekro_port") or 8021
        nekro_port = normalize_port(nekro_port, 8021)
        timeout = 300
        start = time.time()
        interval = 2.0

        while time.time() - start < timeout and not self._stop_event.is_set():
            if self._health_generation != my_gen:
                return
            try:
                with urlopen(f"http://localhost:{nekro_port}", timeout=5) as resp:
                    ready = resp.status == 200
                if ready:
                    if self._health_generation != my_gen:
                        return
                    elapsed = time.time() - start
                    self.log_received.emit(f"服务已就绪！(耗时 {elapsed:.1f}s)", "info")
                    self.progress_updated.emit("__deploy_progress__|done|服务已就绪")
                    self.boot_finished.emit()
                    self.status_changed.emit("运行中")
                    return
            except Exception:
                pass

            time.sleep(interval)

        if not self._stop_event.is_set() and self._health_generation == my_gen:
            self.is_running = False
            self.log_received.emit(
                "服务启动超时\n"
                f"访问地址: http://localhost:{nekro_port}\n"
                f"等待时长: {timeout}s\n"
                "建议检查 Docker Compose 日志、端口占用和容器健康状态。",
                "error",
            )
            self.status_changed.emit("启动超时")
