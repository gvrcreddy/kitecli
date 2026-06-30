import sys
import unittest
import datetime
from unittest.mock import MagicMock, patch

# Ensure project root is in path
sys.path.append("/Users/vgolugur/Documents/Projects/kitecli")

from cli.live_session import KCLILiveSession

class TestCLIParser(unittest.IsolatedAsyncioTestCase):

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

        # Dummy position data
        self.session.last_positions_response = {
            "accounts": [
                {
                    "name": "ZK8719",
                    "api_key": "api_zk",
                    "status": "success",
                    "positions": [
                        {"tradingsymbol": "NIFTY26JUN22200PE", "quantity": -50}
                    ]
                }
            ]
        }
        self.session.active_positions = [
            {"tradingsymbol": "NIFTY26JUN22200PE", "quantity": -50, "api_key": "api_zk", "account_name": "ZK8719"}
        ]
        self.session.position_id_map = {
            1: self.session.active_positions[0]
        }

        # Dummy pending orders data across ZK8719 and WVV135
        self.session.last_orders_response = {
            "accounts": [
                {
                    "name": "ZK8719",
                    "api_key": "api_zk",
                    "orders": [
                        {
                            "order_id": "111111",
                            "status": "OPEN",
                            "tradingsymbol": "NIFTY26JUN22200PE",
                            "quantity": 50,
                            "price": 1.4,
                            "transaction_type": "BUY"
                        }
                    ]
                },
                {
                    "name": "WVV135",
                    "api_key": "api_wvv",
                    "orders": [
                        {
                            "order_id": "222222",
                            "status": "OPEN",
                            "tradingsymbol": "NIFTY26JUN22200PE",
                            "quantity": 50,
                            "price": 1.4,
                            "transaction_type": "BUY"
                        }
                    ]
                }
            ]
        }

    @patch("cli.advisor.get_nifty_options", return_value=[])
    async def test_exit_with_symbol_and_price(self, mock_options):
        # exit <symbol> <price>
        self.session._execute_single_command("exit NIFTY26JUN22200PE 1.4")
        self.assertIsNotNone(self.session.pending_order)
        self.assertEqual(self.session.pending_order["type"], "exit")
        self.assertEqual(self.session.pending_order["symbol"], "NIFTY26JUN22200PE")
        self.assertEqual(self.session.pending_order["price"], 1.4)
        
        # Confirming triggers execute_exit with the limit price
        with patch.object(self.session, "execute_exit", return_value=None) as mock_exec:
            self.session._execute_single_command("y")
            mock_exec.assert_called_once_with("NIFTY26JUN22200PE", ["api_zk"], 1.4)

    @patch("cli.advisor.get_nifty_options", return_value=[])
    async def test_exit_all_with_price(self, mock_options):
        # exit all <price>
        self.session._execute_single_command("exit all 2.5")
        self.assertIsNotNone(self.session.pending_order)
        self.assertEqual(self.session.pending_order["type"], "exit")
        self.assertEqual(self.session.pending_order["symbol"], "all")
        self.assertEqual(self.session.pending_order["price"], 2.5)

        with patch.object(self.session, "execute_exit", return_value=None) as mock_exec:
            self.session._execute_single_command("y")
            mock_exec.assert_called_once_with("all", [], 2.5)

    @patch("cli.advisor.get_nifty_options", return_value=[])
    async def test_exit_by_id_with_price(self, mock_options):
        # exit <id> <price>
        self.session._execute_single_command("exit 1 0.75")
        self.assertIsNotNone(self.session.pending_order)
        self.assertEqual(self.session.pending_order["type"], "exit")
        self.assertEqual(self.session.pending_order["symbol"], "NIFTY26JUN22200PE")
        self.assertEqual(self.session.pending_order["price"], 0.75)

        with patch.object(self.session, "execute_exit", return_value=None) as mock_exec:
            self.session._execute_single_command("y")
            mock_exec.assert_called_once_with("NIFTY26JUN22200PE", ["api_zk"], 0.75)

    @patch("cli.advisor.get_nifty_options", return_value=[])
    async def test_standard_exit_no_price(self, mock_options):
        # exit <symbol> (market order fallback)
        self.session._execute_single_command("exit NIFTY26JUN22200PE")
        self.assertIsNotNone(self.session.pending_order)
        self.assertEqual(self.session.pending_order["type"], "exit")
        self.assertEqual(self.session.pending_order["symbol"], "NIFTY26JUN22200PE")
        self.assertIsNone(self.session.pending_order["price"])

        with patch.object(self.session, "execute_exit", return_value=None) as mock_exec:
            self.session._execute_single_command("y")
            mock_exec.assert_called_once_with("NIFTY26JUN22200PE", ["api_zk"], None)

    async def test_multi_order_modify_by_symbol(self):
        # order <symbol> <qty> <price>
        self.session._execute_single_command("order NIFTY26JUN22200PE 50 1.55")
        self.assertIsNotNone(self.session.pending_order)
        self.assertEqual(self.session.pending_order["type"], "modify_multi")
        self.assertEqual(len(self.session.pending_order["orders"]), 2)
        self.assertEqual(self.session.pending_order["qty"], "50")
        self.assertEqual(self.session.pending_order["price"], "1.55")

        with patch.object(self.session, "execute_modify", return_value=None) as mock_mod:
            self.session._execute_single_command("y")
            # Verify modify is called for each order in parallel
            self.assertEqual(mock_mod.call_count, 2)
            mock_mod.assert_any_call("111111", "50", "1.55", "api_zk")
            mock_mod.assert_any_call("222222", "50", "1.55", "api_wvv")

    async def test_multi_order_cancel_by_symbol(self):
        # cancel <symbol>
        # Should match both pending orders for NIFTY26JUN22200PE
        self.session._execute_single_command("cancel NIFTY26JUN22200PE")
        self.assertIsNotNone(self.session.pending_order)
        self.assertEqual(self.session.pending_order["type"], "cancel_multi")
        self.assertEqual(len(self.session.pending_order["orders"]), 2)

        with patch.object(self.session, "execute_cancel", return_value=None) as mock_cancel:
            self.session._execute_single_command("y")
            # Verify cancel is called for each order in parallel
            self.assertEqual(mock_cancel.call_count, 2)
            mock_cancel.assert_any_call("111111", "api_zk")
            mock_cancel.assert_any_call("222222", "api_wvv")

if __name__ == "__main__":
    unittest.main()
