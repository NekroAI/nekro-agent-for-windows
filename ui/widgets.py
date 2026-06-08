import re
from collections import OrderedDict

from PyQt6.QtCore import QPoint, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from ui.styles import STYLESHEET

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class SpinnerLabel(QLabel):
    """Reusable braille-dot spinner with start/stop/finish states."""

    def __init__(self, parent=None, color="#58a6ff", size=16):
        super().__init__(parent)
        self._frames = SPINNER_FRAMES
        self._index = 0
        self.setFixedWidth(20)
        self.setStyleSheet(f"font-size: {size}px; color: {color};")
        self.setText(self._frames[0])
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def _tick(self):
        self._index = (self._index + 1) % len(self._frames)
        self.setText(self._frames[self._index])

    def start(self, interval_ms=100):
        self._index = 0
        self.setText(self._frames[0])
        self.setVisible(True)
        self._timer.start(interval_ms)

    def stop(self):
        self._timer.stop()

    def set_finished(self, success):
        self._timer.stop()
        self.setText("✓" if success else "✗")
        self.setStyleSheet(
            "font-size: 16px; color: #3fb950;" if success else "font-size: 16px; color: #f26f82;"
        )

    @property
    def running(self):
        return self._timer.isActive()


def make_button(
    text,
    *,
    object_name=None,
    role=None,
    checkable=False,
    cursor=True,
    fixed_height=None,
    fixed_width=None,
    minimum_width=None,
):
    button = QPushButton(text)
    if object_name:
        button.setObjectName(object_name)
    if role:
        button.setProperty("role", role)
    if checkable:
        button.setCheckable(True)
    if cursor:
        button.setCursor(Qt.CursorShape.PointingHandCursor)
    if fixed_height is not None:
        button.setFixedHeight(fixed_height)
    if fixed_width is not None:
        button.setFixedWidth(fixed_width)
    if minimum_width is not None:
        button.setMinimumWidth(minimum_width)
    return button


def make_secondary_button(text, **kwargs):
    return make_button(text, object_name="HeroSecondary", **kwargs)


def make_segment_button(text, *, checkable=False, **kwargs):
    return make_button(text, object_name="SegmentBtn", checkable=checkable, **kwargs)


WIZARD_BUTTON_VARIANTS = {
    "primary": "WizardPrimary",
    "secondary": "WizardSecondary",
    "accent": "WizardAccent",
}


def set_wizard_button_variant(button, variant="primary", *, repolish=True):
    button.setObjectName(WIZARD_BUTTON_VARIANTS.get(variant, WIZARD_BUTTON_VARIANTS["primary"]))
    if repolish:
        button.style().unpolish(button)
        button.style().polish(button)
    return button


def make_wizard_button(text, variant="primary", **kwargs):
    button = make_button(text, **kwargs)
    return set_wizard_button_variant(button, variant, repolish=False)


class DialogShell(QDialog):
    """Shared title/body/button shell for small modal dialogs."""

    def __init__(
        self,
        parent,
        title,
        text="",
        *,
        minimum_width=360,
        maximum_width=460,
        rich_text=False,
        modal=True,
        spacing=12,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(minimum_width)
        self.setMaximumWidth(maximum_width)
        if modal:
            self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setStyleSheet(STYLESHEET)

        self.content_layout = QVBoxLayout(self)
        self.content_layout.setContentsMargins(20, 18, 20, 18)
        self.content_layout.setSpacing(spacing)

        title_label = QLabel(title)
        title_label.setProperty("role", "dialog_title")
        title_label.setWordWrap(True)
        title_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.content_layout.addWidget(title_label)

        self.desc_label = QLabel(text)
        self.desc_label.setProperty("role", "dialog_desc")
        self.desc_label.setWordWrap(True)
        self.desc_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        if rich_text:
            self.desc_label.setOpenExternalLinks(True)
            self.desc_label.setTextFormat(Qt.TextFormat.RichText)
            self.desc_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        else:
            self.desc_label.setTextFormat(Qt.TextFormat.PlainText)
        if text:
            self.content_layout.addWidget(self.desc_label)

    def add_button_row(self, *buttons):
        button_row = QHBoxLayout()
        button_row.setSpacing(10)
        button_row.addStretch()
        for button in buttons:
            button_row.addWidget(button)
        self.content_layout.addLayout(button_row)
        return button_row


def show_notice_dialog(parent, title, text, button_text="确定", danger=False):
    dialog = DialogShell(parent, title, text, minimum_width=340, maximum_width=440)
    btn = make_button(button_text, cursor=True)
    if danger:
        btn.setProperty("role", "danger")
    btn.clicked.connect(dialog.accept)
    dialog.add_button_row(btn)
    dialog.adjustSize()
    dialog.exec()


def show_confirm_dialog(
    parent,
    title,
    text,
    *,
    confirm_text="确认",
    cancel_text="取消",
    danger=False,
    rich_text=False,
    minimum_width=360,
    maximum_width=460,
):
    dialog = DialogShell(
        parent,
        title,
        text,
        minimum_width=minimum_width,
        maximum_width=maximum_width,
        rich_text=rich_text,
    )
    cancel_button = make_button(cancel_text)
    cancel_button.clicked.connect(dialog.reject)
    confirm_button = make_button(confirm_text, role="danger" if danger else "primary")
    confirm_button.clicked.connect(dialog.accept)
    dialog.add_button_row(cancel_button, confirm_button)
    dialog.adjustSize()
    return dialog.exec() == int(QDialog.DialogCode.Accepted)


def show_choice_dialog(parent, title, text, choices, *, minimum_width=360, maximum_width=460):
    dialog = DialogShell(parent, title, text, minimum_width=minimum_width, maximum_width=maximum_width)
    buttons = []
    selected = {"value": None}
    for label, value, role in choices:
        button = make_button(label, role=role or None)
        button.clicked.connect(lambda _checked=False, v=value: (selected.__setitem__("value", v), dialog.accept()))
        buttons.append(button)
    dialog.add_button_row(*buttons)
    dialog.adjustSize()
    if dialog.exec() == int(QDialog.DialogCode.Accepted):
        return selected["value"]
    return None


def show_combo_choice_dialog(
    parent,
    title,
    text,
    items,
    *,
    confirm_text="确认",
    cancel_text="取消",
    danger=False,
    active_data=None,
    minimum_width=400,
    maximum_width=500,
    combo_minimum_width=360,
):
    dialog = DialogShell(parent, title, text, minimum_width=minimum_width, maximum_width=maximum_width, spacing=14)
    combo = StyledComboBox()
    combo.setMinimumWidth(combo_minimum_width)
    current_index = 0
    for index, (label, data) in enumerate(items):
        combo.addItem(label, data)
        if active_data is not None and data == active_data:
            current_index = index
    combo.setCurrentIndex(current_index)
    dialog.content_layout.addWidget(combo)

    cancel_button = make_button(cancel_text, cursor=True)
    cancel_button.clicked.connect(dialog.reject)
    confirm_button = make_button(confirm_text, role="danger" if danger else "primary", fixed_height=36)
    confirm_button.clicked.connect(dialog.accept)
    dialog.add_button_row(cancel_button, confirm_button)
    if dialog.exec() == int(QDialog.DialogCode.Accepted):
        return combo.currentData()
    return None


def create_install_progress_bar(minimum=0, maximum=0, height=8, radius=4):
    bar = QProgressBar()
    bar.setRange(minimum, maximum)
    bar.setFixedHeight(height)
    bar.setTextVisible(False)
    bar.setStyleSheet(
        f"QProgressBar {{ border: none; background: #e8e9eb; border-radius: {radius}px; }}"
        f"QProgressBar::chunk {{ background: #0969da; border-radius: {radius}px; }}"
    )
    return bar


class CreateRuntimeThread(QThread):
    result_ready = pyqtSignal(bool)
    error_ready = pyqtSignal(str)

    def __init__(self, backend, install_dir):
        super().__init__()
        self.backend = backend
        self.install_dir = install_dir

    def run(self):
        try:
            ok = self.backend.create_runtime(self.install_dir)
        except Exception as e:
            self.error_ready.emit(str(e))
            ok = False
        self.result_ready.emit(ok)


class WizardDialogBase(QDialog):
    """Base class for stacked wizard dialogs with tracked background threads."""

    def __init__(
        self,
        title,
        steps,
        *,
        parent=None,
        size=(680, 600),
        minimum_size=(620, 520),
        margins=(30, 24, 30, 30),
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(*size)
        self.setMinimumSize(*minimum_size)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*margins)
        layout.setSpacing(0)

        self._step_indicator = StepIndicator(steps, current=0)
        layout.addWidget(self._step_indicator)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        self._page_index = {}
        self._active_threads: list[QThread] = []

    def _track_thread(self, thread: QThread):
        self._active_threads.append(thread)
        thread.finished.connect(
            lambda _=None, t=thread: self._active_threads.remove(t)
            if t in self._active_threads
            else None
        )

    def _disconnect_dialog_signals(self):
        pass

    def reject(self):
        for thread in list(self._active_threads):
            if thread.isRunning():
                thread.quit()
                thread.wait(3000)
        self._active_threads.clear()
        self._disconnect_dialog_signals()
        super().reject()

    def _add_page(self, page: QWidget, name: str):
        idx = self.stack.addWidget(page)
        self._page_index[name] = idx

    def _page_step(self, name: str) -> int:
        return 0

    def _show_step_indicator_for_page(self, name: str) -> bool:
        return True

    def _goto_page(self, name: str):
        self.stack.setCurrentIndex(self._page_index[name])
        self._step_indicator.set_step(self._page_step(name))
        self._step_indicator.setVisible(self._show_step_indicator_for_page(name))

    def _current_page_name(self) -> str:
        idx = self.stack.currentIndex()
        for name, i in self._page_index.items():
            if i == idx:
                return name
        return ""


class ScanProgressDialog(QDialog):
    """扫描本地实例时的进度对话框，避免长时间无响应的视觉假象。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("正在检测本地实例")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setFixedWidth(420)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        title = QLabel("正在扫描本地 Nekro Agent 部署")
        title.setStyleSheet("font-size: 15px; font-weight: 600; color: #24384a;")
        layout.addWidget(title)

        desc = QLabel("正在遍历本机所有 WSL 发行版，首次检测可能需要十几秒，请稍候。")
        desc.setWordWrap(True)
        desc.setStyleSheet("font-size: 12px; color: #6e8396;")
        layout.addWidget(desc)

        self._step_label = QLabel("正在准备扫描...")
        self._step_label.setWordWrap(True)
        self._step_label.setStyleSheet("font-size: 12px; color: #24384a;")
        layout.addWidget(self._step_label)

        self._progress = create_install_progress_bar(0, 0, height=8, radius=4)
        layout.addWidget(self._progress)

    def update_step(self, text: str):
        if text:
            self._step_label.setText(text)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            event.ignore()
            return
        super().keyPressEvent(event)


class PullProgressView(QFrame):
    def __init__(self, parent=None, dark=False):
        super().__init__(parent)
        self._stage_header = ""
        self._summary_text = ""
        self._layers = OrderedDict()
        self._layer_order = []
        self._image_index = 0
        self._image_total = 0
        self._failed = False

        self.setObjectName("SectionCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        self.status_label = QLabel("")
        self.status_label.setObjectName("SectionDesc")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("SectionDesc")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.bar_row = QHBoxLayout()
        self.bar_row.setSpacing(10)
        self.spinner_label = SpinnerLabel(self)
        self.bar_row.addWidget(self.spinner_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setTextVisible(False)
        if dark:
            self.progress_bar.setStyleSheet(
                "QProgressBar { border: none; background: #1e3a52; border-radius: 4px; }"
                "QProgressBar::chunk { background: #58a6ff; border-radius: 4px; }"
            )
        else:
            self.progress_bar.setStyleSheet(
                "QProgressBar { border: none; background: #e8e9eb; border-radius: 4px; }"
                "QProgressBar::chunk { background: #0969da; border-radius: 4px; }"
            )
        self.bar_row.addWidget(self.progress_bar)
        layout.addLayout(self.bar_row)
        self._set_bar_visible(False)
        self.setVisible(False)

    @property
    def stage_header(self):
        return self._stage_header

    @property
    def summary_text(self):
        return self._summary_text

    @property
    def has_layers(self):
        return bool(self._layer_order)

    @property
    def value(self):
        return self.progress_bar.value()

    def _set_bar_visible(self, visible):
        self.spinner_label.setVisible(visible)
        self.progress_bar.setVisible(visible)

    def set_active(self, active, show_bar=None):
        self.setVisible(active)
        if show_bar is not None:
            self._set_bar_visible(show_bar)
        if active and self.progress_bar.isVisible():
            self.spinner_label.start(80)
        else:
            self.spinner_label.stop()

    def reset(self):
        self._stage_header = ""
        self._summary_text = ""
        self._layers.clear()
        self._layer_order.clear()
        self._image_index = 0
        self._image_total = 0
        self._failed = False
        self.status_label.setStyleSheet("")
        self.summary_label.setStyleSheet("")
        self.status_label.setText("")
        self.summary_label.setText("")
        self.summary_label.setVisible(False)
        self.progress_bar.setValue(0)
        self.set_active(False)

    def start(self, header):
        self.reset()
        self.update(header=header, show_bar=False)

    def begin_stage(self, header, current=0, total=0):
        self._failed = False
        self.status_label.setStyleSheet("")
        self.summary_label.setStyleSheet("")
        self._stage_header = header
        self._summary_text = ""
        self._layers.clear()
        self._layer_order.clear()
        self._image_index = current
        self._image_total = total
        self.progress_bar.setValue(0)
        self.update(header=header, show_bar=False)

    def finish(self, header):
        self._failed = False
        self.status_label.setStyleSheet("")
        self.summary_label.setStyleSheet("")
        self.progress_bar.setValue(100)
        self.update(header=header, show_bar=bool(self._layer_order))

    def fail(self, header):
        self._failed = True
        self.status_label.setStyleSheet("color: #f26f82;")
        self.summary_label.setStyleSheet("")
        self._summary_text = ""
        self.update(header=header, show_bar=False)

    def update(self, header="", detail="", show_bar=None):
        if header:
            self._stage_header = header
        parsed_layer = False
        if detail:
            parsed_layer = self._update_layer(detail)
        if parsed_layer:
            show_bar = True
        self._refresh_status_label()
        self.set_active(True, show_bar=show_bar)

    def _update_layer(self, detail):
        layer_match = re.match(r"^([a-f0-9]{6,64}):\s*(.+)$", detail, re.IGNORECASE)
        if not layer_match:
            return False
        layer_id, status = layer_match.groups()
        short_id = layer_id[:12]
        if short_id not in self._layers:
            self._layer_order.append(short_id)
        self._layers[short_id] = status
        total = len(self._layer_order)
        done = sum(
            1
            for lid in self._layer_order
            if self._layers.get(lid, "").startswith(("Pull complete", "Already exists", "Download complete"))
        )
        if total > 0:
            self.progress_bar.setValue(int(done * 100 / total))
        self._summary_text = self._summarize_layers()
        return True

    def _summarize_layers(self):
        total = len(self._layer_order)
        if total <= 0:
            return ""

        done = 0
        downloading = 0
        extracting = 0
        verifying = 0
        waiting = 0
        for layer_id in self._layer_order:
            status = self._layers.get(layer_id, "")
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

    def _refresh_status_label(self):
        self.status_label.setText(self._stage_header)
        summary = self._summary_text
        if not summary and self._image_index and self._image_total:
            summary = f"正在拉取第 {self._image_index}/{self._image_total} 个镜像，等待 Docker 返回下载进度"
        self.summary_label.setText(summary)
        self.summary_label.setVisible(bool(summary))


class UpdateProgressDialog(QDialog):
    confirmed = pyqtSignal()

    def __init__(self, parent, title, text, confirm_text="开始更新"):
        super().__init__(parent)
        self._running = False
        self._completed = False

        self.setWindowTitle(title)
        self.setMinimumWidth(400)
        self.setMaximumWidth(520)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title_label = QLabel(title)
        title_label.setProperty("role", "dialog_title")
        title_label.setWordWrap(True)
        layout.addWidget(title_label)

        self.desc_label = QLabel(text)
        self.desc_label.setProperty("role", "dialog_desc")
        self.desc_label.setTextFormat(Qt.TextFormat.PlainText)
        self.desc_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.desc_label.setWordWrap(True)
        layout.addWidget(self.desc_label)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)

        self.spinner_label = SpinnerLabel(self)
        self.spinner_label.setVisible(False)
        progress_row.addWidget(self.spinner_label, 0, Qt.AlignmentFlag.AlignTop)

        progress_body = QVBoxLayout()
        progress_body.setSpacing(8)

        self.status_label = QLabel("")
        self.status_label.setProperty("role", "dialog_desc")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        progress_body.addWidget(self.status_label)

        self.detail_label = QLabel("")
        self.detail_label.setProperty("role", "dialog_desc")
        self.detail_label.setWordWrap(True)
        self.detail_label.setVisible(False)
        progress_body.addWidget(self.detail_label)

        self.progress_bar = create_install_progress_bar(0, 100, height=8, radius=4)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        progress_body.addWidget(self.progress_bar)

        progress_row.addLayout(progress_body, 1)
        layout.addLayout(progress_row)

        button_row = QHBoxLayout()
        button_row.setSpacing(10)
        button_row.addStretch()

        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        button_row.addWidget(self.cancel_button)

        self.action_button = QPushButton(confirm_text)
        self.action_button.setProperty("role", "primary")
        self.action_button.clicked.connect(self._handle_action)
        button_row.addWidget(self.action_button)

        layout.addLayout(button_row)

    def _schedule_resize(self):
        QTimer.singleShot(0, self._refresh_size)

    def _refresh_size(self):
        layout = self.layout()
        if layout is None:
            return
        layout.activate()
        self.adjustSize()

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_resize()

    def _handle_action(self):
        if self._completed:
            self.accept()
            return
        if self._running:
            return
        self.begin()
        self.confirmed.emit()

    def begin(self, status_text="正在准备更新..."):
        self._running = True
        self._completed = False
        self.spinner_label.start()
        self.status_label.setVisible(True)
        self.status_label.setText(status_text)
        self.detail_label.clear()
        self.detail_label.setVisible(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.cancel_button.setVisible(False)
        self.action_button.setText("处理中...")
        self.action_button.setEnabled(False)
        self._schedule_resize()

    def set_progress(self, status_text=None, detail_text=None, value=None, busy=None):
        self.spinner_label.setVisible(True)
        self.status_label.setVisible(True)
        self.progress_bar.setVisible(True)

        if status_text is not None:
            self.status_label.setText(status_text)
        if detail_text is not None:
            self.detail_label.setText(detail_text)
            self.detail_label.setVisible(bool(detail_text))
        if busy is True:
            self.progress_bar.setRange(0, 0)
        elif busy is False:
            if self.progress_bar.maximum() == 0:
                self.progress_bar.setRange(0, 100)
                if value is None:
                    value = 0
        if value is not None and self.progress_bar.maximum() != 0:
            self.progress_bar.setValue(max(0, min(100, int(value))))
        self._schedule_resize()

    def set_finished(self, success, status_text, detail_text=""):
        self._running = False
        self._completed = True
        self.spinner_label.set_finished(success)
        self.status_label.setVisible(True)
        self.status_label.setText(status_text)
        self.detail_label.setText(detail_text)
        self.detail_label.setVisible(bool(detail_text))
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100 if success else max(1, self.progress_bar.value()))
        self.cancel_button.setVisible(False)
        self.action_button.setEnabled(True)
        self.action_button.setText("完成")
        self._schedule_resize()

    def reject(self):
        if self._running:
            return
        super().reject()


class ActionButton(QPushButton):
    def __init__(self, badge, title, desc, variant="default", parent=None):
        super().__init__(parent)
        self.setProperty("variant", variant)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(112)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._base_min_height = 112

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)
        self._layout = layout

        self.badge_label = QLabel(badge)
        self.badge_label.setObjectName("ActionBadge")
        self.badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge_label.setFixedSize(42, 42)
        layout.addWidget(self.badge_label)

        text_container = QWidget()
        text_container.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        text_layout = QVBoxLayout(text_container)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(6)
        self._text_layout = text_layout

        self.title_label = QLabel(title)
        self.title_label.setObjectName("ActionTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.desc_label = QLabel(desc)
        self.desc_label.setObjectName("ActionDesc")
        self.desc_label.setWordWrap(True)
        self.desc_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.desc_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.desc_label)
        text_layout.addStretch()
        layout.addWidget(text_container, 1)

    def set_scale(self, scale):
        scale = max(0.78, min(scale, 1.0))

        badge_size = int(42 * scale)
        self.badge_label.setFixedSize(badge_size, badge_size)

        title_font = QFont(self.title_label.font())
        title_font.setPointSizeF(15 * scale)
        self.title_label.setFont(title_font)

        desc_font = QFont(self.desc_label.font())
        desc_font.setPointSizeF(12 * scale)
        self.desc_label.setFont(desc_font)

        badge_font = QFont(self.badge_label.font())
        badge_font.setPointSizeF(13 * scale)
        badge_font.setBold(True)
        self.badge_label.setFont(badge_font)

        self._layout.setContentsMargins(
            int(18 * scale),
            int(18 * scale),
            int(18 * scale),
            int(18 * scale),
        )
        self._layout.setSpacing(int(14 * scale))
        self._text_layout.setSpacing(max(2, int(4 * scale)))
        self.setMinimumHeight(int(self._base_min_height * scale))


class _DropdownPopup(QFrame):
    """浮动弹出菜单，用于 StyledComboBox 的选项列表。"""
    item_clicked = pyqtSignal(int)

    def __init__(self, parent_btn):
        super().__init__(None)
        self._parent_btn = parent_btn
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setObjectName("DropdownPopup")
        self.setStyleSheet("""
            #DropdownPopup {
                background: #ffffff;
                border: 1px solid #d7e2ec;
                border-radius: 8px;
            }
            #DropdownItem {
                padding: 8px 14px;
                font-size: 13px;
                color: #264057;
                border-radius: 6px;
                border: none;
                background: transparent;
                text-align: left;
            }
            #DropdownItem:hover {
                background: #fff0ed;
                color: #c46b62;
            }
            #DropdownItem[selected="true"] {
                background: #fff5f3;
                color: #bf655d;
                font-weight: 600;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(2)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        layout.addWidget(self._scroll)

        self._item_container = QWidget()
        self._item_container.setStyleSheet("background: transparent;")
        self._item_layout = QVBoxLayout(self._item_container)
        self._item_layout.setContentsMargins(0, 0, 0, 0)
        self._item_layout.setSpacing(2)
        self._scroll.setWidget(self._item_container)

        self._buttons: list[QPushButton] = []

    def rebuild(self, items: list[tuple[str, object]], selected_index: int):
        for btn in self._buttons:
            btn.deleteLater()
        self._buttons.clear()

        for i, (text, _data) in enumerate(items):
            btn = QPushButton(text)
            btn.setObjectName("DropdownItem")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("selected", "true" if i == selected_index else "false")
            btn.setFixedHeight(34)
            btn.setStyleSheet(btn.styleSheet())
            idx = i
            btn.clicked.connect(lambda _checked=False, ii=idx: self._on_click(ii))
            self._item_layout.addWidget(btn)
            self._buttons.append(btn)

        count = len(items)
        visible = min(count, 8)
        item_h = 34 + 2
        popup_h = visible * item_h + 12
        btn_w = self._parent_btn.width()
        self.setFixedWidth(max(btn_w, 160))
        self.setFixedHeight(min(popup_h, 320))

    def _on_click(self, index):
        self.item_clicked.emit(index)
        self.close()

    def show_below(self, ref_widget):
        pos = ref_widget.mapToGlobal(QPoint(0, ref_widget.height() + 4))
        self.move(pos)
        self.show()

    def focusOutEvent(self, event):
        self.close()
        super().focusOutEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.activateWindow()
        self.setFocus()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        super().keyPressEvent(event)


class StyledComboBox(QWidget):
    """自绘下拉框，不依赖 QComboBox，避免平台样式问题。"""
    currentIndexChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[tuple[str, object]] = []
        self._current = -1
        self._signals_blocked = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._btn = QPushButton("")
        self._btn.setObjectName("DropdownTrigger")
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.setMinimumHeight(38)
        self._btn.setStyleSheet("""
            QPushButton#DropdownTrigger {
                background: #ffffff;
                border: 1px solid #d7e2ec;
                border-radius: 8px;
                padding: 8px 32px 8px 12px;
                font-size: 13px;
                color: #264057;
                text-align: left;
            }
            QPushButton#DropdownTrigger:hover {
                border-color: #8fc5dd;
            }
        """)
        self._btn.clicked.connect(self._toggle_popup)
        layout.addWidget(self._btn)

        self._arrow = QLabel("▾")
        self._arrow.setFixedSize(28, 38)
        self._arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._arrow.setStyleSheet(
            "color: #7B90A3; font-size: 14px; background: transparent; border: none;"
        )
        self._arrow.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._arrow.setParent(self._btn)

        self._popup: _DropdownPopup | None = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrow.move(self._btn.width() - 28, 0)
        self._arrow.setFixedHeight(self._btn.height())

    def _toggle_popup(self):
        if self._popup and self._popup.isVisible():
            self._popup.close()
            return
        if not self._items:
            return
        self._popup = _DropdownPopup(self._btn)
        self._popup.item_clicked.connect(self._on_popup_item)
        self._popup.rebuild(self._items, self._current)
        self._popup.show_below(self._btn)

    def _on_popup_item(self, index):
        if index == self._current:
            return
        self._current = index
        self._btn.setText(self._items[index][0] if 0 <= index < len(self._items) else "")
        if not self._signals_blocked:
            self.currentIndexChanged.emit(index)

    def addItem(self, text: str, data=None):
        self._items.append((text, data))
        if self._current < 0:
            self._current = 0
            self._btn.setText(text)

    def clear(self):
        self._items.clear()
        self._current = -1
        self._btn.setText("")

    def setCurrentIndex(self, index):
        if 0 <= index < len(self._items):
            self._current = index
            self._btn.setText(self._items[index][0])

    def currentIndex(self):
        return self._current

    def itemData(self, index):
        if 0 <= index < len(self._items):
            return self._items[index][1]
        return None

    def findData(self, data):
        for i, (_, d) in enumerate(self._items):
            if d == data:
                return i
        return -1

    def count(self):
        return len(self._items)

    def setMinimumWidth(self, w):
        super().setMinimumWidth(w)
        self._btn.setMinimumWidth(w)

    def blockSignals(self, block):
        self._signals_blocked = block

    def currentText(self):
        if 0 <= self._current < len(self._items):
            return self._items[self._current][0]
        return ""

    def currentData(self):
        if 0 <= self._current < len(self._items):
            return self._items[self._current][1]
        return None


class MetricCard(QFrame):
    clicked = pyqtSignal()

    def __init__(self, label, value, hint="", accent="blue", clickable=False, parent=None):
        super().__init__(parent)
        self.setProperty("accent", accent)
        self.setObjectName("MetricCard")
        self._clickable = clickable

        if clickable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(8)

        label_widget = QLabel(label)
        label_widget.setObjectName("MetricLabel")
        value_widget = QLabel(value)
        value_widget.setObjectName("MetricValue")
        value_widget.setWordWrap(True)
        value_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        layout.addWidget(label_widget)
        layout.addWidget(value_widget)

        if hint:
            hint_widget = QLabel(hint)
            hint_widget.setObjectName("MetricHint")
            hint_widget.setWordWrap(True)
            hint_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            layout.addWidget(hint_widget)

    def set_clickable(self, clickable):
        self._clickable = clickable
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.ArrowCursor
        )

    def mousePressEvent(self, event):
        if self._clickable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class SectionCard(QFrame):
    def __init__(self, title, desc="", parent=None):
        super().__init__(parent)
        self.setObjectName("SectionCard")

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(22, 22, 22, 22)
        self._layout.setSpacing(16)

        header = QVBoxLayout()
        header.setSpacing(4)

        title_widget = QLabel(title)
        title_widget.setObjectName("SectionTitle")
        header.addWidget(title_widget)

        if desc:
            desc_widget = QLabel(desc)
            desc_widget.setObjectName("SectionDesc")
            desc_widget.setWordWrap(True)
            desc_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            header.addWidget(desc_widget)

        self._layout.addLayout(header)

    def body_layout(self):
        return self._layout


class StepIndicator(QWidget):
    """Horizontal step dots for wizard dialogs."""

    def __init__(self, steps, current=0, parent=None):
        super().__init__(parent)
        self._steps = steps
        self._current = current
        self._dots = []
        self._labels = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(0)
        layout.addStretch()

        for i, name in enumerate(steps):
            if i > 0:
                line = QFrame()
                line.setFixedSize(32, 2)
                line.setObjectName("StepLine")
                layout.addWidget(line, 0, Qt.AlignmentFlag.AlignVCenter)

            col = QVBoxLayout()
            col.setSpacing(4)
            col.setAlignment(Qt.AlignmentFlag.AlignCenter)

            dot = QLabel()
            dot.setFixedSize(10, 10)
            dot.setObjectName("StepDot")
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(dot, 0, Qt.AlignmentFlag.AlignCenter)

            label = QLabel(name)
            label.setObjectName("StepLabel")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(label)

            layout.addLayout(col)
            self._dots.append(dot)
            self._labels.append(label)

        layout.addStretch()
        self._refresh()

    def set_step(self, index):
        self._current = max(0, min(index, len(self._steps) - 1))
        self._refresh()

    def _refresh(self):
        for i, (dot, label) in enumerate(zip(self._dots, self._labels)):
            if i < self._current:
                dot.setProperty("state", "done")
                label.setProperty("state", "done")
            elif i == self._current:
                dot.setProperty("state", "active")
                label.setProperty("state", "active")
            else:
                dot.setProperty("state", "pending")
                label.setProperty("state", "pending")
            dot.style().unpolish(dot)
            dot.style().polish(dot)
            label.style().unpolish(label)
            label.style().polish(label)

        for child in self.findChildren(QFrame, "StepLine"):
            idx = self.layout().indexOf(child)
            step_idx = idx // 2
            if step_idx <= self._current:
                child.setStyleSheet("background: #e88478;")
            else:
                child.setStyleSheet("background: #dfe7ef;")
