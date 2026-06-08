import unittest

from core.port_utils import (
    normalize_port,
    validate_instance_port_conflicts,
    validate_port_bindings,
)


class PortUtilsTests(unittest.TestCase):
    def test_normalize_port_uses_valid_value(self):
        self.assertEqual(normalize_port("8021", 1), 8021)

    def test_normalize_port_falls_back_for_invalid_value(self):
        self.assertEqual(normalize_port("abc", 8021), 8021)
        self.assertEqual(normalize_port("70000", 8021), 8021)

    def test_validate_port_bindings_reports_invalid_values(self):
        ok, message = validate_port_bindings([("Nekro Agent 端口", "abc")])
        self.assertFalse(ok)
        self.assertIn("Nekro Agent 端口", message)

    def test_validate_port_bindings_reports_duplicates_even_when_ignored(self):
        ok, message = validate_port_bindings(
            [("Nekro Agent 端口", 8021), ("NapCat 端口", "8021")],
            ignore_ports={8021},
        )
        self.assertFalse(ok)
        self.assertIn("不能使用同一个端口", message)

    def test_validate_port_bindings_accepts_ignored_available_check(self):
        ok, message = validate_port_bindings(
            [("Nekro Agent 端口", "8021")],
            ignore_ports={"8021"},
        )
        self.assertTrue(ok, message)

    def test_validate_instance_port_conflicts_rejects_other_instance_port(self):
        instances = [
            (
                "default",
                {
                    "instance_name": "",
                    "deploy_mode": "napcat",
                    "nekro_port": 8021,
                    "napcat_port": 6099,
                },
            )
        ]

        ok, message = validate_instance_port_conflicts(
            instances,
            [("Nekro Agent 端口", 6099)],
            current_instance_id="inst_2",
        )

        self.assertFalse(ok)
        self.assertIn("NapCat 端口", message)

    def test_validate_instance_port_conflicts_ignores_current_instance(self):
        instances = [
            (
                "default",
                {
                    "instance_name": "",
                    "deploy_mode": "lite",
                    "nekro_port": 8021,
                },
            )
        ]

        ok, message = validate_instance_port_conflicts(
            instances,
            [("Nekro Agent 端口", 8021)],
            current_instance_id="default",
        )

        self.assertTrue(ok, message)

    def test_validate_instance_port_conflicts_rejects_registered_duplicates(self):
        instances = [
            ("default", {"deploy_mode": "lite", "nekro_port": 8021}),
            ("inst_2", {"deploy_mode": "lite", "nekro_port": "8021"}),
        ]

        ok, message = validate_instance_port_conflicts(instances)

        self.assertFalse(ok)
        self.assertIn("重复", message)


if __name__ == "__main__":
    unittest.main()
