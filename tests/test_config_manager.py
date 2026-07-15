import os
import tempfile
import unittest
from unittest.mock import patch

from core.config_manager import ConfigManager


class ConfigManagerTests(unittest.TestCase):
    def test_corrupt_config_is_quarantined_before_defaults_are_used(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("{bad json")

            config = ConfigManager(config_path=config_path)

            self.assertTrue(config.get("first_run"))
            quarantined = [
                name
                for name in os.listdir(temp_dir)
                if name.startswith("config.json.corrupt.")
            ]
            self.assertEqual(len(quarantined), 1)
            self.assertFalse(os.path.exists(config_path))

    def test_set_active_preview_backup_updates_global_and_instance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.json")
            config = ConfigManager(config_path=config_path)
            config.set_instance("default", {"preview_backup_available": False})
            config.set("active_instance", "default")

            self.assertTrue(config.set_active_preview_backup_available(True))

            self.assertTrue(config.get("preview_backup_available"))
            inst = config.get_instance("default")
            self.assertIsNotNone(inst)
            assert inst is not None
            self.assertTrue(inst.get("preview_backup_available"))

    def test_failed_save_rolls_back_global_and_instance_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = ConfigManager(config_path=os.path.join(temp_dir, "config.json"))
            self.assertTrue(config.set_instance("default", {"nekro_port": 8021}))
            self.assertTrue(config.set("active_instance", "default"))

            with patch.object(config, "_save_config_locked", return_value=False):
                saved = config.update_instance_with_globals(
                    "default",
                    instance_updates={"nekro_port": 19001},
                    global_updates={"nekro_port": 19001},
                )

            self.assertFalse(saved)
            self.assertEqual(config.get("nekro_port"), 8021)
            inst = config.get_instance("default")
            self.assertIsNotNone(inst)
            assert inst is not None
            self.assertEqual(inst.get("nekro_port"), 8021)

    def test_set_copies_mutable_values_before_storing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = ConfigManager(config_path=os.path.join(temp_dir, "config.json"))
            value = {"port": "8021"}

            self.assertTrue(config.set("deploy_info", value))
            value["port"] = "19001"

            self.assertEqual(config.get("deploy_info"), {"port": "8021"})

    def test_real_save_failure_keeps_previous_in_memory_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            blocker = os.path.join(temp_dir, "blocker")
            with open(blocker, "w", encoding="utf-8") as f:
                f.write("not a directory")
            config = ConfigManager(config_path=os.path.join(blocker, "config.json"))

            saved = config.set("nekro_port", 19001)

            self.assertFalse(saved)
            self.assertEqual(config.get("nekro_port"), 8021)
            self.assertTrue(config.last_save_error)

    def test_remove_instance_with_globals_is_atomic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = ConfigManager(config_path=os.path.join(temp_dir, "config.json"))
            self.assertTrue(
                config.set_instance(
                    "default",
                    {"deploy_mode": "lite", "nekro_port": 8021},
                )
            )
            self.assertTrue(
                config.set_instance(
                    "inst_2",
                    {"deploy_mode": "napcat", "nekro_port": 18021},
                )
            )
            self.assertTrue(
                config.set_many(
                    {
                        "active_instance": "default",
                        "default_instance": "default",
                        "deploy_mode": "lite",
                        "nekro_port": 8021,
                    }
                )
            )

            with patch.object(config, "_save_config_locked", return_value=False):
                saved = config.remove_instance_with_globals(
                    "default",
                    global_updates={
                        "active_instance": "inst_2",
                        "deploy_mode": "napcat",
                        "nekro_port": 18021,
                    },
                )

            self.assertFalse(saved)
            self.assertIsNotNone(config.get_instance("default"))
            self.assertEqual(config.get_active_instance_id(), "default")
            self.assertEqual(config.get("deploy_mode"), "lite")
            self.assertEqual(config.get("nekro_port"), 8021)

    def test_remove_instance_with_globals_commits_fallback_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = ConfigManager(config_path=os.path.join(temp_dir, "config.json"))
            self.assertTrue(config.set_instance("default", {"deploy_mode": "lite"}))
            self.assertTrue(
                config.set_instance("inst_2", {"deploy_mode": "napcat"})
            )
            self.assertTrue(
                config.set_many(
                    {
                        "active_instance": "default",
                        "default_instance": "default",
                    }
                )
            )

            saved = config.remove_instance_with_globals(
                "default",
                global_updates={
                    "active_instance": "inst_2",
                    "deploy_mode": "napcat",
                },
            )

            self.assertTrue(saved)
            self.assertIsNone(config.get_instance("default"))
            self.assertEqual(config.get_active_instance_id(), "inst_2")
            self.assertEqual(config.get_default_instance_id(), "inst_2")
            self.assertEqual(config.get("deploy_mode"), "napcat")


if __name__ == "__main__":
    unittest.main()
