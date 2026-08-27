import sys
import unittest
import datetime
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

# Ensure project root is in path
sys.path.append("/Users/vgolugur/Documents/Projects/kitecli")

from cli.parser import (
    parse_command_line, parse_single_command,
    PlaceOrderCommand, ExitCommand,
    ConditionalOrderCommand, CancelConditionalOrderCommand,
    ConditionalOrdersViewCommand
)
from cli.recorder import DataRecorder
from cli.live_session import KCLILiveSession


class TestConditionalOrderParser(unittest.TestCase):

    def test_parse_then_place_and_place(self):
        cmd_str = "buy NIFTY2682025000CE 1L 150 THEN sell NIFTY2682025000CE 1L 180"
        cmds = parse_command_line(cmd_str)
        self.assertEqual(len(cmds), 1)
        self.assertIsInstance(cmds[0], ConditionalOrderCommand)
        co = cmds[0]
        self.assertIsInstance(co.parent_cmd, PlaceOrderCommand)
        self.assertEqual(co.parent_cmd.action, "BUY")
        self.assertEqual(co.parent_cmd.symbol_or_id, "NIFTY2682025000CE")
        self.assertEqual(co.parent_cmd.quantity, "1L")
        self.assertEqual(co.parent_cmd.price, 150.0)

        self.assertIsInstance(co.child_cmd, PlaceOrderCommand)
        self.assertEqual(co.child_cmd.action, "SELL")
        self.assertEqual(co.child_cmd.symbol_or_id, "NIFTY2682025000CE")
        self.assertEqual(co.child_cmd.quantity, "1L")
        self.assertEqual(co.child_cmd.price, 180.0)
        self.assertEqual(co.child_raw_text, "sell NIFTY2682025000CE 1L 180")

    def test_parse_arrow_syntax(self):
        cmd_str = "buy 1L 150 -> sell 1L 180"
        cmds = parse_command_line(cmd_str)
        self.assertEqual(len(cmds), 1)
        self.assertIsInstance(cmds[0], ConditionalOrderCommand)
        co = cmds[0]
        self.assertEqual(co.parent_cmd.action, "BUY")
        self.assertEqual(co.parent_cmd.price, 150.0)
        self.assertEqual(co.child_cmd.action, "SELL")
        self.assertEqual(co.child_cmd.price, 180.0)

    def test_parse_shorthand_child_inheritance(self):
        cmd_str = "buy NIFTY2682025000CE 2L 150 THEN sell 2L 180"
        cmds = parse_command_line(cmd_str)
        self.assertEqual(len(cmds), 1)
        co = cmds[0]
        self.assertEqual(co.child_cmd.action, "SELL")
        self.assertEqual(co.child_cmd.symbol_or_id, "NIFTY2682025000CE")
        self.assertEqual(co.child_cmd.quantity, "2L")
        self.assertEqual(co.child_cmd.price, 180.0)

    def test_parse_conditional_exit(self):
        cmd_str = "buy 1 1L 120 THEN exit 1 150"
        cmds = parse_command_line(cmd_str)
        self.assertEqual(len(cmds), 1)
        co = cmds[0]
        self.assertIsInstance(co.parent_cmd, PlaceOrderCommand)
        self.assertIsInstance(co.child_cmd, ExitCommand)
        self.assertEqual(co.child_cmd.target, "1")
        self.assertEqual(co.child_cmd.price, 150.0)

    def test_parse_cancel_conditional(self):
        cmds = parse_command_line("cancel co 5")
        self.assertEqual(len(cmds), 1)
        self.assertIsInstance(cmds[0], CancelConditionalOrderCommand)
        self.assertEqual(cmds[0].target, "5")

        cmds_all = parse_command_line("cancel cond all")
        self.assertEqual(len(cmds_all), 1)
        self.assertIsInstance(cmds_all[0], CancelConditionalOrderCommand)
        self.assertEqual(cmds_all[0].target, "all")

    def test_parse_conditional_view(self):
        cmds1 = parse_command_line("cond")
        self.assertEqual(len(cmds1), 1)
        self.assertIsInstance(cmds1[0], ConditionalOrdersViewCommand)

        cmds2 = parse_command_line("co")
        self.assertEqual(len(cmds2), 1)
        self.assertIsInstance(cmds2[0], ConditionalOrdersViewCommand)


class TestDataRecorderConditionalOrders(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_data.db"
        self.recorder = DataRecorder(db_path=self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_save_and_retrieve_pending(self):
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        cond_id = self.recorder.save_conditional_order(
            trade_date=today_str,
            parent_order_ids=["240827001", "240827002"],
            parent_symbol="NIFTY2682025000CE",
            parent_action="BUY",
            parent_qty=50,
            parent_price=150.0,
            child_command_raw="sell NIFTY2682025000CE 1L 180",
            child_action="SELL",
            child_symbol="NIFTY2682025000CE",
            child_qty="50",
            child_price=180.0,
            child_product="NRML",
            user_id="USER1",
            account_key="api_key_1",
            account_name="Account1",
        )
        self.assertGreater(cond_id, 0)

        pending = self.recorder.get_pending_conditional_orders(today_str)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], cond_id)
        self.assertEqual(pending[0]["parent_order_ids"], ["240827001", "240827002"])
        self.assertEqual(pending[0]["status"], "PENDING")

    def test_expire_old_conditional_orders(self):
        yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        today_str = datetime.date.today().strftime("%Y-%m-%d")

        # Insert old order
        old_id = self.recorder.save_conditional_order(
            trade_date=yesterday_str,
            parent_order_ids=["240826001"],
            parent_symbol="NIFTY2682025000CE",
            parent_action="BUY",
            parent_qty=50,
            parent_price=150.0,
            child_command_raw="sell 180",
            child_action="SELL",
            child_symbol="NIFTY2682025000CE",
            child_qty="50",
            child_price=180.0,
        )

        # Expire old orders relative to today
        expired_cnt = self.recorder.expire_old_conditional_orders(today_str)
        self.assertEqual(expired_cnt, 1)

        # Ensure yesterday's order is no longer in pending
        pending_today = self.recorder.get_pending_conditional_orders(today_str)
        self.assertEqual(len(pending_today), 0)

        all_yesterday = self.recorder.get_all_conditional_orders_today(yesterday_str)
        self.assertEqual(len(all_yesterday), 1)
        self.assertEqual(all_yesterday[0]["status"], "EXPIRED")

    def test_update_status_and_cancel(self):
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        cond_id = self.recorder.save_conditional_order(
            trade_date=today_str,
            parent_order_ids=["240827003"],
            parent_symbol="NIFTY2682025000CE",
            parent_action="BUY",
            parent_qty=50,
            parent_price=150.0,
            child_command_raw="sell 180",
            child_action="SELL",
            child_symbol="NIFTY2682025000CE",
            child_qty="50",
            child_price=180.0,
        )

        # Update to TRIGGERED
        self.recorder.update_conditional_order_status(cond_id, "TRIGGERED", child_order_ids=["240827999"])
        all_today = self.recorder.get_all_conditional_orders_today(today_str)
        self.assertEqual(all_today[0]["status"], "TRIGGERED")
        self.assertEqual(all_today[0]["child_order_ids"], ["240827999"])
        self.assertIsNotNone(all_today[0]["triggered_at"])

    def test_cancel_by_parent(self):
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        cond_id = self.recorder.save_conditional_order(
            trade_date=today_str,
            parent_order_ids=["240827005"],
            parent_symbol="NIFTY2682025000CE",
            parent_action="BUY",
            parent_qty=50,
            parent_price=150.0,
            child_command_raw="sell 180",
            child_action="SELL",
            child_symbol="NIFTY2682025000CE",
            child_qty="50",
            child_price=180.0,
        )

        cancelled = self.recorder.cancel_conditional_orders_by_parent("240827005")
        self.assertEqual(cancelled, [cond_id])

        pending = self.recorder.get_pending_conditional_orders(today_str)
        self.assertEqual(len(pending), 0)


class TestLiveSessionConditionalOrders(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_live_data.db"

        self.client = MagicMock()
        self.client.get_nfo_lot_sizes.return_value = {"NIFTY2682025000CE": 50}
        self.accounts = [
            {"name": "Account1", "api_key": "api_1", "user_id": "U1"}
        ]
        with patch("cli.live_session.DataRecorder") as mock_rec_cls:
            self.mock_recorder = DataRecorder(db_path=self.db_path)
            mock_rec_cls.return_value = self.mock_recorder
            self.session = KCLILiveSession(self.client, self.accounts)

        self.session.prompt_control = MagicMock()
        self.session.header_control = MagicMock()
        self.session.log_message = MagicMock()
        self.session.active_positions = []
        self.session.position_id_map = {}

    async def asyncTearDown(self):
        self.tmp_dir.cleanup()

    async def test_conditional_order_confirmation_flow(self):
        # 1. User enters conditional command
        cmd_str = "buy NIFTY2682025000CE 1L 150 THEN sell NIFTY2682025000CE 1L 180"
        self.session._execute_single_command(cmd_str)

        # Check that confirmation was requested
        self.assertIsNotNone(self.session.pending_order)
        self.assertEqual(self.session.pending_order["type"], "conditional")

        # Mock place_order response
        self.client.place_order.return_value = {
            "status": "success",
            "results": [
                {
                    "name": "Account1",
                    "api_key": "api_1",
                    "status": "success",
                    "order_id": "240827101",
                    "message": "Order placed successfully"
                }
            ]
        }

        # 2. Confirm order
        self.session._execute_single_command("y")
        # Give asyncio event loop a brief moment to run tasks
        import asyncio
        await asyncio.sleep(0.05)

        # Verify parent order was placed
        self.client.place_order.assert_called_once()
        self.assertEqual(self.client.place_order.call_args[1]["quantity"], 50)
        self.assertEqual(self.client.place_order.call_args[1]["price"], 150.0)

        # Verify conditional order was registered in memory & SQLite
        self.assertIn("240827101", self.session.conditional_orders_map)
        cond_entry = self.session.conditional_orders_map["240827101"]
        self.assertEqual(cond_entry["status"], "PENDING")
        self.assertEqual(cond_entry["parent_symbol"], "NIFTY2682025000CE")

    async def test_websocket_trigger_on_parent_complete(self):
        # Register a conditional order
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        cond_id = self.session.recorder.save_conditional_order(
            trade_date=today_str,
            parent_order_ids=["240827201"],
            parent_symbol="NIFTY2682025000CE",
            parent_action="BUY",
            parent_qty=50,
            parent_price=150.0,
            child_command_raw="sell NIFTY2682025000CE 1L 180",
            child_action="SELL",
            child_symbol="NIFTY2682025000CE",
            child_qty="50",
            child_price=180.0,
            child_product="NRML",
            account_key="api_1",
            account_name="Account1",
        )
        self.session.conditional_orders_map["240827201"] = {
            "id": cond_id,
            "trade_date": today_str,
            "parent_order_ids": ["240827201"],
            "parent_symbol": "NIFTY2682025000CE",
            "parent_action": "BUY",
            "parent_qty": 50,
            "child_command_raw": "sell NIFTY2682025000CE 1L 180",
            "child_action": "SELL",
            "child_symbol": "NIFTY2682025000CE",
            "child_qty": "50",
            "child_price": 180.0,
            "child_product": "NRML",
            "status": "PENDING",
            "account_key": "api_1",
            "account_name": "Account1",
        }

        # Mock second place_order for child
        self.client.place_order.return_value = {
            "status": "success",
            "results": [{"name": "Account1", "api_key": "api_1", "status": "success", "order_id": "240827202"}]
        }

        # Simulate WebSocket order update with COMPLETE
        ws_callback = self.session._make_on_order_update("api_1")
        ws_callback(
            None,
            {
                "order_id": "240827201",
                "status": "COMPLETE",
                "tradingsymbol": "NIFTY2682025000CE",
                "quantity": 50,
                "filled_quantity": 50,
                "transaction_type": "BUY"
            }
        )

        import asyncio
        await asyncio.sleep(0.05)

        # Verify child order was submitted
        self.client.place_order.assert_called_once()
        self.assertEqual(self.client.place_order.call_args[1]["transaction_type"], "SELL")
        self.assertEqual(self.client.place_order.call_args[1]["quantity"], 50)
        self.assertEqual(self.client.place_order.call_args[1]["price"], 180.0)

        # Verify removed from in-memory watch map
        self.assertNotIn("240827201", self.session.conditional_orders_map)

        # Verify DB status updated to TRIGGERED
        all_today = self.session.recorder.get_all_conditional_orders_today(today_str)
        self.assertEqual(all_today[0]["status"], "TRIGGERED")

    async def test_parent_cancelled_cancels_child(self):
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        cond_id = self.session.recorder.save_conditional_order(
            trade_date=today_str,
            parent_order_ids=["240827301"],
            parent_symbol="NIFTY2682025000CE",
            parent_action="BUY",
            parent_qty=50,
            parent_price=150.0,
            child_command_raw="sell 180",
            child_action="SELL",
            child_symbol="NIFTY2682025000CE",
            child_qty="50",
            child_price=180.0,
        )
        self.session.conditional_orders_map["240827301"] = {
            "id": cond_id,
            "trade_date": today_str,
            "parent_order_ids": ["240827301"],
            "parent_symbol": "NIFTY2682025000CE",
            "status": "PENDING",
            "child_command_raw": "sell 180",
        }

        # Simulate WebSocket order update: CANCELLED
        ws_callback = self.session._make_on_order_update("api_1")
        ws_callback(
            None,
            {
                "order_id": "240827301",
                "status": "CANCELLED",
                "tradingsymbol": "NIFTY2682025000CE",
                "quantity": 50,
            }
        )

        import asyncio
        await asyncio.sleep(0.05)

        # Ensure not placed
        self.client.place_order.assert_not_called()
        self.assertNotIn("240827301", self.session.conditional_orders_map)

        all_today = self.session.recorder.get_all_conditional_orders_today(today_str)
        self.assertEqual(all_today[0]["status"], "CANCELLED")

    async def test_per_account_dynamic_quantity_resolution(self):
        # Account 1 has short -50, Account 2 has short -100 in NIFTY2690822400PE
        self.session.accounts = [
            {"name": "Account1", "api_key": "api_1", "user_id": "U1"},
            {"name": "Account2", "api_key": "api_2", "user_id": "U2"}
        ]
        self.session.api_key_to_user_id = {"api_1": "U1", "api_2": "U2"}
        self.session.active_positions = [
            {"tradingsymbol": "NIFTY2690822400PE", "quantity": -50, "api_key": "api_1", "account_name": "Account1"},
            {"tradingsymbol": "NIFTY2690822400PE", "quantity": -100, "api_key": "api_2", "account_name": "Account2"}
        ]

        def mock_place(api_keys, tradingsymbol, transaction_type, quantity, order_type, price, product, exchange):
            key = api_keys[0]
            oid = "ORD_1" if key == "api_1" else "ORD_2"
            return {"results": [{"name": key, "api_key": key, "status": "success", "order_id": oid}]}

        self.client.place_order.side_effect = mock_place

        # User enters: buy NIFTY2690822400PE 3.5 sell NIFTY2690822500PE 3.65 (no qty specified)
        self.session._execute_single_command("buy NIFTY2690822400PE 3.5 sell NIFTY2690822500PE 3.65")

        # Confirm order
        self.session._execute_single_command("y")
        import asyncio
        await asyncio.sleep(0.05)

        # Verify Account 1 got buy for 50, Account 2 got buy for 100
        self.assertEqual(self.client.place_order.call_count, 2)
        call1 = self.client.place_order.call_args_list[0][1]
        call2 = self.client.place_order.call_args_list[1][1]
        self.assertEqual(call1["quantity"], 50)
        self.assertEqual(call1["api_keys"], ["api_1"])
        self.assertEqual(call2["quantity"], 100)
        self.assertEqual(call2["api_keys"], ["api_2"])

        # Check registered conditional orders have matching child quantities: 50 for ORD_1, 100 for ORD_2
        self.assertIn("ORD_1", self.session.conditional_orders_map)
        self.assertIn("ORD_2", self.session.conditional_orders_map)
        self.assertEqual(self.session.conditional_orders_map["ORD_1"]["child_qty"], "50")
        self.assertEqual(self.session.conditional_orders_map["ORD_2"]["child_qty"], "100")

    async def test_websocket_trigger_from_background_thread(self):
        # Store active event loop in session
        import threading
        import asyncio
        self.session._main_loop = asyncio.get_running_loop()

        today_str = datetime.date.today().strftime("%Y-%m-%d")
        cond_id = self.session.recorder.save_conditional_order(
            trade_date=today_str,
            parent_order_ids=["ORD_BG_1"],
            parent_symbol="NIFTY2690822400PE",
            parent_action="BUY",
            parent_qty=50,
            parent_price=3.5,
            child_command_raw="sell NIFTY2690822500PE 50 3.65",
            child_action="SELL",
            child_symbol="NIFTY2690822500PE",
            child_qty="50",
            child_price=3.65,
            child_product="NRML",
            account_key="api_1",
            account_name="Account1",
        )
        self.session.conditional_orders_map["ORD_BG_1"] = {
            "id": cond_id,
            "trade_date": today_str,
            "parent_order_ids": ["ORD_BG_1"],
            "parent_symbol": "NIFTY2690822400PE",
            "parent_action": "BUY",
            "parent_qty": 50,
            "child_command_raw": "sell NIFTY2690822500PE 50 3.65",
            "child_action": "SELL",
            "child_symbol": "NIFTY2690822500PE",
            "child_qty": "50",
            "child_price": 3.65,
            "child_product": "NRML",
            "status": "PENDING",
            "account_key": "api_1",
            "account_name": "Account1",
        }

        self.client.place_order.return_value = {
            "status": "success",
            "results": [{"name": "Account1", "api_key": "api_1", "status": "success", "order_id": "ORD_CHILD_1"}]
        }

        ws_callback = self.session._make_on_order_update("api_1")

        # Run ws_callback in an actual background OS thread (simulating Twisted reactor thread)
        error_container = []
        def bg_worker():
            try:
                ws_callback(
                    None,
                    {
                        "order_id": "ORD_BG_1",
                        "status": "COMPLETE",
                        "tradingsymbol": "NIFTY2690822400PE",
                        "quantity": 50,
                        "filled_quantity": 50,
                        "transaction_type": "BUY"
                    }
                )
            except Exception as e:
                error_container.append(e)

        thread = threading.Thread(target=bg_worker)
        thread.start()
        thread.join()

        self.assertEqual(len(error_container), 0, f"Background worker raised error: {error_container}")
        await asyncio.sleep(0.05)

        # Verify child order was executed successfully
        self.client.place_order.assert_called_once()
        self.assertEqual(self.client.place_order.call_args[1]["transaction_type"], "SELL")
        self.assertEqual(self.client.place_order.call_args[1]["quantity"], 50)
        self.assertEqual(self.client.place_order.call_args[1]["tradingsymbol"], "NIFTY2690822500PE")


if __name__ == "__main__":
    unittest.main()
