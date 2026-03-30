import os
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QWidget, QStackedWidget,
                             QMessageBox, QLineEdit, QFileDialog,
                             QProgressBar, QSizePolicy)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from ui.styles import STYLESHEET
from ui.widgets import show_notice_dialog


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

        self.setWindowTitle("Nekro Agent 环境配置向导")
        self.resize(660, 560)
        self.setMinimumSize(600, 520)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(0)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        self._init_check_page()       # 页面 0: 环境检测
        self._init_create_page()      # 页面 1: 创建运行环境
        self._init_select_page()      # 页面 2: 版本选择
        self._init_datadir_page()     # 页面 3: 数据目录配置

        self.backend.progress_updated.connect(self._on_progress)
        self.backend.install_error.connect(self._on_install_error)

        self.stack.setCurrentIndex(0)
        self._start_check()

    def _show_notice_dialog(self, title, text, button_text="确定", danger=False):
        show_notice_dialog(self, title, text, button_text, danger)

    # ------------------------------------------------------------------ #
    #  页面 0: 环境检测
    # ------------------------------------------------------------------ #

    def _init_check_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(20)

        title = QLabel("环境检测")
        title.setStyleSheet("font-size: 22px; font-weight: bold; color: #24292f;")
        title.setWordWrap(True)
        layout.addWidget(title)

        desc = QLabel("正在检测系统环境，请稍候...")
        desc.setStyleSheet("font-size: 14px; color: #57606a;")
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
        self.check_progress = QProgressBar()
        self.check_progress.setRange(0, 0)  # 不确定进度（循环动画）
        self.check_progress.setFixedHeight(6)
        self.check_progress.setStyleSheet(
            "QProgressBar { border: none; background: #e8e9eb; border-radius: 3px; }"
            "QProgressBar::chunk { background: #0969da; border-radius: 3px; }"
        )
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
        self.btn_action.setStyleSheet(
            "QPushButton { background-color: #2da44e; color: white; border: none; "
            "border-radius: 6px; font-size: 14px; font-weight: 600; padding: 0 20px; }"
            "QPushButton:hover { background-color: #28943f; }"
            "QPushButton:disabled { background-color: #94d3a2; }"
        )
        self.btn_action.setEnabled(False)
        self.btn_action.clicked.connect(self._handle_action)
        btn_box.addWidget(self.btn_action)

        layout.addLayout(btn_box)
        self.stack.addWidget(page)

    def _create_check_item(self, name):
        lbl = QLabel(f"⏳  {name}")
        lbl.setStyleSheet("font-size: 15px; color: #57606a; padding: 5px 0;")
        lbl.setProperty("check_name", name)
        lbl.setWordWrap(True)
        lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        return lbl

    def _update_check_item(self, label, ok, detail=""):
        name = label.property("check_name")
        if ok:
            label.setText(f"✅  {name}" + (f"  —  {detail}" if detail else ""))
            label.setStyleSheet("font-size: 15px; color: #2da44e; padding: 5px 0;")
        else:
            label.setText(f"❌  {name}" + (f"  —  {detail}" if detail else ""))
            label.setStyleSheet("font-size: 15px; color: #cf222e; padding: 5px 0;")

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
        self._current_step_thread = thread  # prevent GC
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
                labels[i].setStyleSheet("font-size: 15px; color: #8b949e; padding: 5px 0;")
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
            self.stack.setCurrentIndex(2)
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
            self.stack.setCurrentIndex(1)
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
            lbl.setStyleSheet("font-size: 15px; color: #57606a; padding: 5px 0;")

        self._start_check()

    # ------------------------------------------------------------------ #
    #  页面 1: 创建运行环境
    # ------------------------------------------------------------------ #

    def _init_create_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(18)

        title = QLabel("创建 Nekro Agent 运行环境")
        title.setStyleSheet("font-size: 22px; font-weight: bold; color: #24292f;")
        title.setWordWrap(True)
        layout.addWidget(title)

        desc = QLabel("将下载 Ubuntu 并创建专用 WSL2 运行环境，与系统已有环境互不影响。")
        desc.setStyleSheet("font-size: 13px; color: #57606a;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # 安装目录
        lbl_dir = QLabel("安装目录:")
        lbl_dir.setStyleSheet("font-size: 14px; font-weight: 600; color: #24292f; margin-top: 10px;")
        layout.addWidget(lbl_dir)

        dir_box = QHBoxLayout()
        self.dir_edit = QLineEdit(self.backend.get_default_install_dir())
        self.dir_edit.setStyleSheet(
            "padding: 8px; border: 1px solid #d0d7de; border-radius: 6px; "
            "background: white; font-size: 13px;"
        )
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
        hint.setStyleSheet("font-size: 12px; color: #8b949e;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 进度条
        self.create_progress = QProgressBar()
        self.create_progress.setRange(0, 0)  # 不确定进度
        self.create_progress.setFixedHeight(8)
        self.create_progress.setStyleSheet(
            "QProgressBar { border: none; background: #e8e9eb; border-radius: 4px; }"
            "QProgressBar::chunk { background: #0969da; border-radius: 4px; }"
        )
        self.create_progress.setVisible(False)
        layout.addWidget(self.create_progress)

        # 进度状态文本
        self.lbl_progress = QLabel("")
        self.lbl_progress.setStyleSheet("font-size: 13px; color: #0969da; margin-top: 8px;")
        self.lbl_progress.setWordWrap(True)
        layout.addWidget(self.lbl_progress)

        # 错误详情文本
        self.lbl_error = QLabel("")
        self.lbl_error.setStyleSheet(
            "font-size: 12px; color: #cf222e; background: #fff0f0; border: 1px solid #ffcdd2; "
            "border-radius: 6px; padding: 8px; margin-top: 4px;"
        )
        self.lbl_error.setWordWrap(True)
        self.lbl_error.setVisible(False)
        layout.addWidget(self.lbl_error)

        layout.addStretch()

        # 底部按钮
        btn_box = QHBoxLayout()

        self.btn_back = QPushButton("返回")
        self.btn_back.setFixedHeight(38)
        self.btn_back.setFixedWidth(80)
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.setStyleSheet(
            "QPushButton { background-color: #f3f4f6; color: #24292f; border: 1px solid #d0d7de; "
            "border-radius: 6px; font-size: 14px; font-weight: 600; }"
            "QPushButton:hover { background-color: #e8e9eb; }"
        )
        self.btn_back.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        btn_box.addWidget(self.btn_back)

        btn_box.addStretch()

        self.btn_create = QPushButton("开始创建")
        self.btn_create.setFixedHeight(38)
        self.btn_create.setFixedWidth(120)
        self.btn_create.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_create.setStyleSheet(
            "QPushButton { background-color: #2da44e; color: white; border: none; "
            "border-radius: 6px; font-size: 14px; font-weight: 600; }"
            "QPushButton:hover { background-color: #28943f; }"
            "QPushButton:disabled { background-color: #94d3a2; }"
        )
        self.btn_create.clicked.connect(self._start_create)
        btn_box.addWidget(self.btn_create)

        layout.addLayout(btn_box)
        self.stack.addWidget(page)

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
        self._create_thread.start()

    def _on_progress(self, text):
        """接收 wsl_manager.progress_updated 信号"""
        # 检查当前是否在创建页面（页面 1）
        if self.stack.currentIndex() == 1:
            self.lbl_progress.setText(text)
            return

        # 检测页面的处理
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

    def _on_install_error(self, message):
        self.lbl_error.setText(message)
        self.lbl_error.setVisible(bool(message))

    def _on_create_done(self, success):
        self.btn_create.setEnabled(True)
        self.btn_back.setEnabled(True)
        self.dir_edit.setReadOnly(False)
        self.create_progress.setVisible(False)

        if success:
            self.lbl_error.setVisible(False)
            self.lbl_progress.setText("环境创建完成！")
            self.lbl_progress.setStyleSheet("font-size: 13px; color: #2da44e; margin-top: 8px;")
            # 直接跳到版本选择页
            self.stack.setCurrentIndex(2)
        else:
            self.lbl_progress.setStyleSheet("font-size: 13px; color: #cf222e; margin-top: 8px;")
            self.lbl_progress.setText("环境创建失败，请查看下方错误详情后重试。")

    # ------------------------------------------------------------------ #
    #  页面 2: 版本选择
    # ------------------------------------------------------------------ #

    def _init_select_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(20)

        title = QLabel("选择部署版本")
        title.setStyleSheet("font-size: 22px; font-weight: bold; color: #24292f;")
        title.setWordWrap(True)
        layout.addWidget(title)

        desc = QLabel("请选择要部署的 Nekro Agent 版本:")
        desc.setStyleSheet("font-size: 14px; color: #57606a;")
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
        self.stack.addWidget(page)

    def _create_mode_card(self, title, desc, mode):
        card = QPushButton()
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setMinimumHeight(104)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        card.setStyleSheet(
            "QPushButton { background-color: #ffffff; border: 2px solid #d0d7de; "
            "border-radius: 10px; padding: 15px 20px; }"
            "QPushButton:hover { border-color: #0969da; background-color: #f6f8fa; }"
        )

        inner = QVBoxLayout(card)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(4)
        inner.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lbl_title = QLabel(title)
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_title.setWordWrap(True)
        lbl_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #24292f; "
                                "background: transparent; border: none;")
        lbl_title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lbl_desc = QLabel(desc)
        lbl_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet("font-size: 12px; color: #57606a; "
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
        self.stack.setCurrentIndex(3)  # 跳转到数据目录配置页

    # ------------------------------------------------------------------ #
    #  页面 3: 数据目录配置
    # ------------------------------------------------------------------ #

    def _init_datadir_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(18)

        title = QLabel("配置数据目录")
        title.setStyleSheet("font-size: 22px; font-weight: bold; color: #24292f;")
        title.setWordWrap(True)
        layout.addWidget(title)

        desc = QLabel("数据目录用于存储 Nekro Agent 运行数据（数据库、配置、日志等）。")
        desc.setStyleSheet("font-size: 14px; color: #57606a;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        windows_path = self.backend.get_host_access_path("/root/nekro_agent_data")
        lbl_winpath = QLabel("Windows 侧访问路径:")
        lbl_winpath.setStyleSheet("font-size: 14px; font-weight: 600; color: #24292f; margin-top: 10px;")
        layout.addWidget(lbl_winpath)

        self.datadir_path_card = QLabel(windows_path or r"\\wsl$\NekroAgent\root\nekro_agent_data")
        self.datadir_path_card.setStyleSheet(
            "font-size: 13px; color: #24292f; background: #f6f8fa; "
            "border: 1px solid #d0d7de; border-radius: 6px; padding: 10px; margin-top: 2px;"
        )
        self.datadir_path_card.setWordWrap(True)
        self.datadir_path_card.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.datadir_path_card)

        hint = QLabel("数据目录固定存储于 WSL 运行环境内部，可通过上方路径在 Windows 文件管理器中直接访问。")
        hint.setStyleSheet("font-size: 12px; color: #8b949e;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 端口配置
        lbl_ports = QLabel("端口配置:")
        lbl_ports.setStyleSheet("font-size: 14px; font-weight: 600; color: #24292f; margin-top: 10px;")
        layout.addWidget(lbl_ports)

        port_row1 = QHBoxLayout()
        port_row1.addWidget(QLabel("Nekro Agent 端口:"))
        self.nekro_port_edit = QLineEdit(str(self.config.get("nekro_port") or 8021))
        self.nekro_port_edit.setFixedWidth(100)
        self.nekro_port_edit.setStyleSheet(
            "padding: 6px; border: 1px solid #d0d7de; border-radius: 6px; "
            "background: white; font-size: 13px;"
        )
        port_row1.addWidget(self.nekro_port_edit)
        port_row1.addStretch()
        layout.addLayout(port_row1)

        port_row2 = QHBoxLayout()
        port_row2.addWidget(QLabel("NapCat 端口:"))
        self.napcat_port_edit = QLineEdit(str(self.config.get("napcat_port") or 6099))
        self.napcat_port_edit.setFixedWidth(100)
        self.napcat_port_edit.setStyleSheet(
            "padding: 6px; border: 1px solid #d0d7de; border-radius: 6px; "
            "background: white; font-size: 13px;"
        )
        port_row2.addWidget(self.napcat_port_edit)
        port_row2.addStretch()
        layout.addLayout(port_row2)

        port_hint = QLabel("如无特殊需求保持默认即可。端口冲突时可修改。")
        port_hint.setStyleSheet("font-size: 12px; color: #8b949e;")
        layout.addWidget(port_hint)

        layout.addStretch()

        # 底部按钮
        btn_box = QHBoxLayout()

        btn_back = QPushButton("返回")
        btn_back.setFixedHeight(38)
        btn_back.setFixedWidth(80)
        btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_back.setStyleSheet(
            "QPushButton { background-color: #f3f4f6; color: #24292f; border: 1px solid #d0d7de; "
            "border-radius: 6px; font-size: 14px; font-weight: 600; }"
            "QPushButton:hover { background-color: #e8e9eb; }"
        )
        btn_back.clicked.connect(lambda: self.stack.setCurrentIndex(2))
        btn_box.addWidget(btn_back)

        btn_box.addStretch()

        btn_deploy = QPushButton("开始部署")
        btn_deploy.setFixedHeight(38)
        btn_deploy.setFixedWidth(120)
        btn_deploy.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_deploy.setStyleSheet(
            "QPushButton { background-color: #2da44e; color: white; border: none; "
            "border-radius: 6px; font-size: 14px; font-weight: 600; }"
            "QPushButton:hover { background-color: #28943f; }"
        )
        btn_deploy.clicked.connect(self._confirm_datadir)
        btn_box.addWidget(btn_deploy)

        layout.addLayout(btn_box)
        self.stack.addWidget(page)

    def _confirm_datadir(self):
        data_dir = "/root/nekro_agent_data"

        # 校验端口
        try:
            nekro_port = int(self.nekro_port_edit.text().strip())
            napcat_port = int(self.napcat_port_edit.text().strip())
            if not (1 <= nekro_port <= 65535) or not (1 <= napcat_port <= 65535):
                raise ValueError
        except ValueError:
            self._show_notice_dialog("提示", "端口号必须为 1-65535 之间的整数")
            return

        if self.config:
            self.config.set("data_dir", data_dir)
            self.config.set("nekro_port", nekro_port)
            self.config.set("napcat_port", napcat_port)
            self.config.set("first_run", False)
        self.deploy_requested.emit(self._selected_mode)
        self.accept()

    def _check_item_labels(self):
        return (
            self.backend.display_name,
            "Nekro Agent 运行环境",
            "Docker",
            "Docker Compose",
        )
