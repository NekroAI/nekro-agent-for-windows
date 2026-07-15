"""Thin wrapper around QtWebView2Widget providing a QWebEngineView-like API."""

import json
import os

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import QSizePolicy, QWidget

os.environ.setdefault("QT_API", "pyqt6")

from qtwebview2 import QtWebView2Widget  # noqa: E402


def _data_folder(subfolder="default"):
    from core.config_manager import get_app_data_dir
    root = os.path.join(get_app_data_dir(), "webview2_data")
    path = os.path.join(root, subfolder)
    os.makedirs(path, exist_ok=True)
    return path


_NAV_HOOK_JS = r"""
(function() {
    if (window.__navHookInstalled) return;
    window.__navHookInstalled = true;
    function notify() {
        try {
            var payload = JSON.stringify({url: location.href, title: document.title || ''});
            if (window.pywebview && window.pywebview.api && window.pywebview.api.on_nav_change) {
                window.pywebview.api.on_nav_change(payload);
            }
        } catch(e) {}
    }
    var origPush = history.pushState;
    var origReplace = history.replaceState;
    history.pushState = function() { origPush.apply(this, arguments); notify(); };
    history.replaceState = function() { origReplace.apply(this, arguments); notify(); };
    window.addEventListener('popstate', notify);
    window.addEventListener('hashchange', notify);
    new MutationObserver(function() {
        notify();
    }).observe(document.querySelector('title') || document.head, {childList: true, subtree: true, characterData: true});
    notify();
})();
"""

# WebView2 只暴露普通 Reload（相当于 F5），没有忽略缓存的变体；
# 强刷通过 JS 实现：清掉 CacheStorage，再以 cache:'reload' 重新拉取当前
# 文档绕过 HTTP 缓存，最后 location.reload() 取到新文档。兜底定时器保证
# 即使 fetch 挂掉也一定会刷新。
_BYPASS_CACHE_RELOAD_JS = r"""
(function() {
    var reloaded = false;
    function doReload() {
        if (reloaded) return;
        reloaded = true;
        try { location.reload(); } catch (e) {}
    }
    setTimeout(doReload, 3000);
    var clearCaches = Promise.resolve();
    try {
        if (window.caches && caches.keys) {
            clearCaches = caches.keys().then(function(keys) {
                return Promise.all(keys.map(function(key) { return caches.delete(key); }));
            });
        }
    } catch (e) {}
    clearCaches.catch(function() {}).then(function() {
        var refetch;
        try {
            refetch = fetch(location.href, {cache: 'reload', credentials: 'include'});
        } catch (e) {
            doReload();
            return;
        }
        refetch.catch(function() {}).then(doReload);
    });
})();
"""


class WebViewWidget(QWidget):
    """Drop-in replacement for QWebEngineView using WebView2 via qtwebview2."""

    urlChanged = pyqtSignal(str)
    titleChanged = pyqtSignal(str)
    init_failed = pyqtSignal(str)

    def __init__(self, parent=None, data_subfolder="default"):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(200)
        self.setStyleSheet("background: #ffffff;")

        self._current_url = ""
        self._current_title = ""
        self._browser_target = None
        self._ready = False
        self._pending_url = None
        self._pending_html = None

        from PyQt6.QtWidgets import QVBoxLayout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._fill_callback = None

        js_api = {
            "on_nav_change": self._on_nav_change,
            "on_fill_result": self._on_fill_result,
        }

        self._webview = QtWebView2Widget(
            parent=self,
            user_data_folder=_data_folder(data_subfolder),
            handle_new_window=False,
            context_menus=False,
            background_color="#ffffff",
            lazyload=False,
            js_apis=js_api,
        )
        layout.addWidget(self._webview, 1)

        self._webview.bridge.initialization_done.connect(self._on_init_done)
        self._webview.bridge.domContentLoaded.connect(self._on_dom_loaded)

    def _on_init_done(self, success, error_msg=""):
        if success:
            self._ready = True
            if self._pending_html is not None:
                html = self._pending_html
                self._pending_html = None
                self.load_html(html)
            elif self._pending_url is not None:
                url = self._pending_url
                self._pending_url = None
                self.load_url(url)
        else:
            self.init_failed.emit(error_msg or "WebView2 初始化失败")

    def _on_dom_loaded(self):
        self._inject_nav_hook()

    def _inject_nav_hook(self):
        try:
            self._webview.evaluate_js(_NAV_HOOK_JS)
        except Exception:
            pass

    def _on_nav_change(self, payload_json):
        """Called from JS via pywebview.api bridge."""
        try:
            msg = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError):
            return
        new_url = msg.get("url", "")
        new_title = msg.get("title", "")
        if new_url and new_url != self._current_url:
            self._current_url = new_url
            QTimer.singleShot(0, lambda u=new_url: self.urlChanged.emit(u))
        if new_title != self._current_title:
            self._current_title = new_title
            QTimer.singleShot(0, lambda t=new_title: self.titleChanged.emit(t))

    def _on_fill_result(self, result_json):
        """Called from JS via pywebview.api bridge after credential fill."""
        cb = self._fill_callback
        self._fill_callback = None
        if cb:
            QTimer.singleShot(0, lambda: cb(result_json))

    def register_fill_callback(self, callback):
        self._fill_callback = callback

    # -- Public API --

    def load_url(self, url):
        self._current_url = url
        if not self._ready:
            self._pending_url = url
            self._pending_html = None
            return
        try:
            self._webview.load_url(url)
        except Exception:
            pass

    def load_html(self, html):
        if not self._ready:
            self._pending_html = html
            self._pending_url = None
            return
        try:
            self._webview.load_html(html)
        except Exception:
            pass

    def reload(self, bypass_cache=False):
        if not self._ready:
            return
        if bypass_cache and self._current_url.startswith(("http://", "https://")):
            try:
                self._webview.evaluate_js(_BYPASS_CACHE_RELOAD_JS)
                return
            except Exception:
                pass
        try:
            self._webview.reload()
        except Exception:
            pass

    def get_url(self):
        return self._current_url

    def get_title(self):
        return self._current_title

    def go_back(self):
        if self._ready:
            self.evaluate_js("history.back()")

    def go_forward(self):
        if self._ready:
            self.evaluate_js("history.forward()")

    def evaluate_js(self, script, callback=None):
        if not self._ready:
            if callback:
                QTimer.singleShot(0, lambda: callback(None))
            return
        try:
            self._webview.evaluate_js(script)
            if callback:
                QTimer.singleShot(50, lambda: callback(None))
        except Exception:
            if callback:
                QTimer.singleShot(0, lambda: callback(None))

    def clear_data(self):
        """Best-effort cache/cookie clear via JS."""
        if self._ready:
            try:
                self.evaluate_js("""
                    try { caches.keys().then(k => k.forEach(n => caches.delete(n))); } catch(e) {}
                    try { localStorage.clear(); } catch(e) {}
                    try { sessionStorage.clear(); } catch(e) {}
                """)
            except Exception:
                pass
