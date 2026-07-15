"""
启动器自身版本更新弹窗。

显示新版本信息、更新日志，并支持一键下载安装包后自动运行安装程序。
"""

import os
import re
import subprocess

from PyQt6.QtCore import Qt, QThread
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from core.app_updater import APP_VERSION, DownloadWorker
from ui.styles import STYLESHEET


STATUS_INFO_STYLE = "font-size: 12px; color: #7a8d9f;"
STATUS_ERROR_STYLE = "font-size: 12px; color: #e26050;"

# 对话框关闭后，网络请求仍可能等到超时才返回。这里在应用进程级持有
# 已解除 parent 的线程和 worker，直到线程真正结束，避免 QThread 被提前销毁。
_DETACHED_DOWNLOADS = []
_DOWNLOAD_SHUTDOWN_HOOKED = False


def _shutdown_downloads(wait_ms=750):
    """应用退出时确保下载线程停止，避免 QThread 在运行中被析构。"""
    pairs = list(_DETACHED_DOWNLOADS)
    for worker, thread in pairs:
        if thread.isRunning():
            worker.cancel()
            thread.requestInterruption()
            thread.quit()

    for _worker, thread in pairs:
        if not thread.isRunning() or thread.wait(wait_ms):
            continue
        # 网络调用可能仍阻塞到 requests 超时。这里只在 QApplication 已进入
        # aboutToQuit 后兜底终止；普通对话框关闭绝不强制终止线程。
        thread.terminate()
        thread.wait(wait_ms)


def _ensure_download_shutdown_hook():
    global _DOWNLOAD_SHUTDOWN_HOOKED
    if _DOWNLOAD_SHUTDOWN_HOOKED:
        return
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        app.aboutToQuit.connect(_shutdown_downloads)
        _DOWNLOAD_SHUTDOWN_HOOKED = True


def _retain_download_until_finished(worker, thread):
    pair = (worker, thread)
    if pair in _DETACHED_DOWNLOADS:
        return
    _DETACHED_DOWNLOADS.append(pair)
    _ensure_download_shutdown_hook()

    def release():
        try:
            _DETACHED_DOWNLOADS.remove(pair)
        except ValueError:
            pass
        thread.deleteLater()

    thread.finished.connect(release)


def _md_to_html(md: str) -> str:
    """将 GitHub Release 常见 Markdown 转为 Qt 富文本 HTML。

    支持：标题(h1-h3)、粗体、斜体、行内代码、代码块、
    无序/有序列表、链接、水平线、段落。
    """
    html_lines: list[str] = []
    lines = md.replace("\r\n", "\n").split("\n")
    in_code_block = False
    in_list = False
    list_tag = ""
    i = 0

    def _flush_list():
        nonlocal in_list, list_tag
        if in_list:
            html_lines.append(f"</{list_tag}>")
            in_list = False
            list_tag = ""

    while i < len(lines):
        line = lines[i]

        if line.startswith("```"):
            if not in_code_block:
                _flush_list()
                in_code_block = True
                html_lines.append(
                    '<pre style="background:#f0f4f8; border:1px solid #dfe7ef; '
                    'border-radius:6px; padding:10px; font-size:12px; '
                    'font-family:Consolas,monospace; white-space:pre-wrap;">'
                )
            else:
                in_code_block = False
                html_lines.append("</pre>")
            i += 1
            continue

        if in_code_block:
            escaped = (
                line.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            html_lines.append(escaped)
            i += 1
            continue

        stripped = line.strip()

        if not stripped:
            _flush_list()
            i += 1
            continue

        if re.match(r"^---+$|^\*\*\*+$|^___+$", stripped):
            _flush_list()
            html_lines.append('<hr style="border:none; border-top:1px solid #e7eef5; margin:8px 0;">')
            i += 1
            continue

        m_heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if m_heading:
            _flush_list()
            level = len(m_heading.group(1))
            sizes = {1: "16px", 2: "14px", 3: "13px"}
            text = _inline_format(m_heading.group(2))
            html_lines.append(
                f'<p style="font-size:{sizes.get(level, "13px")}; '
                f'font-weight:700; color:#264057; margin:10px 0 4px 0;">{text}</p>'
            )
            i += 1
            continue

        m_ul = re.match(r"^[-*+]\s+(.+)$", stripped)
        m_ol = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if m_ul or m_ol:
            tag = "ul" if m_ul else "ol"
            content = _inline_format((m_ul or m_ol).group(1))
            if not in_list or list_tag != tag:
                _flush_list()
                in_list = True
                list_tag = tag
                html_lines.append(
                    f'<{tag} style="margin:4px 0 4px 18px; padding:0;">'
                )
            html_lines.append(f"<li>{content}</li>")
            i += 1
            continue

        _flush_list()
        html_lines.append(f"<p style=\"margin:4px 0;\">{_inline_format(stripped)}</p>")
        i += 1

    if in_code_block:
        html_lines.append("</pre>")
    _flush_list()

    return "\n".join(html_lines)


def _inline_format(text: str) -> str:
    """处理行内 Markdown 格式。"""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    code_style = (
        "background:#f0f4f8; padding:1px 5px; border-radius:3px; "
        "font-size:12px; font-family:Consolas,monospace;"
    )
    text = re.sub(r"`([^`]+)`", rf'<code style="{code_style}">\1</code>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"_(.+?)_", r"<i>\1</i>", text)
    def _safe_link(m):
        label, url = m.group(1), m.group(2)
        if not url.startswith(("http://", "https://")):
            return label
        return f'<a href="{url}" style="color:#0969da; text-decoration:none;">{label}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _safe_link, text)
    return text


class AppUpdateDialog(QDialog):
    """启动器新版本通知 & 下载弹窗。"""

    def __init__(self, parent, update_info: dict):
        super().__init__(parent)
        self._info = update_info
        self._download_worker = None
        self._download_thread = None
        self._launch_timer_id = None

        self.setWindowTitle("发现新版本")
        self.setMinimumWidth(480)
        self.setMaximumWidth(600)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.setStyleSheet(STYLESHEET)

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)

        title = QLabel("发现新版本")
        title.setProperty("role", "dialog_title")
        layout.addWidget(title)

        version_row = QHBoxLayout()
        version_row.setSpacing(8)

        current_badge = QLabel(f"当前 v{APP_VERSION}")
        current_badge.setObjectName("UpdateBadgeCurrent")
        current_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        version_row.addWidget(current_badge)

        arrow = QLabel("→")
        arrow.setStyleSheet("color: #8fa3b4; font-size: 16px; font-weight: 700;")
        version_row.addWidget(arrow)

        new_tag = self._info.get("tag", "")
        new_badge = QLabel(f"新版 {new_tag}")
        new_badge.setObjectName("UpdateBadgeNew")
        new_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        version_row.addWidget(new_badge)

        version_row.addStretch()
        layout.addLayout(version_row)

        release_name = self._info.get("name", "")
        if release_name and release_name != new_tag:
            name_label = QLabel(release_name)
            name_label.setStyleSheet("font-size: 14px; font-weight: 600; color: #264057;")
            name_label.setWordWrap(True)
            layout.addWidget(name_label)

        body = self._info.get("body", "").strip()
        if body:
            changelog_label = QLabel("更新日志")
            changelog_label.setStyleSheet(
                "font-size: 12px; font-weight: 600; color: #8fa3b4; "
                "text-transform: uppercase; letter-spacing: 1px;"
            )
            layout.addWidget(changelog_label)

            body_browser = QTextBrowser()
            body_browser.setObjectName("UpdateChangelog")
            body_browser.setOpenExternalLinks(True)
            body_browser.setMaximumHeight(220)
            body_browser.setFrameShape(QFrame.Shape.NoFrame)
            body_browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            body_browser.setStyleSheet(
                "QTextBrowser#UpdateChangelog {"
                "  background: #fbfdff; border: 1px solid #e7eef5; border-radius: 8px;"
                "  padding: 14px; font-size: 13px; color: #3d5366;"
                "}"
            )
            body_browser.setHtml(_md_to_html(body))
            layout.addWidget(body_browser)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #e7eef5;")
        layout.addWidget(sep)

        self._progress_container = QVBoxLayout()
        self._progress_container.setSpacing(6)

        progress_header = QHBoxLayout()
        progress_header.setSpacing(8)
        self._mirror_label = QLabel("")
        self._mirror_label.setStyleSheet("font-size: 12px; color: #8fa3b4;")
        progress_header.addWidget(self._mirror_label)
        progress_header.addStretch()
        self._size_label = QLabel("")
        self._size_label.setStyleSheet("font-size: 12px; color: #8fa3b4;")
        progress_header.addWidget(self._size_label)
        self._progress_container.addLayout(progress_header)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setFixedHeight(8)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setStyleSheet(
            "QProgressBar { border: none; background: #e8e9eb; border-radius: 4px; }"
            "QProgressBar::chunk { background: #3db6d1; border-radius: 4px; }"
        )
        self._progress_container.addWidget(self._progress_bar)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(STATUS_INFO_STYLE)
        self._status_label.setWordWrap(True)
        self._progress_container.addWidget(self._status_label)

        self._set_progress_visible(False)
        layout.addLayout(self._progress_container)

        button_row = QHBoxLayout()
        button_row.setSpacing(10)

        self._btn_skip = QPushButton("跳过此版本")
        self._btn_skip.clicked.connect(self._on_skip)
        button_row.addWidget(self._btn_skip)

        button_row.addStretch()

        self._btn_later = QPushButton("稍后提醒")
        self._btn_later.clicked.connect(self.reject)
        button_row.addWidget(self._btn_later)

        self._btn_download = QPushButton("下载更新")
        self._btn_download.setProperty("role", "primary")
        self._btn_download.setStyleSheet(
            "QPushButton { background: #e88478; border-color: #e88478; color: #fff; font-weight: 700; }"
            "QPushButton:hover { background: #d6736a; border-color: #d6736a; }"
            "QPushButton:disabled { background: #f0c4be; border-color: #f0c4be; color: #fff; }"
        )

        if not self._info.get("download_url"):
            self._btn_download.setEnabled(False)
            self._btn_download.setText("无安装包")
        self._btn_download.clicked.connect(self._on_download)
        button_row.addWidget(self._btn_download)

        layout.addLayout(button_row)

    def _set_progress_visible(self, visible: bool):
        self._progress_bar.setVisible(visible)
        self._mirror_label.setVisible(visible)
        self._size_label.setVisible(visible)
        self._status_label.setVisible(visible)

    def _on_skip(self):
        self.done(2)

    def _on_download(self):
        url = self._info.get("download_url", "")
        name = self._info.get("file_name", "NekroAgent-Setup.exe")
        if not url:
            return

        self._cleanup_download_thread()

        self._btn_download.setEnabled(False)
        self._btn_download.setText("下载中...")
        self._btn_skip.setEnabled(False)
        self._btn_later.setEnabled(False)
        self._set_progress_visible(True)
        self._progress_bar.setRange(0, 0)
        self._status_label.setStyleSheet(STATUS_INFO_STYLE)
        self._status_label.setText("正在连接下载源...")

        expected_size = self._info.get("file_size", 0)
        if not isinstance(expected_size, int):
            expected_size = 0
        expected_sha256 = self._info.get("file_sha256", "")
        self._download_worker = DownloadWorker(
            url,
            name,
            expected_size,
            str(expected_sha256 or ""),
        )
        self._download_thread = QThread(self)
        self._download_worker.moveToThread(self._download_thread)

        self._download_worker.progress.connect(self._on_progress)
        self._download_worker.mirror_info.connect(self._on_mirror_info)
        self._download_worker.finished.connect(self._on_download_finished)
        self._download_worker.finished.connect(self._download_worker.deleteLater)
        self._download_worker.finished.connect(self._download_thread.quit)
        self._download_thread.started.connect(self._download_worker.run)
        _retain_download_until_finished(self._download_worker, self._download_thread)

        self._download_thread.start()

    def _on_mirror_info(self, domain: str):
        self._mirror_label.setText(f"源: {domain}")

    def _on_progress(self, downloaded: int, total: int):
        if total > 0:
            self._progress_bar.setRange(0, 100)
            pct = int(downloaded * 100 / total)
            self._progress_bar.setValue(pct)
            self._size_label.setText(f"{_fmt_size(downloaded)} / {_fmt_size(total)}")
            self._status_label.setStyleSheet(STATUS_INFO_STYLE)
            self._status_label.setText(f"正在下载... {pct}%")
        else:
            self._progress_bar.setRange(0, 0)
            self._size_label.setText(_fmt_size(downloaded))
            self._status_label.setStyleSheet(STATUS_INFO_STYLE)
            self._status_label.setText("正在下载...")

    def _cleanup_download_thread(self):
        worker = self._download_worker
        thread = self._download_thread
        self._download_worker = None
        self._download_thread = None
        if not worker or not thread:
            return

        if thread.isRunning():
            worker.cancel()
            connections = (
                (worker.progress, self._on_progress),
                (worker.mirror_info, self._on_mirror_info),
                (worker.finished, self._on_download_finished),
            )
            for signal, slot in connections:
                try:
                    signal.disconnect(slot)
                except TypeError:
                    pass
            thread.setParent(None)
            _retain_download_until_finished(worker, thread)
            return

        worker.deleteLater()
        thread.deleteLater()

    def _on_download_finished(self, success: bool, result: str):
        if self._download_thread is None:
            # 取消清理后仍可能收到已入队的完成信号，直接忽略
            return
        worker = self._download_worker
        thread = self._download_thread
        self._download_worker = None
        self._download_thread = None
        thread.setParent(None)
        _retain_download_until_finished(worker, thread)

        if success:
            self._progress_bar.setRange(0, 100)
            self._progress_bar.setValue(100)
            self._status_label.setStyleSheet(STATUS_INFO_STYLE)
            self._status_label.setText("下载完成！正在启动安装程序...")
            self._btn_download.setText("下载完成")

            self._launch_timer_id = self.startTimer(800)
            self._pending_installer_path = result
        else:
            self._progress_bar.setRange(0, 100)
            self._progress_bar.setValue(0)
            self._status_label.setText(result)
            self._status_label.setStyleSheet(STATUS_ERROR_STYLE)
            self._btn_download.setEnabled(True)
            self._btn_download.setText("重新下载")
            self._btn_skip.setEnabled(True)
            self._btn_later.setEnabled(True)

    def timerEvent(self, event):
        if event.timerId() == self._launch_timer_id:
            self.killTimer(self._launch_timer_id)
            self._launch_timer_id = None
            path = getattr(self, "_pending_installer_path", None)
            if path:
                self._launch_installer(path)
        else:
            super().timerEvent(event)

    def _launch_installer(self, installer_path: str):
        try:
            if not os.path.isfile(installer_path):
                raise FileNotFoundError(installer_path)
            subprocess.Popen(
                [installer_path],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            if app:
                app.quit()
        except Exception as e:
            self._status_label.setText(f"启动启动器更新安装程序失败: {e}")
            self._status_label.setStyleSheet(STATUS_ERROR_STYLE)
            self._btn_download.setEnabled(True)
            self._btn_download.setText("重新下载")
            self._btn_skip.setEnabled(True)
            self._btn_later.setEnabled(True)

    def reject(self):
        if self._launch_timer_id is not None:
            self.killTimer(self._launch_timer_id)
            self._launch_timer_id = None
        self._cleanup_download_thread()
        super().reject()


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    else:
        return f"{n / (1024 * 1024):.1f} MB"
