import json
import os
import shutil
import sys
import threading


class ConfigManager:
    def __init__(self, config_path=None):
        self._lock = threading.Lock()
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
            "active_instance": "",   # 当前活跃的实例 ID
            "instances": {},         # 多实例配置 {id: {instance_name, deploy_dir, data_dir, ...}}
            "skipped_app_version": "",        # 用户选择跳过的启动器版本 tag
            "last_app_update_check_ts": 0,    # 上次检查启动器更新的时间戳
        }
        self.config = self.load_config()
        if "data_dir" in self.config:
            self.config.pop("data_dir", None)
            self.save_config()
        self._migrate_to_multi_instance()

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
        with self._lock:
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
        with self._lock:
            self.config[key] = value
        return self.save_config()

    # ------------------------------------------------------------------ #
    # 多实例管理
    # ------------------------------------------------------------------ #

    def _migrate_to_multi_instance(self):
        """将旧版单实例配置迁移到多实例结构。

        仅当 deploy_mode 非空（已部署过）且 instances 为空时触发。
        """
        instances = self.config.get("instances") or {}
        deploy_mode = self.config.get("deploy_mode")
        if instances or not deploy_mode:
            return

        inst_id = "default"
        instances[inst_id] = {
            "instance_name": "",
            "deploy_dir": "/root/nekro_agent",
            "data_dir": "/root/nekro_agent_data",
            "deploy_mode": deploy_mode,
            "nekro_port": self.config.get("nekro_port", 8021),
            "napcat_port": self.config.get("napcat_port", 6099),
            "release_channel": self.config.get("release_channel", "stable"),
            "deploy_info": self.config.get("deploy_info"),
        }
        with self._lock:
            self.config["instances"] = instances
            self.config["active_instance"] = inst_id
        self.save_config()

    def get_active_instance_id(self):
        return self.config.get("active_instance", "")

    def get_instance(self, inst_id=None):
        """返回指定实例的配置 dict；不传 inst_id 时返回当前活跃实例。"""
        if inst_id is None:
            inst_id = self.get_active_instance_id()
        instances = self.config.get("instances") or {}
        return instances.get(inst_id)

    def set_instance(self, inst_id, data):
        """创建或更新一个实例配置。"""
        with self._lock:
            instances = self.config.setdefault("instances", {})
            instances[inst_id] = data
        return self.save_config()

    def update_instance(self, inst_id, **kwargs):
        """增量更新一个实例的部分字段。"""
        with self._lock:
            instances = self.config.setdefault("instances", {})
            inst = instances.setdefault(inst_id, {})
            inst.update(kwargs)
        return self.save_config()

    def remove_instance(self, inst_id):
        with self._lock:
            instances = self.config.get("instances") or {}
            instances.pop(inst_id, None)
            if self.config.get("active_instance") == inst_id:
                self.config["active_instance"] = next(iter(instances), "")
        return self.save_config()

    def list_instances(self):
        """返回 [(inst_id, inst_data), ...] 列表。"""
        instances = self.config.get("instances") or {}
        return list(instances.items())

    def next_instance_id(self):
        """生成下一个不重复的实例 ID。"""
        instances = self.config.get("instances") or {}
        if not instances:
            return "default"
        idx = len(instances) + 1
        while f"inst_{idx}" in instances:
            idx += 1
        return f"inst_{idx}"

    def get_active_deploy_dir(self):
        inst = self.get_instance()
        if inst:
            return inst.get("deploy_dir", "/root/nekro_agent")
        return "/root/nekro_agent"

    def get_active_data_dir(self):
        inst = self.get_instance()
        if inst:
            return inst.get("data_dir", "/root/nekro_agent_data")
        return "/root/nekro_agent_data"

    def get_active_instance_name(self):
        """返回当前实例的 INSTANCE_NAME 前缀（用于容器/卷命名）。"""
        inst = self.get_instance()
        if inst:
            return inst.get("instance_name", "")
        return ""
