import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root is in path
sys.path.append("/Users/vgolugur/Documents/Projects/kitecli")

from cli.live_session import KCLILiveSession

class TestUIComponents(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.client = MagicMock()
        self.accounts = [
            {"name": "ZK8719", "api_key": "api_zk", "user_id": "ZK8719"},
            {"name": "WVV135", "api_key": "api_wvv", "user_id": "WVV135"}
        ]
        self.session = KCLILiveSession(self.client, self.accounts)
        self.session.prompt_control = MagicMock()
        self.session.header_control = MagicMock()
        self.session.log_message = MagicMock()
        self.session.app = MagicMock()
        self.session.app.loop = MagicMock()

    async def test_header_display_status_colors(self):
        # Case 1: All WebSockets and REST healthy (Good)
        self.session.websocket_connected = {"api_zk": True, "api_wvv": True}
        self.session.account_rest_failed = {"api_zk": False, "api_wvv": False}
        header_text = self.session._get_right_header_text()
        
        # Verify active styling
        status_frag = header_text[0]
        self.assertEqual(status_frag[0], "fg:#00ff00 bold")
        self.assertEqual(status_frag[1], "● Health: Good ")

        # Case 2: Some WebSockets connected (Partial)
        self.session.websocket_connected = {"api_zk": True, "api_wvv": False}
        self.session.account_rest_failed = {"api_zk": False, "api_wvv": False}
        header_text = self.session._get_right_header_text()
        
        status_frag = header_text[0]
        self.assertEqual(status_frag[0], "fg:#ff8700 bold")
        self.assertEqual(status_frag[1], "● Health: Partial (1/2) ")

        # Case 3: REST failed for one account (Partial)
        self.session.websocket_connected = {"api_zk": True, "api_wvv": True}
        self.session.account_rest_failed = {"api_zk": False, "api_wvv": True}
        header_text = self.session._get_right_header_text()
        
        status_frag = header_text[0]
        self.assertEqual(status_frag[0], "fg:#ff8700 bold")
        self.assertEqual(status_frag[1], "● Health: Partial (1/2) ")

        # Case 4: All failed (Critical)
        self.session.websocket_connected = {"api_zk": False, "api_wvv": False}
        self.session.account_rest_failed = {"api_zk": True, "api_wvv": True}
        header_text = self.session._get_right_header_text()
        
        status_frag = header_text[0]
        self.assertEqual(status_frag[0], "fg:#ff0000 bold")
        self.assertEqual(status_frag[1], "● Health: Critical (0/2) ")

    async def test_is_account_healthy(self):
        # WebSocket true, REST success
        self.session.websocket_connected = {"api_zk": True}
        self.session.account_rest_failed = {"api_zk": False}
        self.assertTrue(self.session.is_account_healthy("api_zk"))

        # WebSocket false, REST success
        self.session.websocket_connected = {"api_zk": False}
        self.session.account_rest_failed = {"api_zk": False}
        self.assertFalse(self.session.is_account_healthy("api_zk"))

        # WebSocket true, REST failed
        self.session.websocket_connected = {"api_zk": True}
        self.session.account_rest_failed = {"api_zk": True}
        self.assertFalse(self.session.is_account_healthy("api_zk"))

    @patch("asyncio.run_coroutine_threadsafe")
    async def test_header_click_triggers_reconnect(self, mock_run_coroutine):
        header_text = self.session._get_right_header_text()
        status_frag = header_text[0]
        click_handler = status_frag[2]
        
        # Mock MOUSE_UP click event
        from prompt_toolkit.mouse_events import MouseEventType
        mock_event = MagicMock()
        mock_event.event_type = MouseEventType.MOUSE_UP
        
        # Call click handler
        click_handler(mock_event)
        
        # Verify that reconnection is scheduled asynchronously
        mock_run_coroutine.assert_called_once()
        self.session.log_message.assert_any_call("Triggering manual WebSocket and REST reconnection...")

if __name__ == "__main__":
    unittest.main()
