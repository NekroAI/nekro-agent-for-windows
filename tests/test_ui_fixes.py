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


class _PendingConfig:
    def __init__(self):
        self.last_save_error = "配置目录只读"
        self.calls = []

    @staticmethod
    def get_active_instance_id():
        return "default"

    @staticmethod
    def get(key):
        return "lite" if key == "deploy_mode" else None

    @staticmethod
    def get_default_instance_id():
        return "default"

    def update_instance_with_globals(
        self,
        inst_id,
        instance_updates=None,
        global_updates=None,
    ):
        self.calls.append((inst_id, instance_updates, global_updates))
        return False


class _PendingWindow:
    def __init__(self):
        self.config = _PendingConfig()
        self.backend = _Backend()
        self._pending_inst_data = {
            "inst_id": "inst_2",
            "deploy_mode": "napcat",
            "nekro_port": 18021,
            "napcat_port": 16099,
        }
        self.notices = []
        self.deploy_started = False

    @staticmethod
    def _guard_napcat_network_config_busy(_action):
        return True

    @staticmethod
    def _guard_blocking_status_idle(_action):
        return True

    @staticmethod
    def _backend_runtime_exists():
        return True

    def _show_notice_dialog(self, title, text, **kwargs):
        self.notices.append((title, text, kwargs))

    def refresh_dashboard(self):
        return None

    def _apply_pending_instance(self):
        return MainWindow._apply_pending_instance(self)

    def _do_deploy(self, *_args, **_kwargs):
        self.deploy_started = True
        return True


class _RemovalConfig:
    default_config = {"nekro_port": 8021, "napcat_port": 6099}

    def __init__(self):
        self.last_save_error = "磁盘已满"
        self.calls = []

    @staticmethod
    def list_instances():
        return [
            ("default", {"deploy_mode": "lite", "nekro_port": 8021}),
            (
                "inst_2",
                {
                    "deploy_mode": "napcat",
                    "nekro_port": 18021,
                    "napcat_port": 16099,
                    "release_channel": "stable",
                },
            ),
        ]

    def remove_instance_with_globals(self, inst_id, global_updates=None):
        self.calls.append((inst_id, global_updates))
        return False


class _RemovalWindow:
    def __init__(self):
        self.config = _RemovalConfig()
        self.notices = []
        self.refreshed = False

    def refresh_dashboard(self):
        self.refreshed = True

    def _show_notice_dialog(self, title, text, **kwargs):
        self.notices.append((title, text, kwargs))


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

    def test_pending_instance_save_failure_does_not_start_deploy(self):
        window = _PendingWindow()

        started = MainWindow.start_deploy(window)

        self.assertFalse(started)
        self.assertFalse(window.deploy_started)
        self.assertEqual(len(window.config.calls), 1)
        inst_id, instance_updates, global_updates = window.config.calls[0]
        self.assertEqual(inst_id, "inst_2")
        self.assertEqual(instance_updates["deploy_mode"], "napcat")
        self.assertEqual(global_updates["active_instance"], "inst_2")
        self.assertEqual(window.notices[0][0], "实例配置保存失败")
        self.assertIn(window.config.last_save_error, window.notices[0][1])

    def test_remove_resource_success_reports_config_sync_failure(self):
        window = _RemovalWindow()

        MainWindow._on_remove_instance_done(
            window,
            success=True,
            inst_id="default",
            was_active=True,
        )

        self.assertTrue(window.refreshed)
        self.assertEqual(len(window.config.calls), 1)
        inst_id, global_updates = window.config.calls[0]
        self.assertEqual(inst_id, "default")
        self.assertEqual(global_updates["active_instance"], "inst_2")
        self.assertEqual(global_updates["nekro_port"], 18021)
        self.assertEqual(window.notices[0][0], "运行资源已删除但配置同步失败")
        self.assertIn(window.config.last_save_error, window.notices[0][1])
        self.assertNotIn("移除完成", [notice[0] for notice in window.notices])

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
