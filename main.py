import argparse
import os
import sys

# 全局 debug 标志
DEBUG_MODE = False


class LogRedirector:
    """将 stdout 和 stderr 重定向到文件和控制台（安全处理编码）"""
    def __init__(self, log_file):
        self.log_file = log_file
        self.file = open(log_file, 'w', encoding='utf-8', buffering=1)
        self.console = sys.__stdout__

    def write(self, message):
        try:
            self.file.write(message)
            self.file.flush()
        except Exception:
            pass

        if self.console is None:
            return
        try:
            self.console.write(message)
            self.console.flush()
        except UnicodeEncodeError:
            try:
                self.console.write(message.encode('utf-8', errors='replace').decode('utf-8', errors='replace'))
                self.console.flush()
            except Exception:
                pass
        except Exception:
            pass

    def flush(self):
        try:
            self.file.flush()
        except Exception:
            pass
        if self.console is not None:
            try:
                self.console.flush()
            except Exception:
                pass

    def close(self):
        try:
            self.file.close()
        except Exception:
            pass


def _run_backend_check():
    from core.backend_factory import BackendFactory
    from core.config_manager import ConfigManager

    config = ConfigManager()
    backend = BackendFactory.create(config)
    checks = backend.get_check_funcs()
    labels = [
        "WSL",
        "Nekro Agent runtime",
        "Docker",
        "Docker Compose",
    ]

    print(f"Backend check: {backend.display_name or backend.backend_key or 'backend'}")
    all_passed = True
    for idx, check in enumerate(checks):
        label = labels[idx] if idx < len(labels) else f"Check {idx + 1}"
        try:
            passed, detail = check()
        except Exception as e:
            passed = False
            detail = str(e)
        status = "PASS" if passed else "FAIL"
        suffix = f": {detail}" if detail else ""
        print(f"[{status}] {label}{suffix}")
        if not passed:
            all_passed = False

    print(f"Result: {'PASS' if all_passed else 'FAIL'}")
    return 0 if all_passed else 1


def _run_gui(args):
    global DEBUG_MODE

    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QPalette, QColor, QIcon
    from PyQt6.QtWidgets import QApplication

    from ui.main_window import MainWindow, get_resource_path
    from ui.splash import SplashScreen

    DEBUG_MODE = args.debug

    from core.config_manager import get_app_data_dir
    log_dir = get_app_data_dir()
    log_file = os.path.join(log_dir, "debug.log")

    redirector = LogRedirector(log_file)
    sys.stdout = redirector
    sys.stderr = redirector

    print(f"[LOG] 程序启动，日志文件: {log_file}")

    app = QApplication(sys.argv)
    app_icon_path = get_resource_path(os.path.join("assets", "NekroAgent.ico"))
    if os.path.exists(app_icon_path):
        app.setWindowIcon(QIcon(app_icon_path))

    app.setStyle("Fusion")
    light_palette = QPalette()
    light_palette.setColor(QPalette.ColorRole.Window, QColor(255, 255, 255))
    light_palette.setColor(QPalette.ColorRole.WindowText, QColor(0, 0, 0))
    light_palette.setColor(QPalette.ColorRole.Base, QColor(255, 255, 255))
    light_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(245, 245, 245))
    light_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(255, 255, 255))
    light_palette.setColor(QPalette.ColorRole.ToolTipText, QColor(0, 0, 0))
    light_palette.setColor(QPalette.ColorRole.Text, QColor(0, 0, 0))
    light_palette.setColor(QPalette.ColorRole.Button, QColor(240, 240, 240))
    light_palette.setColor(QPalette.ColorRole.ButtonText, QColor(0, 0, 0))
    light_palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    light_palette.setColor(QPalette.ColorRole.Link, QColor(9, 105, 218))
    light_palette.setColor(QPalette.ColorRole.Highlight, QColor(9, 105, 218))
    light_palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(light_palette)

    splash = SplashScreen()
    splash.start()

    window = None

    def _create_main():
        nonlocal window
        window = MainWindow(splash=splash)
        window.debug_mode = DEBUG_MODE

    QTimer.singleShot(600, _create_main)

    return app.exec()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help='启用 debug 日志')
    parser.add_argument('--backend-check', action='store_true', help='运行后端环境检查后退出')
    args = parser.parse_args()

    if sys.platform == 'win32':
        os.environ['PYTHONIOENCODING'] = 'utf-8'

    if args.backend_check:
        sys.exit(_run_backend_check())

    sys.exit(_run_gui(args))


if __name__ == "__main__":
    main()
