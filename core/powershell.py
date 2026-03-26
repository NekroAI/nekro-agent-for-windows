import os
import time
import tempfile
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import subprocess


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self):
        return self.returncode == 0


def run_powershell(command, timeout=60):
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return CommandResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())


class ElevatedSession:
    """
    启动一个常驻的提权 PowerShell 进程，通过文件交换执行命令。

    工作方式:
    1. start() 通过 ShellExecuteExW(runas) 启动一个提权 PowerShell，运行一个轮询脚本
    2. run(command) 将命令写入 .cmd 文件，轮询脚本执行后把结果写入 .result 文件
    3. stop() 写入 .stop 文件通知轮询脚本退出
    """

    def __init__(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="nekro_elev_").replace("\\", "/")
        self._started = False
        self._cmd_counter = 0

    def start(self, timeout=30):
        """启动提权 PowerShell 进程，等待 ready 信号。"""
        server_script = os.path.join(self._tmp_dir, "server.ps1")
        ready_file = os.path.join(self._tmp_dir, "ready")
        watch_dir = self._tmp_dir

        script_content = f"""\
$watchDir = '{watch_dir}'
# 设置目录 ACL，允许当前登录用户读写结果文件
$acl = Get-Acl $watchDir
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule('Users','FullControl','ContainerInherit,ObjectInherit','None','Allow')
$acl.AddAccessRule($rule)
Set-Acl $watchDir $acl
'1' | Out-File -FilePath (Join-Path $watchDir 'ready') -Encoding utf8
while ($true) {{
    $stopFile = Join-Path $watchDir '.stop'
    if (Test-Path $stopFile) {{
        Remove-Item $stopFile -Force -ErrorAction SilentlyContinue
        break
    }}
    $cmdFiles = Get-ChildItem -Path $watchDir -Filter 'task_*.ps1' -ErrorAction SilentlyContinue
    foreach ($f in $cmdFiles) {{
        $id = $f.BaseName
        $resultFile = Join-Path $watchDir "$id.result"
        $ErrorActionPreference = 'Stop'
        try {{
            $output = & {{ . $f.FullName }} 2>&1 | Out-String
            "$output`n---RC---`n0" | Out-File -FilePath $resultFile -Encoding utf8
        }} catch {{
            "$($_.Exception.Message)`n$($_.ScriptStackTrace)`n---RC---`n1" | Out-File -FilePath $resultFile -Encoding utf8
        }}
        Remove-Item $f.FullName -Force -ErrorAction SilentlyContinue
    }}
    Start-Sleep -Milliseconds 200
}}
"""
        with open(server_script, "w", encoding="utf-8-sig") as f:
            f.write(script_content)

        # UAC 提权启动
        class SHELLEXECUTEINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE),
            ]

        sei = SHELLEXECUTEINFO()
        sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFO)
        sei.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
        sei.lpVerb = "runas"
        sei.lpFile = "powershell.exe"
        sei.lpParameters = f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{server_script}"'
        sei.nShow = 0  # SW_HIDE

        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
            return False

        self._process_handle = sei.hProcess

        # 等待 ready 信号
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(ready_file):
                self._started = True
                return True
            time.sleep(0.2)

        return False

    def run(self, command, timeout=120):
        """向提权进程发送命令并等待结果。"""
        if not self._started:
            if not self.start():
                return CommandResult(1, "", "提权 PowerShell 启动失败（UAC 可能被拒绝）")

        self._cmd_counter += 1
        cmd_id = f"cmd_{self._cmd_counter}"
        cmd_file = os.path.join(self._tmp_dir, f"task_{cmd_id}.ps1")
        result_file = os.path.join(self._tmp_dir, f"task_{cmd_id}.result")

        with open(cmd_file, "w", encoding="utf-8") as f:
            f.write(command)

        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(result_file):
                try:
                    with open(result_file, "r", encoding="utf-8-sig") as f:
                        content = f.read()
                    os.remove(result_file)
                except Exception:
                    content = ""

                if "---RC---" in content:
                    parts = content.rsplit("---RC---", 1)
                    output = parts[0].strip()
                    rc = int(parts[1].strip()) if parts[1].strip().isdigit() else 1
                else:
                    output = content.strip()
                    rc = 1

                if rc == 0:
                    return CommandResult(0, output, "")
                else:
                    return CommandResult(rc, "", output)

            time.sleep(0.2)

        # 超时，清理命令文件
        try:
            os.remove(cmd_file)
        except OSError:
            pass
        return CommandResult(1, "", "命令执行超时")

    def stop(self):
        """通知提权进程退出。"""
        if not self._started:
            return
        stop_file = os.path.join(self._tmp_dir, ".stop")
        try:
            with open(stop_file, "w") as f:
                f.write("stop")
        except OSError:
            pass
        self._started = False

    def __del__(self):
        self.stop()
