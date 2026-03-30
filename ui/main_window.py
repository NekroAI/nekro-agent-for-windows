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
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
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
from ui.widgets import ActionButton, MetricCard, SectionCard, show_notice_dialog


def get_resource_path(relative_path):
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nekro Agent 启动器")
        self.resize(1220, 820)
        self.setMinimumSize(880, 620)
        self.setStyleSheet(STYLESHEET)

        self.config = ConfigManager()
        self.backend = BackendFactory.create(self.config)
        self._quit_after_stop = False
        self._responsive_buttons = []
        self._last_status = ""
        self._uninstall_in_progress = False
        self._update_in_progress = False
        self._pull_layers = OrderedDict()
        self._pull_layer_order = []
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
        self.init_images_page()
        self.init_settings_page()
        self.switch_tab(0)

        self.backend.log_received.connect(self.append_log)
        self.backend.progress_updated.connect(self._on_backend_progress)
        self.backend.status_changed.connect(self.update_status_ui)
        self.backend.deploy_info_ready.connect(self._show_credentials_dialog)
        self.backend.image_status_result.connect(self._on_image_status_result)
        self.backend.image_pull_result.connect(self._on_image_pull_result)
        self.backend.update_optional_confirm.connect(self._on_update_optional_confirm)
        self.backend.update_finished.connect(self._on_update_finished)

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
        self.btn_images = self.create_sidebar_btn("镜像管理", 4)
        self.btn_settings = self.create_sidebar_btn("系统设置", 5)

        for button in [self.btn_home, self.btn_browser, self.btn_logs, self.btn_files, self.btn_images, self.btn_settings]:
            sidebar_layout.addWidget(button)

        sidebar_layout.addStretch()

        footer_row = QHBoxLayout()
        footer_row.setSpacing(8)

        btn_repo = QPushButton("⌂")
        btn_repo.setToolTip("主仓库: KroMiose/nekro-agent")
        btn_repo.setFixedSize(32, 32)
        btn_repo.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_repo.setStyleSheet(
            "QPushButton { background: transparent; color: #57606a; font-size: 18px; border: none; border-radius: 6px; }"
            "QPushButton:hover { background: #f0f2f4; color: #24292f; }"
        )
        btn_repo.clicked.connect(lambda: webbrowser.open("https://github.com/KroMiose/nekro-agent"))
        footer_row.addWidget(btn_repo)

        btn_feedback = QPushButton("✉")
        btn_feedback.setToolTip("反馈问题")
        btn_feedback.setFixedSize(32, 32)
        btn_feedback.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_feedback.setStyleSheet(
            "QPushButton { background: transparent; color: #57606a; font-size: 18px; border: none; border-radius: 6px; }"
            "QPushButton:hover { background: #f0f2f4; color: #24292f; }"
        )
        btn_feedback.clicked.connect(lambda: webbrowser.open("https://github.com/NekroAI/nekro-agent-for-windows/issues/new"))
        footer_row.addWidget(btn_feedback)

        footer_row.addStretch()
        sidebar_layout.addLayout(footer_row)

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
        show_notice_dialog(self, title, text, button_text, danger)

    def _show_confirm_dialog(self, title, text, confirm_text="确认", cancel_text="取消", danger=False):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(360)
        dialog.setMaximumWidth(460)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
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
        confirm_button.setProperty("role", "danger" if danger else "primary")
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
        buttons = [self.btn_home, self.btn_browser, self.btn_logs, self.btn_files, self.btn_images, self.btn_settings]
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
        dialog.exec()

    def _on_deploy_mode_selected(self, mode):
        self._is_first_deploy = True
        # 向导里可能更新了端口，同步刷新 browser_urls 和设置页输入框
        nekro_port = self.config.get("nekro_port") or 8021
        napcat_port = self.config.get("napcat_port") or 6099
        self.browser_urls["nekro"] = f"http://localhost:{nekro_port}"
        self.browser_urls["napcat"] = f"http://localhost:{napcat_port}"
        if hasattr(self, "nekro_port_setting"):
            self.nekro_port_setting.setText(str(nekro_port))
        if hasattr(self, "napcat_port_setting"):
            self.napcat_port_setting.setText(str(napcat_port))
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

    def _tick_pull_spinner(self):
        self._pull_spinner_idx = (self._pull_spinner_idx + 1) % len(self._pull_spinner_frames)
        if hasattr(self, "pull_spinner_label"):
            self.pull_spinner_label.setText(self._pull_spinner_frames[self._pull_spinner_idx])

    def _set_pull_view_visible(self, visible):
        if hasattr(self, "pull_view_frame"):
            self.pull_view_frame.setVisible(visible)
        if hasattr(self, "_pull_spinner_timer"):
            if visible:
                self._pull_spinner_timer.start(80)
            else:
                self._pull_spinner_timer.stop()
                if hasattr(self, "pull_spinner_label"):
                    self.pull_spinner_label.setText("⠋")

    def _update_pull_view(self, header="", detail=""):
        if header:
            self.pull_status_label.setText(header)
        if detail:
            layer_match = re.match(r"^([a-f0-9]{6,64}):\s*(.+)$", detail, re.IGNORECASE)
            if layer_match:
                layer_id, status = layer_match.groups()
                short_id = layer_id[:12]
                if short_id not in self._pull_layers:
                    self._pull_layer_order.append(short_id)
                self._pull_layers[short_id] = status
                # 更新进度条
                total = len(self._pull_layer_order)
                done = sum(
                    1 for lid in self._pull_layer_order
                    if self._pull_layers.get(lid, "").startswith(("Pull complete", "Already exists", "Download complete"))
                )
                if total > 0:
                    self.pull_overall_bar.setValue(int(done * 100 / total))
        self._set_pull_view_visible(True)

    def _clear_pull_progress(self):
        self._pull_layers.clear()
        self._pull_layer_order.clear()
        self.pull_status_label.setText("")
        self.pull_overall_bar.setValue(0)
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
                self._pull_layers.clear()
                self._pull_layer_order.clear()
                self.pull_overall_bar.setValue(0)
                self._update_pull_view(header=message)
            elif phase == "done":
                self.pull_overall_bar.setValue(100)
                self._update_pull_view(header=message)
                QTimer.singleShot(2000, self._clear_pull_progress)
            elif phase == "error":
                self._update_pull_view(header=message)
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

        updating = status == "更新中..."
        running = status == "运行中"
        was_running = previous_status == "运行中"
        self.metric_status.findChild(QLabel, "MetricValue").setText(status)
        if updating:
            metric_hint = "正在执行升级步骤"
            accent = "amber"
        else:
            metric_hint = "服务可访问" if running else "等待部署或启动"
            accent = "green" if running else "red"
        self.metric_status.findChild(QLabel, "MetricHint").setText(metric_hint)
        self.metric_status.setProperty("accent", accent)
        self.metric_status.style().unpolish(self.metric_status)
        self.metric_status.style().polish(self.metric_status)

        self.btn_deploy_action.setEnabled(not running and not updating)
        self.btn_primary_deploy.setEnabled(not running and not updating)
        can_update = bool(self.config.get("deploy_mode")) and not updating
        self.btn_update_action.setEnabled(can_update)
        self.btn_primary_update.setEnabled(can_update)

        if running:
            self.btn_primary_deploy.setText("服务运行中")
            self.btn_primary_update.setText("升级 Nekro Agent")
            if hasattr(self, "_is_first_deploy") and self._is_first_deploy:
                self._is_first_deploy = False
            if hasattr(self, "webview") and not was_running:
                self.switch_tab(1)
                self._set_browser_target(self.current_browser_target, force_reload=True)
            self._clear_pull_progress()
            self.btn_log_nekro.setVisible(True)
            self.btn_log_napcat.setVisible(self.config.get("deploy_mode") == "napcat")
        else:
            self.btn_log_nekro.setVisible(False)
            self.btn_log_napcat.setVisible(False)
            self.btn_primary_deploy.setText("开始部署")
            self.btn_primary_update.setText("升级 Nekro Agent" if not updating else "升级中...")
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

    def _on_update_optional_confirm(self, step_label, prompt):
        confirmed = self._show_confirm_dialog(
            step_label,
            prompt,
            confirm_text="执行",
            cancel_text="跳过",
        )
        self.backend.reply_update_optional(confirmed)

    def _on_update_finished(self, success, message):
        self._update_in_progress = False
        self.btn_primary_update.setText("升级 Nekro Agent")
        self.btn_update_action.setEnabled(bool(self.config.get("deploy_mode")))
        self.btn_primary_update.setEnabled(bool(self.config.get("deploy_mode")))
        if success:
            self._show_notice_dialog("升级完成", message)
        else:
            self._show_notice_dialog("升级失败", message, danger=True)

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
        self.btn_primary_update = QPushButton("升级 Nekro Agent")
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
        self.btn_update_action = ActionButton("UPD", "升级 Nekro Agent", "拉取镜像并重启服务")
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
        self.browser_reload_btn.setObjectName("SegmentBtn")
        self.browser_reload_btn.clicked.connect(self._reload_browser_view)
        toolbar.addWidget(self.browser_reload_btn)

        self.browser_open_external_btn = QPushButton("在系统浏览器打开")
        self.browser_open_external_btn.setObjectName("SegmentBtn")
        self.browser_open_external_btn.clicked.connect(self._open_current_in_browser)
        toolbar.addWidget(self.browser_open_external_btn)
        card_layout.addLayout(toolbar)

        self.browser_hint = QLabel()
        self.browser_hint.setObjectName("SectionDesc")
        self.browser_hint.setWordWrap(True)
        card_layout.addWidget(self.browser_hint)

        self.webview = QWebEngineView()
        self.webview.setMinimumHeight(200)
        self.webview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.webview.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.webview.setAutoFillBackground(True)
        self.webview.setStyleSheet("background: #f4f7fb; border: 1px solid #dfe7ef; border-radius: 8px;")
        self.webview.page().setBackgroundColor(QColor("#f4f7fb"))
        card_layout.addWidget(self.webview, 1)

        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(card, 1)
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

        bar_row = QHBoxLayout()
        bar_row.setSpacing(10)

        self.pull_spinner_label = QLabel("⠋")
        self.pull_spinner_label.setStyleSheet("font-size: 16px; color: #58a6ff;")
        self.pull_spinner_label.setFixedWidth(20)
        bar_row.addWidget(self.pull_spinner_label)

        self.pull_overall_bar = QProgressBar()
        self.pull_overall_bar.setRange(0, 100)
        self.pull_overall_bar.setValue(0)
        self.pull_overall_bar.setFixedHeight(8)
        self.pull_overall_bar.setTextVisible(False)
        self.pull_overall_bar.setStyleSheet(
            "QProgressBar { border: none; background: #1e3a52; border-radius: 4px; }"
            "QProgressBar::chunk { background: #58a6ff; border-radius: 4px; }"
        )
        bar_row.addWidget(self.pull_overall_bar)
        pull_view_layout.addLayout(bar_row)

        self._pull_spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._pull_spinner_idx = 0
        self._pull_spinner_timer = QTimer(self)
        self._pull_spinner_timer.timeout.connect(self._tick_pull_spinner)

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
        self.btn_log_nekro.setVisible(False)
        self.btn_log_napcat.setVisible(False)
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

    def init_images_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(34, 30, 34, 30)
        layout.setSpacing(22)

        card = SectionCard("镜像管理", "查看 Nekro Agent 相关镜像的本地与远程版本状态。")
        card_layout = card.layout()

        # 表头
        header = QHBoxLayout()
        for text, stretch in [("镜像", 3), ("本地 Digest", 2), ("远程 Digest", 2), ("状态", 2), ("", 2)]:
            lbl = QLabel(text)
            lbl.setObjectName("SectionDesc")
            header.addWidget(lbl, stretch)
        card_layout.addLayout(header)

        # 镜像行容器
        self._image_rows_layout = QVBoxLayout()
        self._image_rows_layout.setSpacing(6)
        card_layout.addLayout(self._image_rows_layout)
        self._image_row_widgets = {}  # image_ref -> dict of labels

        from core.wsl_manager import MANAGED_IMAGES
        deploy_mode = self.config.get("deploy_mode") or "lite"
        for image_ref, name, desc, modes in MANAGED_IMAGES:
            if deploy_mode not in modes:
                continue
            row = QHBoxLayout()
            name_lbl = QLabel(f"<b>{name}</b><br><span style='color:#57606a;font-size:11px;'>{image_ref}</span>")
            name_lbl.setTextFormat(Qt.TextFormat.RichText)
            local_lbl = QLabel("—")
            local_lbl.setObjectName("SectionDesc")
            remote_lbl = QLabel("—")
            remote_lbl.setObjectName("SectionDesc")
            status_lbl = QLabel("未检测")
            status_lbl.setObjectName("SectionDesc")
            btn_single = QPushButton("检查更新")
            btn_single.setObjectName("HeroSecondary")
            btn_single.setFixedHeight(32)
            btn_single.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_single.clicked.connect(lambda checked, ref=image_ref: self._check_single_image(ref))
            row.addWidget(name_lbl, 3)
            row.addWidget(local_lbl, 2)
            row.addWidget(remote_lbl, 2)
            row.addWidget(status_lbl, 2)
            row.addWidget(btn_single, 2)
            self._image_rows_layout.addLayout(row)
            self._image_row_widgets[image_ref] = {
                "local": local_lbl, "remote": remote_lbl, "status": status_lbl, "btn": btn_single
            }

        btn_row = QHBoxLayout()
        self.btn_check_images = QPushButton("检查全部更新")
        self.btn_check_images.setObjectName("HeroSecondary")
        self.btn_check_images.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_check_images.clicked.connect(self._check_images)
        self._img_spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._img_spinner_idx = 0
        self._img_spinner_timer = QTimer(self)
        self._img_spinner_timer.timeout.connect(self._tick_img_spinner)
        self._img_checking_ref = None  # 当前单独检测的 image_ref，None 表示全量
        btn_row.addWidget(self.btn_check_images)
        btn_row.addStretch()
        card_layout.addLayout(btn_row)

        layout.addWidget(card)
        layout.addStretch()
        self._add_page(page)

    def _tick_img_spinner(self):
        self._img_spinner_idx = (self._img_spinner_idx + 1) % len(self._img_spinner_frames)
        frame = self._img_spinner_frames[self._img_spinner_idx]
        if self._img_checking_ref is None:
            self.btn_check_images.setText(f"{frame} 检测中...")
        else:
            widgets = self._image_row_widgets.get(self._img_checking_ref)
            if widgets:
                widgets["btn"].setText(f"{frame}")

    def _check_single_image(self, image_ref):
        if self._img_spinner_timer.isActive():
            return
        widgets = self._image_row_widgets.get(image_ref)
        if not widgets:
            return
        self._img_checking_ref = image_ref
        widgets["btn"].setEnabled(False)
        widgets["btn"].setText(self._img_spinner_frames[0])
        widgets["local"].setText("...")
        widgets["remote"].setText("...")
        widgets["status"].setText("检测中")
        self._img_spinner_timer.start(100)
        self.backend.check_images_status(only_image=image_ref)

    def _check_images(self):
        if self._img_spinner_timer.isActive():
            return
        self._img_checking_ref = None
        self.btn_check_images.setEnabled(False)
        self.btn_check_images.setText(f"{self._img_spinner_frames[0]} 检测中...")
        for widgets in self._image_row_widgets.values():
            widgets["local"].setText("...")
            widgets["remote"].setText("...")
            widgets["status"].setText("检测中")
            widgets["btn"].setEnabled(False)
        self._img_spinner_timer.start(100)
        self.backend.check_images_status()

    def _on_image_status_result(self, results):
        self._img_spinner_timer.stop()
        self.btn_check_images.setEnabled(True)
        self.btn_check_images.setText("检查全部更新")
        for entry in results:
            widgets = self._image_row_widgets.get(entry["image"])
            if not widgets:
                continue
            if entry["error"]:
                widgets["local"].setText("—")
                widgets["remote"].setText("—")
                widgets["status"].setText("<span style='color:#f26f82;'>错误</span>")
                widgets["status"].setTextFormat(Qt.TextFormat.RichText)
                widgets["btn"].setEnabled(True)
                widgets["btn"].setText("检查更新")
            elif entry["local"] is None:
                widgets["local"].setText("未安装")
                widgets["remote"].setText(entry["remote"] or "—")
                widgets["status"].setText("<span style='color:#d29922;'>未拉取</span>")
                widgets["status"].setTextFormat(Qt.TextFormat.RichText)
                widgets["btn"].setEnabled(True)
                widgets["btn"].setText("检查更新")
            elif entry["has_update"]:
                widgets["local"].setText(entry["local"] or "—")
                widgets["remote"].setText(entry["remote"] or "—")
                widgets["status"].setText("<span style='color:#58a6ff;'>有更新</span>")
                widgets["status"].setTextFormat(Qt.TextFormat.RichText)
                image_ref = entry["image"]
                widgets["btn"].setEnabled(True)
                widgets["btn"].setText("立即更新")
                try:
                    widgets["btn"].clicked.disconnect()
                except Exception:
                    pass
                if image_ref == "kromiose/nekro-agent:latest":
                    widgets["btn"].clicked.connect(lambda checked: self._update_services())
                else:
                    widgets["btn"].clicked.connect(lambda checked, ref=image_ref: self._do_image_update(ref))
            else:
                widgets["local"].setText(entry["local"] or "—")
                widgets["remote"].setText(entry["remote"] or "—")
                widgets["status"].setText("<span style='color:#3fb950;'>最新</span>")
                widgets["status"].setTextFormat(Qt.TextFormat.RichText)
                widgets["btn"].setEnabled(True)
                widgets["btn"].setText("检查更新")
                try:
                    widgets["btn"].clicked.disconnect()
                except Exception:
                    pass
                widgets["btn"].clicked.connect(lambda checked, ref=entry["image"]: self._check_single_image(ref))

    def _do_image_update(self, image_ref):
        widgets = self._image_row_widgets.get(image_ref)
        if widgets:
            widgets["btn"].setEnabled(False)
            widgets["btn"].setText(self._img_spinner_frames[0])
            widgets["status"].setText("更新中")
        self._img_checking_ref = image_ref
        self._img_spinner_timer.start(100)
        self.backend.pull_single_image(image_ref)

    def _on_image_pull_result(self, image_ref, success, message):
        self._img_spinner_timer.stop()
        widgets = self._image_row_widgets.get(image_ref)
        if not widgets:
            return
        widgets["btn"].setEnabled(True)
        if success:
            widgets["btn"].setText("检查更新")
            try:
                widgets["btn"].clicked.disconnect()
            except Exception:
                pass
            widgets["btn"].clicked.connect(lambda checked, ref=image_ref: self._check_single_image(ref))
            widgets["status"].setText("<span style='color:#3fb950;'>已更新</span>")
            widgets["status"].setTextFormat(Qt.TextFormat.RichText)
            # 镜像特化提示
            if image_ref == "kromiose/nekro-cc-sandbox":
                self._show_notice_dialog(
                    "NA CC 沙盒已更新",
                    "镜像拉取完成。\n\n请前往 WebUI → 工作区 → 工作区管理 → 对应工作区 → 沙盒容器，手动重建沙盒以应用新版本。"
                )
            elif image_ref == "kromiose/nekro-agent-sandbox":
                self._show_notice_dialog(
                    "NA 沙盒已更新",
                    "镜像拉取完成。\n\n沙盒由 Nekro Agent 动态创建销毁，新版本将在下次任务执行时自动生效，无需手动操作。"
                )
        else:
            widgets["btn"].setText("立即更新")
            widgets["status"].setText("<span style='color:#f26f82;'>更新失败</span>")
            widgets["status"].setTextFormat(Qt.TextFormat.RichText)

    def init_settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(34, 30, 34, 30)
        layout.setSpacing(18)

        card = SectionCard("系统设置", "控制部署、数据路径和系统集成选项。")
        card_layout = card.body_layout()

        self.check_auto = QCheckBox("开机自动启动 Nekro Agent 管理系统")
        self.check_auto.setChecked(self.config.get("autostart"))
        self.check_auto.stateChanged.connect(lambda state: self.config.set("autostart", state == 2))
        card_layout.addWidget(self.check_auto)

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
        self.datadir_edit = QLineEdit("/root/nekro_agent_data")
        self.datadir_edit.setReadOnly(True)
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

    def _save_ports(self):
        try:
            nekro_port = int(self.nekro_port_setting.text().strip())
            napcat_port = int(self.napcat_port_setting.text().strip())
            if 1 <= nekro_port <= 65535 and 1 <= napcat_port <= 65535:
                self.config.set("nekro_port", nekro_port)
                self.config.set("napcat_port", napcat_port)
                self.browser_urls["nekro"] = f"http://localhost:{nekro_port}"
                self.browser_urls["napcat"] = f"http://localhost:{napcat_port}"
                # 同步更新已保存的 deploy_info 里的端口，避免凭据弹窗显示旧端口
                deploy_info = self.config.get("deploy_info")
                if deploy_info:
                    deploy_info["port"] = str(nekro_port)
                    deploy_info["napcat_port"] = str(napcat_port)
                    self.config.set("deploy_info", deploy_info)
                # 刷新内置浏览器地址栏（强制更新 URL，不依赖 is_running 状态）
                target_url = self._target_url(self.current_browser_target)
                if hasattr(self, "browser_url_label"):
                    self.browser_url_label.setText(f"当前地址: {target_url}")
                if getattr(self.backend, "is_running", False) and hasattr(self, "webview"):
                    self.webview.setUrl(QUrl(target_url))
        except ValueError:
            pass

    def _refresh_datadir_hint(self):
        sample_path = self.backend.get_host_access_path("/root/nekro_agent_data")
        if sample_path:
            self.datadir_hint.setText(f"宿主机可访问路径: {sample_path}")
        else:
            self.datadir_hint.setText(f"当前后端 {self.backend.display_name} 暂未提供宿主机侧直接打开路径。")

    def _open_datadir_in_explorer(self):
        data_dir = "/root/nekro_agent_data"
        win_path = self.backend.get_host_access_path(data_dir)
        if not win_path:
            self._show_notice_dialog("提示", f"当前后端 {self.backend.display_name} 暂不支持直接打开宿主机路径。")
            return
        try:
            os.startfile(win_path)
        except Exception as error:
            self._show_notice_dialog("提示", f"无法打开目录，请确认服务已启动且目录已创建。\n\n路径: {win_path}\n错误: {error}", danger=True)

    def _update_services(self):
        if self._update_in_progress:
            self._show_notice_dialog("提示", "升级流程正在进行中，请等待当前操作完成。")
            return
        if not self.config.get("deploy_mode"):
            self._show_notice_dialog("提示", "尚未完成部署，无法执行升级。")
            return
        if not hasattr(self.backend, "run_remote_update"):
            self._show_notice_dialog("提示", f"当前后端 {self.backend.display_name} 暂不支持远程升级。")
            return

        reply = self._show_confirm_dialog(
            "确认升级",
            "将从远端获取最新升级步骤，拉取最新版 Nekro Agent 镜像并重启服务。\n\n升级过程中可能需要确认是否备份数据目录。",
            confirm_text="开始升级",
        )
        if not reply:
            return

        self._update_in_progress = True
        self.switch_tab(2)
        self.log_viewer_app.append("<span style='color:#7ce0a3;'>[INFO]</span> 开始升级 Nekro Agent...")
        if hasattr(self, "log_preview"):
            self.log_preview.append("<span style='color:#7ce0a3;'>[INFO]</span> 开始升级 Nekro Agent...")
        self.btn_update_action.setEnabled(False)
        self.btn_primary_update.setEnabled(False)
        self.btn_primary_update.setText("升级中...")
        self.backend.run_remote_update()

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

    def _ask_close_action(self):
        """返回 1=最小化到托盘, 2=停止并退出, 其他=取消"""
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
        quit_button.setProperty("role", "danger")
        quit_button.clicked.connect(lambda: choice.done(2))
        button_row.addWidget(quit_button)

        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(choice.reject)
        button_row.addWidget(cancel_button)

        layout.addLayout(button_row)
        choice.adjustSize()
        return choice.exec()

    def closeEvent(self, event: QCloseEvent):
        if self.backend.is_running:
            result = self._ask_close_action()
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

    def _show_credentials_dialog(self, info, wait_for_boot=True):
        dialog = QDialog(self)
        dialog.setWindowTitle("部署凭据信息")
        dialog.resize(560, 500)
        dialog.setMinimumSize(520, 460)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setStyleSheet(STYLESHEET)
        # 禁止点 X 关闭
        dialog.setWindowFlags(dialog.windowFlags() & ~Qt.WindowType.WindowCloseButtonHint)

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

        # 启动状态行
        boot_status = QLabel()
        boot_status.setWordWrap(True)
        layout.addWidget(boot_status)

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

        if wait_for_boot:
            btn_close.setEnabled(False)
            # spinner 动画
            _spin_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            _spin = [0]
            _timer = QTimer(dialog)

            def _tick():
                boot_status.setText(
                    f"<span style='color:#58a6ff;'>{_spin_frames[_spin[0] % len(_spin_frames)]} 等待服务启动...</span>"
                )
                _spin[0] += 1
            _timer.timeout.connect(_tick)
            _timer.start(100)
            _tick()

            def _on_boot_finished():
                _timer.stop()
                boot_status.setText("<span style='color:#3fb950;'>✓ 服务已就绪，可以开始使用</span>")
                btn_close.setEnabled(True)
                try:
                    self.backend.boot_finished.disconnect(_on_boot_finished)
                except Exception:
                    pass

            def _on_timeout(status):
                if status in {"启动超时", "启动失败"}:
                    _timer.stop()
                    boot_status.setText(f"<span style='color:#f26f82;'>✗ {status}，请检查日志</span>")
                    btn_close.setEnabled(True)
                    try:
                        self.backend.status_changed.disconnect(_on_timeout)
                    except Exception:
                        pass

            self.backend.boot_finished.connect(_on_boot_finished)
            self.backend.status_changed.connect(_on_timeout)
        else:
            boot_status.setVisible(False)

        dialog.exec()

    def _show_saved_credentials(self):
        info = self.config.get("deploy_info")
        if not info:
            self._show_notice_dialog("提示", "尚未部署，暂无凭据信息。\n请先完成部署。")
            return
        self._show_credentials_dialog(info, wait_for_boot=False)
