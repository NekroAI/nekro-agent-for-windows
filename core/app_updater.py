"""
启动器自身更新检测与下载模块。

通过 GitHub Release API 检查新版本，使用国内加速镜像下载安装包。
"""

import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from PyQt6.QtCore import QObject, pyqtSignal

_FALLBACK_VERSION = "1.0.0"


def _read_version() -> str:
    """从打包资源或项目根目录的 version.txt 读取版本号。"""
    candidates = []
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.join(base, "version.txt"))
    candidates.append(os.path.join(base, "data", "version.txt"))

    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                ver = f.read().strip()
                if ver:
                    return ver
        except OSError:
            continue
    return _FALLBACK_VERSION


APP_VERSION = _read_version()

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


@dataclass(frozen=True)
class UpdateCheckResult:
    """启动器更新检查结果。"""

    status: str
    update_info: dict | None = None
    message: str = ""
    detail: str = ""


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


def _short_error(error: Exception) -> str:
    message = str(error).strip()
    if not message:
        return type(error).__name__
    if len(message) > 120:
        message = message[:117] + "..."
    return f"{type(error).__name__}: {message}"


def _try_github_api(url: str) -> tuple[dict | None, list[str]]:
    """依次尝试直连和镜像获取 Release JSON。"""
    candidates = [url]
    for prefix in GITHUB_MIRROR_PREFIXES:
        candidates.append(prefix + url)

    failures: list[str] = []
    for candidate in candidates:
        domain = urlparse(candidate).netloc or candidate
        try:
            with requests.get(
                candidate,
                timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/vnd.github+json"},
            ) as resp:
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        return data, failures
                    failures.append(f"{domain}: 响应格式无效")
                    continue
                failures.append(f"{domain}: HTTP {resp.status_code}")
        except (requests.RequestException, ValueError) as e:
            failures.append(f"{domain}: {_short_error(e)}")
            continue
    return None, failures


def _compact_failures(failures: list[str], limit: int = 4) -> str:
    if not failures:
        return ""
    selected = failures[-limit:]
    return "；".join(selected)


def _source_names(urls: list[str]) -> str:
    names = []
    for url in urls:
        domain = urlparse(url).netloc or url
        if domain not in names:
            names.append(domain)
    return "、".join(names)


def check_update() -> UpdateCheckResult:
    """检查是否有新版本。

    返回 UpdateCheckResult，区分发现更新、已是最新版和检查失败。
    """
    data, failures = _try_github_api(GITHUB_API_URL)
    if not data:
        detail = _compact_failures(failures) or "GitHub Release API 未返回可用数据"
        return UpdateCheckResult(
            status="failed",
            message="启动器更新检查失败，请检查网络后重试。",
            detail=detail,
        )

    tag = str(data.get("tag_name", ""))
    if not tag:
        return UpdateCheckResult(
            status="failed",
            message="启动器更新信息格式异常，未找到版本号。",
            detail="GitHub Release 数据缺少 tag_name。",
        )

    if not is_newer(tag):
        return UpdateCheckResult(status="latest")

    asset_url = ""
    asset_name = ""
    asset_size = 0
    assets = data.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name", ""))
            if ASSET_PATTERN.match(name):
                asset_url = str(asset.get("browser_download_url", ""))
                asset_name = name
                asset_size = asset.get("size", 0)
                break

    if not asset_url:
        return UpdateCheckResult(
            status="failed",
            message=f"发现启动器新版本 {tag}，但没有找到 Windows 安装包。",
            detail="Release 资产中未匹配 NekroAgent*Setup*.exe。",
        )

    return UpdateCheckResult(
        status="available",
        update_info={
            "tag": tag,
            "name": str(data.get("name", tag)),
            "body": str(data.get("body", "")),
            "published_at": str(data.get("published_at", "")),
            "download_url": asset_url,
            "file_name": asset_name,
            "file_size": asset_size,
        },
    )


def _accelerated_download_url(original_url: str) -> list[str]:
    """生成加速 URL 列表：镜像优先，原始兜底。"""
    urls = []
    for prefix in GITHUB_MIRROR_PREFIXES:
        urls.append(prefix + original_url)
    urls.append(original_url)
    return urls


def format_download_failure(urls: list[str], failures: list[str]) -> str:
    """生成简洁的启动器安装包下载失败文案。"""
    sources = _source_names(urls)
    detail = _compact_failures(failures, limit=6) or "无可用下载源"
    return (
        "启动器更新安装包下载失败，请检查网络、DNS 或代理后重试。\n"
        f"已尝试下载源：{sources}\n"
        f"详情：{detail}"
    )


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
        from core.config_manager import get_app_data_dir
        urls = _accelerated_download_url(self._download_url)
        dest_dir = os.path.join(get_app_data_dir(), "updates")
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, self._file_name)
        failures = []

        for url in urls:
            if self._cancelled:
                self.finished.emit(False, "已取消")
                return

            try:
                domain = urlparse(url).netloc
                self.mirror_info.emit(domain)

                with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT) as resp:
                    if resp.status_code != 200:
                        failures.append(f"{domain}: HTTP {resp.status_code}")
                        continue

                    total = int(resp.headers.get("content-length", 0))
                    downloaded = 0
                    cancelled = False

                    with open(dest_path, "wb") as f:
                        for chunk in resp.iter_content(DOWNLOAD_CHUNK_SIZE):
                            if self._cancelled:
                                cancelled = True
                                break
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded += len(chunk)
                            self.progress.emit(downloaded, total)

                    if cancelled:
                        self._safe_remove(dest_path)
                        self.finished.emit(False, "已取消")
                        return

                    if downloaded <= 0:
                        self._safe_remove(dest_path)
                        failures.append(f"{domain}: 未下载到数据")
                        continue

                self.finished.emit(True, dest_path)
                return

            except (OSError, requests.RequestException) as e:
                self._safe_remove(dest_path)
                domain = urlparse(url).netloc or url
                failures.append(f"{domain}: {_short_error(e)}")
                continue

        self.finished.emit(False, format_download_failure(urls, failures))

    @staticmethod
    def _safe_remove(path):
        try:
            os.remove(path)
        except OSError:
            pass


class UpdateChecker(QObject):
    """在后台线程中检查更新，完成后发信号。"""

    update_available = pyqtSignal(dict)
    check_failed = pyqtSignal(str, str)
    check_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self):
        try:
            result = check_update()
            if result.status == "available" and result.update_info:
                self.update_available.emit(result.update_info)
            elif result.status == "failed":
                self.check_failed.emit(result.message, result.detail)
        except Exception as e:
            self.check_failed.emit(
                "启动器更新检查失败，请检查网络后重试。",
                _short_error(e),
            )
        finally:
            self.check_finished.emit()
