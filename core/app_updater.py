"""
启动器自身更新检测与下载模块。

通过 GitHub Release API 检查新版本，使用国内加速镜像下载安装包。
"""

import os
import re
import tempfile
from urllib.parse import urlparse

import requests
from PyQt6.QtCore import QObject, pyqtSignal

APP_VERSION = "1.0.0"

GITHUB_REPO = "NekroAI/nekro-agent-for-windows"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

GITHUB_MIRROR_PREFIXES = [
    "https://ghfast.top/",
    "https://gh-proxy.com/",
    "https://mirror.ghproxy.com/",
]

REQUEST_TIMEOUT = 20
DOWNLOAD_CHUNK_SIZE = 65536

ASSET_PATTERN = re.compile(r"NekroAgent.*Setup.*\.exe", re.IGNORECASE)


def _parse_version(tag: str) -> tuple:
    """将 'v1.2.3' 或 '1.2.3' 解析为可比较的元组。"""
    tag = tag.lstrip("vV")
    parts = []
    for seg in tag.split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(remote_tag: str) -> bool:
    return _parse_version(remote_tag) > _parse_version(APP_VERSION)


def _try_github_api(url: str) -> dict | None:
    """依次尝试直连和镜像获取 Release JSON。"""
    candidates = [url]
    for prefix in GITHUB_MIRROR_PREFIXES:
        candidates.append(prefix + url)

    for candidate in candidates:
        try:
            resp = requests.get(
                candidate,
                timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/vnd.github+json"},
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            continue
    return None


def check_update() -> dict | None:
    """检查是否有新版本。

    返回 dict: {tag, name, body, published_at, download_url, file_name, file_size}
    无新版本或请求失败返回 None。
    """
    data = _try_github_api(GITHUB_API_URL)
    if not data:
        return None

    tag = data.get("tag_name", "")
    if not tag or not is_newer(tag):
        return None

    asset_url = ""
    asset_name = ""
    asset_size = 0
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if ASSET_PATTERN.match(name):
            asset_url = asset.get("browser_download_url", "")
            asset_name = name
            asset_size = asset.get("size", 0)
            break

    return {
        "tag": tag,
        "name": data.get("name", tag),
        "body": data.get("body", ""),
        "published_at": data.get("published_at", ""),
        "download_url": asset_url,
        "file_name": asset_name,
        "file_size": asset_size,
    }


def _accelerated_download_url(original_url: str) -> list[str]:
    """生成加速 URL 列表：镜像优先，原始兜底。"""
    urls = []
    for prefix in GITHUB_MIRROR_PREFIXES:
        urls.append(prefix + original_url)
    urls.append(original_url)
    return urls


class DownloadWorker(QObject):
    """后台下载线程，带进度上报。"""

    progress = pyqtSignal(int, int)      # downloaded_bytes, total_bytes
    finished = pyqtSignal(bool, str)     # success, file_path_or_error
    mirror_info = pyqtSignal(str)        # 正在使用的镜像域名

    def __init__(self, download_url: str, file_name: str, parent=None):
        super().__init__(parent)
        self._download_url = download_url
        self._file_name = file_name
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        urls = _accelerated_download_url(self._download_url)
        dest_dir = os.path.join(
            os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
            "NekroAgent", "updates",
        )
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, self._file_name)

        for url in urls:
            if self._cancelled:
                self.finished.emit(False, "已取消")
                return

            try:
                domain = urlparse(url).netloc
                self.mirror_info.emit(domain)

                resp = requests.get(url, stream=True, timeout=REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    continue

                total = int(resp.headers.get("content-length", 0))
                downloaded = 0

                with open(dest_path, "wb") as f:
                    for chunk in resp.iter_content(DOWNLOAD_CHUNK_SIZE):
                        if self._cancelled:
                            f.close()
                            self._safe_remove(dest_path)
                            self.finished.emit(False, "已取消")
                            return
                        f.write(chunk)
                        downloaded += len(chunk)
                        self.progress.emit(downloaded, total)

                self.finished.emit(True, dest_path)
                return

            except Exception:
                self._safe_remove(dest_path)
                continue

        self.finished.emit(False, "所有下载源均失败，请检查网络连接后重试")

    @staticmethod
    def _safe_remove(path):
        try:
            os.remove(path)
        except OSError:
            pass


class UpdateChecker(QObject):
    """在后台线程中检查更新，完成后发信号。"""

    update_available = pyqtSignal(dict)
    check_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self):
        try:
            result = check_update()
            if result:
                self.update_available.emit(result)
        except Exception:
            pass
        finally:
            self.check_finished.emit()
