import sys
import os
import argparse
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtWidgets import QApplication
from ui.main_window import MainWindow

# 全局 debug 标志
DEBUG_MODE = False


class LogRedirector:
    """将 stdout 和 stderr 重定向到文件和控制台（安全处理编码）"""
    def __init__(self, log_file):
        self.log_file = log_file
        self.file = open(log_file, 'w', encoding='utf-8', buffering=1)
        # 保存原始的 stdout/stderr（已经是打开状态的文件对象）
        self.console = sys.__stdout__

    def write(self, message):
        try:
            # 写入文件（使用 UTF-8）
            self.file.write(message)
            self.file.flush()
        except Exception as e:
            pass

        try:
            # 写入控制台（尝试用原始 stdout）
            self.console.write(message)
            self.console.flush()
        except UnicodeEncodeError:
            # 如果编码出错，用 errors='replace' 重新尝试
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
    parser.add_argument(
        '--disable-webview-gpu',
        action='store_true',
        help='禁用内置 WebView GPU 加速，仅在出现闪烁或渲染异常时使用',
    )
    args = parser.parse_args()
    DEBUG_MODE = args.debug

    # 强制设置控制台编码为 UTF-8（Windows 兼容）
    if sys.platform == 'win32':
        os.environ['PYTHONIOENCODING'] = 'utf-8'

    # 设置日志文件（放到用户目录，避免权限问题）
    log_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "NekroAgent")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "debug.log")

    # 重定向 stdout 和 stderr 到日志文件和控制台
    redirector = LogRedirector(log_file)
    sys.stdout = redirector
    sys.stderr = redirector

    print(f"[LOG] 程序启动，日志文件: {log_file}")

    # 尝试禁用无障碍功能以规避某些 Windows 环境下的刷屏报错
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--disable-features=Accessibility"

    chromium_flags = ["--disable-features=CalculateNativeWinOcclusion"]

    # 默认保留 GPU 加速，避免内置 WebView 退化为软件渲染导致明显卡顿。
    # 同时关闭 Windows 原生遮挡检测，减少部分机器上的偶发闪烁。
    # 仅在个别机器出现驱动兼容问题时允许手动降级。
    if args.disable_webview_gpu:
        chromium_flags.append("--disable-gpu")
        print("[LOG] 已按参数禁用 WebView GPU 加速")
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(chromium_flags)

    # WebEngine 官方要求在创建 QApplication 前设置，能减少多窗口/嵌入场景的图形问题。
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

    app = QApplication(sys.argv)

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

    # 实例化并显示主窗口
    window = MainWindow()
    window.debug_mode = DEBUG_MODE
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
