import json
import os
import shutil
import sys


class ConfigManager:
    def __init__(self, config_path=None):
        # 确定基础路径
        if getattr(sys, 'frozen', False):
            self.base_path = os.path.dirname(sys.executable)
        else:
            self.base_path = os.path.dirname(os.path.abspath(__file__))
            if self.base_path.endswith('core'):
                self.base_path = os.path.dirname(self.base_path)

        self.app_data_dir = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "NekroAgent",
        )
        self.browser_profile_dir = os.path.join(self.app_data_dir, "browser_profile")
        self._legacy_config_path = os.path.join(self.base_path, "config.json")
        self._legacy_browser_profile_dir = os.path.join(self.base_path, "browser_profile")

        if config_path:
            self.config_path = config_path
        else:
            self.config_path = os.path.join(self.app_data_dir, "config.json")

        self.last_save_error = ""
        self._migrate_legacy_state()

        self.default_config = {
            "backend": "wsl",
            "shared_dir": "shared",
            "autostart": False,
            "advanced_features_enabled": False,
            "release_channel": "stable",
            "preview_backup_available": False,
            "first_run": True,
            "deploy_mode": "",       # "lite" 或 "napcat"
            "wsl_distro": "",        # 检测到的发行版名称
            "wsl_install_dir": "",   # WSL 发行版安装目录 (Windows 路径)
            "nekro_port": 8021,      # Nekro Agent 对外端口
            "napcat_port": 6099,     # NapCat 对外端口
            "image_update_check_interval_hours": 24,
            "last_image_update_check_ts": 0,
            "image_status_cache": {},
            "image_update_last_alert_signature": "",
            "runtime_image_cache": "runtime_cache",
        }
        self.config = self.load_config()
        if "data_dir" in self.config:
            self.config.pop("data_dir", None)
            self.save_config()

    def _migrate_legacy_state(self):
        os.makedirs(self.app_data_dir, exist_ok=True)

        config_path = os.path.abspath(self.config_path)
        legacy_config_path = os.path.abspath(self._legacy_config_path)
        if config_path != legacy_config_path:
            if not os.path.exists(self.config_path) and os.path.exists(self._legacy_config_path):
                try:
                    shutil.copy2(self._legacy_config_path, self.config_path)
                except Exception:
                    pass

            if not os.path.exists(self.browser_profile_dir) and os.path.isdir(self._legacy_browser_profile_dir):
                try:
                    shutil.copytree(self._legacy_browser_profile_dir, self.browser_profile_dir)
                except Exception:
                    pass

    def load_config(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return {**self.default_config, **json.load(f)}
            except Exception:
                return self.default_config.copy()
        return self.default_config.copy()

    def save_config(self):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.config_path)), exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            self.last_save_error = ""
            return True
        except Exception as e:
            self.last_save_error = str(e)
            return False

    def get(self, key):
        return self.config.get(key, self.default_config.get(key))

    def set(self, key, value):
        self.config[key] = value
        return self.save_config()
