import os
import re
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QWidget, QStackedWidget,
                             QMessageBox, QLineEdit, QFileDialog,
                             QSizePolicy, QScrollArea, QFrame)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from core.port_utils import validate_port_bindings
from ui.styles import STYLESHEET
from ui.widgets import StepIndicator, create_install_progress_bar, show_notice_dialog


class CheckStepThread(QThread):
    """运行单个环境检测步骤"""
    step_done = pyqtSignal(int, bool, str)

    def __init__(self, func, step_index):
        super().__init__()
        self._func = func
        self._step = step_index

    def run(self):
        passed, detail = self._func()
        self.step_done.emit(self._step, passed, detail)


class ScanInstancesThread(QThread):
    """后台扫描已有实例线程"""
    scan_done = pyqtSignal(list)
    scan_step = pyqtSignal(str)  # 当前扫描步骤描述

    def __init__(self, backend):
        super().__init__()
        self.backend = backend

    def run(self):
        instances = self.backend.scan_existing_instances(on_step=self.scan_step.emit)
        self.scan_done.emit(instances)


class TakeoverThread(QThread):
    """后台执行接管线程"""
    finished = pyqtSignal(bool)

    def __init__(self, backend, instance):
        super().__init__()
        self.backend = backend
        self.instance = instance

    def run(self):
        ok = self.backend.takeover_instance(self.instance)
        self.finished.emit(ok)


class CreateRuntimeThread(QThread):
    """后台创建运行环境线程"""
    finished = pyqtSignal(bool)

    def __init__(self, backend, install_dir):
        super().__init__()
        self.backend = backend
        self.install_dir = install_dir

    def run(self):
        ok = self.backend.create_runtime(self.install_dir)
        self.finished.emit(ok)


class FirstRunDialog(QDialog):
    """首次运行向导对话框"""

    deploy_requested = pyqtSignal(str)  # 发出部署模式: "lite" 或 "napcat"

    def __init__(self, backend, config, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.config = config
        self.env_result = None
        self._check_in_progress = False
        self._selected_mode = self.config.get("deploy_mode") or "lite"
        self._pending_takeover_instance = None  # 场景 B 需要先建发行版时暂存

        self.setWindowTitle("Nekro Agent 环境配置向导")
        self.resize(660, 560)
        self.setMinimumSize(600, 520)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 24, 30, 30)
        layout.setSpacing(0)

        self._step_indicator = StepIndicator(
            ["检测环境", "创建运行环境", "选择版本", "配置端口"],
            current=0,
        )
        layout.addWidget(self._step_indicator)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        # 页面名称 → stack index 映射
        self._page_index = {}

        self._init_scan_page()           # scan（第一页：扫描已有实例）
        self._init_found_page()          # found_instances（扫描到实例时显示）
        self._init_check_page()          # env_check（未发现实例时的全新部署流程入口）
        self._init_create_page()         # create_runtime
        self._init_select_page()         # select_mode
        self._init_datadir_page()        # data_dir
        self._init_takeover_page()       # takeover_progress

        self._active_threads: list[QThread] = []

        self.backend.progress_updated.connect(self._on_progress)
        self.backend.install_error.connect(self._on_install_error)

        self._goto_page("scan")
        self._start_scan()

    def _track_thread(self, thread: QThread):
        self._active_threads.append(thread)
        thread.finished.connect(lambda t=thread: self._active_threads.remove(t) if t in self._active_threads else None)

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
        super().reject()

    def _add_page(self, page: QWidget, name: str):
        idx = self.stack.addWidget(page)
        self._page_index[name] = idx

    def _goto_page(self, name: str):
        self.stack.setCurrentIndex(self._page_index[name])
        step_map = {
            "scan": 0, "env_check": 0, "found_instances": 0,
            "create_runtime": 1,
            "select_mode": 2,
            "data_dir": 3,
            "takeover_progress": -1,
        }
        step = step_map.get(name, -1)
        if step >= 0:
            self._step_indicator.set_step(step)
            self._step_indicator.setVisible(True)
        else:
            self._step_indicator.setVisible(False)

    def _show_notice_dialog(self, title, text, button_text="确定", danger=False):
        show_notice_dialog(self, title, text, button_text, danger)

    # ------------------------------------------------------------------ #
    #  页面 scan: 检测已有实例（第一页）
    # ------------------------------------------------------------------ #

    def _init_scan_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(20)

        title = QLabel("检测本机部署")
        title.setObjectName("WizardTitle")
        layout.addWidget(title)

        self.scan_desc = QLabel("正在扫描本机已有的 Nekro Agent 实例，请稍候...")
        self.scan_desc.setObjectName("WizardDesc")
        self.scan_desc.setWordWrap(True)
        layout.addWidget(self.scan_desc)

        self.scan_step_label = QLabel("")
        self.scan_step_label.setObjectName("WizardStepHint")
        self.scan_step_label.setWordWrap(True)
        layout.addWidget(self.scan_step_label)

        self.scan_progress = create_install_progress_bar(0, 0, height=8, radius=4)
        layout.addWidget(self.scan_progress)

        layout.addStretch()

        btn_box = QHBoxLayout()
        btn_box.addStretch()
        self.scan_skip_btn = QPushButton("跳过，全新部署")
        self.scan_skip_btn.setFixedHeight(38)
        self.scan_skip_btn.setFixedWidth(150)  # 固定宽度，扫描前后不变
        self.scan_skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scan_skip_btn.setObjectName("WizardSecondary")
        self.scan_skip_btn.setEnabled(False)
        self.scan_skip_btn.clicked.connect(self._skip_to_fresh_deploy)
        btn_box.addWidget(self.scan_skip_btn)
        layout.addLayout(btn_box)

        self._add_page(page, "scan")

    def _start_scan(self):
        self.scan_desc.setText("正在扫描本机已有的 Nekro Agent 实例，请稍候...")
        self.scan_step_label.setText("")
        self.scan_progress.setRange(0, 0)
        self.scan_skip_btn.setText("跳过，全新部署")
        self.scan_skip_btn.setEnabled(False)
        self._scan_thread = ScanInstancesThread(self.backend)
        self._scan_thread.scan_step.connect(self.scan_step_label.setText)
        self._scan_thread.scan_done.connect(self._on_scan_done)
        self._track_thread(self._scan_thread)
        self._scan_thread.start()

    def _on_scan_done(self, instances: list):
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(1)
        self.scan_step_label.setText("")
        if instances:
            self._populate_found_page(instances)
            self._goto_page("found_instances")
        else:
            self.scan_desc.setText("未检测到已有实例，点击下方按钮开始全新安装配置。")
            self.scan_skip_btn.setText("全新部署")
            self.scan_skip_btn.setEnabled(True)

    def _skip_to_fresh_deploy(self):
        """跳过扫描结果，进入全新部署流程（环境检测）"""
        self._goto_page("env_check")
        self._start_check()

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

        # 检测项
        labels = self._check_item_labels()
        self.lbl_wsl = self._create_check_item(labels[0])
        self.lbl_distro = self._create_check_item(labels[1])
        self.lbl_docker = self._create_check_item(labels[2])
        self.lbl_compose = self._create_check_item(labels[3])

        layout.addWidget(self.lbl_wsl)
        layout.addWidget(self.lbl_distro)
        layout.addWidget(self.lbl_docker)
        layout.addWidget(self.lbl_compose)

        # 进度条（安装 Docker 时显示）
        self.check_progress = create_install_progress_bar(0, 0, height=6, radius=3)
        self.check_progress.setVisible(False)
        layout.addWidget(self.check_progress)

        layout.addStretch()

        # 底部按钮
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
        """启动第 step 步检测的子线程"""
        if step >= len(self._check_funcs):
            self._on_all_checks_done()
            return
        thread = CheckStepThread(self._check_funcs[step], step)
        thread.step_done.connect(self._on_step_done)
        self._current_step_thread = thread
        self._track_thread(thread)
        thread.start()

    def _on_step_done(self, step, passed, detail):
        """单步检测完成，更新 UI 后启动下一步"""
        labels = [self.lbl_wsl, self.lbl_distro, self.lbl_docker, self.lbl_compose]
        self._check_results[step] = (passed, detail)
        self._update_check_item(labels[step], passed, detail)
        if not passed:
            # 当前步骤失败，跳过后续检测，直接标记为未检测
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
        """全部检测完成，更新描述文字和按钮状态"""
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
        """根据环境检测结果更新描述和按钮（原有逻辑）"""
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
        """重新执行环境检测"""
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
    #  页面 found_instances: 发现已有实例
    # ------------------------------------------------------------------ #

    def _init_found_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(16)

        title = QLabel("发现已有 Nekro Agent 实例")
        title.setObjectName("WizardTitle")
        title.setWordWrap(True)
        layout.addWidget(title)

        desc = QLabel("检测到本机已安装以下 Nekro Agent 实例，可将其接管到此启动器进行统一管理。")
        desc.setObjectName("WizardDesc")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # 实例卡片列表区域（可滚动）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent;")
        self._found_cards_widget = QWidget()
        self._found_cards_layout = QVBoxLayout(self._found_cards_widget)
        self._found_cards_layout.setSpacing(10)
        self._found_cards_layout.setContentsMargins(0, 0, 0, 0)
        self._found_cards_layout.addStretch()
        scroll.setWidget(self._found_cards_widget)
        layout.addWidget(scroll, 1)

        # 底部按钮
        btn_box = QHBoxLayout()
        btn_box.addStretch()
        btn_skip = QPushButton("忽略，全新部署")
        btn_skip.setFixedHeight(38)
        btn_skip.setFixedWidth(150)
        btn_skip.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_skip.setObjectName("WizardSecondary")
        btn_skip.clicked.connect(self._skip_takeover)
        btn_box.addWidget(btn_skip)
        layout.addLayout(btn_box)

        self._add_page(page, "found_instances")

    def _populate_found_page(self, instances: list):
        """用扫描结果填充实例卡片"""
        # 清空旧卡片（保留末尾的 stretch）
        while self._found_cards_layout.count() > 1:
            item = self._found_cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for instance in instances:
            card = self._create_instance_card(instance)
            self._found_cards_layout.insertWidget(
                self._found_cards_layout.count() - 1, card
            )

    def _create_instance_card(self, instance: dict) -> QWidget:
        card = QFrame()
        card.setObjectName("WizardInstanceCard")
        card.setFrameShape(QFrame.Shape.StyledPanel)

        inner = QHBoxLayout(card)
        inner.setContentsMargins(16, 14, 14, 14)
        inner.setSpacing(12)

        info_col = QVBoxLayout()
        info_col.setSpacing(4)

        distro = instance["distro"]
        status = instance["status"]
        deploy_mode = instance["deploy_mode"]
        port = instance["env"].get("NEKRO_EXPOSE_PORT") or "8021"
        data_dir = instance["data_dir"]
        deploy_dir = instance["deploy_dir"]
        instance_name = instance.get("instance_name", "")
        is_managed = instance["is_managed"]

        status_icon = "🟢" if status == "running" else "⚪"
        status_text = "运行中" if status == "running" else "已停止"

        title_text = f"{distro}  {status_icon} {status_text}"
        if is_managed:
            title_text += "  （本启动器发行版）"
        if instance_name:
            title_text += f"  [{instance_name}]"

        lbl_title = QLabel(title_text)
        lbl_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #24384a; border: none;")
        info_col.addWidget(lbl_title)

        mode_label = "完整版 (NapCat)" if deploy_mode == "napcat" else "精简版 (Lite)"
        lbl_detail = QLabel(f"端口: {port}  |  模式: {mode_label}")
        lbl_detail.setStyleSheet("font-size: 13px; color: #6e8396; border: none;")
        info_col.addWidget(lbl_detail)

        lbl_dir = QLabel(f"部署: {deploy_dir}  |  数据: {data_dir}")
        lbl_dir.setStyleSheet("font-size: 12px; color: #8a98a6; border: none;")
        lbl_dir.setWordWrap(True)
        info_col.addWidget(lbl_dir)

        inner.addLayout(info_col, 1)

        btn_takeover = QPushButton("接管此实例")
        btn_takeover.setFixedHeight(36)
        btn_takeover.setMinimumWidth(100)
        btn_takeover.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_takeover.setStyleSheet(
            "QPushButton { background: #1b6db4; color: white; border: none; "
            "border-radius: 8px; font-size: 13px; font-weight: 600; padding: 0 14px; }"
            "QPushButton:hover { background: #185f9d; }"
        )
        btn_takeover.clicked.connect(lambda _checked, inst=instance: self._start_takeover(inst))
        inner.addWidget(btn_takeover, 0, Qt.AlignmentFlag.AlignVCenter)

        return card

    def _skip_takeover(self):
        """用户选择忽略已有实例，进入全新部署流程（环境检测）"""
        self._goto_page("env_check")
        self._start_check()

    def _start_takeover(self, instance: dict):
        """用户点击接管某个实例"""
        need_create = not instance["is_managed"] and not self.backend.runtime_exists()

        if need_create:
            # 需要先创建 NekroAgent 发行版，暂存实例信息
            self._pending_takeover_instance = instance
            self._show_notice_dialog(
                "需要先创建运行环境",
                "该实例位于其他 WSL 发行版中。\n\n"
                "接管前需先创建 NekroAgent 专用运行环境，创建完成后将自动继续迁移。",
                button_text="继续",
            )
            self._goto_page("create_runtime")
            return

        self._goto_page("takeover_progress")
        self._run_takeover(instance)

    def _run_takeover(self, instance: dict):
        """启动接管线程"""
        self._current_takeover_instance = instance
        self.takeover_lbl_status.setText("正在接管实例，请稍候...")
        self.takeover_progress.setRange(0, 0)
        self.takeover_progress.setVisible(True)
        self.takeover_lbl_error.setVisible(False)
        self.btn_takeover_retry.setVisible(False)

        self._takeover_thread = TakeoverThread(self.backend, instance)
        self._takeover_thread.finished.connect(self._on_takeover_done)
        self._track_thread(self._takeover_thread)
        self._takeover_thread.start()

    def _on_takeover_done(self, success: bool):
        self.takeover_progress.setVisible(False)
        if success:
            self.takeover_lbl_status.setText("✅ 接管成功！正在启动服务...")
            mode = self.config.get("deploy_mode") or "lite"
            self.deploy_requested.emit(mode)
            self.accept()
        else:
            # 检查是否需要先创建发行版（场景 B）
            if self._pending_takeover_instance is not None:
                return
            self.takeover_lbl_status.setText("❌ 接管失败，请查看下方错误详情后重试。")
            self.btn_takeover_retry.setVisible(True)

    # ------------------------------------------------------------------ #
    #  页面 takeover_progress: 接管进度
    # ------------------------------------------------------------------ #

    def _init_takeover_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(18)

        title = QLabel("正在接管 Nekro Agent 实例")
        title.setObjectName("WizardTitle")
        title.setWordWrap(True)
        layout.addWidget(title)

        desc = QLabel("正在将数据迁移到启动器管理环境，此过程可能需要几分钟，请耐心等待。")
        desc.setObjectName("WizardDesc")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        self.takeover_progress = create_install_progress_bar(0, 0, height=8, radius=4)
        self.takeover_progress.setVisible(False)
        layout.addWidget(self.takeover_progress)

        self.takeover_lbl_status = QLabel("")
        self.takeover_lbl_status.setObjectName("WizardDesc")
        self.takeover_lbl_status.setWordWrap(True)
        layout.addWidget(self.takeover_lbl_status)

        self.takeover_lbl_error = QLabel("")
        self.takeover_lbl_error.setObjectName("WizardError")
        self.takeover_lbl_error.setWordWrap(True)
        self.takeover_lbl_error.setVisible(False)
        layout.addWidget(self.takeover_lbl_error)

        layout.addStretch()

        btn_box = QHBoxLayout()
        btn_box.addStretch()
        self.btn_takeover_retry = QPushButton("重试")
        self.btn_takeover_retry.setFixedHeight(38)
        self.btn_takeover_retry.setFixedWidth(100)
        self.btn_takeover_retry.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_takeover_retry.setObjectName("WizardPrimary")
        self.btn_takeover_retry.setVisible(False)
        self.btn_takeover_retry.clicked.connect(self._retry_takeover)
        btn_box.addWidget(self.btn_takeover_retry)
        layout.addLayout(btn_box)

        self._add_page(page, "takeover_progress")

    def _retry_takeover(self):
        inst = getattr(self, "_current_takeover_instance", None)
        if inst:
            self.btn_takeover_retry.setVisible(False)
            self._run_takeover(inst)

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

        # 进度条
        self.create_progress = create_install_progress_bar(0, 0, height=8, radius=4)
        self.create_progress.setVisible(False)
        layout.addWidget(self.create_progress)

        # 进度状态文本
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

        # 底部按钮
        btn_box = QHBoxLayout()

        self.btn_back = QPushButton("返回")
        self.btn_back.setObjectName("WizardSecondary")
        self.btn_back.setFixedHeight(38)
        self.btn_back.setFixedWidth(80)
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.clicked.connect(self._create_page_back)
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

    def _create_page_back(self):
        """创建页的返回：若是从接管流程进来则回到实例列表，否则回到环境检测"""
        if self._pending_takeover_instance is not None:
            self._goto_page("found_instances")
        else:
            self._goto_page("env_check")

    def _browse_install_dir(self):
        d = QFileDialog.getExistingDirectory(
            self, "选择安装目录", self.dir_edit.text()
        )
        if d:
            # 在选择的目录下加上 NekroAgent 子目录
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
        self._create_thread.finished.connect(self._on_create_done)
        self._track_thread(self._create_thread)
        self._create_thread.start()

    def _on_progress(self, text):
        """接收 wsl_manager.progress_updated 信号"""
        current_page = self._current_page_name()

        if current_page == "create_runtime":
            self.lbl_progress.setText(text)
            self._update_create_progress(text)
            return

        if current_page == "takeover_progress":
            if text == "__need_create_runtime__":
                inst = getattr(self, "_current_takeover_instance", None)
                if inst:
                    self._pending_takeover_instance = inst
                self._goto_page("create_runtime")
                return
            self.takeover_lbl_status.setText(text)
            return

        # 检测页的处理
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

    def _current_page_name(self) -> str:
        idx = self.stack.currentIndex()
        for name, i in self._page_index.items():
            if i == idx:
                return name
        return ""

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
        elif current_page == "takeover_progress":
            self.takeover_lbl_error.setText(message)
            self.takeover_lbl_error.setVisible(bool(message))

    def _on_create_done(self, success):
        self.btn_create.setEnabled(True)
        self.btn_back.setEnabled(True)
        self.dir_edit.setReadOnly(False)
        self.create_progress.setVisible(False)

        if success:
            self.lbl_error.setVisible(False)
            self.lbl_progress.setText("✅ 环境创建完成！")
            # 若是为接管外部实例而创建的发行版，继续接管流程
            if self._pending_takeover_instance is not None:
                instance = self._pending_takeover_instance
                self._pending_takeover_instance = None
                self._goto_page("takeover_progress")
                self._run_takeover(instance)
            else:
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

        # 实例名称
        lbl_instance = QLabel("实例名称（可选）:")
        lbl_instance.setStyleSheet("font-size: 14px; font-weight: 600; color: #24384a; margin-top: 10px;")
        layout.addWidget(lbl_instance)

        self.instance_name_edit = QLineEdit("")
        self.instance_name_edit.setPlaceholderText("留空为默认实例，多实例时建议填写如 bot1_")
        self.instance_name_edit.setFixedWidth(300)
        self.instance_name_edit.textChanged.connect(self._on_instance_name_changed)
        layout.addWidget(self.instance_name_edit)

        instance_hint = QLabel("实例名称将作为容器和数据卷的前缀，用于隔离多个实例。建议以下划线结尾，如 bot1_。")
        instance_hint.setObjectName("WizardHint")
        instance_hint.setWordWrap(True)
        layout.addWidget(instance_hint)

        # Windows 侧访问路径
        lbl_winpath = QLabel("Windows 侧数据访问路径:")
        lbl_winpath.setStyleSheet("font-size: 14px; font-weight: 600; color: #24384a; margin-top: 10px;")
        layout.addWidget(lbl_winpath)

        windows_path = self.backend.get_host_access_path("/root/nekro_agent_data")
        self.datadir_path_card = QLabel(windows_path or r"\\wsl$\NekroAgent\root\nekro_agent_data")
        self.datadir_path_card.setProperty("role", "info_block")
        self.datadir_path_card.setWordWrap(True)
        self.datadir_path_card.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.datadir_path_card)

        # 端口配置
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

        # 底部按钮
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

    def _on_instance_name_changed(self, text):
        """实例名称变化时，更新数据目录预览路径。"""
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

        # 校验端口
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

        instance_name = self.instance_name_edit.text().strip()
        if instance_name and not instance_name.endswith("_"):
            instance_name += "_"

        if instance_name:
            deploy_dir = f"/root/{instance_name}nekro_agent"
            data_dir = f"/root/{instance_name}nekro_agent_data"
        else:
            deploy_dir = "/root/nekro_agent"
            data_dir = "/root/nekro_agent_data"

        inst_id = instance_name.rstrip("_") if instance_name else self.config.next_instance_id()
        inst_data = {
            "instance_name": instance_name,
            "deploy_dir": deploy_dir,
            "data_dir": data_dir,
            "deploy_mode": mode,
            "nekro_port": nekro_port,
            "napcat_port": napcat_port,
            "release_channel": self.config.get("release_channel") or "stable",
        }
        self.config.set_instance(inst_id, inst_data)
        self.config.set("active_instance", inst_id)

        if self.config:
            self.config.set("nekro_port", nekro_port)
            if mode == "napcat":
                self.config.set("napcat_port", napcat_port)
            self.config.set("deploy_mode", mode)
            self.config.set("first_run", False)
        self.deploy_requested.emit(mode)
        self.accept()

    def _check_item_labels(self):
        return (
            self.backend.display_name,
            "Nekro Agent 运行环境",
            "Docker",
            "Docker Compose",
        )
