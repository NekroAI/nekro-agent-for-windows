from PyQt6.QtWidgets import QVBoxLayout, QWidget

from ui.widgets import ActionButton, SectionCard


class FilesPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.w = window

        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 30, 34, 30)
        layout.setSpacing(18)

        card = SectionCard("存储与路径", "通过 Windows 资源管理器访问运行环境内的重要目录。")
        card_layout = card.body_layout()

        self.path_buttons = {}
        dirs_info = [
            ("data", "DATA", "数据目录", "存储数据库、配置、日志等运行数据"),
            ("deploy", "CONF", "部署目录", "存储 docker-compose 和 .env 配置文件"),
        ]
        for key, badge, title, hint in dirs_info:
            button = ActionButton(badge, title, hint)
            button.clicked.connect(lambda checked, path_key=key: self._open_current_path(path_key))
            card_layout.addWidget(button)
            self.path_buttons[key] = (button, hint)
            self.w._register_responsive_buttons(button)

        layout.addWidget(card)
        layout.addStretch()
        self.refresh_paths()

    def _current_path(self, key):
        if key == "deploy":
            return self.w.config.get_active_deploy_dir()
        return self.w.config.get_active_data_dir()

    def _open_current_path(self, key):
        self.w._open_wsl_path(self._current_path(key))

    def refresh_paths(self):
        for key, (button, hint) in self.path_buttons.items():
            path = self._current_path(key)
            button.desc_label.setText(f"{hint}\n{path}")
            button.setToolTip(path)
