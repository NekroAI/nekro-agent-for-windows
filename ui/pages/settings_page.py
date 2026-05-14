from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from core.app_updater import APP_VERSION
from ui.widgets import SectionCard, StyledComboBox


class SettingsPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.w = window

        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 30, 34, 30)
        layout.setSpacing(18)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(18)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        self._build_instance_card(grid, 0, 0)
        self._build_deploy_card(grid, 1, 0)
        self._build_storage_card(grid, 2, 0)
        self._build_general_card(grid, 0, 1)
        self._build_advanced_card(grid, 1, 1)
        self._build_about_card(grid, 2, 1)

        layout.addLayout(grid)
        layout.addStretch()

    def _build_instance_card(self, parent_layout, row, column):
        card = SectionCard("当前实例", "查看启动器当前管理的 Nekro Agent 实例。")
        card_layout = card.body_layout()

        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        self.w.instance_title_label = QLabel("")
        self.w.instance_title_label.setObjectName("VersionDisplay")
        title_row.addWidget(self.w.instance_title_label)
        title_row.addStretch()
        card_layout.addLayout(title_row)

        default_row = QHBoxLayout()
        default_row.setSpacing(12)
        default_row.addWidget(QLabel("启动默认实例"))
        self.w.default_instance_combo = StyledComboBox()
        self.w.default_instance_combo.setMinimumWidth(300)
        self.w.default_instance_combo.currentIndexChanged.connect(self._on_default_instance_changed)
        default_row.addWidget(self.w.default_instance_combo, 0, Qt.AlignmentFlag.AlignLeft)
        default_row.addStretch()
        card_layout.addLayout(default_row)

        remark_row = QHBoxLayout()
        remark_row.setSpacing(12)
        remark_row.addWidget(QLabel("实例备注"))
        self.w.instance_remark_edit = QLineEdit()
        self.w.instance_remark_edit.setPlaceholderText("例如：主号、测试 Bot、群管实例")
        remark_row.addWidget(self.w.instance_remark_edit, 1)
        btn_save_remark = QPushButton("保存备注")
        btn_save_remark.setObjectName("HeroSecondary")
        btn_save_remark.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save_remark.clicked.connect(self._save_instance_remark)
        remark_row.addWidget(btn_save_remark)
        card_layout.addLayout(remark_row)

        self.w.instance_info_label = QLabel("")
        self.w.instance_info_label.setObjectName("SectionDesc")
        self.w.instance_info_label.setWordWrap(True)
        card_layout.addWidget(self.w.instance_info_label)

        self._refresh_instance_info()

        parent_layout.addWidget(card, row, column)

    def _refresh_default_instance_combo(self):
        if not hasattr(self.w, "default_instance_combo"):
            return
        combo = self.w.default_instance_combo
        combo.blockSignals(True)
        combo.clear()
        instances = self.w.config.list_instances()
        default_id = self.w.config.get_default_instance_id()
        current_idx = 0
        for i, (inst_id, inst_data) in enumerate(instances):
            display = inst_data.get("remark") or self.w._instance_display_name(inst_id, inst_data)
            mode = "napcat" if inst_data.get("deploy_mode") == "napcat" else "lite"
            port = inst_data.get("nekro_port", 8021)
            combo.addItem(f"{display}  ({mode}, 端口：{port})", inst_id)
            if inst_id == default_id:
                current_idx = i
        if not instances:
            combo.addItem("无实例", "")
        combo.setCurrentIndex(current_idx)
        combo.setEnabled(bool(instances))
        combo.blockSignals(False)

    def _on_default_instance_changed(self, index):
        inst_id = self.w.default_instance_combo.itemData(index)
        if not inst_id:
            return
        if self.w.config.set_default_instance_id(inst_id):
            self._refresh_instance_info()

    def _save_instance_remark(self):
        inst_id = self.w.config.get_active_instance_id()
        if not inst_id:
            return
        remark = self.w.instance_remark_edit.text().strip()
        self.w.config.update_instance(inst_id, remark=remark)
        self._refresh_instance_info()
        self.w.refresh_dashboard()

    def _refresh_instance_info(self):
        self._refresh_default_instance_combo()
        inst = self.w.config.get_instance()
        inst_id = self.w.config.get_active_instance_id()
        if not inst:
            self.w.instance_title_label.setText("尚未部署实例")
            self.w.instance_info_label.setText("尚未部署任何实例。")
            return

        display = self.w._instance_display_name(inst_id, inst)
        mode = "NapCat 完整版" if inst.get("deploy_mode") == "napcat" else "Lite 精简版"
        port = inst.get("nekro_port", 8021)
        channel = inst.get("release_channel", "stable")
        remark = inst.get("remark", "")
        title = remark or display
        self.w.instance_title_label.setText(f"{title}  ({mode}, 端口：{port}, {channel})")
        self.w.instance_remark_edit.blockSignals(True)
        self.w.instance_remark_edit.setText(remark)
        self.w.instance_remark_edit.blockSignals(False)

        parts = []
        parts.append(f"实例 ID: {inst_id}")
        if inst.get("instance_name"):
            parts.append(f"实例前缀: {inst['instance_name']}")
        if inst.get("deploy_dir"):
            parts.append(f"部署目录: {inst['deploy_dir']}")
        if inst.get("data_dir"):
            parts.append(f"数据目录: {inst['data_dir']}")
        self.w.instance_info_label.setText("  |  ".join(parts))

    def _build_general_card(self, parent_layout, row, column):
        card = SectionCard("通用设置", "控制系统集成和自动检查选项。")
        card_layout = card.body_layout()

        self.w.check_auto = QCheckBox("开机自动启动 Nekro Agent 管理系统")
        self.w.check_auto.setChecked(self.w.config.get("autostart"))
        self.w.check_auto.stateChanged.connect(self.w._on_autostart_changed)
        card_layout.addWidget(self.w.check_auto)

        image_check_row = QHBoxLayout()
        image_check_row.setSpacing(12)
        image_check_label = QLabel("镜像更新检查")
        image_check_row.addWidget(image_check_label)

        self.w.image_update_interval_combo = StyledComboBox()
        for hours, label in self.w._image_update_check_interval_options():
            self.w.image_update_interval_combo.addItem(label, hours)
        current_hours = self.w._image_update_check_interval_hours()
        current_index = self.w.image_update_interval_combo.findData(current_hours)
        if current_index < 0:
            current_index = self.w.image_update_interval_combo.findData(24)
        self.w.image_update_interval_combo.setCurrentIndex(max(0, current_index))
        self.w.image_update_interval_combo.currentIndexChanged.connect(
            lambda _index: self.w._on_image_update_interval_changed()
        )
        image_check_row.addWidget(self.w.image_update_interval_combo, 0, Qt.AlignmentFlag.AlignLeft)
        image_check_row.addStretch()
        card_layout.addLayout(image_check_row)

        self.w.image_update_check_hint = QLabel()
        self.w.image_update_check_hint.setObjectName("SectionDesc")
        self.w.image_update_check_hint.setWordWrap(True)
        card_layout.addWidget(self.w.image_update_check_hint)
        self.w._refresh_image_update_check_hint()

        parent_layout.addWidget(card, row, column)

    def _build_about_card(self, parent_layout, row, column):
        card = SectionCard("关于启动器", f"Nekro Agent Windows 启动器 v{APP_VERSION}")
        card_layout = card.body_layout()

        version_row = QHBoxLayout()
        version_row.setSpacing(12)

        self.w.version_label = QLabel(f"当前版本: v{APP_VERSION}")
        self.w.version_label.setObjectName("VersionDisplay")
        version_row.addWidget(self.w.version_label)

        version_row.addStretch()

        self.w.btn_check_update = QPushButton("检查更新")
        self.w.btn_check_update.setObjectName("HeroSecondary")
        self.w.btn_check_update.setCursor(Qt.CursorShape.PointingHandCursor)
        self.w.btn_check_update.clicked.connect(self.w.check_app_update_manual)
        version_row.addWidget(self.w.btn_check_update)
        card_layout.addLayout(version_row)

        info = QLabel(
            f"配置目录: {self.w.config.app_data_dir}\n"
            f"浏览器数据: {self.w.config.browser_profile_dir}\n"
            f"当前后端: {self.w.backend.display_name}"
        )
        info.setObjectName("SectionDesc")
        info.setWordWrap(True)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card_layout.addWidget(info)

        link_row = QHBoxLayout()
        link_row.setSpacing(10)

        btn_open_config = QPushButton("打开配置目录")
        btn_open_config.setObjectName("HeroSecondary")
        btn_open_config.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_open_config.clicked.connect(lambda: self.w._open_path_in_explorer(self.w.config.app_data_dir))
        link_row.addWidget(btn_open_config)

        btn_open_logs = QPushButton("查看应用日志")
        btn_open_logs.setObjectName("HeroSecondary")
        btn_open_logs.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_open_logs.clicked.connect(lambda: self.w.switch_tab(2))
        link_row.addWidget(btn_open_logs)
        link_row.addStretch()
        card_layout.addLayout(link_row)

        parent_layout.addWidget(card, row, column)

    def _build_advanced_card(self, parent_layout, row, column):
        card = SectionCard("高级功能", "启用后可在总览控制台使用预览版切换等功能。")
        card_layout = card.body_layout()

        advanced_row = QHBoxLayout()
        advanced_row.setSpacing(12)

        self.w.btn_enable_advanced = QPushButton()
        self.w.btn_enable_advanced.setObjectName("SegmentBtn")
        self.w.btn_enable_advanced.setCheckable(True)
        self.w.btn_enable_advanced.setCursor(Qt.CursorShape.PointingHandCursor)
        self.w.btn_enable_advanced.setFixedWidth(136)
        self.w.btn_enable_advanced.clicked.connect(self.w._toggle_advanced_features)
        advanced_row.addWidget(self.w.btn_enable_advanced, 0, Qt.AlignmentFlag.AlignLeft)
        advanced_row.addStretch()
        card_layout.addLayout(advanced_row)

        self.w.advanced_hint = QLabel()
        self.w.advanced_hint.setObjectName("SectionDesc")
        self.w.advanced_hint.setWordWrap(True)
        card_layout.addWidget(self.w.advanced_hint)

        parent_layout.addWidget(card, row, column)

    def _build_deploy_card(self, parent_layout, row, column):
        card = SectionCard("部署配置", "管理部署版本和服务端口。")
        card_layout = card.body_layout()

        form = QGridLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(12)
        form.setColumnStretch(0, 0)
        form.setColumnStretch(1, 0)
        form.setColumnStretch(2, 0)
        form.setColumnStretch(3, 1)

        mode_label = QLabel("部署版本")
        mode_label.setMinimumWidth(118)
        form.addWidget(mode_label, 0, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.w.mode_display = QLabel(self.w._format_mode_text(self.w.config.get("deploy_mode")))
        self.w.mode_display.setObjectName("VersionDisplay")
        self.w.mode_display.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.addWidget(self.w.mode_display, 0, 1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.w.nekro_port_label = QLabel("Nekro Agent 端口")
        self.w.nekro_port_label.setMinimumWidth(118)
        form.addWidget(self.w.nekro_port_label, 1, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.w.nekro_port_setting = QLineEdit(str(self.w.config.get("nekro_port") or 8021))
        self.w.nekro_port_setting.setPlaceholderText("8021")
        self.w.nekro_port_setting.setFixedWidth(120)
        form.addWidget(self.w.nekro_port_setting, 1, 1, Qt.AlignmentFlag.AlignLeft)

        self.w.napcat_port_label = QLabel("NapCat 端口")
        self.w.napcat_port_label.setMinimumWidth(118)
        form.addWidget(self.w.napcat_port_label, 2, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.w.napcat_port_setting = QLineEdit(str(self.w.config.get("napcat_port") or 6099))
        self.w.napcat_port_setting.setPlaceholderText("6099")
        self.w.napcat_port_setting.setFixedWidth(120)
        form.addWidget(self.w.napcat_port_setting, 2, 1, Qt.AlignmentFlag.AlignLeft)

        btn_save_ports = QPushButton("保存端口设置")
        btn_save_ports.setObjectName("HeroSecondary")
        btn_save_ports.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save_ports.clicked.connect(self.w._save_ports)
        form.addWidget(btn_save_ports, 1, 2, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        card_layout.addLayout(form)

        self.w.port_hint_label = QLabel()
        self.w.port_hint_label.setObjectName("SectionDesc")
        self.w.port_hint_label.setWordWrap(True)
        card_layout.addWidget(self.w.port_hint_label)

        parent_layout.addWidget(card, row, column)

    def _build_storage_card(self, parent_layout, row, column):
        card = SectionCard("存储路径", "查看运行环境的安装位置和数据目录。")
        card_layout = card.body_layout()

        card_layout.addWidget(QLabel(f"{self.w.backend.display_name} 安装目录"))
        self.w.wsldir_edit = QLineEdit(self.w.config.get("wsl_install_dir") or "未配置")
        self.w.wsldir_edit.setReadOnly(True)
        card_layout.addWidget(self.w.wsldir_edit)

        card_layout.addWidget(QLabel("数据目录 (运行环境内路径)"))
        datadir_box = QHBoxLayout()
        self.w.datadir_edit = QLineEdit(self.w.config.get_active_data_dir())
        self.w.datadir_edit.setReadOnly(True)
        datadir_box.addWidget(self.w.datadir_edit)

        btn_open_datadir = QPushButton("打开目录")
        btn_open_datadir.setObjectName("HeroSecondary")
        btn_open_datadir.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_open_datadir.clicked.connect(self.w._open_datadir_in_explorer)
        datadir_box.addWidget(btn_open_datadir)
        card_layout.addLayout(datadir_box)

        self.w.datadir_hint = QLabel()
        self.w.datadir_hint.setObjectName("SectionDesc")
        self.w.datadir_hint.setWordWrap(True)
        card_layout.addWidget(self.w.datadir_hint)
        self.w._refresh_datadir_hint()

        parent_layout.addWidget(card, row, column)
