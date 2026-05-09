from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QListView, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget

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


def show_notice_dialog(parent, title, text, button_text="确定", danger=False):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumWidth(340)
    dialog.setMaximumWidth(440)
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
    button_row.addStretch()
    btn = QPushButton(button_text)
    if danger:
        btn.setProperty("role", "danger")
    btn.clicked.connect(dialog.accept)
    button_row.addWidget(btn)
    layout.addLayout(button_row)
    dialog.adjustSize()
    dialog.exec()


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


class StyledComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FieldSelect")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(38)

        popup_view = QListView(self)
        popup_view.setObjectName("FieldSelectPopup")
        popup_view.setFrameShape(QFrame.Shape.NoFrame)
        self.setView(popup_view)


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
