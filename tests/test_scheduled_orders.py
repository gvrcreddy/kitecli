import sys
import asyncio
import unittest
from datetime import datetime, date, timedelta
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

# Ensure project root is in path
sys.path.append("/Users/vgolugur/Documents/Projects/kitecli")

from cli.parser import (
    parse_command_line, parse_single_command,
    PlaceOrderCommand, ExitCommand,
    ConditionalOrderCommand,
    CancelScheduledOrderCommand, ScheduledOrdersViewCommand
)
from cli.recorder import DataRecorder
from cli.live_session import KCLILiveSession


class TestScheduledOrderParser(unittest.TestCase):

    def test_parse_timed_buy(self):
        cmds = parse_command_line("b INFY 10 1450 at 23:50")
        self.assertEqual(len(cmds), 1)
        cmd = cmds[0]
        self.assertIsInstance(cmd, PlaceOrderCommand)
        self.assertEqual(cmd.action, "BUY")
        self.assertEqual(cmd.symbol_or_id, "INFY")
        self.assertEqual(cmd.quantity, "10")
        self.assertEqual(cmd.price, 1450.0)
        self.assertIsNotNone(cmd.trigger_time)
        self.assertEqual(cmd.trigger_time.hour, 23)
        self.assertEqual(cmd.trigger_time.minute, 50)

    def test_parse_timed_sell_with_seconds(self):
        cmds = parse_command_line("s NIFTY26JUL22200PE 50 120 at 23:55:05")
        self.assertEqual(len(cmds), 1)
        cmd = cmds[0]
        self.assertIsInstance(cmd, PlaceOrderCommand)
        self.assertEqual(cmd.action, "SELL")
        self.assertEqual(cmd.symbol_or_id, "NIFTY26JUL22200PE")
        self.assertEqual(cmd.quantity, "50")
        self.assertEqual(cmd.price, 120.0)
        self.assertIsNotNone(cmd.trigger_time)
        self.assertEqual(cmd.trigger_time.hour, 23)
        self.assertEqual(cmd.trigger_time.minute, 55)
        self.assertEqual(cmd.trigger_time.second, 5)

    def test_parse_timed_exit(self):
        cmds = parse_command_line("exit 1 at 23:58")
        self.assertEqual(len(cmds), 1)
        cmd = cmds[0]
        self.assertIsInstance(cmd, ExitCommand)
        self.assertEqual(cmd.target, "1")
        self.assertIsNotNone(cmd.trigger_time)
        self.assertEqual(cmd.trigger_time.hour, 23)
        self.assertEqual(cmd.trigger_time.minute, 58)

    def test_parse_timed_order_with_date_first(self):
        cmds = parse_command_line("b INFY 10 1450 on 2026-09-05 at 09:20")
        self.assertEqual(len(cmds), 1)
        cmd = cmds[0]
        self.assertIsInstance(cmd, PlaceOrderCommand)
        self.assertIsNotNone(cmd.trigger_time)
        self.assertEqual(cmd.trigger_time.year, 2026)
        self.assertEqual(cmd.trigger_time.month, 9)
        self.assertEqual(cmd.trigger_time.day, 5)
        self.assertEqual(cmd.trigger_time.hour, 9)
        self.assertEqual(cmd.trigger_time.minute, 20)

    def test_parse_timed_order_with_date_after_time(self):
        cmds = parse_command_line("b INFY 10 1450 at 09:20 2026-09-05")
        self.assertEqual(len(cmds), 1)
        cmd = cmds[0]
        self.assertIsInstance(cmd, PlaceOrderCommand)
        self.assertIsNotNone(cmd.trigger_time)
        self.assertEqual(cmd.trigger_time.year, 2026)
        self.assertEqual(cmd.trigger_time.month, 9)
        self.assertEqual(cmd.trigger_time.day, 5)
        self.assertEqual(cmd.trigger_time.hour, 9)
        self.assertEqual(cmd.trigger_time.minute, 20)

    def test_parse_timed_conditional_chain(self):
        cmds = parse_command_line("buy NIFTY2682025000CE 1L 150 at 23:50 THEN sell 1L 180")
        self.assertEqual(len(cmds), 1)
        self.assertIsInstance(cmds[0], ConditionalOrderCommand)
        co = cmds[0]
        self.assertIsInstance(co.parent_cmd, PlaceOrderCommand)
        self.assertEqual(co.parent_cmd.action, "BUY")
        self.assertEqual(co.parent_cmd.symbol_or_id, "NIFTY2682025000CE")
        self.assertIsNotNone(co.parent_cmd.trigger_time)
        self.assertEqual(co.parent_cmd.trigger_time.hour, 23)
        self.assertEqual(co.parent_cmd.trigger_time.minute, 50)

        self.assertIsInstance(co.child_cmd, PlaceOrderCommand)
        self.assertEqual(co.child_cmd.action, "SELL")
        self.assertEqual(co.child_cmd.price, 180.0)

    def test_parse_cancel_sched(self):
        cmds = parse_command_line("cancel sched 3")
        self.assertEqual(len(cmds), 1)
        self.assertIsInstance(cmds[0], CancelScheduledOrderCommand)
        self.assertEqual(cmds[0].target, "3")

        cmds_all = parse_command_line("cancel sched all")
        self.assertEqual(len(cmds_all), 1)
        self.assertIsInstance(cmds_all[0], CancelScheduledOrderCommand)
        self.assertEqual(cmds_all[0].target, "all")

    def test_parse_sched_view(self):
        cmds1 = parse_command_line("sched")
        self.assertEqual(len(cmds1), 1)
        self.assertIsInstance(cmds1[0], ScheduledOrdersViewCommand)

        cmds2 = parse_command_line("scheduled")
        self.assertEqual(len(cmds2), 1)
        self.assertIsInstance(cmds2[0], ScheduledOrdersViewCommand)


class TestDataRecorderScheduledOrders(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_data.db"
        self.recorder = DataRecorder(db_path=self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_save_and_retrieve_pending(self):
        sched_id = self.recorder.save_scheduled_order(
            trigger_time="2026-09-01 09:20:00",
            command_raw="b INFY 10 1450 at 09:20",
            action="BUY",
            symbol_or_id="INFY",
            quantity="10",
            price=1450.0,
            product="NRML",
            account_key="api_1",
            account_name="Account1",
            is_conditional=False,
        )
        self.assertGreater(sched_id, 0)

        pending = self.recorder.get_pending_scheduled_orders()
        self.assertEqual(len(pending), 1)
        item = pending[0]
        self.assertEqual(item["id"], sched_id)
        self.assertEqual(item["symbol_or_id"], "INFY")
        self.assertEqual(item["action"], "BUY")
        self.assertEqual(item["status"], "PENDING")

    def test_update_status(self):
        sched_id = self.recorder.save_scheduled_order(
            trigger_time="2026-09-01 09:20:00",
            command_raw="b INFY 10 1450 at 09:20",
            action="BUY",
            symbol_or_id="INFY",
            quantity="10",
            price=1450.0,
        )
        self.recorder.update_scheduled_order_status(sched_id, "TRIGGERED")
        pending = self.recorder.get_pending_scheduled_orders()
        self.assertEqual(len(pending), 0)

        today_str = datetime.now().strftime("%Y-%m-%d")
        all_today = self.recorder.get_all_scheduled_orders_today(today_str)
        self.assertEqual(len(all_today), 1)
        self.assertEqual(all_today[0]["status"], "TRIGGERED")

    def test_cancel_scheduled_order(self):
        sched_id = self.recorder.save_scheduled_order(
            trigger_time="2026-09-01 09:20:00",
            command_raw="b INFY 10 1450 at 09:20",
            action="BUY",
            symbol_or_id="INFY",
            quantity="10",
            price=1450.0,
        )
        ok = self.recorder.cancel_scheduled_order(sched_id)
        self.assertTrue(ok)
        pending = self.recorder.get_pending_scheduled_orders()
        self.assertEqual(len(pending), 0)

    def test_cancel_all_scheduled_orders(self):
        self.recorder.save_scheduled_order(
            trigger_time="2026-09-01 09:20:00",
            command_raw="b INFY 10 1450 at 09:20",
            action="BUY",
            symbol_or_id="INFY",
            quantity="10",
            price=1450.0,
        )
        self.recorder.save_scheduled_order(
            trigger_time="2026-09-01 09:25:00",
            command_raw="b TCS 5 3500 at 09:25",
            action="BUY",
            symbol_or_id="TCS",
            quantity="5",
            price=3500.0,
        )
        cnt = self.recorder.cancel_all_scheduled_orders()
        self.assertEqual(cnt, 2)
        pending = self.recorder.get_pending_scheduled_orders()
        self.assertEqual(len(pending), 0)

    def test_expire_old_scheduled_orders(self):
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.recorder.save_scheduled_order(
            trigger_time=f"{yesterday_str} 09:20:00",
            command_raw="b INFY 10 1450 at 09:20",
            action="BUY",
            symbol_or_id="INFY",
            quantity="10",
            price=1450.0,
        )
        today_str = datetime.now().strftime("%Y-%m-%d")
        expired_cnt = self.recorder.expire_old_scheduled_orders(today_str)
        self.assertEqual(expired_cnt, 1)
        pending = self.recorder.get_pending_scheduled_orders()
        self.assertEqual(len(pending), 0)


class TestLiveSessionScheduledOrders(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_live_data.db"
        self.client = MagicMock()
        self.client.init_accounts = MagicMock(return_value={})
        self.accounts = [{"api_key": "api_1", "name": "Account1", "broker": "zerodha"}]

        with patch("cli.live_session.DataRecorder") as mock_rec_cls:
            self.real_recorder = DataRecorder(db_path=self.db_path)
            mock_rec_cls.return_value = self.real_recorder
            self.session = KCLILiveSession(self.client, self.accounts)
            self.session.recorder = self.real_recorder
            self.session.app = MagicMock()
            self.session.app.loop = asyncio.get_running_loop()
            self.session.active_positions = [
                {"tradingsymbol": "NIFTY2690822400PE", "quantity": 50, "api_key": "api_1", "position_id": 1}
            ]

    async def asyncTearDown(self):
        self.session.running = False
        self.tmp_dir.cleanup()

    async def test_register_and_poll_scheduled_trigger(self):
        past_dt = datetime.now() - timedelta(seconds=1)
        cmd = PlaceOrderCommand(
            action="BUY",
            symbol_or_id="NIFTY2690822400PE",
            quantity="50",
            price=120.0,
            product="NRML",
            trigger_time=past_dt
        )
        self.session.execute_order = AsyncMock()

        sched_id = self.session.register_scheduled_order(cmd, "b NIFTY2690822400PE 50 120 at ...", ["api_1"])
        self.assertIn(sched_id, self.session.scheduled_orders_map)

        # Run poller step
        poll_task = asyncio.create_task(self.session._poll_scheduled_orders())
        await asyncio.sleep(0.6)
        self.session.running = False
        await poll_task

        self.session.execute_order.assert_called_once()
        self.assertEqual(self.session.execute_order.call_args[1]["symbol"], "NIFTY2690822400PE")
        self.assertEqual(self.session.execute_order.call_args[1]["transaction_type"], "BUY")

    async def test_drift_protection_expires_delayed_orders(self):
        # Trigger time 120 seconds in the past (e.g. laptop was suspended)
        drift_dt = datetime.now() - timedelta(seconds=120)
        cmd = PlaceOrderCommand(
            action="BUY",
            symbol_or_id="NIFTY2690822400PE",
            quantity="50",
            price=120.0,
            product="NRML",
            trigger_time=drift_dt
        )
        self.session.execute_order = AsyncMock()

        sched_id = self.session.register_scheduled_order(cmd, "b NIFTY2690822400PE 50 120 at ...", ["api_1"])

        poll_task = asyncio.create_task(self.session._poll_scheduled_orders())
        await asyncio.sleep(0.6)
        self.session.running = False
        await poll_task

        # Verify order was NOT executed and was marked EXPIRED
        self.session.execute_order.assert_not_called()
        today_str = datetime.now().strftime("%Y-%m-%d")
        all_today = self.session.recorder.get_all_scheduled_orders_today(today_str)
        self.assertEqual(len(all_today), 1)
        self.assertEqual(all_today[0]["status"], "EXPIRED")

    async def test_scheduled_conditional_trigger(self):
        past_dt = datetime.now() - timedelta(seconds=1)
        parent_cmd = PlaceOrderCommand(
            action="BUY",
            symbol_or_id="NIFTY2690822400PE",
            quantity="50",
            price=120.0,
            product="NRML",
            trigger_time=past_dt
        )
        child_cmd = PlaceOrderCommand(
            action="SELL",
            symbol_or_id="NIFTY2690822400PE",
            quantity="50",
            price=150.0,
            product="NRML"
        )
        cond_cmd = ConditionalOrderCommand(
            parent_cmd=parent_cmd,
            child_cmd=child_cmd,
            child_raw_text="sell 150"
        )
        self.session.execute_conditional_order = AsyncMock()

        sched_id = self.session.register_scheduled_order(cond_cmd, "b ... at ... then s 150", ["api_1"])
        self.assertIn(sched_id, self.session.scheduled_orders_map)

        poll_task = asyncio.create_task(self.session._poll_scheduled_orders())
        await asyncio.sleep(0.6)
        self.session.running = False
        await poll_task

        self.session.execute_conditional_order.assert_called_once()

    async def test_scheduled_orders_pane_rendering(self):
        self.session.recorder.save_scheduled_order(
            trigger_time="2026-09-01 09:20:00",
            command_raw="b INFY 10 1450 at 09:20",
            action="BUY",
            symbol_or_id="INFY",
            quantity="10",
            price=1450.0,
            account_name="Account1",
        )
        rendered = self.session._render_orders_pane({"accounts": []}, mode="orders_pending")
        self.assertIn("⏰ Scheduled Orders (Today):", rendered)
        self.assertIn("INFY", rendered)
        self.assertIn("Account1", rendered)
        self.assertIn("09:20:00", rendered)

    async def test_multi_account_omitted_quantity_scheduled_trigger(self):
        self.session.active_positions = [
            {"tradingsymbol": "NIFTY2690125000CE", "quantity": 50, "api_key": "api_1"},
            {"tradingsymbol": "NIFTY2690125000CE", "quantity": 100, "api_key": "api_2"},
        ]
        cmd = PlaceOrderCommand(
            action="BUY",
            symbol_or_id="NIFTY2690125000CE",
            quantity=None,  # Omitted quantity
            price=0.5,
            product="NRML",
            trigger_time=datetime.now() - timedelta(seconds=1)
        )
        self.session.execute_order = AsyncMock()
        self.session.register_scheduled_order(cmd, "b NIFTY2690125000CE 0.5 at ...", [])

        poll_task = asyncio.create_task(self.session._poll_scheduled_orders())
        await asyncio.sleep(0.6)
        self.session.running = False
        await poll_task

        # Verify each account's order was placed with its respective position quantity!
        self.assertEqual(self.session.execute_order.call_count, 2)
        self.session.execute_order.assert_any_call(
            symbol="NIFTY2690125000CE",
            transaction_type="BUY",
            qty_str="50",
            price_str="0.5",
            product="NRML",
            api_keys=["api_1"]
        )
        self.session.execute_order.assert_any_call(
            symbol="NIFTY2690125000CE",
            transaction_type="BUY",
            qty_str="100",
            price_str="0.5",
            product="NRML",
            api_keys=["api_2"]
        )


if __name__ == "__main__":
    unittest.main()
