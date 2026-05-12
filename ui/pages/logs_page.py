from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from ui.widgets import PullProgressView, SectionCard


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
        self.w.pull_progress_view = PullProgressView(self.w, dark=True)
        card_layout.addWidget(self.w.pull_progress_view)

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
