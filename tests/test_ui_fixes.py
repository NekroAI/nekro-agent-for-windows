import inspect
import unittest
from unittest.mock import patch

from ui.main_window import MainWindow
from ui.webview_widget import _NAV_HOOK_JS


class _LineEdit:
    def __init__(self, value):
        self._value = value

    def text(self):
        return self._value


class _PortConfig:
    def __init__(self, save_result):
        self._save_result = save_result
        self.last_save_error = "配置目录只读"
        self.calls = []
        self.values = {
            "deploy_mode": "napcat",
            "nekro_port": 8021,
            "napcat_port": 6099,
            "deploy_info": {"port": "8021", "napcat_port": "6099"},
        }

    def get(self, key):
        return self.values.get(key)

    def get_active_instance_id(self):
        return "default"

    def list_instances(self):
        return {"default": {"nekro_port": 8021, "napcat_port": 6099}}

    def update_instance_with_globals(
        self,
        inst_id,
        instance_updates=None,
        global_updates=None,
    ):
        self.calls.append((inst_id, instance_updates, global_updates))
        return self._save_result


class _Backend:
    is_running = True


class _PortWindow:
    def __init__(self, save_result):
        self.config = _PortConfig(save_result)
        self.backend = _Backend()
        self.nekro_port_setting = _LineEdit("18021")
        self.napcat_port_setting = _LineEdit("16099")
        self.notices = []
        self.browser_urls = {}
        self.current_browser_target = "nekro"

    def _show_notice_dialog(self, title, text, **kwargs):
        self.notices.append((title, text, kwargs))

    def _current_webview(self):
        return None


class UIFixTests(unittest.TestCase):
    def test_webview_scripts_use_qtwebview2_bridge(self):
        self.assertIn("window.qtwebview2.api.on_nav_change", _NAV_HOOK_JS)
        self.assertNotIn("window.pywebview", _NAV_HOOK_JS)

        fill_source = inspect.getsource(MainWindow._fill_browser_credentials)
        self.assertIn("window.qtwebview2.api.on_fill_result", fill_source)
        self.assertNotIn("window.pywebview", fill_source)

    def test_preview_backup_size_is_requested_off_the_ui_thread(self):
        switch_source = inspect.getsource(MainWindow._switch_to_preview_build)
        worker_source = inspect.getsource(MainWindow._request_preview_backup_size)

        self.assertNotIn("get_backup_size_hint", switch_source)
        self.assertIn("threading.Thread", worker_source)
        self.assertIn("daemon=True", worker_source)
        self.assertIn("notifier.finished.emit", worker_source)

    @patch("ui.main_window.validate_instance_port_conflicts", return_value=(True, ""))
    @patch("ui.main_window.validate_port_bindings", return_value=(True, ""))
    def test_save_ports_submits_globals_and_instance_once(
        self,
        _validate_bindings,
        _validate_instances,
    ):
        window = _PortWindow(save_result=False)

        MainWindow._save_ports(window)

        self.assertEqual(len(window.config.calls), 1)
        inst_id, instance_updates, global_updates = window.config.calls[0]
        self.assertEqual(inst_id, "default")
        self.assertEqual(instance_updates["nekro_port"], 18021)
        self.assertEqual(instance_updates["napcat_port"], 16099)
        self.assertEqual(global_updates["nekro_port"], 18021)
        self.assertEqual(global_updates["napcat_port"], 16099)
        self.assertEqual(global_updates["deploy_info"]["port"], "18021")
        self.assertEqual(window.config.values["deploy_info"]["port"], "8021")
        self.assertEqual(window.notices[0][0], "保存失败")
        self.assertIn(window.config.last_save_error, window.notices[0][1])
        self.assertTrue(window.notices[0][2]["danger"])


if __name__ == "__main__":
    unittest.main()
