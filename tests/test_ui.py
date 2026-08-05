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
        self.assertEqual(status_frag[1], "● Health: Good (2/2) ")

        # Case 2: Some WebSockets connected (Partial)
        self.session.websocket_connected = {"api_zk": True, "api_wvv": False}
        self.session.account_rest_failed = {"api_zk": False, "api_wvv": False}
        header_text = self.session._get_right_header_text()
        
        status_frag = header_text[0]
        self.assertEqual(status_frag[0], "fg:#ff8700 bold")
        self.assertEqual(status_frag[1], "● Health: (1/2) [")

        # Case 3: REST failed for one account (Partial)
        self.session.websocket_connected = {"api_zk": True, "api_wvv": True}
        self.session.account_rest_failed = {"api_zk": False, "api_wvv": True}
        header_text = self.session._get_right_header_text()
        
        status_frag = header_text[0]
        self.assertEqual(status_frag[0], "fg:#ff8700 bold")
        self.assertEqual(status_frag[1], "● Health: (1/2) [")

        # Case 4: All failed (Critical)
        self.session.websocket_connected = {"api_zk": False, "api_wvv": False}
        self.session.account_rest_failed = {"api_zk": True, "api_wvv": True}
        header_text = self.session._get_right_header_text()
        
        status_frag = header_text[0]
        self.assertEqual(status_frag[0], "fg:#ff0000 bold")
        self.assertEqual(status_frag[1], "● Health: (0/2) [")

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

    @patch("cli.live_session.KCLILiveSession._update_info_buffer")
    def test_info_pane_tabs(self, mock_update_buffer):
        self.session.info_mode = "orders_pending"
        fragments = self.session._get_tab_bar_text()
        
        self.assertEqual(fragments[1][0], "class:tab.active")
        self.assertEqual(fragments[1][1], " Orders ")
        self.assertEqual(fragments[3][0], "class:tab.inactive")
        self.assertEqual(fragments[3][1], " Executed ")
        
        click_handler_oc = fragments[5][2]
        
        from prompt_toolkit.mouse_events import MouseEventType
        mock_event = MagicMock()
        mock_event.event_type = MouseEventType.MOUSE_UP
        
        click_handler_oc(mock_event)
        
        self.assertEqual(self.session.info_mode, "oc")
        mock_update_buffer.assert_called_once()
        self.session.app.invalidate.assert_called_once()

    def test_pnl_date_ranges(self):
        from datetime import datetime
        # Ref date: Thursday July 30, 2026
        ref_dt = datetime(2026, 7, 30, 12, 0, 0)
        with patch("cli.live_session.datetime") as mock_dt:
            mock_dt.now.return_value = ref_dt
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            
            # Week P&L date range (recent Wednesday Jul 29)
            s, e, l = self.session._get_pnl_date_range("week")
            self.assertEqual(s.strftime("%Y-%m-%d"), "2026-07-29")
            
            # Month P&L date range (day after last month's last Tuesday -> Wed Jul 01)
            s, e, l = self.session._get_pnl_date_range("month")
            self.assertEqual(s.strftime("%Y-%m-%d"), "2026-07-01")
            
            # Financial Year P&L date range (Apr 01, 2026)
            s, e, l = self.session._get_pnl_date_range("year")
            self.assertEqual(s.strftime("%Y-%m-%d"), "2026-04-01")

    @patch("cli.live_session.KCLILiveSession._update_info_buffer")
    def test_pnl_tab_switching(self, mock_update_buffer):
        fragments = self.session._get_tab_bar_text()
        # Tab 5 is P&L
        click_handler_pnl = fragments[9][2]
        
        from prompt_toolkit.mouse_events import MouseEventType
        mock_event = MagicMock()
        mock_event.event_type = MouseEventType.MOUSE_UP
        
        click_handler_pnl(mock_event)
        self.assertEqual(self.session.info_mode, "pnl")

if __name__ == "__main__":
    unittest.main()
