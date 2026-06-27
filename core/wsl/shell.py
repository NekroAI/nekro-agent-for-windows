import base64
import logging
import os
import re
import secrets
import shlex
import string
import subprocess
import sys

logger = logging.getLogger(__name__)

from core.wsl.constants import DISTRO_NAME


class WSLShellMixin:
    _SENSITIVE_KEYS = (
        "NEKRO_ADMIN_PASSWORD",
        "ONEBOT_ACCESS_TOKEN",
        "QDRANT_API_KEY",
        "POSTGRES_PASSWORD",
        "JWT_SECRET_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "NA_TOOLS_DAEMON_TOKEN",
        "NA_TOOLS_DAEMON_TOKEN_FILE",
        "Authorization",
        "token",
    )

    def runtime_exists(self):
        return self._distro_exists()

    def _wsl_run(self, distro, cmd, timeout=60, user=None):
        """在 WSL 发行版中执行命令并返回原始进程结果。

        user: 可选，指定运行用户（如 "root"）。不传则使用发行版默认用户。
        """
        args = ["wsl", "-d", distro]
        if user:
            args += ["-u", user]
        args += ["--", "bash", "-c", cmd]
        return subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=self._creation_flags(),
        )

    def _distro_exists(self):
        """检查 NekroAgent 专用发行版是否已存在"""
        self._last_distro_check_error = ""
        try:
            proc = subprocess.run(
                ["wsl", "-l", "-q"],
                capture_output=True,
                timeout=10,
                creationflags=self._creation_flags(),
            )
            if proc.returncode != 0:
                detail = self._format_command_failure(
                    "获取 WSL 发行版列表失败",
                    args=["wsl", "-l", "-q"],
                    timeout=10,
                    returncode=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                )
                self._last_distro_check_error = detail
                self.log_received.emit(detail, "debug")
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
            detail = self._format_command_failure(
                "获取 WSL 发行版列表异常",
                args=["wsl", "-l", "-q"],
                timeout=10,
                exception=e,
            )
            self._last_distro_check_error = detail
            self.log_received.emit(detail, "debug")
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

    def _redact_for_log(self, text):
        if not text:
            return ""

        result = self._safe_decode(text)
        for key in self._SENSITIVE_KEYS:
            result = re.sub(
                rf"({re.escape(key)}\s*[:=]\s*)([^\s,\n\"']+)",
                rf"\1<redacted>",
                result,
                flags=re.IGNORECASE,
            )
            result = re.sub(
                rf"([\"']{re.escape(key)}[\"']\s*:\s*[\"'])(.*?)([\"'])",
                rf"\1<redacted>\3",
                result,
                flags=re.IGNORECASE,
            )

        if "base64 -d" in result and "printf %s" in result:
            result = re.sub(r"printf %s\s+\S+", "printf %s <base64-content>", result)
        result = re.sub(
            r"([?&](?:access_)?token=)([^&\s]+)",
            r"\1<redacted>",
            result,
            flags=re.IGNORECASE,
        )
        result = re.sub(
            r"((?:管理员)?密码\s*[:：]\s*)([^\s,，\n\"']+)",
            r"\1<redacted>",
            result,
            flags=re.IGNORECASE,
        )
        result = re.sub(
            r"((?:登录\s*)?(?:Token|令牌)\s*[:：]\s*)([^\s,，\n\"']+)",
            r"\1<redacted>",
            result,
            flags=re.IGNORECASE,
        )
        return result

    def _command_for_log(self, cmd=None, args=None):
        if cmd:
            return self._redact_for_log(cmd)
        if args:
            return self._redact_for_log(" ".join(shlex.quote(str(arg)) for arg in args))
        return ""

    def _format_command_failure(
        self,
        action,
        *,
        cmd=None,
        args=None,
        distro=None,
        user=None,
        cwd=None,
        timeout=None,
        returncode=None,
        stdout=None,
        stderr=None,
        exception=None,
        max_output=3000,
    ):
        """生成面向排障的命令失败详情，避免各处只输出“失败/异常”。"""
        lines = [action]
        if distro:
            lines.append(f"发行版: {distro}")
        if user:
            lines.append(f"用户: {user}")
        if cwd:
            lines.append(f"工作目录: {cwd}")
        if timeout:
            lines.append(f"超时: {timeout}s")
        if returncode is not None:
            lines.append(f"返回码: {returncode}")

        command_text = self._command_for_log(cmd=cmd, args=args)
        if command_text:
            lines.append(f"命令: {command_text}")

        if exception is not None:
            lines.append(f"异常: {type(exception).__name__}: {exception}")

        clean_stdout = self._redact_for_log(
            self._clean_command_output(stdout or "", max_output)
        )
        clean_stderr = self._redact_for_log(self._clean_stderr(stderr or "", max_output))
        if clean_stdout:
            lines.append(f"STDOUT:\n{clean_stdout}")
        if clean_stderr:
            lines.append(f"STDERR:\n{clean_stderr}")
        if not clean_stdout and not clean_stderr and exception is None:
            lines.append("输出: <empty>")
        return "\n".join(lines)

    def _wsl_exec(self, distro, cmd, timeout=60, user=None):
        """在 WSL 发行版中执行命令并返回 stdout"""
        try:
            proc = self._wsl_run(distro, cmd, timeout=timeout, user=user)
            return self._safe_decode(proc.stdout)
        except Exception as e:
            logger.debug("_wsl_exec failed: cmd=%s err=%s", cmd[:120], e)
            return ""

    def _wsl_exec_checked(self, distro, cmd, timeout=60, user=None):
        """在 WSL 中执行命令，失败时抛出异常。"""
        try:
            proc = self._wsl_run(distro, cmd, timeout=timeout, user=user)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                self._format_command_failure(
                    "WSL 命令执行超时",
                    cmd=cmd,
                    distro=distro,
                    user=user,
                    timeout=timeout,
                    stdout=e.stdout,
                    stderr=e.stderr,
                    exception=e,
                )
            ) from e
        stdout = self._safe_decode(proc.stdout)
        if proc.returncode != 0:
            raise RuntimeError(
                self._format_command_failure(
                    "WSL 命令执行失败",
                    cmd=cmd,
                    distro=distro,
                    user=user,
                    timeout=timeout,
                    returncode=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                )
            )
        return stdout

    def _run_wsl_checked(
        self,
        distro,
        cmd,
        *,
        action="WSL 命令执行失败",
        timeout=60,
        user=None,
        cwd=None,
        ok_returncodes=(0,),
    ):
        """执行 WSL 命令并在失败时抛出带上下文的 RuntimeError。"""
        run_cmd = f"cd {shlex.quote(cwd)} && {cmd}" if cwd else cmd
        try:
            proc = self._wsl_run(distro, run_cmd, timeout=timeout, user=user)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                self._format_command_failure(
                    f"{action}（超时）",
                    cmd=cmd,
                    distro=distro,
                    user=user,
                    cwd=cwd,
                    timeout=timeout,
                    stdout=e.stdout,
                    stderr=e.stderr,
                    exception=e,
                )
            ) from e

        if proc.returncode not in ok_returncodes:
            raise RuntimeError(
                self._format_command_failure(
                    action,
                    cmd=cmd,
                    distro=distro,
                    user=user,
                    cwd=cwd,
                    timeout=timeout,
                    returncode=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                )
            )
        return proc

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
        self._wsl_exec_checked(distro, f'printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(wsl_path)}')

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
