import re
import subprocess
import time
from urllib.request import urlopen


class WSLMonitorMixin:
    def _log_reader(self, distro, deploy_dir):
        """通过 docker compose logs -f 流式读取日志"""
        napcat_token_pattern = re.compile(r"WebUi.*token=([a-zA-Z0-9]+)")
        try:
            self._log_process = subprocess.Popen(
                ["wsl", "-d", distro, "--", "bash", "-c", f"cd {deploy_dir} && docker compose -f docker-compose.yml logs -f --tail=50"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=self._creation_flags(),
            )

            for line in iter(self._log_process.stdout.readline, b""):
                if self._stop_event.is_set():
                    break
                text = line.decode("utf-8", errors="ignore").rstrip()
                if text:
                    self.log_received.emit(text, "vm")
                    m = napcat_token_pattern.search(text)
                    if m and self.config:
                        token = m.group(1)
                        info = self.config.get("deploy_info") or {}
                        if info.get("napcat_token") != token:
                            info["napcat_token"] = token
                            self.config.set("deploy_info", info)
                            self.log_received.emit(f"[NapCat] 已捕获 WebUI Token: {token}", "info")
                            if self._pending_deploy_info:
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
