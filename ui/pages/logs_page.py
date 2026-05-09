from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from ui.widgets import SectionCard, SpinnerLabel


class LogsPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.w = window

        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 30, 34, 30)
        layout.setSpacing(18)

        card = SectionCard("日志中心", "按来源查看应用日志和容器日志，用于部署排查与运行观察。")
        card_layout = card.body_layout()

        self._build_pull_view(card_layout)
        self._build_log_tabs(card_layout)

        layout.addWidget(card)

    def _build_pull_view(self, card_layout):
        self.w.pull_view_frame = QFrame()
        self.w.pull_view_frame.setObjectName("SectionCard")
        pull_view_layout = QVBoxLayout(self.w.pull_view_frame)
        pull_view_layout.setContentsMargins(16, 14, 16, 14)
        pull_view_layout.setSpacing(8)

        self.w.pull_status_label = QLabel("")
        self.w.pull_status_label.setObjectName("SectionDesc")
        self.w.pull_status_label.setWordWrap(True)
        pull_view_layout.addWidget(self.w.pull_status_label)

        bar_row = QHBoxLayout()
        bar_row.setSpacing(10)

        self.w.pull_spinner_label = SpinnerLabel(self.w)
        bar_row.addWidget(self.w.pull_spinner_label)

        self.w.pull_overall_bar = QProgressBar()
        self.w.pull_overall_bar.setRange(0, 100)
        self.w.pull_overall_bar.setValue(0)
        self.w.pull_overall_bar.setFixedHeight(8)
        self.w.pull_overall_bar.setTextVisible(False)
        self.w.pull_overall_bar.setStyleSheet(
            "QProgressBar { border: none; background: #1e3a52; border-radius: 4px; }"
            "QProgressBar::chunk { background: #58a6ff; border-radius: 4px; }"
        )
        bar_row.addWidget(self.w.pull_overall_bar)
        pull_view_layout.addLayout(bar_row)

        self.w.pull_view_frame.setVisible(False)
        card_layout.addWidget(self.w.pull_view_frame)

    def _build_log_tabs(self, card_layout):
        top = QHBoxLayout()
        self.w.btn_log_app = QPushButton("应用日志")
        self.w.btn_log_nekro = QPushButton("Nekro Agent")
        self.w.btn_log_napcat = QPushButton("NapCat")

        for idx, button in enumerate([self.w.btn_log_app, self.w.btn_log_nekro, self.w.btn_log_napcat]):
            button.setObjectName("SegmentBtn")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda checked, current=idx: self.w._set_log_tab(current))
            top.addWidget(button)
        self.w.btn_log_nekro.setVisible(False)
        self.w.btn_log_napcat.setVisible(False)
        top.addStretch()
        card_layout.addLayout(top)

        self.w.log_viewer_app = QTextEdit()
        self.w.log_viewer_nekro = QTextEdit()
        self.w.log_viewer_napcat = QTextEdit()
        for viewer in [self.w.log_viewer_app, self.w.log_viewer_nekro, self.w.log_viewer_napcat]:
            viewer.setObjectName("LogViewer")
            viewer.setReadOnly(True)
            card_layout.addWidget(viewer)

        self.w._set_log_tab(0)
