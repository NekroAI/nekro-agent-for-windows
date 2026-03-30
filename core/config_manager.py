import json
import os
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

        if config_path:
            self.config_path = config_path
        else:
            self.config_path = os.path.join(self.base_path, "config.json")

        self.default_config = {
            "backend": "wsl",
            "shared_dir": "shared",
            "autostart": False,
            "first_run": True,
            "deploy_mode": "",       # "lite" 或 "napcat"
            "wsl_distro": "",        # 检测到的发行版名称
            "wsl_install_dir": "",   # WSL 发行版安装目录 (Windows 路径)
            "data_dir": "",          # WSL 内数据目录路径
            "nekro_port": 8021,      # Nekro Agent 对外端口
            "napcat_port": 6099,     # NapCat 对外端口
            "runtime_image_cache": "runtime_cache",
        }
        self.config = self.load_config()

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
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            return True
        except Exception:
            return False

    def get(self, key):
        return self.config.get(key, self.default_config.get(key))

    def set(self, key, value):
        self.config[key] = value
        self.save_config()

    def get_absolute_path(self, key):
        """获取配置中路径的绝对路径"""
        value = self.get(key)
        if value and not os.path.isabs(value):
            return os.path.join(self.base_path, value)
        return value
