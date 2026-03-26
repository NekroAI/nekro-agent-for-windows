import re
import os
import sys
import webbrowser
from collections import OrderedDict

from PyQt6.QtCore import QTimer, Qt, QUrl
from PyQt6.QtGui import QCloseEvent, QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView

from core.backend_factory import BackendFactory
from core.config_manager import ConfigManager
from ui.styles import STYLESHEET
from ui.widgets import ActionButton, MetricCard, SectionCard


def get_resource_path(relative_path):
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nekro Agent 管理")
        self.resize(1220, 820)
        self.setMinimumSize(880, 620)
        self.setStyleSheet(STYLESHEET)

        self.config = ConfigManager()
        self.backend = BackendFactory.create(self.config)
        self._quit_after_stop = False
        self._responsive_buttons = []
        self._last_status = ""
        self._uninstall_in_progress = False
        self._pull_layers = OrderedDict()
        self._pull_events = []
        self._pull_header = ""
        self.browser_urls = {
            "nekro": f"http://localhost:{self.config.get('nekro_port') or 8021}",
            "napcat": f"http://localhost:{self.config.get('napcat_port') or 6099}",
        }
        self.current_browser_target = "nekro"

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self._build_sidebar(main_layout)

        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack, 1)

        self.init_home_page()
        self.init_browser_page()
        self.init_logs_page()
        self.init_files_page()
        self.init_settings_page()
        self.switch_tab(0)

        self.backend.log_received.connect(self.append_log)
        self.backend.progress_updated.connect(self._on_backend_progress)
        self.backend.status_changed.connect(self.update_status_ui)
        self.backend.deploy_info_ready.connect(self._show_credentials_dialog)

        self._build_tray_icon()
        QTimer.singleShot(200, self._on_startup)
        QTimer.singleShot(0, self._apply_responsive_layout)

    def _build_sidebar(self, root_layout):
        self.sidebar = QFrame()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(248)

        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(22, 24, 22, 24)
        sidebar_layout.setSpacing(10)

        brand_layout = QHBoxLayout()
        brand_layout.setSpacing(12)

        self.logo_label = QLabel()
        self.logo_label.setFixedSize(42, 42)
        self.logo_label.setScaledContents(True)

        icon_path = get_resource_path(os.path.join("assets", "NekroAgent.png"))
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
            self.logo_label.setPixmap(QPixmap(icon_path))

        brand_text = QVBoxLayout()
        eyebrow = QLabel("本地部署控制台")
        eyebrow.setObjectName("SidebarEyebrow")
        title = QLabel("Nekro Agent")
        title.setObjectName("SidebarTitle")
        subtitle = QLabel("Windows 启动器")
        subtitle.setObjectName("SidebarSubtitle")
        brand_text.addWidget(eyebrow)
        brand_text.addWidget(title)
        brand_text.addWidget(subtitle)

        brand_layout.addWidget(self.logo_label)
        brand_layout.addLayout(brand_text, 1)
        sidebar_layout.addLayout(brand_layout)
        sidebar_layout.addSpacing(22)

        self.btn_home = self.create_sidebar_btn("总览控制台", 0)
        self.btn_browser = self.create_sidebar_btn("服务访问", 1)
        self.btn_logs = self.create_sidebar_btn("日志中心", 2)
        self.btn_files = self.create_sidebar_btn("存储与路径", 3)
        self.btn_settings = self.create_sidebar_btn("系统设置", 4)

        for button in [self.btn_home, self.btn_browser, self.btn_logs, self.btn_files, self.btn_settings]:
            sidebar_layout.addWidget(button)

        sidebar_layout.addStretch()

        footnote = QLabel("为 Nekro Agent 提供本地部署与运行入口")
        footnote.setObjectName("SidebarFootnote")
        footnote.setWordWrap(True)
        sidebar_layout.addWidget(footnote)

        root_layout.addWidget(self.sidebar)

    def _add_page(self, page):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(page)
        self.stack.addWidget(scroll)

    def _register_responsive_buttons(self, *buttons):
        self._responsive_buttons.extend(buttons)

    def _apply_responsive_layout(self):
        width = max(self.width(), 880)
        scale = min(1.0, width / 1220.0)
        compact = width < 1040

        sidebar_width = 248 if not compact else 212
        self.sidebar.setFixedWidth(sidebar_width)

        logo_size = 42 if not compact else 34
        self.logo_label.setFixedSize(logo_size, logo_size)

        for button in self._responsive_buttons:
            button.set_scale(scale)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def _build_tray_icon(self):
        self.tray_icon = QSystemTrayIcon(self)
        icon_path = get_resource_path(os.path.join("assets", "NekroAgent.png"))
        if os.path.exists(icon_path):
            self.tray_icon.setIcon(QIcon(icon_path))

        tray_menu = QMenu()
        show_action = tray_menu.addAction("显示主窗口")
        show_action.triggered.connect(self.show)
        quit_action = tray_menu.addAction("退出")
        quit_action.triggered.connect(self._quit_app)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)

    def _show_notice_dialog(self, title, text, button_text="确定", danger=False):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(340)
        dialog.setMaximumWidth(440)
        dialog.setModal(True)
        dialog.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title_label = QLabel(title)
        title_label.setProperty("role", "dialog_title")
        title_label.setWordWrap(True)
        title_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(title_label)

        desc_label = QLabel(text)
        desc_label.setProperty("role", "dialog_desc")
        desc_label.setWordWrap(True)
        desc_label.setTextFormat(Qt.TextFormat.PlainText)
        desc_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(desc_label)

        button_row = QHBoxLayout()
        button_row.addStretch()

        confirm_button = QPushButton(button_text)
        if danger:
            confirm_button.setStyleSheet(
                "QPushButton { min-height: 36px; background: #c94f63; color: white; "
                "border: 1px solid #c94f63; border-radius: 10px; padding: 0 16px; font-size: 13px; font-weight: 600; }"
                "QPushButton:hover { background: #b84558; border-color: #b84558; }"
            )
        confirm_button.clicked.connect(dialog.accept)
        button_row.addWidget(confirm_button)

        layout.addLayout(button_row)
        dialog.adjustSize()
        dialog.exec()

    def _show_confirm_dialog(self, title, text, confirm_text="确认", cancel_text="取消", danger=False):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(360)
        dialog.setMaximumWidth(460)
        dialog.setModal(True)
        dialog.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title_label = QLabel(title)
        title_label.setProperty("role", "dialog_title")
        title_label.setWordWrap(True)
        title_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(title_label)

        desc_label = QLabel(text)
        desc_label.setProperty("role", "dialog_desc")
        desc_label.setWordWrap(True)
        desc_label.setTextFormat(Qt.TextFormat.PlainText)
        desc_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(desc_label)

        button_row = QHBoxLayout()
        button_row.setSpacing(10)
        button_row.addStretch()

        cancel_button = QPushButton(cancel_text)
        cancel_button.clicked.connect(dialog.reject)
        button_row.addWidget(cancel_button)

        confirm_button = QPushButton(confirm_text)
        if danger:
            confirm_button.setStyleSheet(
                "QPushButton { min-height: 36px; background: #c94f63; color: white; "
                "border: 1px solid #c94f63; border-radius: 10px; padding: 0 16px; font-size: 13px; font-weight: 600; }"
                "QPushButton:hover { background: #b84558; border-color: #b84558; }"
            )
        else:
            confirm_button.setStyleSheet(
                "QPushButton { min-height: 36px; background: #1b6db4; color: white; "
                "border: 1px solid #1b6db4; border-radius: 10px; padding: 0 16px; font-size: 13px; font-weight: 600; }"
                "QPushButton:hover { background: #185f9d; border-color: #185f9d; }"
            )
        confirm_button.clicked.connect(dialog.accept)
        button_row.addWidget(confirm_button)

        layout.addLayout(button_row)
        dialog.adjustSize()
        return dialog.exec() == int(QDialog.DialogCode.Accepted)

    def create_sidebar_btn(self, text, index):
        button = QPushButton(text)
        button.setProperty("nav", True)
        button.setCheckable(True)
        button.setFixedHeight(46)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(lambda: self.switch_tab(index))
        return button

    def switch_tab(self, index):
        self.stack.setCurrentIndex(index)
        buttons = [self.btn_home, self.btn_browser, self.btn_logs, self.btn_files, self.btn_settings]
        for current, button in enumerate(buttons):
            button.setChecked(current == index)

    def _on_startup(self):
        if self.config.get("first_run") or not self.config.get("deploy_mode"):
            self._show_first_run_dialog()
        else:
            self.start_deploy(show_logs=False)

    def _show_first_run_dialog(self):
        from ui.first_run_dialog import FirstRunDialog

        dialog = FirstRunDialog(self.backend, self.config, parent=self)
        dialog.deploy_requested.connect(self._on_deploy_mode_selected)
        dialog.backend_changed.connect(lambda key: self._switch_backend(key, dialog))
        dialog.exec()

    def _switch_backend(self, backend_key, dialog):
        """用户在首次运行向导中选择了后端，重建 backend 实例"""
        # 断开旧后端信号
        self.backend.log_received.disconnect(self.append_log)
        self.backend.progress_updated.disconnect(self._on_backend_progress)
        self.backend.status_changed.disconnect(self.update_status_ui)
        self.backend.deploy_info_ready.disconnect(self._show_credentials_dialog)

        # 创建新后端
        self.backend = BackendFactory.create(self.config)

        # 连接新后端信号
        self.backend.log_received.connect(self.append_log)
        self.backend.progress_updated.connect(self._on_backend_progress)
        self.backend.status_changed.connect(self.update_status_ui)
        self.backend.deploy_info_ready.connect(self._show_credentials_dialog)

        # 更新对话框中的后端引用
        dialog.set_backend(self.backend)

    def _on_deploy_mode_selected(self, mode):
        self._is_first_deploy = True
        self.refresh_dashboard()
        self.start_deploy()

    def append_log(self, msg, level="info"):
        if level == "debug" and not getattr(self, "debug_mode", False):
            return
        if msg.startswith("[镜像拉取]") or msg.startswith("[沙盒镜像]"):
            return

        original_level = level
        color = {
            "error": "#f26f82",
            "warning": "#f2c15f",
            "warn": "#f2c15f",
            "debug": "#8fa4b8",
            "vm": "#8fa4b8",
        }.get(level, "#7ce0a3")

        if level == "warn":
            level = "warning"

        if level == "vm":
            formatted = f"<span style='color:{color};'>{msg}</span>"
        else:
            formatted = f"<span style='color:{color};'>[{level.upper()}]</span> {msg}"

        if level == "vm":
            if "napcat" in msg.lower():
                self.log_viewer_napcat.append(formatted)
            else:
                self.log_viewer_nekro.append(formatted)
        else:
            self.log_viewer_app.append(formatted)
            if hasattr(self, "log_preview"):
                self.log_preview.append(f"<span style='color:{color};'>[{level.upper()}] {msg}</span>")

        try:
            if original_level == "vm":
                print(msg)
            else:
                print(f"[{level.upper()}] {msg}")
        except Exception:
            pass

    def _set_log_tab(self, index):
        viewers = [self.log_viewer_app, self.log_viewer_nekro, self.log_viewer_napcat]
        buttons = [self.btn_log_app, self.btn_log_nekro, self.btn_log_napcat]
        for current, viewer in enumerate(viewers):
            viewer.setVisible(current == index)
        for current, button in enumerate(buttons):
            button.setChecked(current == index)

    def _set_pull_view_visible(self, visible):
        if hasattr(self, "pull_view_frame"):
            self.pull_view_frame.setVisible(visible)

    # layer 状态排序优先级：进行中的在上面，已完成的沉底
    _LAYER_STATUS_ORDER = {
        "Waiting":          0,
        "Pulling fs layer": 1,
        "Downloading":      2,
        "Verifying":        3,
        "Extracting":       4,
        "Download complete": 5,
        "Pull complete":    6,
        "Already exists":   7,
    }

    def _layer_sort_key(self, item):
        """按状态优先级排序 layer，未知状态排在中间"""
        _layer_id, status = item
        # 状态文本可能带进度条，只取第一个单词或已知前缀匹配
        for key, order in self._LAYER_STATUS_ORDER.items():
            if status.startswith(key):
                return order
        return 3  # 未知状态排中间

    def _render_pull_view(self):
        if not hasattr(self, "pull_viewer"):
            return
        lines = []
        if self._pull_header:
            lines.append(self._pull_header)
            lines.append("")

        # 按状态分组排序：进行中在上，已完成在下
        sorted_layers = sorted(self._pull_layers.items(), key=self._layer_sort_key)
        for layer_id, status in sorted_layers:
            lines.append(f"{layer_id:<14s}{status}")

        # 底部汇总信息（Digest / Status 等）
        if self._pull_events:
            if self._pull_layers:
                lines.append("")
            for event in self._pull_events[-6:]:
                lines.append(event)

        self.pull_viewer.setPlainText("\n".join(lines).strip())
        self.pull_viewer.verticalScrollBar().setValue(self.pull_viewer.verticalScrollBar().maximum())

    def _update_pull_view(self, header="", detail=""):
        if not hasattr(self, "pull_viewer"):
            return
        if header:
            self._pull_header = header
            self.pull_status_label.setText(header)
        if detail:
            # 匹配 docker pull 的 layer 行: "abc123ef: Downloading [==>   ] 12.3MB/45.6MB"
            layer_match = re.match(r"^([a-f0-9]{6,64}):\s*(.+)$", detail, re.IGNORECASE)
            if layer_match:
                layer_id, status = layer_match.groups()
                # 截取短 ID（前12位），与原生 docker pull 保持一致
                short_id = layer_id[:12]
                self._pull_layers[short_id] = status
            elif detail.startswith(("Pulling ", "Digest:", "Status:", "latest:", "Already exists")):
                # docker pull 的汇总信息，放到 events
                self._pull_events.append(detail)
                self._pull_events = self._pull_events[-12:]
            else:
                # 其他进度行也作为 event 显示
                self._pull_events.append(detail)
                self._pull_events = self._pull_events[-12:]
        self._set_pull_view_visible(True)
        self._render_pull_view()

    def _clear_pull_progress(self):
        if not hasattr(self, "pull_viewer"):
            return
        self._pull_layers.clear()
        self._pull_events.clear()
        self._pull_header = ""
        self.pull_status_label.setText("")
        self.pull_viewer.clear()
        self._set_pull_view_visible(False)

    def _on_backend_progress(self, text):
        if text.startswith("__pull_progress__|"):
            _, phase, message = text.split("|", 2)
            if phase == "start":
                self._clear_pull_progress()
                self._update_pull_view(header=message)
            elif phase == "update":
                self._update_pull_view(detail=message)
            elif phase == "stage":
                # 切换到下一个镜像阶段，清理旧 layer 状态
                self._pull_layers.clear()
                self._update_pull_view(header=message)
            elif phase == "done":
                self._pull_events.append("")
                self._pull_events.append(f"✓ {message}")
                self._update_pull_view(header=message)
                QTimer.singleShot(2000, self._clear_pull_progress)
            elif phase == "error":
                self._pull_events.append(f"✗ {message}")
                self._update_pull_view(header=message)
            return

        if text in {"启动 Compose 服务...", "拉取 Docker 镜像..."}:
            # 普通阶段提示，只更新状态标签，不显示 pull 终端框
            if hasattr(self, "pull_status_label"):
                self.pull_status_label.setText(text)
            return
        if text in {"__docker_done__", "__docker_fail__"}:
            self._clear_pull_progress()
            return

    def _format_mode_text(self, mode):
        if mode == "napcat":
            return "完整版 (napcat)"
        if mode == "lite":
            return "精简版 (lite)"
        return "未选择"

    def _target_label(self, target):
        return "NapCat" if target == "napcat" else "Nekro Agent"

    def _target_url(self, target=None):
        return self.browser_urls.get(target or self.current_browser_target, self.browser_urls["nekro"])

    def _can_access_target(self, target):
        return target == "nekro" or self.config.get("deploy_mode") == "napcat"

    def _set_browser_target(self, target, force_reload=False):
        if not self._can_access_target(target):
            self._show_notice_dialog("提示", "当前部署模式未启用 NapCat。")
            return

        self.current_browser_target = target
        self.btn_browser_nekro.setChecked(target == "nekro")
        self.btn_browser_napcat.setChecked(target == "napcat")
        self.btn_browser_napcat.setVisible(self.config.get("deploy_mode") == "napcat")

        target_name = self._target_label(target)
        target_url = self._target_url(target)
        self.browser_url_label.setText(f"当前地址: {target_url}")
        self.browser_hint.setText(f"内置 WebView 访问 {target_name} 管理界面；服务未就绪时可点击刷新重试。")

        if getattr(self.backend, "is_running", False):
            current_url = self.webview.url().toString()
            if force_reload or current_url != target_url:
                self.webview.setUrl(QUrl(target_url))
            else:
                self.webview.reload()
        else:
            placeholder = (
                f"{target_name} 服务尚未启动。<br><br>"
                "先在“总览控制台”完成部署，然后回到这里点击“刷新内嵌页面”。"
            )
            self.webview.setHtml(f"<html><body style='font-family:Segoe UI;padding:24px;color:#243649;'>{placeholder}</body></html>")

    def _reload_browser_view(self):
        self._set_browser_target(self.current_browser_target, force_reload=True)

    def _open_current_in_browser(self):
        webbrowser.open(self._target_url())

    def refresh_dashboard(self):
        if not hasattr(self, "status_badge"):
            return

        mode_text = self._format_mode_text(self.config.get("deploy_mode"))
        data_dir = self.config.get("data_dir") or "/root/nekro_agent_data"
        host_data = self.backend.get_host_access_path(data_dir) or "当前后端暂未提供直接映射"

        self.metric_backend.findChild(QLabel, "MetricValue").setText(self.backend.display_name)
        self.metric_mode.findChild(QLabel, "MetricValue").setText(mode_text)
        self.metric_data_dir.findChild(QLabel, "MetricValue").setText(data_dir)
        self.metric_data_dir.findChild(QLabel, "MetricHint").setText(
            f"宿主机访问路径: {host_data}"
        )

        if hasattr(self, "mode_display"):
            self.mode_display.setText(mode_text)
        if hasattr(self, "wsldir_edit"):
            self.wsldir_edit.setText(self.config.get("wsl_install_dir") or "未配置")

    def start_deploy(self, show_logs=True):
        if self.backend.is_running:
            self._show_notice_dialog("提示", "服务已在运行中")
            return

        deploy_mode = self.config.get("deploy_mode")
        if not deploy_mode:
            self._show_first_run_dialog()
            deploy_mode = self.config.get("deploy_mode")
            if not deploy_mode:
                return

        if show_logs:
            self.switch_tab(2)
        self.log_viewer_app.clear()
        self.log_viewer_app.append(f"<span style='color:#7ce0a3;'>[INFO]</span> 开始部署服务 (模式: {deploy_mode})...")
        if hasattr(self, "log_preview"):
            self.log_preview.clear()
            self.log_preview.append(f"<span style='color:#7ce0a3;'>[INFO]</span> 开始部署服务 (模式: {deploy_mode})...")

        self.backend.start_services(deploy_mode)

    def update_status_ui(self, status):
        previous_status = self._last_status
        self._last_status = status
        self.status_badge.setText(f"状态: {status}")

        running = status == "运行中"
        was_running = previous_status == "运行中"
        self.metric_status.findChild(QLabel, "MetricValue").setText(status)
        self.metric_status.findChild(QLabel, "MetricHint").setText("服务可访问" if running else "等待部署或启动")
        self.metric_status.setProperty("accent", "green" if running else "red")
        self.metric_status.style().unpolish(self.metric_status)
        self.metric_status.style().polish(self.metric_status)

        self.btn_deploy_action.setEnabled(not running)
        self.btn_primary_deploy.setEnabled(not running)

        if running:
            self.btn_primary_deploy.setText("服务运行中")
            if hasattr(self, "_is_first_deploy") and self._is_first_deploy:
                deploy_mode = self.config.get("deploy_mode")
                nekro_port = self.config.get("nekro_port") or 8021
                napcat_port = self.config.get("napcat_port") or 6099
                message = "服务已启动。\n\n建议收藏以下地址：\n\n"
                message += f"Nekro Agent: http://localhost:{nekro_port}"
                if deploy_mode == "napcat":
                    message += f"\nNapCat: http://localhost:{napcat_port}"
                self._show_notice_dialog("服务已启动", message)
                self._is_first_deploy = False
            if hasattr(self, "webview") and not was_running:
                self.switch_tab(1)
                self._set_browser_target(self.current_browser_target, force_reload=True)
            self._clear_pull_progress()
        else:
            self.btn_primary_deploy.setText("开始部署")
            if self._quit_after_stop and status in {"已停止", "已卸载"}:
                self._quit_after_stop = False
                QApplication.quit()
            if hasattr(self, "webview") and was_running:
                self._set_browser_target(self.current_browser_target, force_reload=False)
            if status in {"启动失败", "更新失败", "启动超时", "已停止", "已卸载"}:
                self._clear_pull_progress()

        if status == "已卸载":
            self.refresh_dashboard()
            if self._uninstall_in_progress:
                self._uninstall_in_progress = False
                self._show_notice_dialog("卸载完成", "运行环境已卸载完成。")

        if hasattr(self, "btn_browser_napcat"):
            self.btn_browser_napcat.setVisible(self.config.get("deploy_mode") == "napcat")
        if hasattr(self, "browser_open_external_btn"):
            self.browser_open_external_btn.setEnabled(running)
        if hasattr(self, "browser_reload_btn"):
            self.browser_reload_btn.setEnabled(True)

    def init_home_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(34, 30, 34, 30)
        layout.setSpacing(22)

        hero = QFrame()
        hero.setObjectName("HeroCard")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(24, 22, 24, 22)
        hero_layout.setSpacing(14)

        hero_top = QHBoxLayout()
        hero_text = QVBoxLayout()
        hero_text.setSpacing(6)

        hero_eyebrow = QLabel("运行状态总览")
        hero_eyebrow.setObjectName("HeroEyebrow")
        hero_title = QLabel("Nekro Agent 启动控制台")
        hero_title.setObjectName("HeroTitle")
        hero_desc = QLabel("集中处理环境检查、部署启动、日志查看和本地运行入口。")
        hero_desc.setObjectName("HeroDesc")
        hero_desc.setWordWrap(True)

        hero_text.addWidget(hero_eyebrow)
        hero_text.addWidget(hero_title)
        hero_text.addWidget(hero_desc)
        hero_top.addLayout(hero_text, 1)

        self.status_badge = QLabel("状态: 未就绪")
        self.status_badge.setObjectName("StatusBadge")
        hero_top.addWidget(self.status_badge, 0, Qt.AlignmentFlag.AlignTop)
        hero_layout.addLayout(hero_top)

        hero_actions = QHBoxLayout()
        self.btn_primary_deploy = QPushButton("开始部署")
        self.btn_primary_deploy.setObjectName("HeroPrimary")
        self.btn_primary_deploy.clicked.connect(self.start_deploy)
        self.btn_primary_update = QPushButton("更新运行环境")
        self.btn_primary_update.setObjectName("HeroSecondary")
        self.btn_primary_update.clicked.connect(self._update_services)
        self.btn_primary_creds = QPushButton("查看部署凭据")
        self.btn_primary_creds.setObjectName("HeroSecondary")
        self.btn_primary_creds.clicked.connect(self._show_saved_credentials)

        hero_actions.addWidget(self.btn_primary_deploy)
        hero_actions.addWidget(self.btn_primary_update)
        hero_actions.addWidget(self.btn_primary_creds)
        hero_actions.addStretch()
        hero_layout.addLayout(hero_actions)

        layout.addWidget(hero)

        metrics = QGridLayout()
        metrics.setHorizontalSpacing(16)
        metrics.setVerticalSpacing(16)
        self.metric_status = MetricCard("服务状态", "未就绪", "等待部署或启动", "red")
        self.metric_backend = MetricCard("运行后端", self.backend.display_name, "当前系统配置", "blue")
        self.metric_mode = MetricCard("部署版本", self._format_mode_text(self.config.get("deploy_mode")), "首次运行向导可修改", "amber")
        self.metric_data_dir = MetricCard(
            "数据目录",
            self.config.get("data_dir") or "/root/nekro_agent_data",
            "宿主机访问路径将在这里显示",
            "green",
        )
        metrics.addWidget(self.metric_status, 0, 0)
        metrics.addWidget(self.metric_backend, 0, 1)
        metrics.addWidget(self.metric_mode, 1, 0)
        metrics.addWidget(self.metric_data_dir, 1, 1)
        layout.addLayout(metrics)

        bottom_grid = QGridLayout()
        bottom_grid.setHorizontalSpacing(16)
        bottom_grid.setVerticalSpacing(16)

        actions_card = SectionCard("快速操作", "保留最常用的部署与维护入口。")
        actions_layout = actions_card.body_layout()
        actions_grid = QGridLayout()
        actions_grid.setHorizontalSpacing(14)
        actions_grid.setVerticalSpacing(16)

        self.btn_env_check = ActionButton("CHK", "环境检查", f"重新运行 {self.backend.display_name} 初始化向导")
        self.btn_deploy_action = ActionButton("RUN", "一键部署", "启动容器并写入运行配置", "primary")
        self.btn_update_action = ActionButton("UPD", "检查更新", "拉取镜像并重启服务")
        self.btn_uninstall_action = ActionButton("DEL", "卸载清理", "删除容器、镜像和运行环境", "danger")

        self.btn_env_check.clicked.connect(self._show_first_run_dialog)
        self.btn_deploy_action.clicked.connect(self.start_deploy)
        self.btn_update_action.clicked.connect(self._update_services)
        self.btn_uninstall_action.clicked.connect(self._uninstall_environment)

        actions_grid.addWidget(self.btn_env_check, 0, 0)
        actions_grid.addWidget(self.btn_deploy_action, 0, 1)
        actions_grid.addWidget(self.btn_update_action, 1, 0)
        actions_grid.addWidget(self.btn_uninstall_action, 1, 1)
        actions_layout.addLayout(actions_grid)

        activity_card = SectionCard("实时摘要", "显示最近的应用日志，完整内容在日志中心查看。")
        activity_layout = activity_card.body_layout()
        self.log_preview = QTextEdit()
        self.log_preview.setObjectName("LogViewer")
        self.log_preview.setReadOnly(True)
        self.log_preview.setMinimumHeight(250)
        activity_layout.addWidget(self.log_preview)

        bottom_grid.addWidget(actions_card, 0, 0)
        bottom_grid.addWidget(activity_card, 0, 1)
        layout.addLayout(bottom_grid)

        self._register_responsive_buttons(
            self.btn_env_check,
            self.btn_deploy_action,
            self.btn_update_action,
            self.btn_uninstall_action,
        )
        self._add_page(page)
        self.refresh_dashboard()

    def init_browser_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(34, 30, 34, 30)
        layout.setSpacing(18)

        card = SectionCard("服务访问", "在应用内直接访问管理界面，仍可按需切换到系统浏览器。")
        card_layout = card.body_layout()

        target_row = QHBoxLayout()
        self.btn_browser_nekro = QPushButton("Nekro Agent")
        self.btn_browser_nekro.setObjectName("SegmentBtn")
        self.btn_browser_nekro.setCheckable(True)
        self.btn_browser_nekro.clicked.connect(lambda: self._set_browser_target("nekro"))
        target_row.addWidget(self.btn_browser_nekro)

        self.btn_browser_napcat = QPushButton("NapCat")
        self.btn_browser_napcat.setObjectName("SegmentBtn")
        self.btn_browser_napcat.setCheckable(True)
        self.btn_browser_napcat.clicked.connect(lambda: self._set_browser_target("napcat"))
        self.btn_browser_napcat.setVisible(self.config.get("deploy_mode") == "napcat")
        target_row.addWidget(self.btn_browser_napcat)
        target_row.addStretch()
        card_layout.addLayout(target_row)

        toolbar = QHBoxLayout()
        self.browser_url_label = QLabel()
        self.browser_url_label.setObjectName("SectionDesc")
        toolbar.addWidget(self.browser_url_label, 1)

        self.browser_reload_btn = QPushButton("刷新内嵌页面")
        self.browser_reload_btn.clicked.connect(self._reload_browser_view)
        toolbar.addWidget(self.browser_reload_btn)

        self.browser_open_external_btn = QPushButton("在系统浏览器打开")
        self.browser_open_external_btn.clicked.connect(self._open_current_in_browser)
        toolbar.addWidget(self.browser_open_external_btn)
        card_layout.addLayout(toolbar)

        self.browser_hint = QLabel()
        self.browser_hint.setObjectName("SectionDesc")
        self.browser_hint.setWordWrap(True)
        card_layout.addWidget(self.browser_hint)

        self.webview = QWebEngineView()
        self.webview.setMinimumHeight(560)
        self.webview.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.webview.setAutoFillBackground(True)
        self.webview.setStyleSheet("background: #f4f7fb; border: 1px solid #dfe7ef; border-radius: 8px;")
        self.webview.page().setBackgroundColor(QColor("#f4f7fb"))
        card_layout.addWidget(self.webview)

        layout.addWidget(card)
        layout.addStretch()
        self._set_browser_target("nekro")
        self._add_page(page)

    def init_logs_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(34, 30, 34, 30)
        layout.setSpacing(18)

        card = SectionCard("日志中心", "按来源查看应用日志和容器日志，用于部署排查与运行观察。")
        card_layout = card.body_layout()

        self.pull_view_frame = QFrame()
        self.pull_view_frame.setObjectName("SectionCard")
        pull_view_layout = QVBoxLayout(self.pull_view_frame)
        pull_view_layout.setContentsMargins(16, 14, 16, 14)
        pull_view_layout.setSpacing(8)

        self.pull_status_label = QLabel("")
        self.pull_status_label.setObjectName("SectionDesc")
        self.pull_status_label.setWordWrap(True)
        pull_view_layout.addWidget(self.pull_status_label)

        self.pull_viewer = QTextEdit()
        self.pull_viewer.setObjectName("LogViewer")
        self.pull_viewer.setReadOnly(True)
        self.pull_viewer.setMinimumHeight(200)
        self.pull_viewer.setStyleSheet(
            "QTextEdit { font-family: 'Cascadia Mono', 'Consolas', 'Courier New', monospace; "
            "font-size: 12px; background: #1e1e1e; color: #cccccc; "
            "border: 1px solid #333; border-radius: 6px; padding: 10px; }"
        )
        pull_view_layout.addWidget(self.pull_viewer)

        self.pull_view_frame.setVisible(False)
        card_layout.addWidget(self.pull_view_frame)

        top = QHBoxLayout()
        self.btn_log_app = QPushButton("应用日志")
        self.btn_log_nekro = QPushButton("Nekro Agent")
        self.btn_log_napcat = QPushButton("NapCat")

        for idx, button in enumerate([self.btn_log_app, self.btn_log_nekro, self.btn_log_napcat]):
            button.setObjectName("SegmentBtn")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda checked, current=idx: self._set_log_tab(current))
            top.addWidget(button)
        top.addStretch()
        card_layout.addLayout(top)

        self.log_viewer_app = QTextEdit()
        self.log_viewer_nekro = QTextEdit()
        self.log_viewer_napcat = QTextEdit()
        for viewer in [self.log_viewer_app, self.log_viewer_nekro, self.log_viewer_napcat]:
            viewer.setObjectName("LogViewer")
            viewer.setReadOnly(True)
            card_layout.addWidget(viewer)

        self._set_log_tab(0)

        layout.addWidget(card)
        self._add_page(page)

    def init_settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(34, 30, 34, 30)
        layout.setSpacing(18)

        card = SectionCard("系统设置", "控制后端、数据路径和系统集成选项。")
        card_layout = card.body_layout()

        self.check_auto = QCheckBox("开机自动启动 Nekro Agent 管理系统")
        self.check_auto.setChecked(self.config.get("autostart"))
        self.check_auto.stateChanged.connect(lambda state: self.config.set("autostart", state == 2))
        card_layout.addWidget(self.check_auto)

        self._backend_label = QLabel("运行时后端")
        card_layout.addWidget(self._backend_label)
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("WSL", "wsl")
        self.backend_combo.addItem("Hyper-V", "hyperv")
        current_backend = self.config.get("backend") or "wsl"
        self.backend_combo.setCurrentIndex(0 if current_backend == "wsl" else 1)
        self.backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        # 当前版本仅支持 WSL，隐藏后端切换
        self._backend_label.setVisible(False)
        self.backend_combo.setVisible(False)
        card_layout.addWidget(self.backend_combo)

        card_layout.addWidget(QLabel("部署版本"))
        self.mode_display = QLineEdit(self._format_mode_text(self.config.get("deploy_mode")))
        self.mode_display.setReadOnly(True)
        card_layout.addWidget(self.mode_display)

        card_layout.addWidget(QLabel(f"{self.backend.display_name} 安装目录"))
        self.wsldir_edit = QLineEdit(self.config.get("wsl_install_dir") or "未配置")
        self.wsldir_edit.setReadOnly(True)
        card_layout.addWidget(self.wsldir_edit)

        card_layout.addWidget(QLabel("数据目录 (运行环境内路径)"))
        datadir_box = QHBoxLayout()
        self.datadir_edit = QLineEdit(self.config.get("data_dir") or "/root/nekro_agent_data")
        self.datadir_edit.setPlaceholderText("/root/nekro_agent_data")
        self.datadir_edit.editingFinished.connect(self._save_data_dir)
        datadir_box.addWidget(self.datadir_edit)

        btn_open_datadir = QPushButton("打开目录")
        btn_open_datadir.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_open_datadir.clicked.connect(self._open_datadir_in_explorer)
        datadir_box.addWidget(btn_open_datadir)
        card_layout.addLayout(datadir_box)

        self.datadir_hint = QLabel()
        self.datadir_hint.setObjectName("SectionDesc")
        self.datadir_hint.setWordWrap(True)
        card_layout.addWidget(self.datadir_hint)
        self._refresh_datadir_hint()

        # 端口配置
        card_layout.addWidget(QLabel("Nekro Agent 端口"))
        self.nekro_port_setting = QLineEdit(str(self.config.get("nekro_port") or 8021))
        self.nekro_port_setting.setPlaceholderText("8021")
        self.nekro_port_setting.editingFinished.connect(self._save_ports)
        card_layout.addWidget(self.nekro_port_setting)

        card_layout.addWidget(QLabel("NapCat 端口"))
        self.napcat_port_setting = QLineEdit(str(self.config.get("napcat_port") or 6099))
        self.napcat_port_setting.setPlaceholderText("6099")
        self.napcat_port_setting.editingFinished.connect(self._save_ports)
        card_layout.addWidget(self.napcat_port_setting)

        port_hint = QLabel("修改端口后需重新部署服务才能生效。")
        port_hint.setObjectName("SectionDesc")
        port_hint.setWordWrap(True)
        card_layout.addWidget(port_hint)

        layout.addWidget(card)
        layout.addStretch()
        self._add_page(page)

    def _save_data_dir(self):
        self.config.set("data_dir", self.datadir_edit.text().strip())
        self._refresh_datadir_hint()
        self.refresh_dashboard()

    def _save_ports(self):
        try:
            nekro_port = int(self.nekro_port_setting.text().strip())
            napcat_port = int(self.napcat_port_setting.text().strip())
            if 1 <= nekro_port <= 65535 and 1 <= napcat_port <= 65535:
                self.config.set("nekro_port", nekro_port)
                self.config.set("napcat_port", napcat_port)
                self.browser_urls["nekro"] = f"http://localhost:{nekro_port}"
                self.browser_urls["napcat"] = f"http://localhost:{napcat_port}"
        except ValueError:
            pass

    def _refresh_datadir_hint(self):
        sample_path = self.backend.get_host_access_path(self.datadir_edit.text().strip() or "/root/nekro_agent_data")
        if sample_path:
            self.datadir_hint.setText(f"宿主机可访问路径: {sample_path}")
        else:
            self.datadir_hint.setText(f"当前后端 {self.backend.display_name} 暂未提供宿主机侧直接打开路径。")

    def _on_backend_changed(self, index):
        backend_key = self.backend_combo.itemData(index)
        if backend_key == self.config.get("backend"):
            return
        self.config.set("backend", backend_key)
        self._show_notice_dialog("提示", "后端已切换，重启应用后生效。")

    def _open_datadir_in_explorer(self):
        data_dir = self.datadir_edit.text().strip() or "/root/nekro_agent_data"
        win_path = self.backend.get_host_access_path(data_dir)
        if not win_path:
            self._show_notice_dialog("提示", f"当前后端 {self.backend.display_name} 暂不支持直接打开宿主机路径。")
            return
        try:
            os.startfile(win_path)
        except Exception as error:
            self._show_notice_dialog("提示", f"无法打开目录，请确认服务已启动且目录已创建。\n\n路径: {win_path}\n错误: {error}", danger=True)

    def _update_services(self):
        if not self.backend.is_running:
            self._show_notice_dialog("提示", "服务未运行，请先部署启动。")
            return

        reply = self._show_confirm_dialog(
            "确认更新",
            "将拉取最新镜像并重启所有容器，期间服务会短暂中断。\n确定要继续吗？",
            confirm_text="继续更新",
        )
        if not reply:
            return

        self.switch_tab(2)
        self.log_viewer_app.append("<span style='color:#7ce0a3;'>[INFO]</span> 开始更新服务...")
        self.backend.update_services()

    def _uninstall_environment(self):
        reply = self._show_confirm_dialog(
            "确认卸载",
            "此操作将：\n"
            "  1. 停止所有运行中的容器\n"
            "  2. 删除所有容器和镜像数据\n"
            f"  3. 删除 {self.backend.display_name} 运行环境\n\n"
            "此操作不可撤销，确定要继续吗？",
            confirm_text="确认卸载",
            danger=True,
        )
        if not reply:
            return

        self._uninstall_in_progress = True
        self.switch_tab(2)
        self.log_viewer_app.append("<span style='color:#7ce0a3;'>[INFO]</span> 开始卸载环境...")
        self.backend.uninstall_environment()

    def init_files_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(34, 30, 34, 30)
        layout.setSpacing(18)

        card = SectionCard("存储与路径", "通过 Windows 资源管理器访问运行环境内的重要目录。")
        card_layout = card.body_layout()

        dirs_info = [
            ("DATA", "数据目录", "存储数据库、配置、日志等运行数据", "data_dir", "/root/nekro_agent_data"),
            ("CONF", "部署目录", "存储 docker-compose 和 .env 配置文件", None, "/root/nekro_agent"),
        ]
        for badge, title, hint, config_key, default_path in dirs_info:
            button = ActionButton(badge, title, hint)
            wsl_path = self.config.get(config_key) or default_path if config_key else default_path
            button.clicked.connect(lambda checked, path=wsl_path: self._open_wsl_path(path))
            card_layout.addWidget(button)
            self._register_responsive_buttons(button)

        layout.addWidget(card)
        layout.addStretch()
        self._add_page(page)

    def _open_wsl_path(self, wsl_path):
        win_path = self.backend.get_host_access_path(wsl_path)
        if not win_path:
            self._show_notice_dialog("提示", f"当前后端 {self.backend.display_name} 暂不支持直接打开宿主机路径。")
            return
        try:
            os.startfile(win_path)
        except Exception as error:
            self._show_notice_dialog("提示", f"无法打开目录，请确认服务已启动且目录已创建。\n\n路径: {win_path}\n错误: {error}", danger=True)

    def closeEvent(self, event: QCloseEvent):
        if self.backend.is_running:
            choice = QDialog(self)
            choice.setWindowTitle("选择操作")
            choice.setMinimumWidth(360)
            choice.setMaximumWidth(460)
            choice.setModal(True)
            choice.setStyleSheet(STYLESHEET)

            layout = QVBoxLayout(choice)
            layout.setContentsMargins(20, 18, 20, 18)
            layout.setSpacing(12)

            title = QLabel("服务正在运行")
            title.setProperty("role", "dialog_title")
            title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            layout.addWidget(title)

            desc = QLabel("请选择关闭窗口时的处理方式。")
            desc.setProperty("role", "dialog_desc")
            desc.setWordWrap(True)
            desc.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            layout.addWidget(desc)

            button_row = QHBoxLayout()
            button_row.setSpacing(10)
            tray_button = QPushButton("最小化到托盘")
            tray_button.clicked.connect(lambda: choice.done(1))
            button_row.addWidget(tray_button)

            quit_button = QPushButton("停止服务并退出")
            quit_button.setStyleSheet(
                "QPushButton { min-height: 36px; background: #c94f63; color: white; "
                "border: 1px solid #c94f63; border-radius: 10px; padding: 0 16px; font-size: 13px; font-weight: 600; }"
                "QPushButton:hover { background: #b84558; border-color: #b84558; }"
            )
            quit_button.clicked.connect(lambda: choice.done(2))
            button_row.addWidget(quit_button)

            cancel_button = QPushButton("取消")
            cancel_button.clicked.connect(choice.reject)
            button_row.addWidget(cancel_button)

            layout.addLayout(button_row)
            choice.adjustSize()

            result = choice.exec()
            if result == 1:
                self.hide()
                self.tray_icon.show()
                self.tray_icon.showMessage("Nekro Agent", "已最小化到托盘，服务继续运行", QSystemTrayIcon.MessageIcon.Information, 2000)
                event.ignore()
            elif result == 2:
                self._quit_after_stop = True
                self.backend.stop_services()
                event.ignore()
            else:
                event.ignore()
        else:
            event.accept()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show()
            self.tray_icon.hide()

    def _quit_app(self):
        if self.backend.is_running:
            reply = self._show_confirm_dialog(
                "确认退出",
                "服务正在运行，退出将停止所有容器。确定要退出吗？",
                confirm_text="确认退出",
                danger=True,
            )
            if reply:
                self._quit_after_stop = True
                self.backend.stop_services()
        else:
            QApplication.quit()

    def _show_credentials_dialog(self, info):
        dialog = QDialog(self)
        dialog.setWindowTitle("部署凭据信息")
        dialog.resize(560, 460)
        dialog.setMinimumSize(520, 420)
        dialog.setModal(True)
        dialog.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(14)

        title = QLabel("部署完成，请妥善保存以下信息")
        title.setProperty("role", "dialog_title")
        title.setWordWrap(True)
        layout.addWidget(title)

        port = info.get("port", "8021")
        na_info = QLabel(
            f"<b style='color: #1b6db4;'>Nekro Agent</b><br>"
            f"<b>访问地址:</b> http://127.0.0.1:{port}<br>"
            f"<b>管理员账号:</b> admin<br>"
            f"<b>管理员密码:</b> {info.get('admin_password', '')}<br>"
            f"<b>OneBot 令牌:</b> {info.get('onebot_token', '')}"
        )
        na_info.setProperty("role", "info_block")
        na_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        na_info.setWordWrap(True)
        layout.addWidget(na_info)

        if info.get("deploy_mode") == "napcat":
            napcat_port = info.get("napcat_port", "6099")
            napcat_token = info.get("napcat_token", "") or "(等待捕获)"
            napcat_info = QLabel(
                f"<b style='color: #2b8a57;'>NapCat</b><br>"
                f"<b>访问地址:</b> http://127.0.0.1:{napcat_port}<br>"
                f"<b>登录 Token:</b> {napcat_token}"
            )
            napcat_info.setProperty("role", "info_block")
            napcat_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            napcat_info.setWordWrap(True)
            layout.addWidget(napcat_info)

        button_row = QHBoxLayout()
        button_row.addStretch()

        btn_copy = QPushButton("复制到剪贴板")
        btn_close = QPushButton("关闭")

        copy_text = (
            f"=== Nekro Agent ===\n"
            f"访问地址: http://127.0.0.1:{port}\n"
            f"管理员账号: admin\n"
            f"管理员密码: {info.get('admin_password', '')}\n"
            f"OneBot 令牌: {info.get('onebot_token', '')}"
        )
        if info.get("deploy_mode") == "napcat":
            copy_text += (
                f"\n\n=== NapCat ===\n"
                f"访问地址: http://127.0.0.1:{info.get('napcat_port', '6099')}\n"
                f"登录 Token: {info.get('napcat_token', '') or '(等待捕获)'}"
            )

        btn_copy.clicked.connect(lambda: (QApplication.clipboard().setText(copy_text), btn_copy.setText("已复制")))
        btn_close.clicked.connect(dialog.accept)

        button_row.addWidget(btn_copy)
        button_row.addWidget(btn_close)
        layout.addLayout(button_row)

        dialog.exec()

    def _show_saved_credentials(self):
        info = self.config.get("deploy_info")
        if not info:
            self._show_notice_dialog("提示", "尚未部署，暂无凭据信息。\n请先完成部署。")
            return
        self._show_credentials_dialog(info)
