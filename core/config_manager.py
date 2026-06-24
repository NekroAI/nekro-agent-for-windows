import json
import logging
import os
import shutil
import sys
import threading
import time

logger = logging.getLogger(__name__)

_app_data_dir_cache: str | None = None
_app_data_dir_lock = threading.Lock()


def get_app_data_dir() -> str:
    """返回应用数据根目录。

    优先使用 <exe所在目录>/data（安装目录下），写入失败（如 Program Files）
    则自动 fallback 到 %LOCALAPPDATA%/NekroAgent。
    结果会被缓存，整个进程生命周期只探测一次。
    """
    global _app_data_dir_cache
    if _app_data_dir_cache is not None:
        return _app_data_dir_cache

    with _app_data_dir_lock:
        if _app_data_dir_cache is not None:
            return _app_data_dir_cache

        if getattr(sys, "frozen", False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        candidate = os.path.join(base, "data")
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, f".probe_{os.getpid()}")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            try:
                os.remove(probe)
            except OSError:
                pass
            _app_data_dir_cache = candidate
            return candidate
        except (OSError, PermissionError):
            pass

        local = os.environ.get("LOCALAPPDATA", "") or os.path.expanduser("~")
        fallback = os.path.join(local, "NekroAgent")
        os.makedirs(fallback, exist_ok=True)
        _app_data_dir_cache = fallback
        return fallback


class ConfigManager:
    def __init__(self, config_path=None):
        self._lock = threading.RLock()
        # 确定基础路径
        if getattr(sys, 'frozen', False):
            self.base_path = os.path.dirname(sys.executable)
        else:
            self.base_path = os.path.dirname(os.path.abspath(__file__))
            if self.base_path.endswith('core'):
                self.base_path = os.path.dirname(self.base_path)

        self.app_data_dir = get_app_data_dir()
        self.browser_profile_dir = os.path.join(self.app_data_dir, "browser_profile")
        self._legacy_config_path = os.path.join(self.base_path, "config.json")
        self._legacy_browser_profile_dir = os.path.join(self.base_path, "browser_profile")
        self._legacy_localappdata_dir = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "NekroAgent",
        )

        if config_path:
            self.config_path = config_path
        else:
            self.config_path = os.path.join(self.app_data_dir, "config.json")

        self.last_save_error = ""
        self.last_load_error = ""
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
            "default_instance": "",  # 启动后默认展示和访问的实例 ID
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

        legacy_sources = [
            self._legacy_config_path,
            os.path.join(self._legacy_localappdata_dir, "config.json"),
        ]
        for src in legacy_sources:
            src = os.path.abspath(src)
            if src == os.path.abspath(self.config_path):
                continue
            if not os.path.exists(self.config_path) and os.path.exists(src):
                try:
                    shutil.copy2(src, self.config_path)
                    break
                except Exception as e:
                    logger.warning("迁移配置文件失败 %s -> %s: %s", src, self.config_path, e)
                    continue

        legacy_browser_dirs = [
            self._legacy_browser_profile_dir,
            os.path.join(self._legacy_localappdata_dir, "browser_profile"),
        ]
        for src in legacy_browser_dirs:
            src = os.path.abspath(src)
            if src == os.path.abspath(self.browser_profile_dir):
                continue
            if not os.path.exists(self.browser_profile_dir) and os.path.isdir(src):
                try:
                    shutil.copytree(src, self.browser_profile_dir)
                    break
                except Exception as e:
                    logger.warning("迁移浏览器数据失败 %s -> %s: %s", src, self.browser_profile_dir, e)
                    continue

    def load_config(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return {**self.default_config, **json.load(f)}
            except json.JSONDecodeError as e:
                self.last_load_error = f"配置文件格式错误: {e}"
                corrupt_path = self._quarantine_corrupt_config()
                if corrupt_path:
                    logger.error(
                        "配置文件格式错误，已保留损坏文件 %s，将使用默认配置: %s",
                        corrupt_path,
                        e,
                    )
                else:
                    logger.error("配置文件格式错误，将使用默认配置: %s", e)
                return self.default_config.copy()
            except Exception as e:
                self.last_load_error = f"读取配置文件失败: {e}"
                logger.error("读取配置文件失败: %s", e)
                return self.default_config.copy()
        return self.default_config.copy()

    def _quarantine_corrupt_config(self):
        try:
            if not os.path.exists(self.config_path):
                return ""
            stamp = time.strftime("%Y%m%d-%H%M%S")
            corrupt_path = f"{self.config_path}.corrupt.{stamp}"
            os.replace(self.config_path, corrupt_path)
            return corrupt_path
        except Exception as e:
            logger.warning("保留损坏配置文件失败 %s: %s", self.config_path, e)
            return ""

    def _save_config_locked(self):
        tmp_path = ""
        try:
            config_dir = os.path.dirname(os.path.abspath(self.config_path))
            os.makedirs(config_dir, exist_ok=True)
            tmp_path = os.path.join(config_dir, f".{os.path.basename(self.config_path)}.{os.getpid()}.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.config_path)
            self.last_save_error = ""
            return True
        except Exception as e:
            self.last_save_error = str(e)
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return False

    def save_config(self):
        with self._lock:
            return self._save_config_locked()

    def get(self, key):
        return self.config.get(key, self.default_config.get(key))

    def set(self, key, value):
        with self._lock:
            self.config[key] = value
            return self._save_config_locked()

    def set_many(self, values):
        with self._lock:
            self.config.update(values)
            return self._save_config_locked()

    def update_instance_with_globals(
        self,
        inst_id,
        instance_updates=None,
        global_updates=None,
    ):
        with self._lock:
            if global_updates:
                self.config.update(global_updates)
            if inst_id and instance_updates:
                instances = self.config.setdefault("instances", {})
                inst = instances.setdefault(inst_id, {})
                inst.update(instance_updates)
            return self._save_config_locked()

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
            "preview_backup_available": bool(
                self.config.get("preview_backup_available", False)
            ),
        }
        with self._lock:
            self.config["instances"] = instances
            self.config["active_instance"] = inst_id
            self.config["default_instance"] = inst_id
            self._save_config_locked()

    def get_active_instance_id(self):
        return self.config.get("active_instance", "")

    def get_default_instance_id(self):
        instances = self.config.get("instances") or {}
        default_id = self.config.get("default_instance", "")
        if default_id in instances:
            return default_id
        active_id = self.get_active_instance_id()
        if active_id in instances:
            return active_id
        return next(iter(instances), "")

    def set_default_instance_id(self, inst_id):
        instances = self.config.get("instances") or {}
        if inst_id not in instances:
            return False
        return self.set("default_instance", inst_id)

    def get_instance_value(self, inst_id, key, default=None):
        inst = self.get_instance(inst_id)
        if not inst:
            return default
        return inst.get(key, default)

    def get_active_instance_value(self, key, default=None):
        inst = self.get_instance()
        if not inst:
            return default
        return inst.get(key, default)

    def get_active_preview_backup_available(self) -> bool:
        inst = self.get_instance()
        if inst is not None and "preview_backup_available" in inst:
            return bool(inst.get("preview_backup_available"))
        return bool(self.config.get("preview_backup_available", False))

    def set_active_preview_backup_available(self, available: bool):
        inst_id = self.get_active_instance_id()
        value = bool(available)
        if inst_id:
            return self.update_instance_with_globals(
                inst_id,
                instance_updates={"preview_backup_available": value},
                global_updates={"preview_backup_available": value},
            )
        return self.set("preview_backup_available", value)

    def clear_runtime_state(self, keep_first_run=False):
        with self._lock:
            self.config["active_instance"] = ""
            self.config["default_instance"] = ""
            self.config["deploy_mode"] = ""
            self.config["release_channel"] = "stable"
            self.config["preview_backup_available"] = False
            self.config["deploy_info"] = None
            self.config["nekro_port"] = self.default_config["nekro_port"]
            self.config["napcat_port"] = self.default_config["napcat_port"]
            if keep_first_run:
                self.config["first_run"] = True
            return self._save_config_locked()

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
            return self._save_config_locked()

    def update_instance(self, inst_id, **kwargs):
        """增量更新一个实例的部分字段。"""
        with self._lock:
            instances = self.config.setdefault("instances", {})
            inst = instances.setdefault(inst_id, {})
            inst.update(kwargs)
            return self._save_config_locked()

    def remove_instance(self, inst_id):
        with self._lock:
            instances = self.config.get("instances") or {}
            instances.pop(inst_id, None)
            fallback = next(iter(instances), "")
            if self.config.get("active_instance") == inst_id:
                self.config["active_instance"] = fallback
            if self.config.get("default_instance") == inst_id:
                self.config["default_instance"] = self.config.get("active_instance") or fallback
            return self._save_config_locked()

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
