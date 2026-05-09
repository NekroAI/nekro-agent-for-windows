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

        data_dir = self.w.config.get_active_data_dir()
        deploy_dir = self.w.config.get_active_deploy_dir()
        dirs_info = [
            ("DATA", "数据目录", "存储数据库、配置、日志等运行数据", data_dir),
            ("CONF", "部署目录", "存储 docker-compose 和 .env 配置文件", deploy_dir),
        ]
        for badge, title, hint, wsl_path in dirs_info:
            button = ActionButton(badge, title, hint)
            button.clicked.connect(lambda checked, path=wsl_path: self.w._open_wsl_path(path))
            card_layout.addWidget(button)
            self.w._register_responsive_buttons(button)

        layout.addWidget(card)
        layout.addStretch()
