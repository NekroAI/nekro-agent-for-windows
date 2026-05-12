import os
import re

from PyQt6.QtCore import QTimer, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.port_utils import validate_port_bindings
from ui.styles import STYLESHEET
from ui.widgets import PullProgressView, StepIndicator, create_install_progress_bar, show_notice_dialog


class CheckStepThread(QThread):
    step_done = pyqtSignal(int, bool, str)

    def __init__(self, func, step_index):
        super().__init__()
        self._func = func
        self._step = step_index

    def run(self):
        passed, detail = self._func()
        self.step_done.emit(self._step, passed, detail)


class CreateRuntimeThread(QThread):
    result_ready = pyqtSignal(bool)

    def __init__(self, backend, install_dir):
        super().__init__()
        self.backend = backend
        self.install_dir = install_dir

    def run(self):
        ok = self.backend.create_runtime(self.install_dir)
        self.result_ready.emit(ok)


class FirstRunDialog(QDialog):
    """全新部署向导对话框（不含迁移逻辑，迁移使用 MigrationDialog）。"""

    deploy_requested = pyqtSignal(str, dict)

    def __init__(self, backend, config, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.config = config
        self.env_result = None
        self._check_in_progress = False
        self._selected_mode = self.config.get("deploy_mode") or "lite"

        self.setWindowTitle("Nekro Agent 环境配置向导")
        self.resize(660, 560)
        self.setMinimumSize(600, 520)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 24, 30, 30)
        layout.setSpacing(0)

        self._step_indicator = StepIndicator(
            ["检测环境", "创建运行环境", "选择版本", "配置实例", "部署服务"],
            current=0,
        )
        layout.addWidget(self._step_indicator)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        self._page_index = {}
        self._active_threads: list[QThread] = []

        self._init_check_page()
        self._init_create_page()
        self._init_select_page()
        self._init_datadir_page()
        self._init_deploy_page()

        self.backend.progress_updated.connect(self._on_progress)
        self.backend.install_error.connect(self._on_install_error)
        if hasattr(self.backend, "deploy_optional_confirm"):
            self.backend.deploy_optional_confirm.connect(self._show_deploy_optional_confirm)

        self._goto_page("env_check")
        self._start_check()

    def _track_thread(self, thread: QThread):
        self._active_threads.append(thread)
        thread.finished.connect(lambda _=None, t=thread: self._active_threads.remove(t) if t in self._active_threads else None)

    def reject(self):
        for thread in list(self._active_threads):
            if thread.isRunning():
                thread.quit()
                thread.wait(3000)
        self._active_threads.clear()
        try:
            self.backend.progress_updated.disconnect(self._on_progress)
        except (TypeError, RuntimeError):
            pass
        try:
            self.backend.install_error.disconnect(self._on_install_error)
        except (TypeError, RuntimeError):
            pass
        try:
            if hasattr(self.backend, "deploy_optional_confirm"):
                self.backend.deploy_optional_confirm.disconnect(self._show_deploy_optional_confirm)
        except (TypeError, RuntimeError):
            pass
        try:
            self.backend.status_changed.disconnect(self._on_deploy_status_changed)
        except (TypeError, RuntimeError):
            pass
        super().reject()

    def _add_page(self, page: QWidget, name: str):
        idx = self.stack.addWidget(page)
        self._page_index[name] = idx

    def _goto_page(self, name: str):
        self.stack.setCurrentIndex(self._page_index[name])
        step_map = {"env_check": 0, "create_runtime": 1, "select_mode": 2, "data_dir": 3, "deploy": 4}
        step = step_map.get(name, 0)
        self._step_indicator.set_step(step)

    def _show_notice_dialog(self, title, text, button_text="确定", danger=False):
        show_notice_dialog(self, title, text, button_text, danger)

    def _current_page_name(self) -> str:
        idx = self.stack.currentIndex()
        for name, i in self._page_index.items():
            if i == idx:
                return name
        return ""

    # ------------------------------------------------------------------ #
    #  页面 env_check: 环境检测
    # ------------------------------------------------------------------ #

    def _init_check_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(20)

        title = QLabel("环境检测")
        title.setObjectName("WizardTitle")
        title.setWordWrap(True)
        layout.addWidget(title)

        desc = QLabel("正在检测系统环境，请稍候...")
        desc.setObjectName("WizardDesc")
        desc.setWordWrap(True)
        layout.addWidget(desc)
        self.check_desc = desc

        labels = self._check_item_labels()
        self.lbl_wsl = self._create_check_item(labels[0])
        self.lbl_distro = self._create_check_item(labels[1])
        self.lbl_docker = self._create_check_item(labels[2])
        self.lbl_compose = self._create_check_item(labels[3])

        layout.addWidget(self.lbl_wsl)
        layout.addWidget(self.lbl_distro)
        layout.addWidget(self.lbl_docker)
        layout.addWidget(self.lbl_compose)

        self.check_progress = create_install_progress_bar(0, 0, height=6, radius=3)
        self.check_progress.setVisible(False)
        layout.addWidget(self.check_progress)

        layout.addStretch()

        btn_box = QHBoxLayout()
        btn_box.addStretch()

        self.btn_action = QPushButton("检测中...")
        self.btn_action.setFixedHeight(38)
        self.btn_action.setMinimumWidth(120)
        self.btn_action.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_action.setObjectName("WizardPrimary")
        self.btn_action.setEnabled(False)
        self.btn_action.clicked.connect(self._handle_action)
        btn_box.addWidget(self.btn_action)

        layout.addLayout(btn_box)
        self._add_page(page, "env_check")

    def _create_check_item(self, name):
        lbl = QLabel(f"⏳  {name}")
        lbl.setObjectName("WizardCheckItem")
        lbl.setProperty("state", "pending")
        lbl.setProperty("check_name", name)
        lbl.setWordWrap(True)
        lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        return lbl

    def _update_check_item(self, label, ok, detail=""):
        name = label.property("check_name")
        if ok:
            label.setText(f"✅  {name}" + (f"  —  {detail}" if detail else ""))
            label.setProperty("state", "pass")
        else:
            label.setText(f"❌  {name}" + (f"  —  {detail}" if detail else ""))
            label.setProperty("state", "fail")
        label.style().unpolish(label)
        label.style().polish(label)

    def _start_check(self):
        self._check_in_progress = True
        self._check_funcs = self.backend.get_check_funcs()
        self._check_results = {}
        self._run_check_step(0)

    def _run_check_step(self, step):
        if step >= len(self._check_funcs):
            self._on_all_checks_done()
            return
        thread = CheckStepThread(self._check_funcs[step], step)
        thread.step_done.connect(self._on_step_done)
        self._current_step_thread = thread
        self._track_thread(thread)
        thread.start()

    def _on_step_done(self, step, passed, detail):
        labels = [self.lbl_wsl, self.lbl_distro, self.lbl_docker, self.lbl_compose]
        self._check_results[step] = (passed, detail)
        self._update_check_item(labels[step], passed, detail)
        if not passed:
            for i in range(step + 1, len(labels)):
                self._check_results[i] = (False, "")
                name = labels[i].property("check_name")
                labels[i].setText(f"—  {name}")
                labels[i].setProperty("state", "skip")
                labels[i].style().unpolish(labels[i])
                labels[i].style().polish(labels[i])
            self._on_all_checks_done()
            return
        self._run_check_step(step + 1)

    def _on_all_checks_done(self):
        self._check_in_progress = False
        r = self._check_results
        self.env_result = {
            "wsl_installed": r.get(0, (False, ""))[0],
            "distro": r.get(1, (False, ""))[1] if r.get(1, (False, ""))[0] else "",
            "docker_available": r.get(2, (False, ""))[0],
            "compose_available": r.get(3, (False, ""))[0],
        }
        self._apply_check_result()

    def _apply_check_result(self):
        result = self.env_result
        all_ok = (result["wsl_installed"] and result["distro"]
                  and result["docker_available"] and result["compose_available"])

        if all_ok:
            self.check_desc.setText("所有环境组件已就绪！请点击下一步选择部署版本。")
            self.btn_action.setText("下一步")
            self.btn_action.setEnabled(True)
            self._action_mode = "next"
        else:
            if not result["wsl_installed"]:
                self.check_desc.setText(f"{self.backend.display_name} 未安装或尚未完成启用，请点击安装。")
                self.btn_action.setText(f"安装 {self.backend.display_name}")
                self._action_mode = "install_wsl"
            elif not result["distro"]:
                self.check_desc.setText("Nekro Agent 运行环境未创建，请点击创建。")
                self.btn_action.setText("创建运行环境")
                self._action_mode = "create_runtime"
            elif not result["docker_available"] or not result["compose_available"]:
                self.check_desc.setText("Docker 未安装，请点击安装。")
                self.btn_action.setText("安装 Docker")
                self._action_mode = "install_docker"
            self.btn_action.setEnabled(True)

    def _handle_action(self):
        mode = getattr(self, '_action_mode', None)

        if mode == "next":
            self._goto_page("select_mode")
            return

        if mode == "install_wsl":
            dialog = QMessageBox(self)
            dialog.setIcon(QMessageBox.Icon.Information)
            dialog.setWindowTitle(f"安装 {self.backend.display_name}")
            dialog.setText(
                f"将以管理员权限安装 {self.backend.display_name}。\n\n"
                "注意：安装过程需要 5-10 分钟，请耐心等待。\n"
                "安装完成后将自动重启电脑。"
            )
            dialog.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            dialog.setStyleSheet(STYLESHEET)
            for label in dialog.findChildren(QLabel):
                label.setWordWrap(True)
            reply = dialog.exec()
            if reply == QMessageBox.StandardButton.Ok:
                self.backend.install_wsl()
            return

        if mode == "create_runtime":
            self._goto_page("create_runtime")
            return

        if mode == "install_docker":
            self.backend.install_docker()
            self.check_desc.setText("正在安装 Docker...")
            self.btn_action.setEnabled(False)
            self.check_progress.setVisible(True)
            self._action_mode = "recheck"
            return

        if mode == "recheck":
            self._recheck()
            return

    def _recheck(self):
        self.btn_action.setEnabled(False)
        self.btn_action.setText("检测中...")
        self.check_desc.setText("正在重新检测...")
        self.check_progress.setVisible(False)

        for lbl in [self.lbl_wsl, self.lbl_distro, self.lbl_docker, self.lbl_compose]:
            name = lbl.property("check_name")
            lbl.setText(f"⏳  {name}")
            lbl.setProperty("state", "pending")
            lbl.style().unpolish(lbl)
            lbl.style().polish(lbl)

        self._start_check()

    # ------------------------------------------------------------------ #
    #  页面 create_runtime: 创建运行环境
    # ------------------------------------------------------------------ #

    def _init_create_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(18)

        title = QLabel("创建 Nekro Agent 运行环境")
        title.setObjectName("WizardTitle")
        title.setWordWrap(True)
        layout.addWidget(title)

        desc = QLabel("将下载 Ubuntu 并创建专用 WSL2 运行环境，与系统已有环境互不影响。")
        desc.setObjectName("WizardDesc")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        lbl_dir = QLabel("安装目录:")
        lbl_dir.setStyleSheet("font-size: 14px; font-weight: 600; color: #24384a; margin-top: 10px;")
        layout.addWidget(lbl_dir)

        dir_box = QHBoxLayout()
        self.dir_edit = QLineEdit(self.backend.get_default_install_dir())
        self.dir_edit.setMinimumWidth(260)
        btn_browse = QPushButton("浏览...")
        btn_browse.setFixedHeight(35)
        btn_browse.setMinimumWidth(80)
        btn_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_browse.clicked.connect(self._browse_install_dir)
        dir_box.addWidget(self.dir_edit)
        dir_box.addWidget(btn_browse)
        layout.addLayout(dir_box)

        hint = QLabel("此目录将存放 WSL2 运行时文件，建议预留 10GB 以上空间。")
        hint.setObjectName("WizardHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.create_progress = create_install_progress_bar(0, 0, height=8, radius=4)
        self.create_progress.setVisible(False)
        layout.addWidget(self.create_progress)

        self.lbl_progress = QLabel("")
        self.lbl_progress.setObjectName("WizardDesc")
        self.lbl_progress.setWordWrap(True)
        layout.addWidget(self.lbl_progress)

        self.lbl_error = QLabel("")
        self.lbl_error.setObjectName("WizardError")
        self.lbl_error.setWordWrap(True)
        self.lbl_error.setVisible(False)
        layout.addWidget(self.lbl_error)

        layout.addStretch()

        btn_box = QHBoxLayout()

        self.btn_back = QPushButton("返回")
        self.btn_back.setObjectName("WizardSecondary")
        self.btn_back.setFixedHeight(38)
        self.btn_back.setFixedWidth(80)
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.clicked.connect(lambda: self._goto_page("env_check"))
        btn_box.addWidget(self.btn_back)

        btn_box.addStretch()

        self.btn_create = QPushButton("开始创建")
        self.btn_create.setObjectName("WizardPrimary")
        self.btn_create.setFixedHeight(38)
        self.btn_create.setFixedWidth(120)
        self.btn_create.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_create.clicked.connect(self._start_create)
        btn_box.addWidget(self.btn_create)

        layout.addLayout(btn_box)
        self._add_page(page, "create_runtime")

    def _browse_install_dir(self):
        d = QFileDialog.getExistingDirectory(
            self, "选择安装目录", self.dir_edit.text()
        )
        if d:
            self.dir_edit.setText(os.path.join(d, "NekroAgent", "wsl"))

    def _start_create(self):
        install_dir = self.dir_edit.text().strip()
        if not install_dir:
            self._show_notice_dialog("提示", "请指定安装目录")
            return

        self.btn_create.setEnabled(False)
        self.btn_back.setEnabled(False)
        self.dir_edit.setReadOnly(True)
        self.create_progress.setVisible(True)
        self.lbl_progress.setText("准备下载...")
        self.lbl_error.clear()
        self.lbl_error.setVisible(False)

        self._create_thread = CreateRuntimeThread(self.backend, install_dir)
        self._create_thread.result_ready.connect(self._on_create_done)
        self._track_thread(self._create_thread)
        self._create_thread.start()

    def _on_create_done(self, success):
        self.btn_create.setEnabled(True)
        self.btn_back.setEnabled(True)
        self.dir_edit.setReadOnly(False)
        self.create_progress.setVisible(False)

        if success:
            self.lbl_error.setVisible(False)
            self.lbl_progress.setText("✅ 环境创建完成！")
            self._goto_page("select_mode")
        else:
            self.lbl_progress.setText("❌ 环境创建失败，请查看下方错误详情后重试。")

    # ------------------------------------------------------------------ #
    #  页面 select_mode: 版本选择
    # ------------------------------------------------------------------ #

    def _init_select_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(20)

        title = QLabel("选择部署版本")
        title.setObjectName("WizardTitle")
        title.setWordWrap(True)
        layout.addWidget(title)

        desc = QLabel("请选择要部署的 Nekro Agent 版本:")
        desc.setObjectName("WizardDesc")
        layout.addWidget(desc)

        self.card_lite = self._create_mode_card(
            "精简版 (Lite)",
            "仅包含核心 Nekro Agent 服务\n适合不需要 QQ 机器人功能的用户",
            "lite",
        )
        layout.addWidget(self.card_lite)

        self.card_napcat = self._create_mode_card(
            "完整版 (Napcat)",
            "包含 Nekro Agent + QQ 机器人 (Napcat)\n需要更多系统资源",
            "napcat",
        )
        layout.addWidget(self.card_napcat)

        layout.addStretch()
        self._add_page(page, "select_mode")

    def _create_mode_card(self, title, desc, mode):
        card = QPushButton()
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setMinimumHeight(104)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        card.setStyleSheet(
            "QPushButton { background-color: #ffffff; border: 2px solid #dfe7ef; "
            "border-radius: 10px; padding: 15px 20px; }"
            "QPushButton:hover { border-color: #e88478; background-color: #fff9f8; }"
        )

        inner = QVBoxLayout(card)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(4)
        inner.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lbl_title = QLabel(title)
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_title.setWordWrap(True)
        lbl_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #24384a; "
                                "background: transparent; border: none;")
        lbl_title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lbl_desc = QLabel(desc)
        lbl_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet("font-size: 12px; color: #6e8396; "
                               "background: transparent; border: none;")
        lbl_desc.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        inner.addWidget(lbl_title)
        inner.addWidget(lbl_desc)

        card.clicked.connect(lambda: self._select_mode(mode))
        return card

    def _select_mode(self, mode):
        self._selected_mode = mode
        if self.config:
            self.config.set("deploy_mode", mode)
        self._update_port_inputs_for_mode(mode)
        self._goto_page("data_dir")

    def _update_port_inputs_for_mode(self, mode):
        show_napcat = mode == "napcat"
        if hasattr(self, "napcat_port_row"):
            self.napcat_port_row.setVisible(show_napcat)
        if hasattr(self, "port_hint_label"):
            hint = "如无特殊需求保持默认即可。端口冲突时可修改。"
            if not show_napcat:
                hint = "Lite 模式仅需配置 Nekro Agent 端口，如无特殊需求保持默认即可。"
            self.port_hint_label.setText(hint)

    # ------------------------------------------------------------------ #
    #  页面 data_dir: 数据目录配置
    # ------------------------------------------------------------------ #

    def _init_datadir_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(18)

        title = QLabel("配置实例")
        title.setObjectName("WizardTitle")
        title.setWordWrap(True)
        layout.addWidget(title)

        desc = QLabel("配置实例名称、端口和数据目录。多实例部署时请为每个实例设置不同的名称和端口。")
        desc.setObjectName("WizardDesc")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        lbl_instance = QLabel("实例名称（可选）:")
        lbl_instance.setStyleSheet("font-size: 14px; font-weight: 600; color: #24384a; margin-top: 10px;")
        layout.addWidget(lbl_instance)

        self.instance_name_edit = QLineEdit("")
        self.instance_name_edit.setPlaceholderText("留空为默认实例，多实例时建议填写如 bot1_")
        self.instance_name_edit.setFixedWidth(300)
        self.instance_name_edit.textChanged.connect(self._clear_instance_name_error)
        self.instance_name_edit.textChanged.connect(self._on_instance_name_changed)
        layout.addWidget(self.instance_name_edit)

        instance_hint = QLabel("实例名称将作为容器和数据卷的前缀，仅支持英文字母、数字、下划线和短横线；建议以下划线结尾，如 bot1_。")
        instance_hint.setObjectName("WizardHint")
        instance_hint.setWordWrap(True)
        layout.addWidget(instance_hint)

        lbl_winpath = QLabel("Windows 侧数据访问路径:")
        lbl_winpath.setStyleSheet("font-size: 14px; font-weight: 600; color: #24384a; margin-top: 10px;")
        layout.addWidget(lbl_winpath)

        windows_path = self.backend.get_host_access_path("/root/nekro_agent_data")
        self.datadir_path_card = QLabel(windows_path or r"\\wsl$\NekroAgent\root\nekro_agent_data")
        self.datadir_path_card.setProperty("role", "info_block")
        self.datadir_path_card.setWordWrap(True)
        self.datadir_path_card.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.datadir_path_card)

        lbl_ports = QLabel("端口配置:")
        lbl_ports.setStyleSheet("font-size: 14px; font-weight: 600; color: #24384a; margin-top: 10px;")
        layout.addWidget(lbl_ports)

        port_row1 = QHBoxLayout()
        port_row1.addWidget(QLabel("Nekro Agent 端口:"))
        self.nekro_port_edit = QLineEdit(str(self.config.get("nekro_port") or 8021))
        self.nekro_port_edit.setFixedWidth(100)
        port_row1.addWidget(self.nekro_port_edit)
        port_row1.addStretch()
        layout.addLayout(port_row1)

        self.napcat_port_row = QWidget()
        port_row2 = QHBoxLayout(self.napcat_port_row)
        port_row2.setContentsMargins(0, 0, 0, 0)
        port_row2.addWidget(QLabel("NapCat 端口:"))
        self.napcat_port_edit = QLineEdit(str(self.config.get("napcat_port") or 6099))
        self.napcat_port_edit.setFixedWidth(100)
        port_row2.addWidget(self.napcat_port_edit)
        port_row2.addStretch()
        layout.addWidget(self.napcat_port_row)

        self.port_hint_label = QLabel()
        self.port_hint_label.setObjectName("WizardHint")
        layout.addWidget(self.port_hint_label)
        self._update_port_inputs_for_mode(self._selected_mode)

        layout.addStretch()

        btn_box = QHBoxLayout()

        btn_back = QPushButton("返回")
        btn_back.setObjectName("WizardSecondary")
        btn_back.setFixedHeight(38)
        btn_back.setFixedWidth(80)
        btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_back.clicked.connect(lambda: self._goto_page("select_mode"))
        btn_box.addWidget(btn_back)

        btn_box.addStretch()

        btn_deploy = QPushButton("开始部署")
        btn_deploy.setObjectName("WizardPrimary")
        btn_deploy.setFixedHeight(38)
        btn_deploy.setFixedWidth(120)
        btn_deploy.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_deploy.clicked.connect(self._confirm_datadir)
        btn_box.addWidget(btn_deploy)

        layout.addLayout(btn_box)
        self._add_page(page, "data_dir")

    def _init_deploy_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        title = QLabel("部署服务")
        title.setObjectName("WizardTitle")
        title.setWordWrap(True)
        layout.addWidget(title)

        desc = QLabel("正在准备镜像、写入配置并启动 Nekro Agent。请保持此窗口打开。")
        desc.setObjectName("WizardDesc")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        self.deploy_status_label = QLabel("准备开始部署...")
        self.deploy_status_label.setObjectName("WizardDesc")
        self.deploy_status_label.setWordWrap(True)
        layout.addWidget(self.deploy_status_label)

        self.deploy_detail_label = QLabel("")
        self.deploy_detail_label.setObjectName("WizardHint")
        self.deploy_detail_label.setWordWrap(True)
        self.deploy_detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.deploy_detail_label.setVisible(False)
        layout.addWidget(self.deploy_detail_label)

        self.deploy_pull_view = PullProgressView(self)
        layout.addWidget(self.deploy_pull_view)
        layout.setSpacing(10)

        self.deploy_step_order = ["config", "docker", "images", "optional", "cc_sandbox", "compose", "health"]
        self.deploy_steps = {
            "config": self._create_deploy_step_label("写入部署配置"),
            "docker": self._create_deploy_step_label("检查 Docker 服务"),
            "images": self._create_deploy_step_label("检查并拉取必需镜像"),
            "optional": self._create_deploy_step_label("确认可选组件"),
            "cc_sandbox": self._create_deploy_step_label("按需下载 Claude Code 沙盒"),
            "compose": self._create_deploy_step_label("启动 Compose 服务"),
            "health": self._create_deploy_step_label("等待服务就绪"),
        }
        for label in self.deploy_steps.values():
            layout.addWidget(label)

        layout.addStretch()

        btn_box = QHBoxLayout()
        btn_box.addStretch()
        self.btn_deploy_done = QPushButton("部署中...")
        self.btn_deploy_done.setObjectName("WizardPrimary")
        self.btn_deploy_done.setFixedHeight(38)
        self.btn_deploy_done.setFixedWidth(120)
        self.btn_deploy_done.setEnabled(False)
        self.btn_deploy_done.clicked.connect(self.accept)
        btn_box.addWidget(self.btn_deploy_done)
        layout.addLayout(btn_box)

        self._add_page(page, "deploy")

    def _create_deploy_step_label(self, text):
        label = QLabel(f"⏳  {text}")
        label.setObjectName("WizardCheckItem")
        label.setProperty("state", "pending")
        label.setProperty("check_name", text)
        label.setWordWrap(True)
        return label

    def _set_deploy_step(self, key, state, detail=""):
        label = self.deploy_steps.get(key)
        if not label:
            return
        name = label.property("check_name")
        icon = {"pending": "⏳", "running": "⏳", "done": "✅", "fail": "❌"}.get(state, "⏳")
        label.setText(f"{icon}  {name}" + (f"  —  {detail}" if detail else ""))
        label.setProperty("state", "pass" if state == "done" else "fail" if state == "fail" else "pending")
        label.style().unpolish(label)
        label.style().polish(label)

    def _clear_pull_progress(self):
        self.deploy_pull_view.reset()

    def _clear_instance_name_error(self):
        self.instance_name_edit.setProperty("invalid", False)
        self.instance_name_edit.style().unpolish(self.instance_name_edit)
        self.instance_name_edit.style().polish(self.instance_name_edit)

    def _mark_instance_name_error(self):
        self.instance_name_edit.setProperty("invalid", True)
        self.instance_name_edit.style().unpolish(self.instance_name_edit)
        self.instance_name_edit.style().polish(self.instance_name_edit)
        self.instance_name_edit.setFocus()
        self.instance_name_edit.selectAll()

    def _on_instance_name_changed(self, text):
        name = text.strip()
        if name and not name.endswith("_"):
            name += "_"
        if name:
            data_dir = f"/root/{name}nekro_agent_data"
        else:
            data_dir = "/root/nekro_agent_data"
        windows_path = self.backend.get_host_access_path(data_dir)
        self.datadir_path_card.setText(windows_path)

    def _confirm_datadir(self):
        mode = getattr(self, "_selected_mode", "lite")

        try:
            nekro_port = int(self.nekro_port_edit.text().strip())
            if not (1 <= nekro_port <= 65535):
                raise ValueError
            napcat_port = int(self.config.get("napcat_port") or 6099)
            if mode == "napcat":
                napcat_port = int(self.napcat_port_edit.text().strip())
                if not (1 <= napcat_port <= 65535):
                    raise ValueError
        except ValueError:
            self._show_notice_dialog("提示", "端口号必须为 1-65535 之间的整数")
            return

        port_specs = [("Nekro Agent 端口", nekro_port)]
        if mode == "napcat":
            port_specs.append(("NapCat 端口", napcat_port))
        ok, message = validate_port_bindings(port_specs)
        if not ok:
            self._show_notice_dialog("端口冲突", message)
            return

        if mode == "napcat":
            self._show_notice_dialog(
                "NapCat 登录提示",
                "NapCat 模式在关闭启动器并重新启动后，可能需要重新进行登录。\n\n"
                "在部分情况下，QQ 还可能触发新设备风控。这属于当前 NapCat 运行方式下的已知现象，请在部署前知悉。",
                button_text="我知道了",
            )

        raw_instance_name = self.instance_name_edit.text().strip()
        if raw_instance_name and not re.fullmatch(r"[A-Za-z0-9_-]+", raw_instance_name):
            self._mark_instance_name_error()
            self._show_notice_dialog("实例名称无效", "实例名称仅支持英文字母、数字、下划线和短横线，不能包含中文、空格或其它特殊字符。\n\n请修改后再部署。")
            return
        instance_name = raw_instance_name
        if instance_name and not instance_name.endswith("_"):
            instance_name += "_"

        if instance_name:
            deploy_dir = f"/root/{instance_name}nekro_agent"
            data_dir = f"/root/{instance_name}nekro_agent_data"
        else:
            deploy_dir = "/root/nekro_agent"
            data_dir = "/root/nekro_agent_data"

        inst_id = instance_name.rstrip("_") if instance_name else self.config.next_instance_id()

        existing = self.config.get_instance(inst_id)
        if existing:
            self._show_notice_dialog("实例名冲突", f"已存在 ID 为「{inst_id}」的实例，请更换实例名称。")
            return

        inst_data = {
            "inst_id": inst_id,
            "instance_name": instance_name,
            "deploy_dir": deploy_dir,
            "data_dir": data_dir,
            "deploy_mode": mode,
            "nekro_port": nekro_port,
            "napcat_port": napcat_port,
            "release_channel": self.config.get("release_channel") or "stable",
        }
        self._start_deploy_progress()
        self.deploy_requested.emit(mode, inst_data)

    def _start_deploy_progress(self):
        self._goto_page("deploy")
        self.deploy_status_label.setText("正在启动部署流程...")
        self.deploy_detail_label.clear()
        self.deploy_detail_label.setVisible(False)
        self._clear_pull_progress()
        for key in self.deploy_step_order:
            self._set_deploy_step(key, "pending")
        self.btn_deploy_done.setText("部署中...")
        self.btn_deploy_done.setEnabled(False)
        try:
            self.backend.status_changed.disconnect(self._on_deploy_status_changed)
        except (TypeError, RuntimeError):
            pass
        self.backend.status_changed.connect(self._on_deploy_status_changed)

    def _show_deploy_optional_confirm(self, title, prompt):
        confirmed = self._show_notice_confirm(title, prompt, confirm_text="下载", cancel_text="跳过")
        if hasattr(self.backend, "reply_deploy_optional"):
            self.backend.reply_deploy_optional(confirmed)
        if confirmed:
            self._set_deploy_step("optional", "done", "已选择下载")
        else:
            self._set_deploy_step("optional", "done", "已跳过")
            self._set_deploy_step("cc_sandbox", "done", "已跳过")

    def _show_notice_confirm(self, title, text, confirm_text="确认", cancel_text="取消"):
        from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(380)
        dialog.setMaximumWidth(500)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        title_label = QLabel(title)
        title_label.setProperty("role", "dialog_title")
        title_label.setWordWrap(True)
        layout.addWidget(title_label)

        desc_label = QLabel(text)
        desc_label.setProperty("role", "dialog_desc")
        desc_label.setWordWrap(True)
        desc_label.setOpenExternalLinks(True)
        desc_label.setTextFormat(Qt.TextFormat.RichText)
        desc_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        layout.addWidget(desc_label)

        button_row = QHBoxLayout()
        button_row.addStretch()
        cancel_button = QPushButton(cancel_text)
        cancel_button.clicked.connect(dialog.reject)
        button_row.addWidget(cancel_button)
        confirm_button = QPushButton(confirm_text)
        confirm_button.setProperty("role", "primary")
        confirm_button.clicked.connect(dialog.accept)
        button_row.addWidget(confirm_button)
        layout.addLayout(button_row)

        return dialog.exec() == int(QDialog.DialogCode.Accepted)

    def _set_deploy_stage(self, key, message=""):
        if key in self.deploy_step_order:
            index = self.deploy_step_order.index(key)
            for done_key in self.deploy_step_order[:index]:
                self._set_deploy_step(done_key, "done")
            self._set_deploy_step(key, "running", message)
        if key not in {"images", "cc_sandbox"}:
            self._clear_pull_progress()
        if message:
            self.deploy_status_label.setText(message)

    def _on_deploy_status_changed(self, status):
        if self._current_page_name() != "deploy":
            return
        if status == "运行中":
            for key in self.deploy_step_order:
                self._set_deploy_step(key, "done")
            self._clear_pull_progress()
            self.deploy_status_label.setText("部署完成，服务已就绪。")
            self.deploy_detail_label.setText("可以开始使用 Nekro Agent。")
            self.deploy_detail_label.setVisible(True)
            self.btn_deploy_done.setText("完成")
            self.btn_deploy_done.setEnabled(True)
            QTimer.singleShot(800, self.accept)
        elif status == "启动失败":
            self.deploy_status_label.setText("部署失败，请查看日志详情后重试。")
            self.deploy_detail_label.setText("详细错误仍会记录到主窗口日志页。")
            self.deploy_detail_label.setVisible(True)
            for key in reversed(self.deploy_step_order):
                label = self.deploy_steps.get(key)
                if label and label.property("state") == "pending" and "—" in label.text():
                    self._set_deploy_step(key, "fail")
                    break
            self.btn_deploy_done.setText("关闭")
            self.btn_deploy_done.setEnabled(True)

    # ------------------------------------------------------------------ #
    #  信号处理
    # ------------------------------------------------------------------ #

    def _on_progress(self, text):
        current_page = self._current_page_name()

        if text.startswith("__pull_progress__|"):
            parts = text.split("|", 2)
            if len(parts) < 3:
                return
            _, phase, message = parts
            if current_page == "deploy":
                if phase == "start":
                    self.deploy_pull_view.start(message)
                    self._set_deploy_stage("images", message)
                elif phase == "stage":
                    current = 0
                    total = 0
                    stage_message = message
                    meta_match = re.match(r"^(\d+)/(\d+)\|(.+)$", message)
                    if meta_match:
                        current = int(meta_match.group(1))
                        total = int(meta_match.group(2))
                        stage_message = meta_match.group(3)
                    self.deploy_pull_view.begin_stage(stage_message, current, total)
                    self._set_deploy_stage("images", stage_message)
                elif phase == "update":
                    self.deploy_pull_view.update(detail=message)
                elif phase == "done":
                    self.deploy_pull_view.finish(message)
                    QTimer.singleShot(500, self._clear_pull_progress)
                    self._set_deploy_step("images", "done")
                elif phase == "error":
                    self._set_deploy_step("images", "fail", "镜像拉取失败")
                    self.deploy_pull_view.fail("镜像拉取失败，请查看主窗口日志。")
            return

        if text.startswith("__deploy_progress__|"):
            parts = text.split("|", 2)
            if len(parts) < 3:
                return
            _, stage, message = parts
            if current_page == "deploy":
                if stage == "done":
                    for key in self.deploy_step_order:
                        if key == "cc_sandbox" and self.deploy_steps[key].property("state") == "pending":
                            self._set_deploy_step(key, "done", "已跳过")
                        else:
                            self._set_deploy_step(key, "done")
                    self._clear_pull_progress()
                    self.deploy_status_label.setText(message)
                else:
                    self._set_deploy_stage(stage, message)
            return

        if current_page == "create_runtime":
            self.lbl_progress.setText(text)
            self._update_create_progress(text)
            return

        if text == "__docker_done__":
            self.check_progress.setVisible(False)
            self._recheck()
            return
        if text == "__docker_fail__":
            self.check_progress.setVisible(False)
            self.check_desc.setText("Docker 安装失败，请重试。")
            self.btn_action.setText("安装 Docker")
            self.btn_action.setEnabled(True)
            self._action_mode = "install_docker"
            return

        if self.check_progress.isVisible():
            self.check_desc.setText(text)

    def _update_create_progress(self, text):
        progress_match = re.search(r"\((\d+)%\)", text)
        if progress_match:
            pct = max(0, min(100, int(progress_match.group(1))))
            self.create_progress.setRange(0, 100)
            self.create_progress.setValue(pct)
            return

        if "下载完成" in text:
            self.create_progress.setRange(0, 100)
            self.create_progress.setValue(100)
            return

        self.create_progress.setRange(0, 0)

    def _on_install_error(self, message):
        current_page = self._current_page_name()
        if current_page == "create_runtime":
            self.lbl_error.setText(message)
            self.lbl_error.setVisible(bool(message))

    def _check_item_labels(self):
        return (
            self.backend.display_name,
            "Nekro Agent 运行环境",
            "Docker",
            "Docker Compose",
        )
