from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel,
    QVBoxLayout, QWidget,
)

from ui.widgets import (
    PullProgressView,
    SectionCard,
    SPINNER_FRAMES,
    make_secondary_button,
)


class ImagesPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.w = window

        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 30, 34, 30)
        layout.setSpacing(22)

        card = SectionCard("镜像管理", "查看 Nekro Agent 相关镜像的本地与远程版本状态。")
        card_layout = card.layout()

        self.w.image_pull_progress_view = PullProgressView(self.w)
        card_layout.addWidget(self.w.image_pull_progress_view)

        header = QFrame()
        header.setObjectName("ImageTableHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 0, 12, 8)
        for text, stretch in [("镜像", 3), ("本地 Digest", 2), ("远程 Digest", 2), ("状态", 2), ("", 2)]:
            lbl = QLabel(text)
            lbl.setObjectName("MetricLabel")
            header_layout.addWidget(lbl, stretch)
        card_layout.addWidget(header)

        self.w._image_rows_layout = QVBoxLayout()
        self.w._image_rows_layout.setSpacing(6)
        card_layout.addLayout(self.w._image_rows_layout)
        self.w._image_row_widgets = {}

        self._rebuild_image_rows()

        btn_row = QHBoxLayout()
        self.w.btn_check_images = make_secondary_button("检查全部更新")
        self.w.btn_check_images.clicked.connect(self.w._check_images)
        self.w._img_spinner_frames = SPINNER_FRAMES
        self.w._img_spinner_idx = 0
        self.w._img_spinner_timer = QTimer(self.w)
        self.w._img_spinner_timer.timeout.connect(self.w._tick_img_spinner)
        self.w._img_checking_ref = None
        btn_row.addWidget(self.w.btn_check_images)
        btn_row.addStretch()
        card_layout.addLayout(btn_row)

        layout.addWidget(card)
        layout.addStretch()

    def _rebuild_image_rows(self):
        self.w._rebuild_image_rows_impl()


def rebuild_image_rows(window):
    """Build image rows with styled QFrame wrappers."""
    if not hasattr(window, "_image_rows_layout"):
        return

    window._clear_layout(window._image_rows_layout)
    window._image_row_widgets = {}

    deploy_mode = window.config.get("deploy_mode") or "lite"
    for image_ref, name, desc, modes in window._managed_images():
        if deploy_mode not in modes:
            continue

        row_frame = QFrame()
        row_frame.setObjectName("ImageRow")
        row = QHBoxLayout(row_frame)
        row.setContentsMargins(12, 8, 12, 8)

        name_lbl = QLabel(f"<b>{name}</b><br><span style='color:#6e8396;font-size:11px;'>{image_ref}</span>")
        name_lbl.setTextFormat(Qt.TextFormat.RichText)
        local_lbl = QLabel("—")
        local_lbl.setObjectName("SectionDesc")
        remote_lbl = QLabel("—")
        remote_lbl.setObjectName("SectionDesc")
        status_lbl = QLabel("未检测")
        status_lbl.setObjectName("SectionDesc")
        btn_single = make_secondary_button("检查更新")
        btn_single.setFixedHeight(32)
        btn_single.clicked.connect(lambda checked, ref=image_ref: window._check_single_image(ref))
        row.addWidget(name_lbl, 3)
        row.addWidget(local_lbl, 2)
        row.addWidget(remote_lbl, 2)
        row.addWidget(status_lbl, 2)
        row.addWidget(btn_single, 2)
        window._image_rows_layout.addLayout(QVBoxLayout())
        window._image_rows_layout.itemAt(window._image_rows_layout.count() - 1).layout().addWidget(row_frame)
        window._image_row_widgets[image_ref] = {
            "local": local_lbl,
            "remote": remote_lbl,
            "status": status_lbl,
            "btn": btn_single,
        }
    window._apply_cached_image_status_to_rows()
