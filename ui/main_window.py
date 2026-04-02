import re
import os
import shutil
import sys
import json
import time
import webbrowser
from collections import OrderedDict

from PyQt6.QtCore import QRect, QSize, QTimer, Qt, QUrl
from PyQt6.QtGui import QCloseEvent, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
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
    QStyle,
    QTabBar,
    QStyleOptionTab,
    QStylePainter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PyQt6.QtWebEngineWidgets import QWebEngineView

from core.backend_factory import BackendFactory
from core.config_manager import ConfigManager
from core.port_utils import validate_port_bindings
from ui.styles import STYLESHEET
from ui.widgets import ActionButton, MetricCard, SectionCard, StyledComboBox, UpdateProgressDialog, show_notice_dialog


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


class LauncherWebPage(QWebEnginePage):
    def __init__(self, profile, parent=None, console_handler=None):
        super().__init__(profile, parent)
        self._console_handler = console_handler

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        if self._console_handler:
            try:
                self._console_handler(level, message, line_number, source_id)
            except Exception:
                pass


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
        self._active_update_dialog = None
        self._active_update_kind = None
        self._pending_remote_update_message = ""
        self._pull_stage_header = ""
        self._pull_summary_text = ""
        self._pull_layers = OrderedDict()
        self._pull_layer_order = []
        self._sidebar_collapsed = False
        self._browser_profile_channel = None
        self._pending_browser_refresh = None
        self._image_status_request_kind = None
        self.browser_urls = {
            "nekro": f"http://localhost:{self.config.get('nekro_port') or 8021}",
            "napcat": f"http://localhost:{self.config.get('napcat_port') or 6099}",
        }
        self.current_browser_target = "nekro"
        self._init_browser_profile()
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
        self._schedule_next_image_update_check()
        QTimer.singleShot(200, self._on_startup)
        QTimer.singleShot(0, self._apply_responsive_layout)

    def _create_browser_profile(self, channel=None, clear_storage=False):
        channel = channel or self._release_channel()
        profile_root = os.path.join(self.config.browser_profile_dir, channel)
        storage_path = os.path.join(profile_root, "storage")
        cache_path = os.path.join(profile_root, "cache")
        if clear_storage:
            shutil.rmtree(profile_root, ignore_errors=True)
        os.makedirs(storage_path, exist_ok=True)
        os.makedirs(cache_path, exist_ok=True)

        browser_profile = QWebEngineProfile(f"nekro_launcher_{channel}", self)
        browser_profile.setPersistentStoragePath(storage_path)
        browser_profile.setCachePath(cache_path)
        browser_profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        browser_profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
        self._browser_profile_channel = channel
        return browser_profile

    def _init_browser_profile(self):
        self.browser_profile = self._create_browser_profile()

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
        return current_view if isinstance(current_view, QWebEngineView) else None

    def _browser_current_navigable_url(self):
        current_view = self._current_webview()
        if current_view:
            current_url = current_view.url()
            if current_url.isValid() and current_url.scheme().lower() in {"http", "https"}:
                return current_url.toString()
        return self._target_url()

    def _sync_browser_url_label(self, url=None):
        if not hasattr(self, "browser_url_label"):
            return

        display_url = None
        if isinstance(url, QUrl) and url.isValid() and url.scheme().lower() in {"http", "https"}:
            display_url = url.toString()
        if not display_url:
            display_url = self._browser_current_navigable_url()
        self.browser_url_label.setText(display_url)

    def _can_show_foreground_notice(self):
        return self.isVisible() and not self.isMinimized()

    def _browser_tab_text(self, browser_view, title=None, url=None):
        title_text = (title or browser_view.title() or "").strip()
        if title_text:
            return f"{title_text[:18]}..." if len(title_text) > 18 else title_text

        target = browser_view.property("browser_target")
        if target in {"nekro", "napcat"}:
            return self._target_label(target)

        current_url = url or browser_view.url()
        if isinstance(current_url, QUrl) and current_url.isValid():
            if current_url.host():
                return current_url.host()
            if current_url.toString():
                return current_url.toString()[:18]

        return "新标签页"

    def _refresh_browser_nav_buttons(self):
        current_view = self._current_webview()
        has_view = current_view is not None

        if hasattr(self, "browser_back_btn"):
            self.browser_back_btn.setEnabled(has_view and current_view.history().canGoBack())
        if hasattr(self, "browser_forward_btn"):
            self.browser_forward_btn.setEnabled(has_view and current_view.history().canGoForward())
        if hasattr(self, "browser_reload_btn"):
            self.browser_reload_btn.setEnabled(has_view)
        if hasattr(self, "browser_fill_credentials_btn"):
            self.browser_fill_credentials_btn.setEnabled(has_view and self._has_fillable_browser_credentials())
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

    def _update_browser_tab_label(self, browser_view, title=None, url=None):
        if not hasattr(self, "browser_tabs"):
            return
        index = self.browser_tabs.indexOf(browser_view)
        if index >= 0:
            self.browser_tabs.setTabText(index, self._browser_tab_text(browser_view, title=title, url=url))

    def _create_browser_tab(self, switch_to=True, title="新标签页"):
        browser_view = QWebEngineView()
        browser_page = self._create_browser_page(browser_view)
        browser_view.setPage(browser_page)
        browser_view.setMinimumHeight(200)
        browser_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        browser_view.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        browser_view.setAutoFillBackground(True)
        browser_view.setStyleSheet("background: #ffffff; border: none;")
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
            self.btn_primary_preview.setEnabled(enabled and bool(self.config.get("deploy_mode")) and self._last_status != "更新中...")
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
        if self._update_in_progress:
            self._show_notice_dialog("提示", "当前已有更新任务正在进行，请等待完成后再试。")
            return
        if not self.config.get("deploy_mode"):
            self._show_notice_dialog("提示", "尚未完成部署，无法切换到预览版。")
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
        if self._update_in_progress:
            self._show_notice_dialog("提示", "升级流程正在进行中，请等待当前操作完成。")
            return
        if not self.config.get("deploy_mode"):
            self._show_notice_dialog("提示", "尚未完成部署，无法执行升级。")
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
        self._refresh_port_settings_ui()
        self.refresh_dashboard()
        self._schedule_next_image_update_check()
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
            _, phase, message = text.split("|", 2)
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
            if isinstance(self.browser_tabs.widget(index), QWebEngineView)
        ]

    def _handle_browser_console_message(self, level, message, line_number, source_id):
        if not message:
            return
        level_name = getattr(level, "name", str(level))
        source_name = source_id or "inline"
        if "/webui/assets/" in source_name:
            source_name = source_name.split("/webui/assets/", 1)[1]
        else:
            source_name = os.path.basename(source_name) or "inline"
        self.append_log(f"[WebView:{level_name}] {message} ({source_name}:{line_number})", "debug")

    def _create_browser_page(self, browser_view):
        browser_page = LauncherWebPage(
            self.browser_profile,
            browser_view,
            console_handler=self._handle_browser_console_message,
        )
        browser_page.setBackgroundColor(QColor("#ffffff"))
        browser_page.newWindowRequested.connect(
            lambda request, view=browser_view: self._handle_browser_new_window(view, request)
        )
        return browser_page

    def _reload_webview(self, browser_view, bypass_cache=True):
        if browser_view is None:
            return
        current_url = browser_view.url()
        if current_url.isValid() and current_url.scheme().lower() in {"http", "https"}:
            if bypass_cache:
                try:
                    browser_view.page().triggerAction(QWebEnginePage.WebAction.ReloadAndBypassCache)
                    return
                except Exception:
                    pass
            browser_view.reload()
            return
        self._set_browser_target(
            browser_view.property("browser_target") or self.current_browser_target,
            force_reload=True,
            browser_view=browser_view,
        )

    def _reload_all_browser_views(self, bypass_cache=True, clear_http_cache=False):
        if clear_http_cache:
            try:
                self.browser_profile.clearHttpCache()
            except Exception:
                pass
        for browser_view in self._browser_views():
            self._reload_webview(browser_view, bypass_cache=bypass_cache)

    def _reset_browser_profile(self, clear_storage=False):
        old_profile = getattr(self, "browser_profile", None)
        tab_states = [
            {
                "view": browser_view,
                "target": browser_view.property("browser_target"),
                "url": browser_view.url().toString(),
                "title": browser_view.title(),
            }
            for browser_view in self._browser_views()
        ]

        self.browser_profile = self._create_browser_profile(clear_storage=clear_storage)

        for state in tab_states:
            browser_view = state["view"]
            old_page = browser_view.page()
            browser_view.setPage(self._create_browser_page(browser_view))
            if old_page is not None:
                old_page.deleteLater()
            self._update_browser_tab_label(browser_view, title=state["title"] or None)

        if old_profile is not None and old_profile is not self.browser_profile:
            old_profile.deleteLater()

        for state in tab_states:
            browser_view = state["view"]
            target = state["target"]
            url = state["url"]
            if target in {"nekro", "napcat"}:
                self._set_browser_target(target, force_reload=True, browser_view=browser_view)
            elif url:
                browser_view.setUrl(QUrl(url))

        self._refresh_browser_nav_buttons()

    def _refresh_browser_after_update(self):
        refresh_mode = self._pending_browser_refresh
        if not refresh_mode:
            return

        self._pending_browser_refresh = None
        if refresh_mode == "profile" or self._browser_profile_channel != self._release_channel():
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
        self._update_browser_tab_label(browser_view, title=target_name, url=QUrl(target_url))
        if browser_view is self._current_webview():
            self._sync_browser_url_label(QUrl(target_url))

        if getattr(self.backend, "is_running", False):
            current_url = browser_view.url().toString()
            if force_reload and current_url == target_url:
                self._reload_webview(browser_view, bypass_cache=True)
            elif force_reload or current_url != target_url:
                browser_view.setUrl(QUrl(target_url))
            else:
                self._reload_webview(browser_view, bypass_cache=False)
        else:
            placeholder = (
                f"{target_name} 服务尚未启动。<br><br>"
                "先在“总览控制台”完成部署，然后回到这里点击“刷新内嵌页面”。"
            )
            browser_view.setHtml(f"<html><body style='font-family:Segoe UI;padding:24px;color:#243649;'>{placeholder}</body></html>")

        self._refresh_browser_nav_buttons()

    def _reload_browser_view(self):
        current_view = self._current_webview()
        if current_view is None:
            return
        self._reload_webview(current_view, bypass_cache=True)

    def _browser_go_back(self):
        current_view = self._current_webview()
        if current_view:
            current_view.back()

    def _browser_go_forward(self):
        current_view = self._current_webview()
        if current_view:
            current_view.forward()

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

    return {{
        filledUser,
        filledPass,
        inputCount: inputs.length,
        target: payload.target,
    }};
}})();
"""

        def _handle_fill_result(result):
            if not isinstance(result, dict):
                self._show_notice_dialog("提示", "当前页面暂时无法自动填充登录凭据。")
                return
            if result.get("filledUser") or result.get("filledPass"):
                return
            self._show_notice_dialog("提示", "当前页面未发现可填充的登录表单。")

        current_view.page().runJavaScript(script, _handle_fill_result)

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
            self._sync_browser_url_label(current_view.url())
        self._refresh_browser_nav_buttons()

    def _handle_browser_new_window(self, source_view, request):
        new_view = self._create_browser_tab(switch_to=True, title="新标签页")
        new_view.setProperty("browser_target", source_view.property("browser_target"))
        request.openIn(new_view.page())

    def _open_browser_devtools(self):
        devtools_window = getattr(self, "_devtools_window", None)
        devtools_view = getattr(self, "_devtools_view", None)
        current_view = self._current_webview()

        if current_view is None:
            return

        if devtools_window is None or devtools_view is None:
            devtools_window = QMainWindow()
            devtools_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            devtools_window.setWindowTitle("WebView DevTools")
            devtools_window.resize(1100, 720)

            devtools_view = QWebEngineView(devtools_window)
            devtools_window.setCentralWidget(devtools_view)

            def _clear_devtools_refs(*_args):
                self._devtools_window = None
                self._devtools_view = None

            devtools_window.destroyed.connect(_clear_devtools_refs)
            self._devtools_window = devtools_window
            self._devtools_view = devtools_view

        self._devtools_view.page().setInspectedPage(current_view.page())
        self._devtools_window.showNormal()
        self._devtools_window.raise_()
        self._devtools_window.activateWindow()

    def refresh_dashboard(self):
        if not hasattr(self, "status_badge"):
            return

        mode_text = self._format_mode_text(self.config.get("deploy_mode"))

        self.metric_mode.findChild(QLabel, "MetricValue").setText(mode_text)
        self._refresh_metric_data_dir_card()

        if hasattr(self, "mode_display"):
            self.mode_display.setText(mode_text)
        if hasattr(self, "wsldir_edit"):
            self.wsldir_edit.setText(self.config.get("wsl_install_dir") or "未配置")
        self._refresh_port_settings_ui()
        self._schedule_next_image_update_check()

    def _refresh_metric_data_dir_card(self):
        if not hasattr(self, "metric_data_dir"):
            return

        data_dir = "/root/nekro_agent_data"
        host_data = self.backend.get_host_access_path(data_dir)
        value_label = self.metric_data_dir.findChild(QLabel, "MetricValue")
        hint_label = self.metric_data_dir.findChild(QLabel, "MetricHint")

        if value_label is not None:
            value_label.setText(host_data or "当前后端暂未提供 Windows 映射路径")
        if hint_label is not None:
            hint_label.setText("点击打开 Windows 侧文件夹" if host_data else f"容器内路径: {data_dir}")

        self.metric_data_dir.setToolTip(host_data or data_dir)
        self.metric_data_dir.setCursor(
            Qt.CursorShape.PointingHandCursor if host_data else Qt.CursorShape.ArrowCursor
        )
        self.metric_data_dir.mousePressEvent = (
            self._open_dashboard_datadir_card if host_data else self._ignore_metric_click
        )

    def _ignore_metric_click(self, event):
        event.ignore()

    def _open_dashboard_datadir_card(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._open_datadir_in_explorer()
            event.accept()
            return
        event.ignore()

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
        if hasattr(self, "btn_primary_preview"):
            self.btn_primary_preview.setEnabled(self._advanced_features_enabled() and bool(self.config.get("deploy_mode")) and not updating)
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
            if self._current_webview() is not None and not was_running:
                self.switch_tab(1)
                self._set_browser_target(self.current_browser_target, force_reload=True)
            self._clear_pull_progress()
            self.btn_log_nekro.setVisible(True)
            self.btn_log_napcat.setVisible(self.config.get("deploy_mode") == "napcat")
        else:
            if hasattr(self, "_auto_image_check_timer"):
                self._auto_image_check_timer.stop()
            self.btn_log_nekro.setVisible(False)
            self.btn_log_napcat.setVisible(False)
            self.btn_primary_deploy.setText("开始部署")
            self.btn_primary_update.setText("升级 Nekro Agent" if not updating else "升级中...")
            if self._active_update_kind in {"remote", "preview", "restore"} and status in {"启动失败", "更新失败", "启动超时", "已停止"}:
                action_text = "恢复正式版后服务" if self._active_update_kind == "restore" else "升级后服务"
                failure_message = (
                    f"{self._pending_remote_update_message}\n\n最终状态：{status}"
                    if self._pending_remote_update_message
                    else f"{action_text}未能恢复：{status}"
                )
                _success_title, failure_title = self._active_update_result_titles()
                self._finish_update_session(False, failure_title, failure_message)
            if self._quit_after_stop and status in {"已停止", "已卸载"}:
                self._quit_after_stop = False
                QApplication.quit()
            if self._current_webview() is not None and was_running:
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
        self.btn_update_action.setEnabled(bool(self.config.get("deploy_mode")))
        self.btn_primary_update.setEnabled(bool(self.config.get("deploy_mode")))
        if hasattr(self, "btn_primary_preview"):
            self.btn_primary_preview.setText(self._preview_button_label())
            self.btn_primary_preview.setEnabled(self._advanced_features_enabled() and bool(self.config.get("deploy_mode")))
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

        advanced_row = QHBoxLayout()
        advanced_row.setSpacing(10)
        self.advanced_status_badge = QLabel("高级功能已启用")
        self.advanced_status_badge.setObjectName("FeatureBadge")
        self.advanced_status_badge.setVisible(False)
        advanced_row.addWidget(self.advanced_status_badge, 0, Qt.AlignmentFlag.AlignLeft)

        self.advanced_status_hint = QLabel("预览版入口已开放，可直接切换至预览版。")
        self.advanced_status_hint.setObjectName("SectionDesc")
        self.advanced_status_hint.setWordWrap(True)
        self.advanced_status_hint.setVisible(False)
        advanced_row.addWidget(self.advanced_status_hint, 1)
        advanced_row.addStretch()
        hero_layout.addLayout(advanced_row)

        hero_actions = QHBoxLayout()
        self.btn_primary_deploy = QPushButton("开始部署")
        self.btn_primary_deploy.setObjectName("HeroPrimary")
        self.btn_primary_deploy.clicked.connect(self.start_deploy)
        self.btn_primary_update = QPushButton("升级 Nekro Agent")
        self.btn_primary_update.setObjectName("HeroSecondary")
        self.btn_primary_update.clicked.connect(self._update_services)
        self.btn_primary_preview = QPushButton("切换至预览版")
        self.btn_primary_preview.setObjectName("HeroSecondary")
        self.btn_primary_preview.clicked.connect(self._switch_to_preview_build)
        self.btn_primary_creds = QPushButton("查看部署凭据")
        self.btn_primary_creds.setObjectName("HeroSecondary")
        self.btn_primary_creds.clicked.connect(self._show_saved_credentials)

        hero_actions.addWidget(self.btn_primary_deploy)
        hero_actions.addWidget(self.btn_primary_update)
        hero_actions.addWidget(self.btn_primary_preview)
        hero_actions.addWidget(self.btn_primary_creds)
        hero_actions.addStretch()
        hero_layout.addLayout(hero_actions)
        self._refresh_advanced_feature_ui()

        layout.addWidget(hero)

        metrics = QGridLayout()
        metrics.setHorizontalSpacing(16)
        metrics.setVerticalSpacing(16)
        self.metric_status = MetricCard("服务状态", "未就绪", "等待部署或启动", "red")
        self.metric_mode = MetricCard("部署版本", self._format_mode_text(self.config.get("deploy_mode")), "运行向导可修改", "amber")
        self.metric_data_dir = MetricCard(
            "数据目录",
            self.backend.get_host_access_path("/root/nekro_agent_data")
            or "当前后端暂未提供 Windows 映射路径",
            "点击打开 Windows 侧文件夹",
            "green",
        )
        metrics.addWidget(self.metric_status, 0, 0)
        metrics.addWidget(self.metric_mode, 0, 1)
        metrics.addWidget(self.metric_data_dir, 0, 2)
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
        self.browser_back_btn = QPushButton("后退")
        self.browser_back_btn.setObjectName("SegmentBtn")
        self.browser_back_btn.clicked.connect(self._browser_go_back)
        toolbar.addWidget(self.browser_back_btn)

        self.browser_forward_btn = QPushButton("前进")
        self.browser_forward_btn.setObjectName("SegmentBtn")
        self.browser_forward_btn.clicked.connect(self._browser_go_forward)
        toolbar.addWidget(self.browser_forward_btn)

        self.browser_reload_btn = QPushButton("刷新")
        self.browser_reload_btn.setObjectName("SegmentBtn")
        self.browser_reload_btn.clicked.connect(self._reload_browser_view)
        toolbar.addWidget(self.browser_reload_btn)

        self.browser_url_label = QLineEdit()
        self.browser_url_label.setObjectName("BrowserAddressBar")
        self.browser_url_label.setReadOnly(True)
        self.browser_url_label.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.browser_url_label.setMinimumWidth(320)
        self.browser_url_label.setPlaceholderText("当前页面地址")
        toolbar.addWidget(self.browser_url_label, 1)

        self.browser_fill_credentials_btn = QPushButton("填充凭据")
        self.browser_fill_credentials_btn.setObjectName("SegmentBtn")
        self.browser_fill_credentials_btn.clicked.connect(self._fill_browser_credentials)
        self.browser_fill_credentials_btn.setToolTip("将已保存的登录凭据填入当前页面")
        toolbar.addWidget(self.browser_fill_credentials_btn)

        self.browser_open_external_btn = QPushButton("在系统浏览器打开")
        self.browser_open_external_btn.setObjectName("SegmentBtn")
        self.browser_open_external_btn.clicked.connect(self._open_current_in_browser)
        toolbar.addWidget(self.browser_open_external_btn)

        self.browser_devtools_btn = QPushButton("开发者工具")
        self.browser_devtools_btn.setObjectName("SegmentBtn")
        self.browser_devtools_btn.clicked.connect(self._open_browser_devtools)
        self.browser_devtools_btn.setVisible(self._advanced_features_enabled())
        toolbar.addWidget(self.browser_devtools_btn)
        card_layout.addLayout(toolbar)

        self.browser_tabs = QTabWidget()
        self.browser_tabs.setObjectName("BrowserTabs")
        self.browser_tabs.setTabBar(BrowserTabBar(self.browser_tabs))
        self.browser_tabs.setDocumentMode(False)
        self.browser_tabs.setMovable(True)
        self.browser_tabs.setTabsClosable(False)
        self.browser_tabs.setUsesScrollButtons(True)
        self.browser_tabs.tabBar().tabCloseRequested.connect(self._close_browser_tab)
        self.browser_tabs.currentChanged.connect(self._on_browser_tab_changed)
        card_layout.addWidget(self.browser_tabs, 1)

        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(card, 1)
        self._create_browser_tab(switch_to=True, title=self._target_label("nekro"))
        self._set_browser_target("nekro")
        self._refresh_browser_nav_buttons()
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

        self._rebuild_image_rows()

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
        if not hasattr(self, "_image_rows_layout"):
            return

        self._clear_layout(self._image_rows_layout)
        self._image_row_widgets = {}

        deploy_mode = self.config.get("deploy_mode") or "lite"
        for image_ref, name, desc, modes in self._managed_images():
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
                "local": local_lbl,
                "remote": remote_lbl,
                "status": status_lbl,
                "btn": btn_single,
            }
        self._apply_cached_image_status_to_rows()

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

        image_check_row = QHBoxLayout()
        image_check_row.setSpacing(12)
        image_check_label = QLabel("镜像更新检查")
        image_check_row.addWidget(image_check_label)

        self.image_update_interval_combo = StyledComboBox()
        for hours, label in self._image_update_check_interval_options():
            self.image_update_interval_combo.addItem(label, hours)
        current_hours = self._image_update_check_interval_hours()
        current_index = self.image_update_interval_combo.findData(current_hours)
        if current_index < 0:
            current_index = self.image_update_interval_combo.findData(24)
        self.image_update_interval_combo.setCurrentIndex(max(0, current_index))
        self.image_update_interval_combo.currentIndexChanged.connect(lambda _index: self._on_image_update_interval_changed())
        image_check_row.addWidget(self.image_update_interval_combo, 0, Qt.AlignmentFlag.AlignLeft)
        image_check_row.addStretch()
        card_layout.addLayout(image_check_row)

        self.image_update_check_hint = QLabel()
        self.image_update_check_hint.setObjectName("SectionDesc")
        self.image_update_check_hint.setWordWrap(True)
        card_layout.addWidget(self.image_update_check_hint)
        self._refresh_image_update_check_hint()

        advanced_row = QHBoxLayout()
        advanced_row.setSpacing(12)
        advanced_label = QLabel("高级功能")
        advanced_row.addWidget(advanced_label)

        self.btn_enable_advanced = QPushButton()
        self.btn_enable_advanced.setObjectName("SegmentBtn")
        self.btn_enable_advanced.setCheckable(True)
        self.btn_enable_advanced.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_enable_advanced.setFixedWidth(136)
        self.btn_enable_advanced.clicked.connect(self._toggle_advanced_features)
        advanced_row.addWidget(self.btn_enable_advanced, 0, Qt.AlignmentFlag.AlignLeft)
        advanced_row.addStretch()
        card_layout.addLayout(advanced_row)

        self.advanced_hint = QLabel()
        self.advanced_hint.setObjectName("SectionDesc")
        self.advanced_hint.setWordWrap(True)
        card_layout.addWidget(self.advanced_hint)

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
        btn_open_datadir.setObjectName("HeroSecondary")
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
        self.nekro_port_label = QLabel("Nekro Agent 端口")
        card_layout.addWidget(self.nekro_port_label)
        self.nekro_port_setting = QLineEdit(str(self.config.get("nekro_port") or 8021))
        self.nekro_port_setting.setPlaceholderText("8021")
        card_layout.addWidget(self.nekro_port_setting)

        self.napcat_port_label = QLabel("NapCat 端口")
        card_layout.addWidget(self.napcat_port_label)
        self.napcat_port_setting = QLineEdit(str(self.config.get("napcat_port") or 6099))
        self.napcat_port_setting.setPlaceholderText("6099")
        card_layout.addWidget(self.napcat_port_setting)

        btn_save_ports = QPushButton("保存端口设置")
        btn_save_ports.setObjectName("HeroSecondary")
        btn_save_ports.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save_ports.clicked.connect(self._save_ports)
        card_layout.addWidget(btn_save_ports, 0, Qt.AlignmentFlag.AlignLeft)

        self.port_hint_label = QLabel()
        self.port_hint_label.setObjectName("SectionDesc")
        self.port_hint_label.setWordWrap(True)
        card_layout.addWidget(self.port_hint_label)

        layout.addWidget(card)
        layout.addStretch()
        self._add_page(page)
        self._refresh_port_settings_ui()
        self._refresh_advanced_feature_ui()

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
            self.browser_urls["nekro"] = f"http://localhost:{nekro_port}"
            self.browser_urls["napcat"] = f"http://localhost:{napcat_port}"
            # 同步更新已保存的 deploy_info 里的端口，避免凭据弹窗显示旧端口
            deploy_info = self.config.get("deploy_info")
            if deploy_info:
                deploy_info["port"] = str(nekro_port)
                if deploy_mode == "napcat":
                    deploy_info["napcat_port"] = str(napcat_port)
                self.config.set("deploy_info", deploy_info)
            current_view = self._current_webview()
            if getattr(self.backend, "is_running", False):
                if hasattr(self, "browser_url_label"):
                    self._sync_browser_url_label()
                self._show_notice_dialog("保存成功", "端口设置已保存，重新部署服务后生效。当前运行中的服务仍使用旧端口。")
            else:
                target_url = self._target_url(self.current_browser_target)
                if hasattr(self, "browser_url_label"):
                    self._sync_browser_url_label(QUrl(target_url))
                if current_view is not None:
                    current_view.setUrl(QUrl(target_url))
                self._show_notice_dialog("保存成功", "端口设置已保存，重新部署服务后生效。")
        except ValueError:
            self._show_notice_dialog("提示", "请输入有效的端口号（1-65535）。")

    def _refresh_datadir_hint(self):
        sample_path = self.backend.get_host_access_path("/root/nekro_agent_data")
        if sample_path:
            self.datadir_hint.setText(f"宿主机可访问路径: {sample_path}")
        else:
            self.datadir_hint.setText(f"当前后端 {self.backend.display_name} 暂未提供宿主机侧直接打开路径。")

    def _open_datadir_in_explorer(self):
        win_path = self.backend.get_host_access_path("/root/nekro_agent_data")
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
            ("DATA", "数据目录", "存储数据库、配置、日志等运行数据", "/root/nekro_agent_data"),
            ("CONF", "部署目录", "存储 docker-compose 和 .env 配置文件", "/root/nekro_agent"),
        ]
        for badge, title, hint, wsl_path in dirs_info:
            button = ActionButton(badge, title, hint)
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
