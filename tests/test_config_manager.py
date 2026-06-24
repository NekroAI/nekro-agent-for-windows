import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
