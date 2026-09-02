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

            # Last Week P&L date range (previous Wednesday Jul 22 -> Tuesday Jul 28)
            s_lw, e_lw, l_lw = self.session._get_pnl_date_range("last_week")
            self.assertEqual(s_lw.strftime("%Y-%m-%d"), "2026-07-22")
            self.assertEqual(e_lw.strftime("%Y-%m-%d"), "2026-07-28")
            self.assertIn("Last Week", l_lw)
            
            # Month P&L date range (day after last month's last Tuesday -> Wed Jul 01)
            s, e, l = self.session._get_pnl_date_range("month")
            self.assertEqual(s.strftime("%Y-%m-%d"), "2026-07-01")
            
            # Financial Year P&L date range (Apr 01, 2026)
            s, e, l = self.session._get_pnl_date_range("year")
            self.assertEqual(s.strftime("%Y-%m-%d"), "2026-04-01")

    @patch("cli.live_session.KCLILiveSession._update_info_buffer")
    def test_pnl_tab_switching(self, mock_update_buffer):
        self.session.pnl_period = "month"  # manually set to something else
        fragments = self.session._get_tab_bar_text()
        # Tab P&L is index 11
        click_handler_pnl = fragments[11][2]
        
        from prompt_toolkit.mouse_events import MouseEventType
        mock_event = MagicMock()
        mock_event.event_type = MouseEventType.MOUSE_UP
        
        click_handler_pnl(mock_event)
        self.assertEqual(self.session.info_mode, "pnl")
        self.assertEqual(self.session.pnl_period, "week")

    def test_partially_filled_cancelled_order_pnl(self):
        import tempfile
        import sqlite3
        from pathlib import Path

        # 1. Test _handle_executed_order_context properly formats partial fill
        self.session.record_context_enabled = True
        mock_rec = MagicMock()
        self.session.recorder = mock_rec

        self.session._handle_executed_order_context("api_zk", {
            "order_id": "ord_buy_partial",
            "tradingsymbol": "NIFTY2691522400PE",
            "transaction_type": "BUY",
            "quantity": 1755,
            "filled_quantity": 1040,
            "price": 5.90,
            "status": "CANCELLED"
        })

        self.assertTrue(mock_rec.enqueue_executed_order_context.called)
        kwargs = mock_rec.enqueue_executed_order_context.call_args[1]
        self.assertEqual(kwargs["order_data"]["quantity"], 1040)
        self.assertEqual(kwargs["order_data"]["status"], "COMPLETE")

        # 2. Test _render_pnl_pane calculations with DB entries
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_p = Path(tmp_dir) / "test_pnl.db"
            conn = sqlite3.connect(str(db_p))
            conn.execute("""
                CREATE TABLE executed_order_context (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    user_id TEXT,
                    account_name TEXT,
                    order_id TEXT,
                    tradingsymbol TEXT,
                    exchange TEXT,
                    transaction_type TEXT,
                    quantity INTEGER,
                    price REAL,
                    status TEXT,
                    is_settled_externally INTEGER DEFAULT 0,
                    total_charges REAL DEFAULT 0.0
                );
            """)
            # Insert sell 1755 @ 4.60, buy 1040 @ 5.90, buy 715 @ 6.80
            conn.execute("INSERT INTO executed_order_context (timestamp, user_id, account_name, order_id, tradingsymbol, transaction_type, quantity, price, status, total_charges) VALUES ('2026-09-01T11:25:01', 'ZK8719', 'ZK8719', 'o1', 'NIFTY2691522400PE', 'SELL', 1755, 4.60, 'COMPLETE', 39.11)")
            conn.execute("INSERT INTO executed_order_context (timestamp, user_id, account_name, order_id, tradingsymbol, transaction_type, quantity, price, status, total_charges) VALUES ('2026-09-01T14:23:34', 'ZK8719', 'ZK8719', 'o2', 'NIFTY2691522400PE', 'BUY', 1040, 5.90, 'COMPLETE', 26.36)")
            conn.execute("INSERT INTO executed_order_context (timestamp, user_id, account_name, order_id, tradingsymbol, transaction_type, quantity, price, status, total_charges) VALUES ('2026-09-01T14:25:49', 'ZK8719', 'ZK8719', 'o3', 'NIFTY2691522400PE', 'BUY', 715, 6.80, 'COMPLETE', 25.79)")
            conn.commit()
            conn.close()

            mock_rec_db = MagicMock()
            mock_rec_db.db_path = db_p
            self.session.recorder = mock_rec_db
            self.session.pnl_period = "month"
            self.session.pnl_selected_account = "ALL"

            rendered = self.session._render_pnl_pane()
            self.assertIn("NIFTY2691522400PE", rendered)
            self.assertIn("1755", rendered)
            self.assertIn("2,925.00", rendered)

    @patch("cli.live_session.KCLILiveSession._update_info_buffer")
    def test_md_tab_switching_and_rendering(self, mock_update_buffer):
        fragments = self.session._get_tab_bar_text()
        # Tab MD is index 13
        self.assertEqual(fragments[13][1], " MD ")
        click_handler_md = fragments[13][2]

        from prompt_toolkit.mouse_events import MouseEventType
        mock_event = MagicMock()
        mock_event.event_type = MouseEventType.MOUSE_UP

        click_handler_md(mock_event)
        self.assertEqual(self.session.info_mode, "md")

        # Test empty symbol rendering
        self.session.md_symbol = ""
        rendered_empty = self.session._render_market_depth_pane()
        self.assertIn("MARKET DEPTH", rendered_empty)
        self.assertIn("No ticker symbol selected", rendered_empty)

        # Test populated symbol rendering
        self.session.md_symbol = "NIFTY2682025000CE"
        self.client.get_market_depth.return_value = {
            "status": "success",
            "symbol": "NIFTY2682025000CE",
            "last_price": 105.50,
            "change": 4.50,
            "change_pct": 4.45,
            "volume": 1500000,
            "oi": 2000000,
            "total_buy_quantity": 50000,
            "total_sell_quantity": 40000,
            "buy_depth": [
                {"orders": 5, "quantity": 500, "price": 105.45}
            ],
            "sell_depth": [
                {"orders": 3, "quantity": 300, "price": 105.55}
            ],
            "ohlc": {"open": 101.0, "high": 108.0, "low": 100.0, "close": 101.0},
            "lower_circuit_limit": 50.0,
            "upper_circuit_limit": 160.0
        }
        rendered = self.session._render_market_depth_pane()
        self.assertIn("NIFTY2682025000CE", rendered)
        self.assertIn("BUY (BIDS)", rendered)
        self.assertIn("SELL (ASKS)", rendered)
        self.assertIn("105.45", rendered)
        self.assertIn("105.55", rendered)

    def test_orders_pane_live_ltp_display(self):
        # Configure test response with open order
        orders_resp = {
            "accounts": [
                {
                    "name": "ZK8719",
                    "status": "success",
                    "orders": [
                        {
                            "order_id": "123456",
                            "tradingsymbol": "NIFTY2682025000CE",
                            "transaction_type": "BUY",
                            "quantity": 100,
                            "filled_quantity": 0,
                            "order_type": "LIMIT",
                            "price": 100.0,
                            "product": "NRML",
                            "status": "OPEN",
                        }
                    ]
                }
            ]
        }
        self.session.ws_symbol_ltp_cache["NIFTY2682025000CE"] = 105.25
        rendered = self.session._render_orders_pane(orders_resp, "orders_pending")
        self.assertIn("Placed Price", rendered)
        self.assertIn("Live LTP", rendered)
        self.assertIn("100.00", rendered)
        self.assertIn("105.25", rendered)

    async def test_pending_order_token_resolution_and_live_ltp_streaming(self):
        self.session.client.get_nfo_symbol_token_map.return_value = {
            "NIFTY2691525200CE": 88888
        }
        self.session.client.get_positions.return_value = {"accounts": []}
        self.session.client.get_margins.return_value = {"accounts": []}
        self.session.client.get_orders.return_value = {
            "accounts": [
                {
                    "name": "ZK8719",
                    "api_key": "api_zk",
                    "status": "success",
                    "orders": [
                        {
                            "order_id": "825472",
                            "tradingsymbol": "NIFTY2691525200CE",
                            "transaction_type": "SELL",
                            "quantity": 1755,
                            "filled_quantity": 0,
                            "order_type": "LIMIT",
                            "price": 3.85,
                            "product": "NRML",
                            "status": "OPEN",
                        }
                    ],
                }
            ]
        }
        self.session.info_mode = "orders_pending"

        # Run refresh
        await self.session._trigger_immediate_refresh()

        # Verify token was resolved and subscribed
        self.assertIn(88888, self.session.subscribed_tokens)
        self.assertEqual(self.session.ws_symbol_to_token.get("NIFTY2691525200CE"), 88888)
        self.assertEqual(self.session.ws_token_to_symbol.get(88888), "NIFTY2691525200CE")

        # Simulate tick on token 88888
        self.session.app = MagicMock()
        self.session.app.loop = MagicMock()
        ticks = [{"instrument_token": 88888, "last_price": 3.85}]
        self.session._on_ticks(None, ticks)

        self.assertEqual(self.session.ws_symbol_ltp_cache.get("NIFTY2691525200CE"), 3.85)
        self.assertEqual(self.session.get_symbol_ltp("NIFTY2691525200CE"), 3.85)

        rendered = self.session._render_orders_pane(self.session.last_orders_response, "orders_pending")
        self.assertIn("NIFTY2691525200CE", rendered)
        self.assertIn("₹3.85", rendered)

    def test_position_expiry_extraction_and_header(self):
        from cli.greeks import get_position_expiry_info, format_expiry_header
        import datetime

        # Weekly option
        p1 = {"tradingsymbol": "NIFTY2682824500CE"}
        info1 = get_position_expiry_info(p1)
        self.assertEqual(info1["expiry_date"], datetime.date(2026, 8, 28))
        self.assertEqual(info1["strike"], 24500.0)
        self.assertEqual(info1["option_type"], "CE")

        # Monthly option
        p2 = {"tradingsymbol": "NIFTY26JUL22500PE"}
        info2 = get_position_expiry_info(p2)
        self.assertEqual(info2["expiry_date"].month, 7)
        self.assertEqual(info2["strike"], 22500.0)
        self.assertEqual(info2["option_type"], "PE")

        # Equity / Non-expiring
        p3 = {"tradingsymbol": "INFY"}
        info3 = get_position_expiry_info(p3)
        self.assertIsNone(info3["expiry_date"])
        self.assertEqual(info3["option_type"], "EQ")

        # Header formatting
        today = datetime.date(2026, 8, 28)
        title, dte = format_expiry_header(datetime.date(2026, 8, 28), today=today)
        self.assertEqual(title, "28 AUG 2026")
        self.assertEqual(dte, "Today • 0 DTE")

        title_eq, dte_eq = format_expiry_header(None, today=today)
        self.assertEqual(title_eq, "EQUITY / OTHER")
        self.assertEqual(dte_eq, "")

        # Strike display formatting
        from cli.greeks import format_strike_display
        p_user = {"tradingsymbol": "NIFTY2690122800PE"}
        info_user = get_position_expiry_info(p_user)
        self.assertEqual(format_strike_display(info_user), "22800 PE")

        self.assertEqual(format_strike_display(info1), "24500 CE")
        self.assertEqual(format_strike_display(info2), "22500 PE")
        self.assertEqual(format_strike_display(info3), "EQ")

    def test_positions_rendered_expiry_wise(self):
        from cli.display import render_positions_to_string
        import datetime

        accounts_data = [
            {
                "name": "Account1",
                "api_key": "api_1",
                "total_pnl": 2000.0,
                "positions": [
                    {
                        "tradingsymbol": "NIFTY2690122800PE",
                        "quantity": 50,
                        "lot_size": 50,
                        "average_price": 30.0,
                        "last_price": 40.0,
                        "pnl": 500.0,
                    },
                    {
                        "tradingsymbol": "NIFTY2682824500CE",
                        "quantity": 50,
                        "lot_size": 50,
                        "average_price": 10.0,
                        "last_price": 40.0,
                        "pnl": 1500.0,
                    },
                ]
            }
        ]

        rendered = render_positions_to_string(accounts_data, width=100, show_indices=True)

        # 28 AUG should come before 01 SEP chronologically
        idx_28aug = rendered.find("28 AUG 2026")
        idx_01sep = rendered.find("01 SEP 2026")
        self.assertNotEqual(idx_28aug, -1)
        self.assertNotEqual(idx_01sep, -1)
        self.assertLess(idx_28aug, idx_01sep)

        # Subtotals and values should be present
        self.assertIn("+₹1,500.00", rendered)
        self.assertIn("+₹500.00", rendered)

        # Strike column should have human readable strikes
        self.assertIn("Strike", rendered)
        self.assertIn("24500 CE", rendered)
        self.assertIn("22800 PE", rendered)

        # Indices should be in chronological order
        self.assertIn("[1] NIFTY2682824500CE", rendered)
        self.assertIn("[2] NIFTY2690122800PE", rendered)

    def test_holdings_tab_switching_and_rendering(self):
        # Configure test holdings response
        self.session.last_holdings_response = {
            "accounts": [
                {
                    "name": "ZK8719",
                    "broker": "zerodha",
                    "status": "success",
                    "total_investment": 25000.0,
                    "total_current_value": 26000.0,
                    "total_pnl": 1000.0,
                    "total_day_change": 500.0,
                    "holdings": [
                        {
                            "tradingsymbol": "RELIANCE",
                            "exchange": "NSE",
                            "quantity": 10,
                            "average_price": 2500.0,
                            "last_price": 2600.0,
                            "investment_value": 25000.0,
                            "current_value": 26000.0,
                            "pnl": 1000.0,
                            "pnl_pct": 4.0,
                            "day_change": 500.0,
                        }
                    ]
                }
            ],
            "grand_total_investment": 25000.0,
            "grand_total_current_value": 26000.0,
            "grand_total_pnl": 1000.0,
            "grand_total_day_change": 500.0,
            "status": "success",
        }

        # 1. Summary view
        self.session.info_mode = "holdings"
        self.session.holdings_view_mode = "summary"
        rendered = self.session._render_holdings_pane()
        self.assertIn("ZK8719", rendered)
        self.assertIn("Invested", rendered)
        self.assertIn("Total", rendered)
        self.assertIn("25,000.00", rendered)
        self.assertIn("26,000.00", rendered)
        self.assertIn("1,000.00", rendered)

        # 2. Deep constituent view
        self.session.holdings_view_mode = "deep"
        rendered_deep = self.session._render_holdings_pane()
        self.assertIn("Zerodha", rendered_deep)
        self.assertIn("RELIANCE", rendered_deep)
        self.assertIn("2,500.00", rendered_deep)
        self.assertIn("2,600.00", rendered_deep)

        # 3. Tab fragments check
        tabs = self.session._get_tab_bar_text()
        tab_names = [frag[1].strip() for frag in tabs if len(frag) >= 2 and frag[1].strip()]
        self.assertIn("Holdings", tab_names)
        self.assertEqual(tab_names.index("Holdings"), tab_names.index("MD") + 1)


if __name__ == "__main__":
    unittest.main()
