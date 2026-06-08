import unittest

from core.port_utils import normalize_port, validate_port_bindings


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


if __name__ == "__main__":
    unittest.main()
