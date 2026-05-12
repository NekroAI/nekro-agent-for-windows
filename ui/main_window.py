import re
import os
import shutil
import sys
import json
import time
import webbrowser
from collections import OrderedDict

from PyQt6.QtCore import QRect, QSize, QThread, QTimer, Qt
from PyQt6.QtGui import QCloseEvent, QColor, QIcon, QPainter, QPixmap, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QStyle,
    QTabBar,
    QStyleOptionTab,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)
from ui.webview_widget import WebViewWidget

from core.autostart import set_autostart_enabled
from core.app_updater import APP_VERSION, UpdateChecker
from core.backend_factory import BackendFactory
from core.config_manager import ConfigManager
from core.port_utils import validate_port_bindings
from ui.styles import STYLESHEET
from ui.widgets import ActionButton, MetricCard, SectionCard, SpinnerLabel, StyledComboBox, SPINNER_FRAMES, UpdateProgressDialog, show_notice_dialog


def get_resource_path(relative_path):
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)


class BrowserTabBar(QTabBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMovable(True)
        self.setDrawBase(False)
        self.setExpanding(False)
        self.setMouseTracking(True)
        self._close_rects = {}
        self._hovered_close_index = -1

    def _close_gap(self, text_width):
        return max(6, min(14, int(text_width * 0.10)))

    def tabSizeHint(self, index):
        text = self.tabText(index) or ""
        text_width = self.fontMetrics().horizontalAdvance(text)
        width = 16 + text_width + self._close_gap(text_width) + 12 + 12
        return QSize(max(96, width), 32)

    def paintEvent(self, event):
        painter = QStylePainter(self)
        self._close_rects = {}
        hovered_tab = self.tabAt(self.mapFromGlobal(self.cursor().pos())) if self.underMouse() else -1

        for index in range(self.count()):
            option = QStyleOptionTab()
            self.initStyleOption(option, index)
            rect = self.tabRect(index)
            if not rect.isValid():
                continue

            option.text = ""
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabShape, option)

            text = self.tabText(index) or ""
            metrics = painter.fontMetrics()
            raw_text_width = metrics.horizontalAdvance(text)
            close_size = 12
            close_gap = self._close_gap(raw_text_width)
            close_x = min(rect.right() - close_size - 8, rect.x() + 16 + raw_text_width + close_gap)
            close_rect = QRect(close_x, rect.y() + (rect.height() - close_size) // 2, close_size, close_size)
            self._close_rects[index] = close_rect

            text_rect = QRect(rect.x() + 14, rect.y(), max(12, close_rect.x() - rect.x() - 20), rect.height())
            elided_text = metrics.elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width())

            if index == self.currentIndex():
                text_color = QColor("#274055")
            elif index == hovered_tab:
                text_color = QColor("#3f5669")
            else:
                text_color = QColor("#607789")

            painter.setPen(text_color)
            baseline_y = text_rect.y() + (text_rect.height() + metrics.ascent() - metrics.descent()) // 2 + 4
            painter.drawText(text_rect.x(), baseline_y, elided_text)

            close_color = QColor("#bf655d") if index == self._hovered_close_index else QColor("#90a3b4")
            painter.setPen(close_color)
            close_font = painter.font()
            close_font.setBold(True)
            close_font.setPointSize(max(9, close_font.pointSize() - 1))
            painter.setFont(close_font)
            painter.drawText(close_rect, int(Qt.AlignmentFlag.AlignCenter), "×")

    def mouseMoveEvent(self, event):
        hovered = -1
        for index, rect in self._close_rects.items():
            if rect.contains(event.pos()):
                hovered = index
                break
        if hovered != self._hovered_close_index:
            self._hovered_close_index = hovered
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._hovered_close_index != -1:
            self._hovered_close_index = -1
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            for index, rect in self._close_rects.items():
                if rect.contains(event.pos()):
                    self.tabCloseRequested.emit(index)
                    event.accept()
                    return
        super().mousePressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, splash=None):
        super().__init__()
        self.setWindowTitle("Nekro Agent 启动器")
        self.resize(1220, 820)
        self.setMinimumSize(880, 620)
        self.setStyleSheet(STYLESHEET)

        self._splash = splash

        self.config = ConfigManager()
        self.backend = BackendFactory.create(self.config)
        self._quit_after_stop = False
        self._responsive_buttons = []
        self._last_status = ""
        self._uninstall_in_progress = False
        self._update_in_progress = False
        self._active_update_dialog = None
        self._active_update_kind = None
        self._pending_remote_update_message = ""
        self._pull_stage_header = ""
        self._pull_summary_text = ""
        self._pull_layers = OrderedDict()
        self._pull_layer_order = []
        self._sidebar_collapsed = False
        self._pending_browser_refresh = None
        self._napcat_network_config_in_progress = False
        self._image_status_request_kind = None
        self.browser_urls = {
            "nekro": f"http://localhost:{self.config.get('nekro_port') or 8021}",
            "napcat": f"http://localhost:{self.config.get('napcat_port') or 6099}",
        }
        self.current_browser_target = "nekro"
        self._auto_image_check_timer = QTimer(self)
        self._auto_image_check_timer.setSingleShot(True)
        self._auto_image_check_timer.timeout.connect(self._run_scheduled_image_check)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self._build_sidebar(main_layout)

        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack, 1)

        from ui.pages.home_page import HomePage
        from ui.pages.browser_page import BrowserPage
        from ui.pages.logs_page import LogsPage
        from ui.pages.files_page import FilesPage
        from ui.pages.images_page import ImagesPage
        from ui.pages.settings_page import SettingsPage

        self._home_page = HomePage(self)
        self._add_page(self._home_page)
        self.refresh_dashboard()

        self._browser_page = BrowserPage(self)
        self._add_page(self._browser_page)

        self._logs_page = LogsPage(self)
        self._add_page(self._logs_page)

        self._files_page = FilesPage(self)
        self._add_page(self._files_page)

        self._images_page = ImagesPage(self)
        self._add_page(self._images_page)

        self._settings_page = SettingsPage(self)
        self._add_page(self._settings_page)
        self._refresh_port_settings_ui()
        self._refresh_advanced_feature_ui()

        self.switch_tab(0)

        self.backend.log_received.connect(self.append_log)
        self.backend.progress_updated.connect(self._on_backend_progress)
        self.backend.status_changed.connect(self.update_status_ui)
        self.backend.deploy_info_ready.connect(self._show_credentials_dialog)
        self.backend.napcat_network_config_finished.connect(self._finish_napcat_network_config)
        self.backend.image_status_result.connect(self._on_image_status_result)
        self.backend.image_pull_result.connect(self._on_image_pull_result)
        self.backend.update_optional_confirm.connect(self._on_update_optional_confirm)
        self.backend.update_finished.connect(self._on_update_finished)
        self.backend.instance_removed.connect(self._on_remove_instance_done)

        if self._splash:
            self.backend.status_changed.connect(self._splash.on_status_changed)
            self.backend.progress_updated.connect(self._splash.on_progress_updated)
            self._splash.finished.connect(self._on_splash_finished)

        self._build_tray_icon()
        self._sync_autostart_setting(silent=True)
        self.update_status_ui("未就绪")
        self._schedule_next_image_update_check()
        self._app_update_checker = None
        self._app_update_thread = None

        QTimer.singleShot(200, self._on_startup)
        QTimer.singleShot(0, self._apply_responsive_layout)

    def _build_sidebar(self, root_layout):
        self.sidebar = QFrame()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(248)

        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(22, 24, 22, 24)
        sidebar_layout.setSpacing(10)
        self.sidebar_layout = sidebar_layout

        self.logo_label = QLabel()
        self.logo_label.setFixedSize(42, 42)
        self.logo_label.setScaledContents(True)

        icon_path = get_resource_path(os.path.join("assets", "NekroAgent.png"))
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
            self.logo_label.setPixmap(QPixmap(icon_path))

        brand_text = QVBoxLayout()
        self.sidebar_eyebrow = QLabel("本地部署控制台")
        self.sidebar_eyebrow.setObjectName("SidebarEyebrow")
        self.sidebar_title = QLabel("Nekro Agent")
        self.sidebar_title.setObjectName("SidebarTitle")
        self.sidebar_subtitle = QLabel("Windows 启动器")
        self.sidebar_subtitle.setObjectName("SidebarSubtitle")
        brand_text.addWidget(self.sidebar_eyebrow)
        brand_text.addWidget(self.sidebar_title)
        brand_text.addWidget(self.sidebar_subtitle)

        self.sidebar_brand_text = QWidget()
        self.sidebar_brand_text.setLayout(brand_text)

        self.sidebar_toggle_btn = QPushButton("«")
        self.sidebar_toggle_btn.setObjectName("SidebarToggle")
        self.sidebar_toggle_btn.setFixedSize(30, 30)
        self.sidebar_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sidebar_toggle_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.sidebar_toggle_btn.setToolTip("收起侧边栏")
        self.sidebar_toggle_btn.clicked.connect(self._toggle_sidebar)

        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(0, 0, 0, 0)
        toggle_row.addStretch()
        toggle_row.addWidget(self.sidebar_toggle_btn)
        sidebar_layout.addLayout(toggle_row)

        brand_layout = QHBoxLayout()
        brand_layout.setSpacing(12)
        self.sidebar_brand_layout = brand_layout
        brand_layout.addWidget(self.logo_label)
        brand_layout.addWidget(self.sidebar_brand_text, 1)
        sidebar_layout.addLayout(brand_layout)
        sidebar_layout.addSpacing(18)

        self.btn_home = self.create_sidebar_btn("总览控制台", "总览", 0)
        self.btn_browser = self.create_sidebar_btn("服务访问", "访问", 1)
        self.btn_logs = self.create_sidebar_btn("日志中心", "日志", 2)
        self.btn_files = self.create_sidebar_btn("存储与路径", "存储", 3)
        self.btn_images = self.create_sidebar_btn("镜像管理", "镜像", 4)
        self.btn_settings = self.create_sidebar_btn("系统设置", "设置", 5)
        self._sidebar_nav_buttons = [
            self.btn_home,
            self.btn_browser,
            self.btn_logs,
            self.btn_files,
            self.btn_images,
            self.btn_settings,
        ]

        for button in self._sidebar_nav_buttons:
            sidebar_layout.addWidget(button)

        sidebar_layout.addStretch()

        footer_row = QHBoxLayout()
        footer_row.setSpacing(8)

        self.btn_repo = QPushButton("◧ 仓库")
        self.btn_repo.setObjectName("SidebarFootBtn")
        self.btn_repo.setProperty("full_text", "◧ 仓库")
        self.btn_repo.setProperty("compact_text", "◧")
        self.btn_repo.setToolTip("主仓库: KroMiose/nekro-agent")
        self.btn_repo.setFixedHeight(32)
        self.btn_repo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_repo.clicked.connect(lambda: webbrowser.open("https://github.com/KroMiose/nekro-agent"))
        footer_row.addWidget(self.btn_repo, 0, Qt.AlignmentFlag.AlignLeft)

        footer_row.addStretch()

        self.btn_feedback = QPushButton("✦ 反馈")
        self.btn_feedback.setObjectName("SidebarFootBtn")
        self.btn_feedback.setProperty("full_text", "✦ 反馈")
        self.btn_feedback.setProperty("compact_text", "✦")
        self.btn_feedback.setToolTip("反馈问题")
        self.btn_feedback.setFixedHeight(32)
        self.btn_feedback.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_feedback.clicked.connect(lambda: webbrowser.open("https://github.com/NekroAI/nekro-agent-for-windows/issues/new"))
        footer_row.addWidget(self.btn_feedback, 0, Qt.AlignmentFlag.AlignRight)
        sidebar_layout.addLayout(footer_row)

        self.sidebar_version_label = QLabel(f"v{APP_VERSION}")
        self.sidebar_version_label.setObjectName("VersionDisplay")
        self.sidebar_version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sidebar_layout.addWidget(self.sidebar_version_label)

        root_layout.addWidget(self.sidebar)
        self._apply_sidebar_state()

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

        if self._sidebar_collapsed:
            sidebar_width = 74 if not compact else 66
        else:
            sidebar_width = 248 if not compact else 212
        self.sidebar.setFixedWidth(sidebar_width)

        logo_size = 42 if not compact else 34
        self.logo_label.setFixedSize(logo_size, logo_size)

        for button in self._responsive_buttons:
            button.set_scale(scale)

    def _toggle_sidebar(self):
        self._sidebar_collapsed = not self._sidebar_collapsed
        self._apply_sidebar_state()
        self._apply_responsive_layout()

    def _apply_sidebar_state(self):
        collapsed = self._sidebar_collapsed
        if hasattr(self, "sidebar"):
            self.sidebar.setProperty("collapsed", collapsed)
            self.sidebar.style().unpolish(self.sidebar)
            self.sidebar.style().polish(self.sidebar)

        if hasattr(self, "sidebar_brand_text"):
            self.sidebar_brand_text.setVisible(not collapsed)
        if hasattr(self, "logo_label"):
            self.logo_label.setVisible(not collapsed)

        if hasattr(self, "sidebar_toggle_btn"):
            self.sidebar_toggle_btn.setText("»" if collapsed else "«")
            self.sidebar_toggle_btn.setToolTip("展开侧边栏" if collapsed else "收起侧边栏")

        if hasattr(self, "sidebar_layout"):
            margins = (10, 18, 10, 18) if collapsed else (22, 24, 22, 24)
            self.sidebar_layout.setContentsMargins(*margins)
            self.sidebar_layout.setSpacing(8 if collapsed else 10)
        if hasattr(self, "sidebar_brand_layout"):
            self.sidebar_brand_layout.setSpacing(0 if collapsed else 12)

        for button in getattr(self, "_sidebar_nav_buttons", []):
            button.setProperty("collapsed", collapsed)
            button.setText(button.property("compact_text") if collapsed else button.property("full_text"))
            button.style().unpolish(button)
            button.style().polish(button)

        for button in [getattr(self, "btn_repo", None), getattr(self, "btn_feedback", None)]:
            if button is None:
                continue
            button.setText(button.property("compact_text") if collapsed else button.property("full_text"))
            button.setFixedWidth(32 if collapsed else 84)

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

    def _sync_autostart_setting(self, enabled=None, silent=False):
        desired = bool(self.config.get("autostart") if enabled is None else enabled)
        try:
            set_autostart_enabled(desired)
            if self.config.get("autostart") != desired:
                self.config.set("autostart", desired)
            return True
        except Exception as error:
            if not silent:
                self._show_notice_dialog(
                    "启动项设置失败",
                    f"无法更新系统启动项，请检查权限或安装位置。\n\n错误: {error}",
                    danger=True,
                )
            return False

    def _on_autostart_changed(self, state):
        desired = state == int(Qt.CheckState.Checked.value)
        previous = bool(self.config.get("autostart"))
        if self._sync_autostart_setting(enabled=desired, silent=False):
            return

        if hasattr(self, "check_auto"):
            self.check_auto.blockSignals(True)
            self.check_auto.setChecked(previous)
            self.check_auto.blockSignals(False)

    def _advanced_features_enabled(self):
        return bool(self.config.get("advanced_features_enabled"))

    def _release_channel(self):
        return self.config.get("release_channel") or "stable"

    def _preview_button_label(self):
        if self._update_in_progress:
            if self._active_update_kind == "preview":
                return "切换中..."
            if self._active_update_kind == "restore":
                return "恢复中..."
        return "恢复正式版" if self._release_channel() == "preview" else "切换至预览版"

    def _agent_image_ref(self):
        from core.wsl import WSLManager

        return WSLManager.get_agent_image_ref(self.config)

    def _managed_images(self):
        from core.wsl import WSLManager

        return WSLManager.get_managed_images(self.config)

    def _current_webview(self):
        if not hasattr(self, "browser_tabs"):
            return None
        current_view = self.browser_tabs.currentWidget()
        return current_view if isinstance(current_view, WebViewWidget) else None

    def _browser_current_navigable_url(self):
        current_view = self._current_webview()
        if current_view:
            current_url = current_view.get_url()
            if current_url and current_url.startswith(("http://", "https://")):
                return current_url
        return self._target_url()

    def _sync_browser_url_label(self, url=None):
        if not hasattr(self, "browser_url_label"):
            return

        display_url = None
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            display_url = url
        if not display_url:
            display_url = self._browser_current_navigable_url()
        self.browser_url_label.setText(display_url)

    def _can_show_foreground_notice(self):
        return self.isVisible() and not self.isMinimized()

    def _browser_tab_text(self, browser_view, title=None, url=None):
        title_text = (title or browser_view.get_title() or "").strip()
        if title_text:
            return f"{title_text[:18]}..." if len(title_text) > 18 else title_text

        target = browser_view.property("browser_target")
        if target in {"nekro", "napcat"}:
            return self._target_label(target)

        current_url = url or browser_view.get_url()
        if current_url:
            try:
                from urllib.parse import urlparse
                host = urlparse(current_url).hostname
                if host:
                    return host
            except Exception:
                pass
            return current_url[:18]

        return "新标签页"

    def _refresh_browser_nav_buttons(self):
        current_view = self._current_webview()
        has_view = current_view is not None
        target = current_view.property("browser_target") if current_view else self.current_browser_target
        napcat_visible = bool(has_view and target == "napcat" and self.config.get("deploy_mode") == "napcat")

        if hasattr(self, "browser_back_btn"):
            self.browser_back_btn.setEnabled(has_view)
        if hasattr(self, "browser_forward_btn"):
            self.browser_forward_btn.setEnabled(has_view)
        if hasattr(self, "browser_reload_btn"):
            self.browser_reload_btn.setEnabled(has_view)
        if hasattr(self, "browser_fill_credentials_btn"):
            self.browser_fill_credentials_btn.setEnabled(has_view and self._has_fillable_browser_credentials())
        if hasattr(self, "browser_config_napcat_btn"):
            self.browser_config_napcat_btn.setVisible(napcat_visible)
            self.browser_config_napcat_btn.setEnabled(
                napcat_visible
                and self._has_napcat_network_config_payload()
                and not self._napcat_network_config_in_progress
            )
            self.browser_config_napcat_btn.setText("配网中..." if self._napcat_network_config_in_progress else "一键配网")
        if hasattr(self, "browser_open_external_btn"):
            self.browser_open_external_btn.setEnabled(has_view)

    def _has_fillable_browser_credentials(self):
        info = self.config.get("deploy_info") or {}
        current_view = self._current_webview()
        target = current_view.property("browser_target") if current_view else self.current_browser_target
        if target == "napcat":
            return bool(info.get("napcat_token"))
        return bool(info.get("admin_password"))

    def _browser_fill_credentials_payload(self):
        info = self.config.get("deploy_info") or {}
        current_view = self._current_webview()
        target = current_view.property("browser_target") if current_view else self.current_browser_target
        if target == "napcat":
            token = info.get("napcat_token", "")
            if not token:
                return None
            return {"username": "", "password": token, "target": "napcat"}

        password = info.get("admin_password", "")
        if not password:
            return None
        return {"username": "admin", "password": password, "target": "nekro"}

    def _has_napcat_network_config_payload(self):
        info = self.config.get("deploy_info") or {}
        return bool(info.get("onebot_token"))

    def _browser_napcat_network_config_payload(self):
        info = self.config.get("deploy_info") or {}
        onebot_token = info.get("onebot_token", "")
        if not onebot_token:
            return None
        return {
            "name": "Nekro Agent",
            "url": "ws://nekro_agent:8021/onebot/v11/ws",
            "token": onebot_token,
        }

    def _guard_napcat_network_config_busy(self, action_text):
        if not self._napcat_network_config_in_progress:
            return True
        self._show_notice_dialog("提示", f"NapCat 一键配网进行中，请等待完成后再{action_text}。")
        return False

    def _blocking_status_detail(self, status=None):
        current_status = status if status is not None else self._last_status
        return {
            "启动中...": "服务正在启动",
            "停止中...": "服务正在停止",
            "卸载中...": "运行环境正在卸载",
            "安装 Docker...": "Docker 正在安装",
            "更新中...": "更新任务正在执行",
        }.get(current_status, "")

    def _guard_blocking_status_idle(self, action_text):
        detail = self._blocking_status_detail()
        if not detail:
            return True
        self._show_notice_dialog("提示", f"{detail}，请等待完成后再{action_text}。")
        return False

    def _service_active(self):
        return bool(getattr(self.backend, "is_running", False))

    def _service_ready(self):
        return self._service_active() and self._last_status == "运行中"

    def _backend_runtime_exists(self):
        runtime_exists = getattr(self.backend, "runtime_exists", None)
        if callable(runtime_exists):
            try:
                return bool(runtime_exists())
            except Exception:
                return False

        distro_exists = getattr(self.backend, "_distro_exists", None)
        if callable(distro_exists):
            try:
                return bool(distro_exists())
            except Exception:
                return False

        return True

    def _update_browser_tab_label(self, browser_view, title=None, url=None):
        if not hasattr(self, "browser_tabs"):
            return
        index = self.browser_tabs.indexOf(browser_view)
        if index >= 0:
            self.browser_tabs.setTabText(index, self._browser_tab_text(browser_view, title=title, url=url))

    def _create_browser_tab(self, switch_to=True, title="新标签页"):
        channel = self._release_channel()
        browser_view = WebViewWidget(parent=self, data_subfolder=channel)
        browser_view.urlChanged.connect(lambda changed_url, view=browser_view: self._on_browser_url_changed(view, changed_url))
        browser_view.titleChanged.connect(lambda changed_title, view=browser_view: self._on_browser_title_changed(view, changed_title))

        index = self.browser_tabs.addTab(browser_view, title)
        if switch_to:
            self.browser_tabs.setCurrentIndex(index)
        self._refresh_browser_nav_buttons()
        return browser_view

    def _close_browser_tab(self, index):
        if not hasattr(self, "browser_tabs") or index < 0 or index >= self.browser_tabs.count():
            return

        widget = self.browser_tabs.widget(index)
        self.browser_tabs.removeTab(index)
        if widget is not None:
            widget.deleteLater()

        if self.browser_tabs.count() == 0:
            browser_view = self._create_browser_tab(switch_to=True, title=self._target_label(self.current_browser_target))
            self._set_browser_target(self.current_browser_target, force_reload=True, browser_view=browser_view)
        else:
            self._on_browser_tab_changed(self.browser_tabs.currentIndex())

    def _refresh_advanced_feature_ui(self):
        enabled = self._advanced_features_enabled()
        preview_mode = self._release_channel() == "preview"
        if hasattr(self, "btn_enable_advanced"):
            self.btn_enable_advanced.setText("关闭高级功能" if enabled else "启用高级功能")
            self.btn_enable_advanced.setChecked(enabled)
            self.btn_enable_advanced.setEnabled(not (enabled and preview_mode))
        if hasattr(self, "advanced_hint"):
            self.advanced_hint.setText(
                "当前处于预览版模式，高级功能已锁定；恢复到正式版后才可关闭。"
                if enabled and preview_mode
                else "已启用后，总览控制台会显示预览版入口。"
                if enabled
                else "开启后会在总览控制台显示“切换至预览版”入口。"
            )
        if hasattr(self, "advanced_status_badge"):
            self.advanced_status_badge.setText("预览版模式" if preview_mode else "高级功能已启用")
            self.advanced_status_badge.setVisible(enabled)
        if hasattr(self, "advanced_status_hint"):
            self.advanced_status_hint.setText(
                "当前运行的是预览版，可使用备份归档恢复到正式版。"
                if preview_mode
                else "预览版入口已开放，可直接切换至预览版。"
            )
            self.advanced_status_hint.setVisible(enabled)
        if hasattr(self, "btn_primary_preview"):
            self.btn_primary_preview.setVisible(enabled)
            self.btn_primary_preview.setEnabled(
                enabled
                and self._service_ready()
                and bool(self.config.get("deploy_mode"))
                and not self._napcat_network_config_in_progress
                and not self._blocking_status_detail()
            )
            self.btn_primary_preview.setText(self._preview_button_label())
        if hasattr(self, "browser_devtools_btn"):
            self.browser_devtools_btn.setVisible(enabled)
        if hasattr(self, "_image_rows_layout"):
            self._rebuild_image_rows()

    def _toggle_advanced_features(self):
        if self._advanced_features_enabled() and self._release_channel() == "preview":
            return
        enabled = not self._advanced_features_enabled()
        self.config.set("advanced_features_enabled", enabled)
        self._refresh_advanced_feature_ui()

    def _active_update_result_titles(self):
        if self._active_update_kind == "preview":
            return "切换完成", "切换失败"
        if self._active_update_kind == "restore":
            return "恢复完成", "恢复失败"
        return "升级完成", "升级失败"

    def _start_preview_switch(self, dialog, create_backup):
        self._begin_update_session(dialog, "preview")
        self.btn_primary_preview.setEnabled(False)
        self.btn_primary_preview.setText(self._preview_button_label())
        self.log_viewer_app.append("<span style='color:#7ce0a3;'>[INFO]</span> 开始切换到预览版 Nekro Agent...")
        if hasattr(self, "log_preview"):
            self.log_preview.append("<span style='color:#7ce0a3;'>[INFO]</span> 开始切换到预览版 Nekro Agent...")
        self.backend.switch_to_preview(create_backup=create_backup)

    def _start_restore_stable(self, dialog):
        self._begin_update_session(dialog, "restore")
        self.btn_primary_preview.setEnabled(False)
        self.btn_primary_preview.setText(self._preview_button_label())
        self.log_viewer_app.append("<span style='color:#7ce0a3;'>[INFO]</span> 开始恢复正式版 Nekro Agent...")
        if hasattr(self, "log_preview"):
            self.log_preview.append("<span style='color:#7ce0a3;'>[INFO]</span> 开始恢复正式版 Nekro Agent...")
        self.backend.restore_stable_from_backup()

    def _show_preview_switch_dialog(self, backup_size):
        dialog = QDialog(self)
        dialog.setWindowTitle("确认切换至预览版")
        dialog.setMinimumWidth(420)
        dialog.setMaximumWidth(560)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title_label = QLabel("确认切换至预览版")
        title_label.setProperty("role", "dialog_title")
        title_label.setWordWrap(True)
        layout.addWidget(title_label)

        desc_label = QLabel(
            "默认会先备份全量数据，再把 Nekro Agent 主容器切换到预览版镜像。\n\n"
            "备份文件将写入 /root/na_preview_backup.tar.gz。\n"
            f"预计备份占用约 {backup_size} 空间。\n\n"
            "如果选择不备份，仍可切换到预览版，但将无法切换回正式版。"
        )
        desc_label.setProperty("role", "dialog_desc")
        desc_label.setWordWrap(True)
        desc_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(desc_label)

        button_row = QHBoxLayout()
        button_row.setSpacing(10)
        button_row.addStretch()

        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(dialog.reject)
        button_row.addWidget(cancel_button)

        skip_backup_button = QPushButton("不备份直接切换")
        skip_backup_button.setProperty("role", "danger")
        skip_backup_button.clicked.connect(lambda: dialog.done(2))
        button_row.addWidget(skip_backup_button)

        confirm_button = QPushButton("备份并切换")
        confirm_button.setProperty("role", "primary")
        confirm_button.clicked.connect(lambda: dialog.done(1))
        button_row.addWidget(confirm_button)

        layout.addLayout(button_row)
        dialog.adjustSize()

        result = dialog.exec()
        if result == 1:
            return "backup"
        if result == 2:
            return "skip"
        return None

    def _switch_to_preview_build(self):
        if not self._guard_napcat_network_config_busy("切换预览版"):
            return
        if not self._guard_blocking_status_idle("切换预览版"):
            return
        if self._update_in_progress:
            self._show_notice_dialog("提示", "当前已有更新任务正在进行，请等待完成后再试。")
            return
        if not self.config.get("deploy_mode"):
            self._show_notice_dialog("提示", "尚未完成部署，无法切换到预览版。")
            return
        if not self._service_ready():
            self._show_notice_dialog("提示", "服务未处于运行中，无法切换预览版。请先启动并等待服务就绪。")
            return
        preview_mode = self._release_channel() == "preview"
        if preview_mode:
            if not hasattr(self.backend, "restore_stable_from_backup"):
                self._show_notice_dialog("提示", f"当前后端 {self.backend.display_name} 暂不支持恢复正式版。")
                return
            if not self.config.get("preview_backup_available"):
                self._show_notice_dialog("提示", "当前预览版是在未备份的情况下切换的，无法恢复到正式版。")
                return
            if hasattr(self.backend, "preview_backup_exists") and not self.backend.preview_backup_exists():
                self._show_notice_dialog("提示", "未找到预览版备份文件，无法恢复到正式版。")
                return

            dialog = self._create_update_dialog(
                "确认恢复正式版",
                "将从 /root/na_preview_backup.tar.gz 恢复正式版所需的数据与配置，然后把 Nekro Agent 主容器切回稳定版镜像。\n\n"
                "警告：预览版期间产生的数据库与相关持久化数据会全部丢失。\n"
                "当前恢复流程会直接回滚 PostgreSQL、Qdrant 和 /root/nekro_agent_data 到切换预览版前的备份状态；由于数据库不兼容，预览版期间写入的数据不会保留。\n\n"
                "恢复过程中会短暂停止相关服务。",
                lambda dlg=None: self._start_restore_stable(dialog),
                confirm_text="开始恢复",
            )
            dialog.exec()
            return

        if not hasattr(self.backend, "switch_to_preview"):
            self._show_notice_dialog("提示", f"当前后端 {self.backend.display_name} 暂不支持切换到预览版。")
            return

        backup_size = "未知"
        if hasattr(self.backend, "get_backup_size_hint"):
            try:
                backup_size = self.backend.get_backup_size_hint()
            except Exception:
                backup_size = "未知"

        action = self._show_preview_switch_dialog(backup_size)
        if not action:
            return

        dialog = self._create_update_dialog(
            "切换到预览版",
            "正在准备切换到预览版 Nekro Agent。",
            lambda dlg=None, create_backup=(action == "backup"): self._start_preview_switch(dialog, create_backup),
            confirm_text="开始切换",
        )
        dialog.exec()

    def _show_confirm_dialog(self, title, text, confirm_text="确认", cancel_text="取消", danger=False, parent=None):
        dialog = QDialog(parent or self)
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

    def _create_update_dialog(self, title, text, on_confirm, confirm_text="开始更新"):
        dialog = UpdateProgressDialog(self, title, text, confirm_text=confirm_text)
        dialog.confirmed.connect(on_confirm)
        dialog.finished.connect(lambda _result, dlg=dialog: self._on_update_dialog_closed(dlg))
        return dialog

    def _on_update_dialog_closed(self, dialog):
        if self._active_update_dialog is dialog and not self._update_in_progress:
            self._active_update_dialog = None
            self._active_update_kind = None
            self._pending_remote_update_message = ""

    def _begin_update_session(self, dialog, kind):
        self._update_in_progress = True
        self._active_update_dialog = dialog
        self._active_update_kind = kind
        self._pending_remote_update_message = ""
        self._pending_browser_refresh = None

    def _finish_update_session(self, success, message, detail_text=""):
        dialog = self._active_update_dialog
        self._update_in_progress = False
        self._pending_remote_update_message = ""
        self._active_update_kind = None
        self._refresh_advanced_feature_ui()
        if dialog:
            dialog.set_finished(success, message, detail_text)

    def _set_active_update_progress(self, phase, message):
        if not self._active_update_dialog:
            return

        header = self._pull_stage_header or message
        detail_text = self._pull_summary_text
        if phase in {"start", "stage"}:
            self._active_update_dialog.set_progress(status_text=header, detail_text="", busy=True)
        elif phase == "update":
            if self._pull_layer_order:
                self._active_update_dialog.set_progress(
                    status_text=header,
                    detail_text=detail_text,
                    value=self.pull_overall_bar.value(),
                    busy=False,
                )
            else:
                self._active_update_dialog.set_progress(status_text=header, detail_text="", busy=True)
        elif phase == "done":
            self._active_update_dialog.set_progress(status_text=header, detail_text=detail_text, value=100, busy=False)
        elif phase == "error":
            self._active_update_dialog.set_progress(status_text=message, detail_text="", busy=False)

    def _lookup_image_meta(self, image_ref):
        for ref, name, desc, modes in self._managed_images():
            if ref == image_ref:
                return name, desc
        return image_ref, ""

    def _start_remote_update(self, dialog):
        self._begin_update_session(dialog, "remote")
        self.log_viewer_app.append("<span style='color:#7ce0a3;'>[INFO]</span> 开始升级 Nekro Agent...")
        if hasattr(self, "log_preview"):
            self.log_preview.append("<span style='color:#7ce0a3;'>[INFO]</span> 开始升级 Nekro Agent...")
        self.btn_update_action.setEnabled(False)
        self.btn_primary_update.setEnabled(False)
        self.btn_primary_update.setText("升级中...")
        self.backend.run_remote_update()

    def _start_single_image_update(self, image_ref, dialog):
        widgets = self._image_row_widgets.get(image_ref)
        if widgets:
            widgets["btn"].setEnabled(False)
            widgets["btn"].setText(self._img_spinner_frames[0])
            widgets["status"].setText("更新中")
        self._begin_update_session(dialog, f"image:{image_ref}")
        self._img_checking_ref = image_ref
        self._img_spinner_timer.start(100)
        self.backend.pull_single_image(image_ref)

    def _show_remote_update_dialog(self):
        if not self._guard_napcat_network_config_busy("升级服务"):
            return
        if not self._guard_blocking_status_idle("升级服务"):
            return
        if self._update_in_progress:
            self._show_notice_dialog("提示", "升级流程正在进行中，请等待当前操作完成。")
            return
        if not self.config.get("deploy_mode"):
            self._show_notice_dialog("提示", "尚未完成部署，无法执行升级。")
            return
        if not self._service_ready():
            self._show_notice_dialog("提示", "服务未处于运行中，无法执行升级。请先启动并等待服务就绪。")
            return
        if not hasattr(self.backend, "run_remote_update"):
            self._show_notice_dialog("提示", f"当前后端 {self.backend.display_name} 暂不支持升级。")
            return

        dialog = self._create_update_dialog(
            "确认升级",
            "将按内置升级流程拉取当前通道的 Nekro Agent 镜像并重建主容器。\n\n升级过程中可能需要确认是否备份当前运行数据。",
            lambda dlg=None: self._start_remote_update(dialog),
        )
        dialog.exec()

    def _show_image_update_dialog(self, image_ref):
        if self._update_in_progress:
            self._show_notice_dialog("提示", "当前已有更新任务正在进行，请等待完成后再试。")
            return

        image_name, image_desc = self._lookup_image_meta(image_ref)
        desc = f"将拉取最新的 {image_name} 镜像并应用更新。"
        if image_desc:
            desc += f"\n\n{image_desc}"
        dialog = self._create_update_dialog(
            f"确认更新 {image_name}",
            desc,
            lambda dlg=None, ref=image_ref: self._start_single_image_update(ref, dialog),
        )
        dialog.exec()

    def create_sidebar_btn(self, text, compact_text, index):
        button = QPushButton(text)
        button.setProperty("nav", True)
        button.setProperty("full_text", text)
        button.setProperty("compact_text", compact_text)
        button.setProperty("collapsed", False)
        button.setCheckable(True)
        button.setFixedHeight(46)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setToolTip(text)
        button.clicked.connect(lambda: self.switch_tab(index))
        return button

    def switch_tab(self, index):
        self.stack.setCurrentIndex(index)
        buttons = [self.btn_home, self.btn_browser, self.btn_logs, self.btn_files, self.btn_images, self.btn_settings]
        for current, button in enumerate(buttons):
            button.setChecked(current == index)

    def _on_splash_finished(self):
        self.show()
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(500, self._check_app_update_on_startup)

    def _dismiss_splash_for_wizard(self):
        if self._splash:
            self._splash.finish_for_wizard()
        else:
            self.show()
            QTimer.singleShot(500, self._check_app_update_on_startup)

    # ── 启动器自身更新检查 ──

    def _check_app_update_on_startup(self):
        """splash 结束后静默检查启动器新版本。"""
        if getattr(self, "_app_update_startup_done", False):
            return
        self._app_update_startup_done = True
        self._run_app_update_check(silent=True)

    def _run_app_update_check(self, silent=False):
        if self._app_update_thread and self._app_update_thread.isRunning():
            if not silent:
                self._show_notice_dialog("提示", "正在检查更新，请稍候...")
            return

        self._app_update_silent = silent
        self._app_update_found = False
        checker = UpdateChecker()
        thread = QThread(self)
        checker.moveToThread(thread)

        checker.update_available.connect(self._on_app_update_available)
        checker.check_finished.connect(self._on_app_update_check_finished)
        thread.started.connect(checker.run)
        thread.finished.connect(thread.deleteLater)

        self._app_update_checker = checker
        self._app_update_thread = thread
        thread.start()

    def _on_app_update_available(self, info: dict):
        import time
        self.config.set("last_app_update_check_ts", int(time.time()))
        self._app_update_found = True

        skipped = self.config.get("skipped_app_version") or ""
        if self._app_update_silent and skipped == info.get("tag", ""):
            return

        self._show_app_update_dialog(info)

    def _on_app_update_check_finished(self):
        if self._app_update_thread:
            self._app_update_thread.quit()
            self._app_update_thread.wait(3000)
            self._app_update_thread = None
            self._app_update_checker = None

        if not getattr(self, "_app_update_silent", True):
            import time
            self.config.set("last_app_update_check_ts", int(time.time()))
            if not getattr(self, "_app_update_found", False):
                self._show_notice_dialog("检查完成", f"当前已是最新版本 v{APP_VERSION}。")

    def _show_app_update_dialog(self, info: dict):
        from ui.update_dialog import AppUpdateDialog

        dialog = AppUpdateDialog(self, info)
        result = dialog.exec()

        if result == 2:
            self.config.set("skipped_app_version", info.get("tag", ""))

    def check_app_update_manual(self):
        """设置页手动触发检查更新。"""
        self._run_app_update_check(silent=False)

    def _on_startup(self):
        is_first_run = self.config.get("first_run") or not self.config.get("deploy_mode")

        if is_first_run:
            self._dismiss_splash_for_wizard()
            if not self._splash:
                self._show_first_run_with_scan()
            else:
                QTimer.singleShot(500, self._show_first_run_with_scan)
            return

        if not self._backend_runtime_exists():
            self._dismiss_splash_for_wizard()
            if not self._splash:
                self._show_notice_dialog(
                    "运行环境缺失",
                    "检测到本地仍保存了部署配置，但 WSL 运行环境不存在。\n\n请先重新创建运行环境，再继续部署。",
                    danger=True,
                )
                self._show_first_run_dialog()
            else:
                def _show_after_splash():
                    self._show_notice_dialog(
                        "运行环境缺失",
                        "检测到本地仍保存了部署配置，但 WSL 运行环境不存在。\n\n请先重新创建运行环境，再继续部署。",
                        danger=True,
                    )
                    self._show_first_run_dialog()
                QTimer.singleShot(500, _show_after_splash)
            return

        if self._splash:
            self._splash.enter_deploy_phase()

        self.start_deploy(show_logs=False)

    def _show_first_run_with_scan(self):
        """首次运行：先快速扫描已有实例，有则弹迁移向导，无则进全新部署向导。"""
        from ui.migration_dialog import ScanInstancesThread

        class _QuickScan(ScanInstancesThread):
            pass

        self._quick_scan = _QuickScan(self.backend)
        self._quick_scan.scan_done.connect(self._on_quick_scan_done)
        self._quick_scan.start()

    def _on_quick_scan_done(self, instances: list):
        if instances:
            self._show_migration_choice(instances)
        else:
            self._show_first_run_dialog()

    def _show_migration_choice(self, instances):
        """发现已有实例，询问用户是迁移还是全新部署。"""
        from PyQt6.QtWidgets import QMessageBox

        count = len(instances)
        msg = QMessageBox(self)
        msg.setWindowTitle("发现已有部署")
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setText(
            f"检测到本机存在 {count} 个 Nekro Agent 部署实例。\n\n"
            "是否将已有实例迁移到此启动器管理？\n"
            "选择「迁移」进入迁移向导，选择「全新部署」忽略已有实例。"
        )
        msg.setStyleSheet(self.styleSheet())
        for label in msg.findChildren(QLabel):
            label.setWordWrap(True)
        btn_migrate = msg.addButton("迁移已有实例", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("全新部署", QMessageBox.ButtonRole.RejectRole)
        msg.exec()

        if msg.clickedButton() == btn_migrate:
            self._show_migration_dialog()
        else:
            self._show_first_run_dialog()

    def _show_first_run_dialog(self):
        from ui.first_run_dialog import FirstRunDialog

        dialog = FirstRunDialog(self.backend, self.config, parent=self)
        dialog.deploy_requested.connect(self._on_deploy_mode_selected)
        dialog.exec()

    def _show_migration_dialog(self):
        from ui.migration_dialog import MigrationDialog

        dialog = MigrationDialog(self.backend, self.config, parent=self)
        dialog.deploy_requested.connect(self._on_deploy_mode_selected)
        dialog.exec()

    def _on_deploy_mode_selected(self, mode, inst_data: dict | None = None):
        self._is_first_deploy = True
        if inst_data:
            self._pending_inst_data = inst_data
        else:
            self._pending_inst_data = None
        nekro_port = (inst_data or {}).get("nekro_port") or self.config.get("nekro_port") or 8021
        napcat_port = (inst_data or {}).get("napcat_port") or self.config.get("napcat_port") or 6099
        self.browser_urls["nekro"] = f"http://localhost:{nekro_port}"
        self.browser_urls["napcat"] = f"http://localhost:{napcat_port}"
        if hasattr(self, "nekro_port_setting"):
            self.nekro_port_setting.setText(str(nekro_port))
        if hasattr(self, "napcat_port_setting"):
            self.napcat_port_setting.setText(str(napcat_port))
        self._refresh_port_settings_ui()
        self._schedule_next_image_update_check()
        self.start_deploy(deploy_mode_override=mode)

    _LOG_MAX_BLOCKS = 5000
    _LOG_PREVIEW_MAX_BLOCKS = 500

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
                self._trim_log_viewer(self.log_viewer_napcat)
            else:
                self.log_viewer_nekro.append(formatted)
                self._trim_log_viewer(self.log_viewer_nekro)
        else:
            self.log_viewer_app.append(formatted)
            self._trim_log_viewer(self.log_viewer_app)
            if hasattr(self, "log_preview"):
                self.log_preview.append(f"<span style='color:{color};'>[{level.upper()}] {msg}</span>")
                self._trim_log_viewer(self.log_preview, self._LOG_PREVIEW_MAX_BLOCKS)

        try:
            if original_level == "vm":
                print(msg)
            else:
                print(f"[{level.upper()}] {msg}")
        except Exception:
            pass

    def _trim_log_viewer(self, viewer, max_blocks=None):
        if max_blocks is None:
            max_blocks = self._LOG_MAX_BLOCKS
        doc = viewer.document()
        overflow = doc.blockCount() - max_blocks
        if overflow > 0:
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(QTextCursor.MoveOperation.Down, QTextCursor.MoveMode.KeepAnchor, overflow)
            cursor.removeSelectedText()
            cursor.deleteChar()

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
        if hasattr(self, "pull_spinner_label"):
            if visible:
                self.pull_spinner_label.start(80)
            else:
                self.pull_spinner_label.stop()

    def _summarize_pull_layers(self):
        total = len(self._pull_layer_order)
        if total <= 0:
            return ""

        done = 0
        downloading = 0
        extracting = 0
        verifying = 0
        waiting = 0

        for layer_id in self._pull_layer_order:
            status = self._pull_layers.get(layer_id, "")
            if status.startswith(("Pull complete", "Already exists", "Download complete")):
                done += 1
            elif status.startswith("Downloading"):
                downloading += 1
            elif status.startswith("Extracting"):
                extracting += 1
            elif status.startswith("Verifying"):
                verifying += 1
            elif status.startswith(("Waiting", "Pulling fs layer")):
                waiting += 1

        parts = [f"已完成 {done}/{total} 层"]
        if downloading:
            parts.append(f"下载中 {downloading} 层")
        if extracting:
            parts.append(f"解压中 {extracting} 层")
        if verifying:
            parts.append(f"校验中 {verifying} 层")
        if waiting:
            parts.append(f"等待中 {waiting} 层")
        return "，".join(parts)

    def _refresh_pull_status_label(self):
        text_parts = [part for part in [self._pull_stage_header, self._pull_summary_text] if part]
        self.pull_status_label.setText("\n".join(text_parts))

    def _update_pull_view(self, header="", detail=""):
        if header:
            self._pull_stage_header = header
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
                self._pull_summary_text = self._summarize_pull_layers()
        self._refresh_pull_status_label()
        self._set_pull_view_visible(True)

    def _clear_pull_progress(self):
        self._pull_stage_header = ""
        self._pull_summary_text = ""
        self._pull_layers.clear()
        self._pull_layer_order.clear()
        self.pull_status_label.setText("")
        self.pull_overall_bar.setValue(0)
        self._set_pull_view_visible(False)

    def _on_backend_progress(self, text):
        if text.startswith("__pull_progress__|"):
            parts = text.split("|", 2)
            if len(parts) < 3:
                return
            _, phase, message = parts
            if phase == "start":
                self._clear_pull_progress()
                self._update_pull_view(header=message)
            elif phase == "update":
                self._update_pull_view(detail=message)
            elif phase == "stage":
                self._pull_stage_header = message
                self._pull_summary_text = ""
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
            self._set_active_update_progress(phase, message)
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

    def _browser_views(self):
        if not hasattr(self, "browser_tabs"):
            return []
        return [
            self.browser_tabs.widget(index)
            for index in range(self.browser_tabs.count())
            if isinstance(self.browser_tabs.widget(index), WebViewWidget)
        ]

    def _reload_webview(self, browser_view, bypass_cache=True):
        if browser_view is None:
            return
        current_url = browser_view.get_url()
        if current_url and current_url.startswith(("http://", "https://")):
            browser_view.reload(bypass_cache=bypass_cache)
            return
        self._set_browser_target(
            browser_view.property("browser_target") or self.current_browser_target,
            force_reload=True,
            browser_view=browser_view,
        )

    def _reload_all_browser_views(self, bypass_cache=True, clear_http_cache=False):
        if clear_http_cache:
            for browser_view in self._browser_views():
                browser_view.clear_data()
        for browser_view in self._browser_views():
            self._reload_webview(browser_view, bypass_cache=bypass_cache)

    def _reset_browser_profile(self, clear_storage=False):
        tab_states = [
            {
                "target": browser_view.property("browser_target"),
                "url": browser_view.get_url(),
            }
            for browser_view in self._browser_views()
        ]

        while self.browser_tabs.count() > 0:
            w = self.browser_tabs.widget(0)
            self.browser_tabs.removeTab(0)
            if w is not None:
                w.deleteLater()

        for state in tab_states:
            target = state["target"]
            url = state["url"]
            title = self._target_label(target) if target in {"nekro", "napcat"} else "新标签页"
            browser_view = self._create_browser_tab(switch_to=False, title=title)
            browser_view.setProperty("browser_target", target)
            if target in {"nekro", "napcat"}:
                self._set_browser_target(target, force_reload=True, browser_view=browser_view)
            elif url:
                browser_view.load_url(url)

        if self.browser_tabs.count() > 0:
            self.browser_tabs.setCurrentIndex(0)
        self._refresh_browser_nav_buttons()

    def _refresh_browser_after_update(self):
        refresh_mode = self._pending_browser_refresh
        if not refresh_mode:
            return

        self._pending_browser_refresh = None
        if refresh_mode == "profile":
            self._reset_browser_profile(clear_storage=True)
            return

        self._reload_all_browser_views(bypass_cache=True, clear_http_cache=True)

    def _set_browser_target(self, target, force_reload=False, browser_view=None):
        if not self._can_access_target(target):
            self._show_notice_dialog("提示", "当前部署模式未启用 NapCat。")
            return

        browser_view = browser_view or self._current_webview()
        if browser_view is None:
            browser_view = self._create_browser_tab(switch_to=True, title=self._target_label(target))

        self.current_browser_target = target
        self.btn_browser_nekro.setChecked(target == "nekro")
        self.btn_browser_napcat.setChecked(target == "napcat")
        self.btn_browser_napcat.setVisible(self.config.get("deploy_mode") == "napcat")

        target_name = self._target_label(target)
        target_url = self._target_url(target)
        browser_view.setProperty("browser_target", target)
        self._update_browser_tab_label(browser_view, title=target_name, url=target_url)
        if browser_view is self._current_webview():
            self._sync_browser_url_label(target_url)

        if getattr(self.backend, "is_running", False):
            current_url = browser_view.get_url()
            if force_reload and current_url == target_url:
                self._reload_webview(browser_view, bypass_cache=True)
            elif force_reload or current_url != target_url:
                browser_view.load_url(target_url)
            else:
                self._reload_webview(browser_view, bypass_cache=False)
        else:
            placeholder = (
                f"{target_name} 服务尚未启动。<br><br>"
                "先在“总览控制台”完成部署，然后回到这里点击“刷新内嵌页面”。"
            )
            browser_view.load_html(f"<html><body style='font-family:Segoe UI;padding:24px;color:#243649;'>{placeholder}</body></html>")

        self._refresh_browser_nav_buttons()

    def _reload_browser_view(self):
        current_view = self._current_webview()
        if current_view is None:
            return
        self._reload_webview(current_view, bypass_cache=True)

    def _browser_go_back(self):
        current_view = self._current_webview()
        if current_view:
            current_view.go_back()

    def _browser_go_forward(self):
        current_view = self._current_webview()
        if current_view:
            current_view.go_forward()

    def _open_current_in_browser(self):
        webbrowser.open(self._browser_current_navigable_url())

    def _fill_browser_credentials(self):
        current_view = self._current_webview()
        if current_view is None:
            return

        payload = self._browser_fill_credentials_payload()
        if not payload:
            self._show_notice_dialog("提示", "尚未找到可用的登录凭据，请先完成部署。")
            return

        script = f"""
(() => {{
    const payload = {json.dumps(payload, ensure_ascii=False)};
    const normalize = (value) => String(value || "").trim().toLowerCase();
    const isVisible = (element) => {{
        if (!element || element.disabled || element.readOnly) return false;
        if (normalize(element.type) === "hidden") return false;
        const style = window.getComputedStyle(element);
        return style.display !== "none" && style.visibility !== "hidden";
    }};
    const inputs = Array.from(document.querySelectorAll("input, textarea")).filter(isVisible);
    const scoreField = (element, hints, allowedTypes) => {{
        const type = normalize(element.type) || "text";
        if (allowedTypes && !allowedTypes.includes(type)) return -1;
        const haystack = [
            element.type,
            element.name,
            element.id,
            element.placeholder,
            element.autocomplete,
            element.getAttribute("aria-label"),
            element.getAttribute("data-testid"),
            element.getAttribute("data-test"),
        ].map(normalize).join(" ");
        let score = 0;
        for (const hint of hints) {{
            if (haystack.includes(hint)) score += 3;
        }}
        if (type === "password" && hints.includes("password")) score += 5;
        return score;
    }};
    const pickBest = (hints, allowedTypes) => {{
        let best = null;
        let bestScore = -1;
        for (const element of inputs) {{
            const score = scoreField(element, hints, allowedTypes);
            if (score > bestScore) {{
                best = element;
                bestScore = score;
            }}
        }}
        return bestScore > 0 ? best : null;
    }};
    const setValue = (element, value) => {{
        if (!element) return false;
        const prototype = element.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
        if (descriptor && descriptor.set) {{
            descriptor.set.call(element, value);
        }} else {{
            element.value = value;
        }}
        element.dispatchEvent(new Event("input", {{ bubbles: true }}));
        element.dispatchEvent(new Event("change", {{ bubbles: true }}));
        return true;
    }};

    const usernameHints = ["username", "user", "account", "login", "admin", "用户名", "账号", "邮箱"];
    const passwordHints = ["password", "passwd", "pass", "pwd", "密码", "token", "令牌", "access_token", "access token"];

    let usernameField = null;
    if (payload.username) {{
        usernameField = pickBest(usernameHints, ["text", "email", "search"]);
        if (!usernameField) {{
            usernameField = inputs.find((element) => ["text", "email", "search"].includes(normalize(element.type) || "text")) || null;
        }}
    }}

    let passwordField = inputs.find((element) => normalize(element.type) === "password") || null;
    if (!passwordField) {{
        passwordField = pickBest(passwordHints, ["password", "text", "search", "textarea"]);
    }}
    if (!passwordField && !payload.username && inputs.length === 1) {{
        passwordField = inputs[0];
    }}

    const filledUser = payload.username ? setValue(usernameField, payload.username) : false;
    const filledPass = payload.password ? setValue(passwordField, payload.password) : false;

    if (window.pywebview && window.pywebview.api && window.pywebview.api.on_fill_result) {{
        window.pywebview.api.on_fill_result(JSON.stringify({{
            filledUser: !!filledUser,
            filledPass: !!filledPass,
            inputCount: inputs.length,
        }}));
    }}
}})();
"""

        def _handle_fill_result(result_json):
            try:
                result = json.loads(result_json)
            except (json.JSONDecodeError, TypeError):
                self._show_notice_dialog("提示", "当前页面暂时无法自动填充登录凭据。")
                return
            if result.get("filledUser") or result.get("filledPass"):
                return
            self._show_notice_dialog("提示", "当前页面未发现可填充的登录表单。")

        current_view.register_fill_callback(_handle_fill_result)
        current_view.evaluate_js(script)

    def _configure_napcat_network(self):
        current_view = self._current_webview()
        if current_view is None:
            return

        target = current_view.property("browser_target") if current_view else self.current_browser_target
        if target != "napcat":
            self._show_notice_dialog("提示", "请先切换到 NapCat 页面，再执行一键配网。")
            return
        if not getattr(self.backend, "is_running", False):
            self._show_notice_dialog("提示", "当前服务未启动，请先完成部署并等待 NapCat 可访问。")
            return
        if self._napcat_network_config_in_progress:
            return

        payload = self._browser_napcat_network_config_payload()
        if not payload:
            self._show_notice_dialog("提示", "未找到 OneBot 令牌，无法执行一键配网。")
            return

        self._napcat_network_config_in_progress = True
        self._refresh_browser_nav_buttons()
        self.backend.configure_napcat_network(payload)

    def _finish_napcat_network_config(self, result):
        self._napcat_network_config_in_progress = False
        self._refresh_browser_nav_buttons()

        if not isinstance(result, dict):
            self._show_notice_dialog("提示", "NapCat 一键配网未返回结果，请稍后重试。")
            return

        status = result.get("status") or "unknown"
        message = result.get("message") or "NapCat 一键配网失败，请稍后重试。"
        if status == "saved":
            self._show_notice_dialog("配网完成", message)
            return
        if status == "login_required":
            self._show_notice_dialog("请先登录", message)
            return
        if status == "restart_failed":
            self._show_notice_dialog("需要手动重启", message)
            return

        self._show_notice_dialog("配网失败", message)

    def _on_browser_url_changed(self, browser_view, url):
        self._update_browser_tab_label(browser_view, url=url)
        if browser_view is self._current_webview():
            self._sync_browser_url_label(url)
            self._refresh_browser_nav_buttons()

    def _on_browser_title_changed(self, browser_view, title):
        self._update_browser_tab_label(browser_view, title=title)

    def _on_browser_tab_changed(self, index):
        if not hasattr(self, "browser_tabs") or index < 0:
            return
        current_view = self._current_webview()
        if current_view:
            self._sync_browser_url_label(current_view.get_url())
        self._refresh_browser_nav_buttons()

    def _open_browser_devtools(self):
        current_url = self._browser_current_navigable_url()
        self._show_notice_dialog(
            "开发者工具",
            "WebView2 不支持内嵌 DevTools。\n\n"
            "请在 Edge 浏览器中打开 edge://inspect 进行远程调试，\n"
            f"或在系统浏览器中按 F12 调试页面：\n{current_url}",
        )
        webbrowser.open(current_url)

    def _switch_log_reader_to_active_instance(self):
        """切换实例时，将日志读取和健康检查指向新的 active 实例。"""
        from core.wsl.constants import DISTRO_NAME

        self.backend._stop_event.set()
        if self.backend._log_process and self.backend._log_process.poll() is None:
            try:
                self.backend._log_process.terminate()
            except Exception:
                pass
            self.backend._log_process = None
        self.backend._stop_event.clear()

        deploy_dir, _, _ = self.backend._get_active_deploy_paths()
        inst_id = self.config.get_active_instance_id() or ""
        inst_display = inst_id if inst_id and inst_id != "default" else ""
        log_prefix = f"[{inst_display}] " if inst_display else ""

        nekro_port = self.config.get("nekro_port") or 8021
        self.browser_urls["nekro"] = f"http://localhost:{nekro_port}"
        napcat_port = self.config.get("napcat_port") or 6099
        self.browser_urls["napcat"] = f"http://localhost:{napcat_port}"

        import threading
        threading.Thread(
            target=self.backend._log_reader,
            args=(DISTRO_NAME, deploy_dir, log_prefix, inst_id),
            daemon=True,
        ).start()

    def refresh_dashboard(self):
        if not hasattr(self, "status_badge"):
            return

        mode_text = self._format_mode_text(self.config.get("deploy_mode"))

        inst = self.config.get_instance()
        inst_id = self.config.get_active_instance_id()
        if inst and inst_id:
            inst_display = inst.get("instance_name", "").rstrip("_") or inst_id
            mode_text = f"{mode_text}  [{inst_display}]"

        mode_value = self.metric_mode.findChild(QLabel, "MetricValue")
        if mode_value:
            mode_value.setText(mode_text)
        self._refresh_metric_data_dir_card()

        if hasattr(self, "mode_display"):
            self.mode_display.setText(mode_text)
        if hasattr(self, "wsldir_edit"):
            self.wsldir_edit.setText(self.config.get("wsl_install_dir") or "未配置")
        if hasattr(self, "_settings_page") and hasattr(self._settings_page, "_refresh_instance_combo"):
            self._settings_page._refresh_instance_combo()
            self._settings_page._refresh_instance_info()
        self._refresh_port_settings_ui()
        if hasattr(self, "datadir_edit"):
            self.datadir_edit.setText(self.config.get_active_data_dir())
        self._schedule_next_image_update_check()

    def _refresh_metric_data_dir_card(self):
        if not hasattr(self, "metric_data_dir"):
            return

        data_dir = self.config.get_active_data_dir()
        host_data = self.backend.get_host_access_path(data_dir)
        value_label = self.metric_data_dir.findChild(QLabel, "MetricValue")
        hint_label = self.metric_data_dir.findChild(QLabel, "MetricHint")

        if value_label is not None:
            value_label.setText(host_data or "当前后端暂未提供 Windows 映射路径")
        if hint_label is not None:
            hint_label.setText("点击打开 Windows 侧文件夹" if host_data else f"容器内路径: {data_dir}")

        self.metric_data_dir.setToolTip(host_data or data_dir)
        self.metric_data_dir.set_clickable(bool(host_data))

    def start_deploy(self, show_logs=True, deploy_mode_override=None):
        if not self._guard_napcat_network_config_busy("开始部署"):
            return
        if not self._guard_blocking_status_idle("开始部署"):
            return

        pending = getattr(self, "_pending_inst_data", None)
        is_new_instance = bool(pending)

        if pending:
            self._apply_pending_instance()
        else:
            self._prev_active_instance = None
            self._prev_deploy_mode = None
            if self.backend.is_running:
                self._show_notice_dialog("提示", "服务已在运行中")
                return

        self._do_deploy(deploy_mode_override, show_logs, force_new_instance=is_new_instance)

    def _apply_pending_instance(self):
        """将 pending 新实例写入 config 并切换为 active。"""
        pending = self._pending_inst_data
        if not pending:
            return
        self._prev_active_instance = self.config.get_active_instance_id()
        self._prev_deploy_mode = self.config.get("deploy_mode")
        inst_id = pending["inst_id"]
        inst_save = {k: v for k, v in pending.items() if k != "inst_id"}
        self.config.set_instance(inst_id, inst_save)
        self.config.set("active_instance", inst_id)
        self.config.set("nekro_port", pending["nekro_port"])
        self.config.set("napcat_port", pending["napcat_port"])
        self.config.set("deploy_mode", pending["deploy_mode"])
        self.config.set("first_run", False)
        self.refresh_dashboard()

    def _do_deploy(self, deploy_mode_override=None, show_logs=True, force_new_instance=False):
        """实际执行部署（前置检查已通过）。"""
        deploy_mode = deploy_mode_override or self.config.get("deploy_mode")
        if not deploy_mode:
            self._show_first_run_dialog()
            return
        if not self._backend_runtime_exists():
            self._show_notice_dialog(
                "运行环境缺失",
                "当前保存的部署配置仍在，但 WSL 运行环境不存在。\n\n请先重新创建运行环境，再继续部署。",
                danger=True,
            )
            self._show_first_run_dialog()
            return

        if show_logs:
            self.switch_tab(2)
        self.log_viewer_app.clear()
        self.log_viewer_app.append(f"<span style='color:#7ce0a3;'>[INFO]</span> 开始部署服务 (模式: {deploy_mode})...")
        if hasattr(self, "log_preview"):
            self.log_preview.clear()
            self.log_preview.append(f"<span style='color:#7ce0a3;'>[INFO]</span> 开始部署服务 (模式: {deploy_mode})...")

        self.backend.start_services(deploy_mode, force_new_instance=force_new_instance)

    def _stop_services_for_mode_change(self):
        if not self._guard_napcat_network_config_busy("关闭服务"):
            return
        if not self._guard_blocking_status_idle("关闭服务"):
            return
        if self._update_in_progress:
            self._show_notice_dialog("提示", "当前有更新任务正在进行，请等待完成后再关闭服务。")
            return
        if not self.backend.is_running:
            self._show_notice_dialog("提示", "当前服务未在运行。")
            return

        reply = self._show_confirm_dialog(
            "关闭 NekroAgent",
            "将停止当前 docker compose 服务。\n\n"
            "运行环境、镜像和已保存配置不会删除。停止后可重新运行初始化向导以修改部署模式。\n\n"
            "确定要继续吗？",
            confirm_text="确认关闭",
        )
        if not reply:
            return

        self.switch_tab(2)
        self.log_viewer_app.append("<span style='color:#7ce0a3;'>[INFO]</span> 开始停止服务，用于修改部署模式...")
        if hasattr(self, "log_preview"):
            self.log_preview.append("<span style='color:#7ce0a3;'>[INFO]</span> 开始停止服务，用于修改部署模式...")
        self.backend.stop_services()

    def _rollback_pending_instance(self):
        """部署新实例失败后，回滚到之前的 active_instance 和 deploy_mode。"""
        prev_id = getattr(self, "_prev_active_instance", None)
        prev_mode = getattr(self, "_prev_deploy_mode", None)
        pending = getattr(self, "_pending_inst_data", None)

        if pending and pending.get("inst_id"):
            self.config.remove_instance(pending["inst_id"])

        if prev_id:
            self.config.set("active_instance", prev_id)
        if prev_mode:
            self.config.set("deploy_mode", prev_mode)
        prev_inst = self.config.get_instance(prev_id) if prev_id else None
        if prev_inst:
            self.config.set("nekro_port", prev_inst.get("nekro_port", 8021))
            self.config.set("napcat_port", prev_inst.get("napcat_port", 6099))

        self._pending_inst_data = None
        self._prev_active_instance = None
        self._prev_deploy_mode = None
        self.refresh_dashboard()

    def update_status_ui(self, status):
        previous_status = self._last_status
        self._last_status = status
        self.status_badge.setText(f"状态: {status}")

        blocking = self._blocking_status_detail(status)
        updating = status == "更新中..."
        running = status == "运行中"
        service_active = self._service_active()
        was_running = previous_status == "运行中"
        status_value = self.metric_status.findChild(QLabel, "MetricValue")
        if status_value:
            status_value.setText(status)
        if updating:
            metric_hint = "正在执行升级步骤"
            accent = "amber"
        elif blocking:
            metric_hint = blocking
            accent = "amber"
        elif service_active:
            metric_hint = "服务状态异常，请查看日志"
            accent = "amber"
        else:
            metric_hint = "服务可访问" if running else "等待部署或启动"
            accent = "green" if running else "red"
        status_hint = self.metric_status.findChild(QLabel, "MetricHint")
        if status_hint:
            status_hint.setText(metric_hint)
        self.metric_status.setProperty("accent", accent)
        self.metric_status.style().unpolish(self.metric_status)
        self.metric_status.style().polish(self.metric_status)

        self.btn_deploy_action.setEnabled(not service_active and not blocking)
        self.btn_primary_deploy.setEnabled(not service_active and not blocking)
        can_update = (
            bool(self.config.get("deploy_mode"))
            and running
            and not blocking
            and not self._napcat_network_config_in_progress
        )
        if hasattr(self, "btn_stop_action"):
            self.btn_stop_action.setEnabled(service_active and not blocking and not self._napcat_network_config_in_progress)
        self.btn_update_action.setEnabled(can_update)
        self.btn_primary_update.setEnabled(can_update)
        if hasattr(self, "btn_uninstall_action"):
            self.btn_uninstall_action.setEnabled(not self._napcat_network_config_in_progress and not blocking)
        if hasattr(self, "btn_primary_preview"):
            self.btn_primary_preview.setEnabled(
                self._advanced_features_enabled()
                and running
                and bool(self.config.get("deploy_mode"))
                and not blocking
                and not self._napcat_network_config_in_progress
            )
            self.btn_primary_preview.setText(self._preview_button_label())

        if running:
            self._refresh_browser_after_update()
            self.btn_primary_deploy.setText("服务运行中")
            self.btn_primary_update.setText("升级 Nekro Agent")
            if not was_running:
                self._schedule_next_image_update_check(delay_ms=5000)
            if self._active_update_kind in {"remote", "preview", "restore"} and self._pending_remote_update_message:
                success_title, _failure_title = self._active_update_result_titles()
                self._finish_update_session(True, success_title, self._pending_remote_update_message)
            if hasattr(self, "_is_first_deploy") and self._is_first_deploy:
                self._is_first_deploy = False
                self._pending_inst_data = None
                self._prev_active_instance = None
                self._prev_deploy_mode = None
            if self._current_webview() is not None and not was_running:
                self.switch_tab(1)
                self._set_browser_target(self.current_browser_target, force_reload=True)
            self._clear_pull_progress()
            self.btn_log_nekro.setVisible(True)
            self.btn_log_napcat.setVisible(self.config.get("deploy_mode") == "napcat")
        else:
            if hasattr(self, "_auto_image_check_timer"):
                self._auto_image_check_timer.stop()
            self.btn_log_nekro.setVisible(service_active)
            self.btn_log_napcat.setVisible(service_active and self.config.get("deploy_mode") == "napcat")
            self.btn_primary_deploy.setText("服务仍在运行" if service_active else "开始部署")
            self.btn_primary_update.setText("升级 Nekro Agent" if not updating else "升级中...")
            update_recovery_failed = (
                self._active_update_kind in {"remote", "preview", "restore"}
                and status in {"启动失败", "更新失败", "启动超时", "已停止"}
                and (not service_active or status == "启动超时")
            )
            if update_recovery_failed:
                action_text = "恢复正式版后服务" if self._active_update_kind == "restore" else "升级后服务"
                failure_message = (
                    f"{self._pending_remote_update_message}\n\n最终状态：{status}"
                    if self._pending_remote_update_message
                    else f"{action_text}未能恢复：{status}"
                )
                _success_title, failure_title = self._active_update_result_titles()
                self._finish_update_session(False, failure_title, failure_message)
            if status == "启动失败" and getattr(self, "_prev_active_instance", None) is not None:
                self._rollback_pending_instance()

            if self._quit_after_stop and status in {"已停止", "已卸载"}:
                self._quit_after_stop = False
                QApplication.quit()
            if self._quit_after_stop and status == "停止失败":
                self._quit_after_stop = False
                self._show_notice_dialog("关闭失败", "服务停止失败，启动器将继续保持打开。", danger=True)
            if self._current_webview() is not None and was_running and not service_active:
                self._set_browser_target(self.current_browser_target, force_reload=False)
            if status in {"启动失败", "更新失败", "启动超时", "已停止", "已卸载"}:
                self._clear_pull_progress()

        if status == "已卸载":
            self.refresh_dashboard()
            if self._uninstall_in_progress:
                self._uninstall_in_progress = False
                self._show_notice_dialog("卸载完成", "运行环境已卸载完成。")
        elif status == "卸载失败" and self._uninstall_in_progress:
            self._uninstall_in_progress = False

        if hasattr(self, "btn_browser_napcat"):
            self.btn_browser_napcat.setVisible(self.config.get("deploy_mode") == "napcat")
        self._refresh_browser_nav_buttons()

    def _on_update_optional_confirm(self, step_label, prompt):
        confirmed = self._show_confirm_dialog(
            step_label,
            prompt,
            confirm_text="执行",
            cancel_text="跳过",
            parent=self._active_update_dialog or self,
        )
        self.backend.reply_update_optional(confirmed)

    def _on_update_finished(self, success, message):
        update_kind = self._active_update_kind
        self.btn_primary_update.setText("升级 Nekro Agent")
        if hasattr(self, "btn_primary_preview"):
            self.btn_primary_preview.setText(self._preview_button_label())
        self._refresh_advanced_feature_ui()
        success_title, failure_title = self._active_update_result_titles()
        if success:
            if update_kind in {"preview", "restore"}:
                self._pending_browser_refresh = "profile"
            elif update_kind == "remote":
                self._pending_browser_refresh = "cache"
            self._pending_remote_update_message = message
            if self._active_update_dialog:
                self._active_update_dialog.set_progress(
                    status_text=message,
                    detail_text="正在等待服务重新就绪...",
                    busy=True,
                )
        else:
            self._finish_update_session(False, failure_title, message)
        self.update_status_ui(self._last_status)

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget is not None:
                widget.deleteLater()
            elif child_layout is not None:
                self._clear_layout(child_layout)

    def _rebuild_image_rows(self):
        from ui.pages.images_page import rebuild_image_rows
        rebuild_image_rows(self)

    def _rebuild_image_rows_impl(self):
        self._rebuild_image_rows()

    def _tick_img_spinner(self):
        self._img_spinner_idx = (self._img_spinner_idx + 1) % len(self._img_spinner_frames)
        frame = self._img_spinner_frames[self._img_spinner_idx]
        if self._img_checking_ref is None:
            self.btn_check_images.setText(f"{frame} 检测中...")
        else:
            widgets = self._image_row_widgets.get(self._img_checking_ref)
            if widgets:
                widgets["btn"].setText(f"{frame}")

    def _image_update_check_interval_options(self):
        return [
            (0, "不自动检查"),
            (1, "每 1 小时"),
            (6, "每 6 小时"),
            (12, "每 12 小时"),
            (24, "每 24 小时"),
        ]

    def _image_update_check_interval_hours(self):
        try:
            return max(0, int(self.config.get("image_update_check_interval_hours") or 0))
        except (TypeError, ValueError):
            return 24

    def _image_update_check_interval_label(self, hours=None):
        current = self._image_update_check_interval_hours() if hours is None else hours
        for value, label in self._image_update_check_interval_options():
            if value == current:
                return label
        return f"每 {current} 小时"

    def _refresh_image_update_check_hint(self):
        if not hasattr(self, "image_update_check_hint"):
            return

        interval_hours = self._image_update_check_interval_hours()
        if interval_hours <= 0:
            text = "已关闭自动检查。仅在启动器运行期间生效，不会自动拉取更新。"
        else:
            text = f"当前：{self._image_update_check_interval_label(interval_hours)}。仅在启动器运行期间自动检查镜像状态，不会自动拉取更新。"
            last_check_ts = int(self.config.get("last_image_update_check_ts") or 0)
            if last_check_ts > 0:
                last_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(last_check_ts))
                text += f"\n上次检查：{last_text}"
        self.image_update_check_hint.setText(text)

    def _schedule_next_image_update_check(self, delay_ms=None):
        if not hasattr(self, "_auto_image_check_timer"):
            return

        self._auto_image_check_timer.stop()
        interval_hours = self._image_update_check_interval_hours()
        if interval_hours <= 0 or not self.config.get("deploy_mode") or self._last_status != "运行中":
            self._refresh_image_update_check_hint()
            return

        if delay_ms is None:
            interval_ms = interval_hours * 60 * 60 * 1000
            last_check_ts = int(self.config.get("last_image_update_check_ts") or 0)
            if last_check_ts > 0:
                elapsed_ms = max(0, int((time.time() - last_check_ts) * 1000))
                delay_ms = max(5000, interval_ms - elapsed_ms)
            else:
                delay_ms = 5000

        delay_ms = max(1000, min(int(delay_ms), 2147483647))
        self._auto_image_check_timer.start(delay_ms)
        self._refresh_image_update_check_hint()

    def _run_scheduled_image_check(self):
        if self._image_status_request_kind is not None or self._update_in_progress:
            self._schedule_next_image_update_check(delay_ms=5 * 60 * 1000)
            return
        if not self.config.get("deploy_mode") or self._last_status != "运行中":
            self._schedule_next_image_update_check()
            return

        self._image_status_request_kind = "auto"
        self.backend.check_images_status()

    def _on_image_update_interval_changed(self):
        if not hasattr(self, "image_update_interval_combo"):
            return

        hours = int(self.image_update_interval_combo.currentData() or 0)
        self.config.set("image_update_check_interval_hours", hours)
        if hours <= 0:
            self.config.set("last_image_update_check_ts", 0)
        self._schedule_next_image_update_check()

    def _check_single_image(self, image_ref):
        if self._image_status_request_kind is not None:
            self._show_notice_dialog("提示", "镜像状态检测正在进行中，请稍后再试。")
            return
        widgets = self._image_row_widgets.get(image_ref)
        if not widgets:
            return
        self._image_status_request_kind = "single"
        self._img_checking_ref = image_ref
        widgets["btn"].setEnabled(False)
        widgets["btn"].setText(self._img_spinner_frames[0])
        widgets["local"].setText("...")
        widgets["remote"].setText("...")
        widgets["status"].setText("检测中")
        self._img_spinner_timer.start(100)
        self.backend.check_images_status(only_image=image_ref)

    def _check_images(self):
        if self._image_status_request_kind is not None:
            self._show_notice_dialog("提示", "镜像状态检测正在进行中，请稍后再试。")
            return
        self._image_status_request_kind = "all"
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

    def _cached_image_status_map(self):
        cached = self.config.get("image_status_cache") or {}
        return cached if isinstance(cached, dict) else {}

    def _cache_image_status_results(self, results):
        cached = dict(self._cached_image_status_map())
        checked_at = int(time.time())
        for entry in results:
            image_ref = entry.get("image")
            if not image_ref:
                continue
            cached[image_ref] = {
                "image": image_ref,
                "name": entry.get("name", ""),
                "modes": list(entry.get("modes", [])),
                "local": entry.get("local"),
                "remote": entry.get("remote"),
                "has_update": bool(entry.get("has_update")),
                "error": entry.get("error"),
                "checked_at": checked_at,
            }
        self.config.set("image_status_cache", cached)

    def _set_image_row_action(self, image_ref, action):
        widgets = self._image_row_widgets.get(image_ref)
        if not widgets:
            return
        try:
            widgets["btn"].clicked.disconnect()
        except Exception:
            pass

        if action == "update":
            if image_ref == self._agent_image_ref():
                widgets["btn"].clicked.connect(lambda checked=False: self._show_remote_update_dialog())
            else:
                widgets["btn"].clicked.connect(lambda checked=False, ref=image_ref: self._show_image_update_dialog(ref))
        else:
            widgets["btn"].clicked.connect(lambda checked=False, ref=image_ref: self._check_single_image(ref))

    def _apply_image_status_entry(self, entry):
        image_ref = entry.get("image")
        widgets = self._image_row_widgets.get(image_ref)
        if not widgets:
            return

        widgets["btn"].setEnabled(True)
        if entry.get("error"):
            widgets["local"].setText("—")
            widgets["remote"].setText("—")
            widgets["status"].setText("<span style='color:#f26f82;'>错误</span>")
            widgets["status"].setTextFormat(Qt.TextFormat.RichText)
            widgets["btn"].setText("检查更新")
            self._set_image_row_action(image_ref, "check")
            return

        if entry.get("local") is None:
            widgets["local"].setText("未安装")
            widgets["remote"].setText(entry.get("remote") or "—")
            widgets["status"].setText("<span style='color:#d29922;'>未拉取</span>")
            widgets["status"].setTextFormat(Qt.TextFormat.RichText)
            widgets["btn"].setText("检查更新")
            self._set_image_row_action(image_ref, "check")
            return

        widgets["local"].setText(entry.get("local") or "—")
        widgets["remote"].setText(entry.get("remote") or "—")
        if entry.get("has_update"):
            widgets["status"].setText("<span style='color:#58a6ff;'>有更新</span>")
            widgets["status"].setTextFormat(Qt.TextFormat.RichText)
            widgets["btn"].setText("立即更新")
            self._set_image_row_action(image_ref, "update")
        else:
            widgets["status"].setText("<span style='color:#3fb950;'>最新</span>")
            widgets["status"].setTextFormat(Qt.TextFormat.RichText)
            widgets["btn"].setText("检查更新")
            self._set_image_row_action(image_ref, "check")

    def _apply_cached_image_status_to_rows(self):
        cached = self._cached_image_status_map()
        for image_ref, widgets in self._image_row_widgets.items():
            entry = cached.get(image_ref)
            if not entry:
                widgets["local"].setText("—")
                widgets["remote"].setText("—")
                widgets["status"].setText("未检测")
                widgets["status"].setTextFormat(Qt.TextFormat.PlainText)
                widgets["btn"].setEnabled(True)
                widgets["btn"].setText("检查更新")
                self._set_image_row_action(image_ref, "check")
                continue
            self._apply_image_status_entry(entry)

    def _update_available_image_entries(self, results):
        updates = []
        for entry in results:
            if entry.get("has_update"):
                updates.append(entry)
        return updates

    def _image_update_alert_signature(self, entries):
        parts = []
        for entry in sorted(entries, key=lambda item: item.get("image", "")):
            parts.append(f"{entry.get('image', '')}|{entry.get('remote', '')}")
        return "\n".join(parts)

    def _notify_image_updates_if_needed(self, results, request_kind):
        if request_kind != "auto":
            return
        if self._last_status != "运行中":
            return

        updates = self._update_available_image_entries(results)
        if not updates:
            self.config.set("image_update_last_alert_signature", "")
            return

        signature = self._image_update_alert_signature(updates)
        if signature == (self.config.get("image_update_last_alert_signature") or ""):
            return

        self.config.set("image_update_last_alert_signature", signature)
        if not self._can_show_foreground_notice():
            return
        names = [entry.get("name") or entry.get("image", "") for entry in updates]
        summary = "、".join(names[:3])
        if len(names) > 3:
            summary += f" 等 {len(names)} 个镜像"
        self._show_notice_dialog(
            "发现镜像更新",
            f"检测到以下镜像有可用更新：\n{summary}\n\n可前往“镜像管理”页面查看详情并手动更新。",
        )

    def _on_image_status_result(self, results):
        request_kind = self._image_status_request_kind
        self._image_status_request_kind = None
        self._img_spinner_timer.stop()
        self.btn_check_images.setEnabled(True)
        self.btn_check_images.setText("检查全部更新")
        self._cache_image_status_results(results)
        for entry in results:
            self._apply_image_status_entry(entry)
        if request_kind in {"all", "auto"}:
            self.config.set("last_image_update_check_ts", int(time.time()))
        self._notify_image_updates_if_needed(results, request_kind)
        self._refresh_image_update_check_hint()
        self._schedule_next_image_update_check()

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
            cached = dict(self._cached_image_status_map())
            cached_entry = dict(cached.get(image_ref) or {})
            cached_entry.update({
                "image": image_ref,
                "has_update": False,
                "error": None,
                "checked_at": int(time.time()),
            })
            if cached_entry.get("remote"):
                cached_entry["local"] = cached_entry["remote"]
            cached[image_ref] = cached_entry
            self.config.set("image_status_cache", cached)
            remaining_updates = self._update_available_image_entries(list(cached.values()))
            self.config.set(
                "image_update_last_alert_signature",
                self._image_update_alert_signature(remaining_updates) if remaining_updates else "",
            )
            detail_text = ""
            if image_ref == "kromiose/nekro-cc-sandbox":
                detail_text = "请前往 WebUI → 工作区 → 工作区管理 → 对应工作区 → 沙盒容器，手动重建沙盒以应用新版本。"
            elif image_ref == "kromiose/nekro-agent-sandbox":
                detail_text = "沙盒由 Nekro Agent 动态创建销毁，新版本将在下次任务执行时自动生效，无需手动操作。"
            self._finish_update_session(True, "更新完成", detail_text or message)
        else:
            widgets["btn"].setText("立即更新")
            widgets["status"].setText("<span style='color:#f26f82;'>更新失败</span>")
            widgets["status"].setTextFormat(Qt.TextFormat.RichText)
            self._finish_update_session(False, "更新失败", message)

    def _refresh_port_settings_ui(self):
        show_napcat = self.config.get("deploy_mode") == "napcat"
        if hasattr(self, "napcat_port_label"):
            self.napcat_port_label.setVisible(show_napcat)
        if hasattr(self, "napcat_port_setting"):
            self.napcat_port_setting.setVisible(show_napcat)
        if hasattr(self, "port_hint_label"):
            hint = "修改端口后需重新部署服务才能生效。"
            if not show_napcat:
                hint = "Lite 模式仅使用 Nekro Agent 端口。修改端口后需重新部署服务才能生效。"
            self.port_hint_label.setText(hint)

    def _save_ports(self):
        deploy_mode = self.config.get("deploy_mode") or "lite"
        try:
            nekro_port = int(self.nekro_port_setting.text().strip())
            if not (1 <= nekro_port <= 65535):
                raise ValueError
            napcat_port = int(self.config.get("napcat_port") or 6099)
            if deploy_mode == "napcat":
                napcat_port = int(self.napcat_port_setting.text().strip())
                if not (1 <= napcat_port <= 65535):
                    raise ValueError

            ignore_ports = set()
            if getattr(self.backend, "is_running", False):
                current_nekro = int(self.config.get("nekro_port") or 8021)
                if nekro_port == current_nekro:
                    ignore_ports.add(nekro_port)
                if deploy_mode == "napcat":
                    current_napcat = int(self.config.get("napcat_port") or 6099)
                    if napcat_port == current_napcat:
                        ignore_ports.add(napcat_port)

            port_specs = [("Nekro Agent 端口", nekro_port)]
            if deploy_mode == "napcat":
                port_specs.append(("NapCat 端口", napcat_port))
            ok, message = validate_port_bindings(
                port_specs,
                ignore_ports=ignore_ports,
            )
            if not ok:
                self._show_notice_dialog("端口冲突", message)
                return

            self.config.set("nekro_port", nekro_port)
            if deploy_mode == "napcat":
                self.config.set("napcat_port", napcat_port)

            active_id = self.config.get_active_instance_id()
            deploy_info = self.config.get("deploy_info")
            if deploy_info:
                deploy_info["port"] = str(nekro_port)
                if deploy_mode == "napcat":
                    deploy_info["napcat_port"] = str(napcat_port)
                self.config.set("deploy_info", deploy_info)

            if active_id:
                update_kwargs = {"nekro_port": nekro_port}
                if deploy_mode == "napcat":
                    update_kwargs["napcat_port"] = napcat_port
                if deploy_info:
                    update_kwargs["deploy_info"] = deploy_info
                self.config.update_instance(active_id, **update_kwargs)

            if not getattr(self.backend, "is_running", False):
                self.browser_urls["nekro"] = f"http://localhost:{nekro_port}"
                self.browser_urls["napcat"] = f"http://localhost:{napcat_port}"
            current_view = self._current_webview()
            if getattr(self.backend, "is_running", False):
                if hasattr(self, "browser_url_label"):
                    self._sync_browser_url_label()
                self._show_notice_dialog("保存成功", "端口设置已保存，重新部署服务后生效。当前运行中的服务仍使用旧端口。")
            else:
                target_url = self._target_url(self.current_browser_target)
                if hasattr(self, "browser_url_label"):
                    self._sync_browser_url_label(target_url)
                if current_view is not None:
                    current_view.load_url(target_url)
                self._show_notice_dialog("保存成功", "端口设置已保存，重新部署服务后生效。")
        except ValueError:
            self._show_notice_dialog("提示", "请输入有效的端口号（1-65535）。")

    def _refresh_datadir_hint(self):
        data_dir = self.config.get_active_data_dir()
        sample_path = self.backend.get_host_access_path(data_dir)
        if sample_path:
            self.datadir_hint.setText(f"宿主机可访问路径: {sample_path}")
        else:
            self.datadir_hint.setText(f"当前后端 {self.backend.display_name} 暂未提供宿主机侧直接打开路径。")
        if hasattr(self, "datadir_edit"):
            self.datadir_edit.setText(data_dir)

    def _open_datadir_in_explorer(self):
        data_dir = self.config.get_active_data_dir()
        win_path = self.backend.get_host_access_path(data_dir)
        if not win_path:
            self._show_notice_dialog("提示", f"当前后端 {self.backend.display_name} 暂不支持直接打开宿主机路径。")
            return
        try:
            os.startfile(win_path)
        except Exception as error:
            self._show_notice_dialog("提示", f"无法打开目录，请确认服务已启动且目录已创建。\n\n路径: {win_path}\n错误: {error}", danger=True)

    def _update_services(self):
        self._show_remote_update_dialog()

    def _uninstall_environment(self):
        if not self._guard_napcat_network_config_busy("卸载环境"):
            return
        if not self._guard_blocking_status_idle("卸载环境"):
            return

        instances = self.config.list_instances()
        if not instances:
            self._show_notice_dialog("无可移除的实例", "当前没有已部署的实例。")
            return

        if len(instances) == 1:
            self._confirm_remove_instance(instances[0][0])
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("移除实例")
        dialog.setMinimumWidth(400)
        dialog.setMaximumWidth(500)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        title = QLabel("选择要移除的实例")
        title.setProperty("role", "dialog_title")
        layout.addWidget(title)

        desc = QLabel(f"当前共有 {len(instances)} 个部署实例，请选择要移除的实例：")
        desc.setProperty("role", "dialog_desc")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        from ui.widgets import StyledComboBox
        combo = StyledComboBox()
        combo.setMinimumWidth(360)
        for inst_id, inst_data in instances:
            name = inst_data.get("instance_name", "").rstrip("_") or inst_id
            mode = "napcat" if inst_data.get("deploy_mode") == "napcat" else "lite"
            port = inst_data.get("nekro_port", 8021)
            combo.addItem(f"{name}  ({mode}, :{port})", inst_id)
        layout.addWidget(combo)

        selected_id = [None]

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        btn_cancel = QPushButton("取消")
        btn_cancel.setFixedHeight(36)
        btn_cancel.clicked.connect(dialog.reject)
        btn_row.addWidget(btn_cancel)

        btn_row.addStretch()

        btn_remove = QPushButton("移除选中实例")
        btn_remove.setProperty("role", "danger")
        btn_remove.setFixedHeight(36)
        btn_remove.setCursor(Qt.CursorShape.PointingHandCursor)
        def _on_remove():
            selected_id[0] = combo.currentData()
            dialog.accept()
        btn_remove.clicked.connect(_on_remove)
        btn_row.addWidget(btn_remove)

        layout.addLayout(btn_row)
        dialog.exec()

        if selected_id[0]:
            self._confirm_remove_instance(selected_id[0])

    def _confirm_remove_instance(self, inst_id):
        """移除单个实例：停止其 compose 服务、删除 deploy_dir、从 config 中移除。"""
        inst = self.config.get_instance(inst_id)
        if not inst:
            return
        name = inst.get("instance_name", "").rstrip("_") or inst_id
        reply = self._show_confirm_dialog(
            f"移除实例「{name}」",
            f"将停止该实例的容器并删除其部署目录。\n\n"
            f"部署目录: {inst.get('deploy_dir', '?')}\n"
            f"数据目录: {inst.get('data_dir', '?')}\n\n"
            "数据目录将保留，不会被删除。此操作不可撤销。",
            confirm_text="确认移除",
            danger=True,
        )
        if not reply:
            return

        is_active = (inst_id == self.config.get_active_instance_id())
        self.switch_tab(2)
        self.log_viewer_app.append(
            f"<span style='color:#7ce0a3;'>[INFO]</span> 开始移除实例「{name}」..."
        )
        self.backend.remove_single_instance(inst_id, inst, was_active=is_active)

    def _on_remove_instance_done(self, success, inst_id, was_active):
        if success:
            self.config.remove_instance(inst_id)
            if was_active:
                remaining = self.config.list_instances()
                if remaining:
                    first_id, first_data = remaining[0]
                    self.config.set("active_instance", first_id)
                    self.config.set("deploy_mode", first_data.get("deploy_mode", ""))
                    self.config.set("nekro_port", first_data.get("nekro_port", 8021))
                    self.config.set("napcat_port", first_data.get("napcat_port", 6099))
                    self._switch_log_reader_to_active_instance()
                else:
                    self.config.set("active_instance", "")
                    self.config.set("deploy_mode", "")
                    self.config.set("first_run", True)
            self.refresh_dashboard()
            self._show_notice_dialog("移除完成", "实例已移除。")
        else:
            self._show_notice_dialog("移除失败", "移除实例时发生错误，请查看日志。", danger=True)

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
        """返回 1=最小化到托盘, 2=停止服务并退出, 其他=取消"""
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
        if not self._guard_napcat_network_config_busy("退出启动器"):
            event.ignore()
            return
        if not self._guard_blocking_status_idle("退出启动器"):
            event.ignore()
            return
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
            self.showNormal()
            self.activateWindow()

    def _quit_app(self):
        if not self._guard_napcat_network_config_busy("退出启动器"):
            return
        if not self._guard_blocking_status_idle("退出启动器"):
            return
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
            _spinner = SpinnerLabel(dialog)
            _signal_cleaned = [False]

            boot_row = QHBoxLayout()
            boot_row.setSpacing(8)
            boot_row.addWidget(_spinner)
            boot_row.addWidget(boot_status, 1)
            layout.insertLayout(layout.count() - 1, boot_row)

            def _update_boot_text():
                boot_status.setText("<span style='color:#58a6ff;'>等待服务启动...</span>")

            def _cleanup_boot_signals():
                if _signal_cleaned[0]:
                    return
                _signal_cleaned[0] = True
                _spinner.stop()
                try:
                    self.backend.boot_finished.disconnect(_on_boot_finished)
                except Exception:
                    pass
                try:
                    self.backend.status_changed.disconnect(_on_timeout)
                except Exception:
                    pass

            _spinner.start()
            _update_boot_text()

            def _on_boot_finished():
                _spinner.set_finished(True)
                boot_status.setText("<span style='color:#3fb950;'>✓ 服务已就绪，可以开始使用</span>")
                btn_close.setEnabled(True)
                _cleanup_boot_signals()

            def _on_timeout(status):
                if status in {"启动超时", "启动失败"}:
                    _spinner.set_finished(False)
                    boot_status.setText(f"<span style='color:#f26f82;'>✗ {status}，请检查日志</span>")
                    btn_close.setEnabled(True)
                    _cleanup_boot_signals()

            self.backend.boot_finished.connect(_on_boot_finished)
            self.backend.status_changed.connect(_on_timeout)
            dialog.finished.connect(lambda _result: _cleanup_boot_signals())
        else:
            boot_status.setVisible(False)

        dialog.exec()

    def _show_saved_credentials(self):
        info = self.config.get("deploy_info")
        if not info:
            self._show_notice_dialog("提示", "尚未部署，暂无凭据信息。\n请先完成部署。")
            return
        self._show_credentials_dialog(info, wait_for_boot=False)
