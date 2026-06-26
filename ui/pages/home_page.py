from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextOption
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel,
    QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

from ui.widgets import ActionButton, MetricCard, SectionCard, make_button, make_secondary_button


class HomePage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.w = window

        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 30, 34, 30)
        layout.setSpacing(22)

        self._build_hero(layout)
        self._build_metrics(layout)
        self._build_bottom(layout)

    def _build_hero(self, parent_layout):
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

        self.w.status_badge = QLabel("状态: 未就绪")
        self.w.status_badge.setObjectName("StatusBadge")
        hero_top.addWidget(self.w.status_badge, 0, Qt.AlignmentFlag.AlignTop)
        hero_layout.addLayout(hero_top)

        advanced_row = QHBoxLayout()
        advanced_row.setSpacing(10)
        self.w.advanced_status_badge = QLabel("高级功能已启用")
        self.w.advanced_status_badge.setObjectName("FeatureBadge")
        self.w.advanced_status_badge.setVisible(False)
        advanced_row.addWidget(self.w.advanced_status_badge, 0, Qt.AlignmentFlag.AlignLeft)

        self.w.advanced_status_hint = QLabel("预览版入口已开放，可直接切换至预览版。")
        self.w.advanced_status_hint.setObjectName("SectionDesc")
        self.w.advanced_status_hint.setWordWrap(True)
        self.w.advanced_status_hint.setVisible(False)
        advanced_row.addWidget(self.w.advanced_status_hint, 1)
        advanced_row.addStretch()
        hero_layout.addLayout(advanced_row)

        hero_actions = QHBoxLayout()
        self.w.btn_primary_deploy = make_button("开始部署", object_name="HeroPrimary")
        self.w.btn_primary_deploy.clicked.connect(self.w.start_deploy)
        self.w.btn_primary_update = make_secondary_button("升级 Nekro Agent")
        self.w.btn_primary_update.clicked.connect(self.w._update_services)
        self.w.btn_primary_preview = make_secondary_button("切换至预览版")
        self.w.btn_primary_preview.clicked.connect(self.w._switch_to_preview_build)
        self.w.btn_primary_creds = make_secondary_button("查看部署凭据")
        self.w.btn_primary_creds.clicked.connect(self.w._show_saved_credentials)
        self.w.btn_instance_switch = make_secondary_button("切换实例")
        self.w.btn_instance_switch.clicked.connect(self.w._show_instance_switch_dialog)
        self.w.btn_instance_switch.setVisible(False)

        hero_actions.addWidget(self.w.btn_primary_deploy)
        hero_actions.addWidget(self.w.btn_primary_update)
        hero_actions.addWidget(self.w.btn_primary_preview)
        hero_actions.addWidget(self.w.btn_primary_creds)
        hero_actions.addWidget(self.w.btn_instance_switch)
        hero_actions.addStretch()
        hero_layout.addLayout(hero_actions)
        self.w._refresh_advanced_feature_ui()

        parent_layout.addWidget(hero)

    def _build_metrics(self, parent_layout):
        metrics = QGridLayout()
        metrics.setHorizontalSpacing(16)
        metrics.setVerticalSpacing(16)
        self.w.metric_status = MetricCard("服务状态", "未就绪", "", "red")
        self.w.metric_mode = MetricCard(
            "部署版本",
            self.w._format_mode_text(self.w.config.get("deploy_mode")),
            "",
            "amber",
        )

        host_path = self.w.backend.get_host_access_path(self.w.config.get_active_data_dir())
        self.w.metric_data_dir = MetricCard(
            "数据目录",
            host_path or "当前后端暂未提供 Windows 映射路径",
            "点击打开 Windows 侧文件夹",
            "green",
            clickable=bool(host_path),
        )
        self.w.metric_data_dir.clicked.connect(self.w._open_datadir_in_explorer)

        metrics.addWidget(self.w.metric_status, 0, 0)
        metrics.addWidget(self.w.metric_mode, 0, 1)
        metrics.addWidget(self.w.metric_data_dir, 0, 2)
        parent_layout.addLayout(metrics)

    def _build_bottom(self, parent_layout):
        bottom_grid = QGridLayout()
        bottom_grid.setHorizontalSpacing(16)
        bottom_grid.setVerticalSpacing(16)

        actions_card = SectionCard("快速操作", "保留最常用的部署与维护入口。")
        actions_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        actions_layout = actions_card.body_layout()
        actions_grid = QGridLayout()
        actions_grid.setHorizontalSpacing(14)
        actions_grid.setVerticalSpacing(16)

        self.w.btn_env_check = ActionButton("CHK", "环境检查", f"重新运行 {self.w.backend.display_name} 初始化向导")
        self.w.btn_deploy_action = ActionButton("RUN", "一键部署", "启动容器并写入运行配置", "primary")
        self.w.btn_new_instance_action = ActionButton("NEW", "部署新实例", "创建并部署新的 Nekro Agent 实例")
        self.w.btn_stop_action = ActionButton("STOP", "关闭服务", "停止当前实例 docker compose 服务")
        self.w.btn_update_action = ActionButton("UPD", "升级 Nekro Agent", "拉取镜像并重启服务")
        self.w.btn_uninstall_action = ActionButton("DEL", "卸载清理", "删除容器、镜像和运行环境", "danger")

        self.w.btn_env_check.clicked.connect(self.w._show_first_run_dialog)
        self.w.btn_deploy_action.clicked.connect(self.w.start_deploy)
        self.w.btn_new_instance_action.clicked.connect(self.w._show_first_run_dialog)
        self.w.btn_stop_action.clicked.connect(self.w._stop_services_for_mode_change)
        self.w.btn_update_action.clicked.connect(self.w._update_services)
        self.w.btn_uninstall_action.clicked.connect(self.w._uninstall_environment)

        actions_grid.addWidget(self.w.btn_env_check, 0, 0)
        actions_grid.addWidget(self.w.btn_deploy_action, 0, 1)
        actions_grid.addWidget(self.w.btn_new_instance_action, 1, 0)
        actions_grid.addWidget(self.w.btn_stop_action, 1, 1)
        actions_grid.addWidget(self.w.btn_update_action, 2, 0)
        actions_grid.addWidget(self.w.btn_uninstall_action, 2, 1)
        actions_layout.addLayout(actions_grid)

        activity_card = SectionCard("实时摘要", "显示最近的应用日志，完整内容在日志中心查看。")
        activity_layout = activity_card.body_layout()
        self.w.log_preview = QTextEdit()
        self.w.log_preview.setObjectName("LogViewer")
        self.w.log_preview.setReadOnly(True)
        self.w.log_preview.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.w.log_preview.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.w.log_preview.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Expanding,
        )
        self.w.log_preview.setMinimumHeight(250)
        activity_layout.addWidget(self.w.log_preview)

        bottom_grid.addWidget(actions_card, 0, 0, Qt.AlignmentFlag.AlignTop)
        bottom_grid.addWidget(activity_card, 0, 1)
        parent_layout.addLayout(bottom_grid)

        self.w._register_responsive_buttons(
            self.w.btn_env_check,
            self.w.btn_deploy_action,
            self.w.btn_new_instance_action,
            self.w.btn_stop_action,
            self.w.btn_update_action,
            self.w.btn_uninstall_action,
        )
