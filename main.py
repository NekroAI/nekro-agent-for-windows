import sys
import os
import argparse
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPalette, QColor, QIcon
from PyQt6.QtWidgets import QApplication
from ui.main_window import MainWindow, get_resource_path
from ui.splash import SplashScreen

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


def main():
    global DEBUG_MODE

    # 解析命令行参数
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help='启用 debug 日志')
    args = parser.parse_args()
    DEBUG_MODE = args.debug

    if sys.platform == 'win32':
        os.environ['PYTHONIOENCODING'] = 'utf-8'

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

    # 强制使用 Fusion 风格 + 亮色调色板，不跟随系统深色模式
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

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
