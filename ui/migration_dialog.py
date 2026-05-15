import os

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ui.styles import STYLESHEET
from ui.widgets import SpinnerLabel, StepIndicator, create_install_progress_bar, show_notice_dialog


class ScanInstancesThread(QThread):
    scan_done = pyqtSignal(list)
    scan_step = pyqtSignal(str)

    def __init__(self, backend):
        super().__init__()
        self.backend = backend

    def run(self):
        try:
            instances = self.backend.scan_existing_instances(on_step=self.scan_step.emit)
        except Exception as e:
            self.scan_step.emit(f"扫描失败: {e}")
            instances = []
        self.scan_done.emit(instances)


class TakeoverThread(QThread):
    result_ready = pyqtSignal(bool)
    step_changed = pyqtSignal(int, int, str)
    error_ready = pyqtSignal(str)

    def __init__(self, backend, instance):
        super().__init__()
        self.backend = backend
        self.instance = instance

    def run(self):
        try:
            ok = self.backend.takeover_instance(self.instance, on_step=self._on_step)
        except Exception as e:
            self.error_ready.emit(str(e))
            ok = False
        self.result_ready.emit(ok)

    def _on_step(self, idx, total, desc):
        self.step_changed.emit(idx, total, desc)


class CreateRuntimeThread(QThread):
    result_ready = pyqtSignal(bool)
    error_ready = pyqtSignal(str)

    def __init__(self, backend, install_dir):
        super().__init__()
        self.backend = backend
        self.install_dir = install_dir

    def run(self):
        try:
            ok = self.backend.create_runtime(self.install_dir)
        except Exception as e:
            self.error_ready.emit(str(e))
            ok = False
        self.result_ready.emit(ok)


class MigrationDialog(QDialog):
    """独立的迁移向导对话框，用于发现并接管已有的非启动器部署实例。"""

    deploy_requested = pyqtSignal(str, dict)

    def __init__(self, backend, config, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.config = config
        self._pending_takeover_instance = None

        self.setWindowTitle("Nekro Agent 迁移向导")
        self.resize(700, 580)
        self.setMinimumSize(640, 520)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 24, 30, 30)
        layout.setSpacing(0)

        self._step_indicator = StepIndicator(["扫描实例", "确认接管", "迁移数据"], current=0)
        layout.addWidget(self._step_indicator)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        self._page_index = {}
        self._active_threads: list[QThread] = []

        self._init_scan_page()
        self._init_found_page()
        self._init_create_runtime_page()
        self._init_progress_page()
        self._init_result_page()

        self.backend.progress_updated.connect(self._on_progress)
        self.backend.install_error.connect(self._on_install_error)

        self._goto_page("scan")
        self._start_scan()

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #

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
        super().reject()

    def _add_page(self, page: QWidget, name: str):
        idx = self.stack.addWidget(page)
        self._page_index[name] = idx

    def _goto_page(self, name: str):
        self.stack.setCurrentIndex(self._page_index[name])
        step_map = {"scan": 0, "found_instances": 1, "create_runtime": 1, "progress": 2, "result": 2}
        step = step_map.get(name, 0)
        self._step_indicator.set_step(step)
        self._step_indicator.setVisible(name != "result")

    def _current_page_name(self) -> str:
        idx = self.stack.currentIndex()
        for name, i in self._page_index.items():
            if i == idx:
                return name
        return ""

    # ------------------------------------------------------------------ #
    # 页面 1：扫描
    # ------------------------------------------------------------------ #

    def _init_scan_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(20)

        title = QLabel("扫描本机部署")
        title.setObjectName("WizardTitle")
        layout.addWidget(title)

        self._scan_desc = QLabel("正在扫描本机所有 WSL 发行版中的 Nekro Agent 部署，请稍候...")
        self._scan_desc.setObjectName("WizardDesc")
        self._scan_desc.setWordWrap(True)
        layout.addWidget(self._scan_desc)

        self._scan_step_label = QLabel("")
        self._scan_step_label.setObjectName("WizardStepHint")
        self._scan_step_label.setWordWrap(True)
        layout.addWidget(self._scan_step_label)

        self._scan_progress = create_install_progress_bar(0, 0, height=8, radius=4)
        layout.addWidget(self._scan_progress)

        layout.addStretch()

        btn_box = QHBoxLayout()
        btn_box.addStretch()
        self._scan_cancel_btn = QPushButton("取消")
        self._scan_cancel_btn.setFixedHeight(38)
        self._scan_cancel_btn.setFixedWidth(100)
        self._scan_cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._scan_cancel_btn.setObjectName("WizardSecondary")
        self._scan_cancel_btn.clicked.connect(self.reject)
        btn_box.addWidget(self._scan_cancel_btn)
        layout.addLayout(btn_box)

        self._add_page(page, "scan")

    def _start_scan(self):
        self._scan_desc.setText("正在扫描本机所有 WSL 发行版中的 Nekro Agent 部署，请稍候...")
        self._scan_step_label.setText("")
        self._scan_progress.setRange(0, 0)

        self._scan_thread = ScanInstancesThread(self.backend)
        self._scan_thread.scan_step.connect(self._scan_step_label.setText)
        self._scan_thread.scan_done.connect(self._on_scan_done)
        self._track_thread(self._scan_thread)
        self._scan_thread.start()

    def _on_scan_done(self, instances: list):
        self._scan_progress.setRange(0, 1)
        self._scan_progress.setValue(1)
        self._scan_step_label.setText("")

        if instances:
            self._populate_found_page(instances)
            self._goto_page("found_instances")
        else:
            self._scan_desc.setText("未检测到已有的 Nekro Agent 部署。")
            self._scan_cancel_btn.setText("关闭")

    # ------------------------------------------------------------------ #
    # 页面 2：发现实例列表
    # ------------------------------------------------------------------ #

    def _init_found_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(16)

        title = QLabel("发现已有部署")
        title.setObjectName("WizardTitle")
        layout.addWidget(title)

        desc = QLabel(
            "以下是在本机 WSL 中发现的 Nekro Agent 部署实例。\n"
            "选择一个实例进行接管，数据将被迁移到启动器管理的专用环境中。"
        )
        desc.setObjectName("WizardDesc")
        desc.setWordWrap(True)
        layout.addWidget(desc)

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

        btn_box = QHBoxLayout()
        self._rescan_btn = QPushButton("重新扫描")
        self._rescan_btn.setFixedHeight(38)
        self._rescan_btn.setFixedWidth(120)
        self._rescan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._rescan_btn.setObjectName("WizardSecondary")
        self._rescan_btn.clicked.connect(self._rescan)
        btn_box.addWidget(self._rescan_btn)
        btn_box.addStretch()
        btn_cancel = QPushButton("取消")
        btn_cancel.setFixedHeight(38)
        btn_cancel.setFixedWidth(100)
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.setObjectName("WizardSecondary")
        btn_cancel.clicked.connect(self.reject)
        btn_box.addWidget(btn_cancel)
        layout.addLayout(btn_box)

        self._add_page(page, "found_instances")

    def _rescan(self):
        self._goto_page("scan")
        self._start_scan()

    def _populate_found_page(self, instances: list):
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

        title_parts = [f"{distro}  {status_icon} {status_text}"]
        if is_managed:
            title_parts.append("（启动器发行版）")
        else:
            title_parts.append("（非启动器部署）")
        if instance_name:
            title_parts.append(f"  [{instance_name}]")

        lbl_title = QLabel("".join(title_parts))
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

        if not is_managed and status == "running":
            lbl_warn = QLabel("⚠ 接管前将自动停止此实例")
            lbl_warn.setStyleSheet("font-size: 11px; color: #d08a30; border: none;")
            info_col.addWidget(lbl_warn)

        inner.addLayout(info_col, 1)

        btn_takeover = QPushButton("接管此实例")
        btn_takeover.setFixedHeight(36)
        btn_takeover.setMinimumWidth(110)
        btn_takeover.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_takeover.setObjectName("WizardAccent")
        btn_takeover.clicked.connect(lambda _checked, inst=instance: self._start_takeover(inst))
        inner.addWidget(btn_takeover, 0, Qt.AlignmentFlag.AlignVCenter)

        return card

    # ------------------------------------------------------------------ #
    # 页面 2.5：创建运行环境（场景 B 前置）
    # ------------------------------------------------------------------ #

    def _init_create_runtime_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(18)

        title = QLabel("创建运行环境")
        title.setObjectName("WizardTitle")
        layout.addWidget(title)

        desc = QLabel(
            "迁移目标需要 NekroAgent 专用 WSL 运行环境。\n"
            "将自动下载 Ubuntu 并创建专用环境，完成后继续迁移。"
        )
        desc.setObjectName("WizardDesc")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        self._create_progress = create_install_progress_bar(0, 0, height=8, radius=4)
        layout.addWidget(self._create_progress)

        self._create_status = QLabel("准备创建...")
        self._create_status.setObjectName("WizardDesc")
        self._create_status.setWordWrap(True)
        layout.addWidget(self._create_status)

        self._create_error = QLabel("")
        self._create_error.setObjectName("WizardError")
        self._create_error.setWordWrap(True)
        self._create_error.setVisible(False)
        layout.addWidget(self._create_error)

        layout.addStretch()
        self._add_page(page, "create_runtime")

    def _auto_create_runtime(self, instance: dict):
        """自动创建运行环境，完成后继续接管。"""
        self._pending_takeover_instance = instance
        self._goto_page("create_runtime")
        self._create_status.setText("正在创建运行环境...")
        self._create_error.setVisible(False)
        self._create_progress.setRange(0, 0)

        install_dir = self.backend.get_default_install_dir()
        self._create_thread = CreateRuntimeThread(self.backend, install_dir)
        self._create_thread.error_ready.connect(self._on_install_error)
        self._create_thread.result_ready.connect(self._on_create_done)
        self._track_thread(self._create_thread)
        self._create_thread.start()

    def _on_create_done(self, success):
        self._create_progress.setRange(0, 1)
        self._create_progress.setValue(1 if success else 0)

        if success:
            self._create_status.setText("✅ 运行环境创建完成，继续迁移...")
            inst = self._pending_takeover_instance
            self._pending_takeover_instance = None
            if inst:
                self._goto_page("progress")
                self._run_takeover(inst)
        else:
            self._create_status.setText("❌ 运行环境创建失败")
            self._create_error.setVisible(True)

    # ------------------------------------------------------------------ #
    # 页面 3：迁移进度
    # ------------------------------------------------------------------ #

    def _init_progress_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(16)

        title = QLabel("正在迁移")
        title.setObjectName("WizardTitle")
        layout.addWidget(title)

        desc = QLabel("正在将数据从源发行版迁移到启动器管理的环境中，请耐心等待...")
        desc.setObjectName("WizardDesc")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # 步骤列表区域
        self._step_list_widget = QWidget()
        self._step_list_layout = QVBoxLayout(self._step_list_widget)
        self._step_list_layout.setSpacing(6)
        self._step_list_layout.setContentsMargins(0, 8, 0, 8)
        layout.addWidget(self._step_list_widget)

        self._migrate_progress = create_install_progress_bar(0, 0, height=8, radius=4)
        layout.addWidget(self._migrate_progress)

        self._migrate_status = QLabel("")
        self._migrate_status.setObjectName("WizardDesc")
        self._migrate_status.setWordWrap(True)
        layout.addWidget(self._migrate_status)

        layout.addStretch()
        self._add_page(page, "progress")

    def _start_takeover(self, instance: dict):
        need_create = not instance["is_managed"] and not self.backend.runtime_exists()

        if need_create:
            self._auto_create_runtime(instance)
            return

        self._goto_page("progress")
        self._run_takeover(instance)

    def _run_takeover(self, instance: dict):
        self._current_takeover_instance = instance
        self._migrate_status.setText("正在准备迁移...")
        self._migrate_progress.setRange(0, 0)
        self._clear_step_list()

        is_managed = instance.get("is_managed", False)
        if is_managed:
            steps_preview = ["同步配置"]
        else:
            steps_preview = [
                "停止源实例容器",
                "迁移 Docker 镜像",
                "打包源实例数据",
                "还原数据到目标环境",
                "整理目录结构",
                "清理临时文件",
                "写入配置并同步",
            ]
        self._step_labels = []
        for text in steps_preview:
            row = QHBoxLayout()
            row.setSpacing(8)
            spinner = SpinnerLabel(self._step_list_widget, color="#a0b0be", size=14)
            spinner.setText("○")
            spinner.setStyleSheet("font-size: 14px; color: #a0b0be;")
            row.addWidget(spinner)
            lbl = QLabel(text)
            lbl.setStyleSheet("font-size: 13px; color: #8a98a6;")
            row.addWidget(lbl, 1)
            self._step_list_layout.addLayout(row)
            self._step_labels.append((spinner, lbl))

        self._takeover_thread = TakeoverThread(self.backend, instance)
        self._takeover_thread.step_changed.connect(self._on_takeover_step)
        self._takeover_thread.error_ready.connect(self._migrate_status.setText)
        self._takeover_thread.result_ready.connect(self._on_takeover_done)
        self._track_thread(self._takeover_thread)
        self._takeover_thread.start()

    def _clear_step_list(self):
        while self._step_list_layout.count():
            item = self._step_list_layout.takeAt(0)
            if item.layout():
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget():
                        child.widget().deleteLater()
            elif item.widget():
                item.widget().deleteLater()

    def _on_takeover_step(self, idx: int, total: int, desc: str):
        self._migrate_status.setText(desc)
        if total > 0:
            self._migrate_progress.setRange(0, total)
            self._migrate_progress.setValue(idx)

        step_idx = idx - 1
        for i, (spinner, lbl) in enumerate(self._step_labels):
            if i < step_idx:
                spinner.setText("✓")
                spinner.setStyleSheet("font-size: 14px; color: #54c08a;")
                lbl.setStyleSheet("font-size: 13px; color: #54c08a;")
            elif i == step_idx:
                spinner.setText("●")
                spinner.setStyleSheet("font-size: 14px; color: #1b6db4;")
                lbl.setStyleSheet("font-size: 13px; color: #264057; font-weight: bold;")
            else:
                spinner.setText("○")
                spinner.setStyleSheet("font-size: 14px; color: #a0b0be;")
                lbl.setStyleSheet("font-size: 13px; color: #8a98a6;")

    def _on_takeover_done(self, success: bool):
        for spinner, lbl in self._step_labels:
            spinner.setText("✓")
            spinner.setStyleSheet("font-size: 14px; color: #54c08a;")
            lbl.setStyleSheet("font-size: 13px; color: #54c08a;")

        if success:
            self._migrate_progress.setRange(0, 1)
            self._migrate_progress.setValue(1)
            self._show_result(True, "迁移成功！", "实例已成功接管到启动器管理。点击下方按钮启动服务。")
        else:
            if self._pending_takeover_instance is not None:
                return
            self._show_result(False, "迁移失败", "接管过程中出现错误，请查看日志了解详情。可以重试或手动处理。")

    # ------------------------------------------------------------------ #
    # 页面 4：结果
    # ------------------------------------------------------------------ #

    def _init_result_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(20)

        self._result_icon = QLabel()
        self._result_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._result_icon.setStyleSheet("font-size: 48px;")
        layout.addWidget(self._result_icon)

        self._result_title = QLabel()
        self._result_title.setObjectName("WizardTitle")
        self._result_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._result_title)

        self._result_desc = QLabel()
        self._result_desc.setObjectName("WizardDesc")
        self._result_desc.setWordWrap(True)
        self._result_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._result_desc)

        layout.addStretch()

        btn_box = QHBoxLayout()
        btn_box.addStretch()

        self._result_retry_btn = QPushButton("重试")
        self._result_retry_btn.setFixedHeight(38)
        self._result_retry_btn.setFixedWidth(100)
        self._result_retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._result_retry_btn.setObjectName("WizardSecondary")
        self._result_retry_btn.clicked.connect(self._retry_takeover)
        self._result_retry_btn.setVisible(False)
        btn_box.addWidget(self._result_retry_btn)

        self._result_action_btn = QPushButton("启动服务")
        self._result_action_btn.setFixedHeight(38)
        self._result_action_btn.setFixedWidth(120)
        self._result_action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._result_action_btn.setObjectName("WizardPrimary")
        self._result_action_btn.clicked.connect(self._on_result_action)
        btn_box.addWidget(self._result_action_btn)

        layout.addLayout(btn_box)
        self._add_page(page, "result")

    def _show_result(self, success: bool, title: str, desc: str):
        self._goto_page("result")
        self._result_success = success

        if success:
            self._result_icon.setText("✅")
            self._result_retry_btn.setVisible(False)
            self._result_action_btn.setText("启动服务")
            self._result_action_btn.setObjectName("WizardPrimary")
        else:
            self._result_icon.setText("❌")
            self._result_retry_btn.setVisible(True)
            self._result_action_btn.setText("关闭")
            self._result_action_btn.setObjectName("WizardSecondary")

        self._result_action_btn.style().unpolish(self._result_action_btn)
        self._result_action_btn.style().polish(self._result_action_btn)

        self._result_title.setText(title)
        self._result_desc.setText(desc)

    def _on_result_action(self):
        if self._result_success:
            mode = self.config.get("deploy_mode") or "lite"
            self.deploy_requested.emit(mode, {})
        self.accept()

    def _retry_takeover(self):
        inst = getattr(self, "_current_takeover_instance", None)
        if inst:
            self._goto_page("progress")
            self._run_takeover(inst)

    # ------------------------------------------------------------------ #
    # 信号处理
    # ------------------------------------------------------------------ #

    def _on_progress(self, text):
        page = self._current_page_name()

        if page == "create_runtime":
            self._create_status.setText(text)
            if "%" in text:
                import re
                m = re.search(r"\((\d+)%\)", text)
                if m:
                    pct = max(0, min(100, int(m.group(1))))
                    self._create_progress.setRange(0, 100)
                    self._create_progress.setValue(pct)
            elif "下载完成" in text:
                self._create_progress.setRange(0, 100)
                self._create_progress.setValue(100)
            elif text == "__need_create_runtime__":
                inst = getattr(self, "_current_takeover_instance", None)
                if inst:
                    self._auto_create_runtime(inst)
            return

        if page == "progress":
            if text == "__need_create_runtime__":
                inst = getattr(self, "_current_takeover_instance", None)
                if inst:
                    self._auto_create_runtime(inst)
                return
            self._migrate_status.setText(text)

    def _on_install_error(self, message):
        page = self._current_page_name()
        if page == "create_runtime":
            self._create_error.setText(message)
            self._create_error.setVisible(bool(message))
