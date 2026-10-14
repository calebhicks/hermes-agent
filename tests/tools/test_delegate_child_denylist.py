import unittest
from unittest.mock import patch
from tests.tools.test_delegate import _make_mock_parent
from tools.delegate_tool import _resolve_child_toolsets

class TestChildDenylist(unittest.TestCase):
    def test_configured_child_block_wins_after_default_inheritance(self):
        parent = _make_mock_parent()
        parent.enabled_toolsets = ["terminal", "conductor-cloud"]
        parent.disabled_toolsets = []

        with (
            patch(
                "tools.delegate_tool_toolsets._get_blocked_child_toolsets",
                return_value=[" conductor-cloud "],
            ),
            patch(
                "tools.delegate_tool_toolsets._canonical_toolset_name",
                side_effect=lambda value: {
                    "conductor-cloud": "mcp-conductor-cloud",
                }.get(str(value).strip(), str(value).strip()),
            ),
        ):
            enabled, disabled = _resolve_child_toolsets(parent, None, "leaf")

        self.assertEqual(enabled, ["terminal"])
        self.assertIn("conductor-cloud", disabled)
        self.assertIn("mcp-conductor-cloud", disabled)


    def test_malformed_configured_child_block_refuses_resolution(self):
        parent = _make_mock_parent()
        parent.enabled_toolsets = ["terminal", "conductor-cloud"]
        parent.disabled_toolsets = []

        with patch(
            "tools.delegate_tool._load_config",
            return_value={"blocked_child_toolsets": "conductor-cloud"},
        ):
            with self.assertRaisesRegex(ValueError, "must be a list"):
                _resolve_child_toolsets(parent, None, "leaf")
