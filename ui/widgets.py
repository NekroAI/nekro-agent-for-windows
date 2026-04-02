from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QListView, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ui.styles import STYLESHEET


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
        self._spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._spinner_index = 0

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

        self.spinner_label = QLabel("⠋")
        self.spinner_label.setStyleSheet("font-size: 16px; color: #58a6ff;")
        self.spinner_label.setFixedWidth(20)
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

        self._spinner_timer = QTimer(self)
        self._spinner_timer.timeout.connect(self._tick_spinner)

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

    def _tick_spinner(self):
        self._spinner_index = (self._spinner_index + 1) % len(self._spinner_frames)
        self.spinner_label.setText(self._spinner_frames[self._spinner_index])

    def begin(self, status_text="正在准备更新..."):
        self._running = True
        self._completed = False
        self.spinner_label.setVisible(True)
        self.status_label.setVisible(True)
        self.status_label.setText(status_text)
        self.detail_label.clear()
        self.detail_label.setVisible(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.cancel_button.setVisible(False)
        self.action_button.setText("处理中...")
        self.action_button.setEnabled(False)
        self._spinner_timer.start(100)
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
        self._spinner_timer.stop()
        self.spinner_label.setVisible(True)
        self.spinner_label.setText("✓" if success else "✗")
        self.spinner_label.setStyleSheet(
            "font-size: 16px; color: #3fb950;" if success else "font-size: 16px; color: #f26f82;"
        )
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
    def __init__(self, label, value, hint="", accent="blue", parent=None):
        super().__init__(parent)
        self.setProperty("accent", accent)
        self.setObjectName("MetricCard")

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
