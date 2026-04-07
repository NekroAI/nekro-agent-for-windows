import os
import sys

try:
    import winreg
except ImportError:  # pragma: no cover - only available on Windows
    winreg = None


RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "NekroAgentLauncher"


def _launcher_command():
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}"'

    python_exe = os.path.abspath(sys.executable)
    pythonw_exe = os.path.join(os.path.dirname(python_exe), "pythonw.exe")
    if os.path.basename(python_exe).lower() == "python.exe" and os.path.exists(pythonw_exe):
        python_exe = pythonw_exe

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main_script = os.path.join(project_root, "main.py")
    return f'"{python_exe}" "{main_script}"'


def set_autostart_enabled(enabled: bool):
    if os.name != "nt" or winreg is None:
        raise OSError("当前平台不支持启动项注册。")

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        RUN_KEY_PATH,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, _launcher_command())
            return

        try:
            winreg.DeleteValue(key, RUN_VALUE_NAME)
        except FileNotFoundError:
            pass
