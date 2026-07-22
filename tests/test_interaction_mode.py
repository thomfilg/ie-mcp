import unittest

import ie_mcp


class InteractionModeTests(unittest.TestCase):
    def test_native_mode_preserves_focused_os_events(self):
        mode = ie_mcp.resolve_interaction_mode({"IE_INTERACTION_MODE": "native"})

        self.assertEqual(mode.name, "native")
        self.assertTrue(mode.require_window_focus)
        self.assertTrue(mode.native_events)

    def test_background_mode_disables_focus_and_native_events(self):
        mode = ie_mcp.resolve_interaction_mode({"IE_INTERACTION_MODE": "background"})

        self.assertEqual(mode.name, "background")
        self.assertFalse(mode.require_window_focus)
        self.assertFalse(mode.native_events)

    def test_native_mode_is_the_backward_compatible_default(self):
        self.assertEqual(ie_mcp.resolve_interaction_mode({}).name, "native")

    def test_unknown_mode_fails_with_actionable_message(self):
        with self.assertRaisesRegex(ValueError, "native or background"):
            ie_mcp.resolve_interaction_mode({"IE_INTERACTION_MODE": "headless"})

    def test_mcp_instructions_explain_when_agents_should_use_each_mode(self):
        response = ie_mcp.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        instructions = response["result"]["instructions"]

        self.assertIn("IE_INTERACTION_MODE=background", instructions)
        self.assertIn("IE_INTERACTION_MODE=native", instructions)
        self.assertIn("active desktop", instructions)
        self.assertIn("fallback", instructions)


if __name__ == "__main__":
    unittest.main()
