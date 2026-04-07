import base64
import os
import re
import secrets
import string
import subprocess
import sys

from core.wsl.constants import DISTRO_NAME


class WSLShellMixin:
    def runtime_exists(self):
        return self._distro_exists()

    def _wsl_run(self, distro, cmd, timeout=60):
        """在 WSL 发行版中执行命令并返回原始进程结果。"""
        return subprocess.run(
            ["wsl", "-d", distro, "--", "bash", "-c", cmd],
            capture_output=True,
            timeout=timeout,
            creationflags=self._creation_flags(),
        )

    def _distro_exists(self):
        """检查 NekroAgent 专用发行版是否已存在"""
        try:
            proc = subprocess.run(
                ["wsl", "-l", "-q"],
                capture_output=True,
                timeout=10,
                creationflags=self._creation_flags(),
            )
            if proc.returncode != 0:
                self.log_received.emit(f"wsl -l 失败，返回码: {proc.returncode}", "debug")
                return False

            output = self._safe_decode(proc.stdout)
            lines = [l.strip().strip("\x00") for l in output.splitlines() if l.strip().strip("\x00")]
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
            if data.startswith((b"\xff\xfe", b"\xfe\xff")):
                try:
                    return data.decode("utf-16")
                except Exception:
                    pass

            if len(data) >= 4:
                null_at_odd = sum(1 for i in range(1, len(data), 2) if data[i] == 0)
                total_odd = (len(data) + 1) // 2
                if total_odd > 0 and null_at_odd / total_odd > 0.7:
                    try:
                        return data.decode("utf-16-le")
                    except Exception:
                        pass

            for encoding in ["utf-8", "gbk", "latin1"]:
                try:
                    return data.decode(encoding)
                except (UnicodeDecodeError, LookupError):
                    continue

            return data.decode("latin1", errors="replace")
        return str(data)

    def _is_wsl_noise(self, text):
        """判断是否为 WSL 系统噪音（如 hostname 解析警告的 UTF-16 乱码）"""
        stripped = text.strip().lstrip("\ufeff\x00")
        stripped_clean = stripped.replace("\x00", "")
        compact = re.sub(r"\s+", "", stripped_clean).lower()
        if compact.startswith("wsl:") or compact.startswith("wsl"):
            return True

        null_ratio = text.count("\x00") / len(text) if text else 0
        if null_ratio > 0.2:
            return True

        noise_patterns = (
            "localhost 代理配置",
            "localhost proxy",
            "NAT 模式下的 WSL",
            "NAT mode",
            "未镜像到 WSL",
        )
        lower = stripped_clean.lower()
        if any(pattern.lower() in lower for pattern in noise_patterns):
            return True

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

    def _wsl_exec(self, distro, cmd, timeout=60):
        """在 WSL 发行版中执行命令并返回 stdout"""
        try:
            proc = self._wsl_run(distro, cmd, timeout=timeout)
            return self._safe_decode(proc.stdout)
        except Exception:
            return ""

    def _wsl_exec_checked(self, distro, cmd, timeout=60):
        """在 WSL 中执行命令，失败时抛出异常。"""
        proc = self._wsl_run(distro, cmd, timeout=timeout)
        stdout = self._safe_decode(proc.stdout)
        if proc.returncode != 0:
            stderr = self._clean_stderr(proc.stderr, 0)
            detail = stderr or self._clean_command_output(stdout, 0) or f"返回码: {proc.returncode}"
            raise RuntimeError(detail)
        return stdout

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
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        self._wsl_exec_checked(distro, f'echo "{encoded}" | base64 -d > "{wsl_path}"')

    @staticmethod
    def _random_token(length=32):
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(length))

    @staticmethod
    def _creation_flags():
        """返回 Windows 平台的进程创建标志，打包后隐藏控制台窗口"""
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            return 0x08000000
        return 0
