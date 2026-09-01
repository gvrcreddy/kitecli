"""
Exhaustive Golden Matrix Regression Suite for KiteCLI Commands.

This suite ensures that new features, syntax expansions, or refactorings NEVER
regress existing shorthand, single-account, multi-account, order modification,
exit, or cancellation commands.
"""

import sys
import os
import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cli.parser import (
    parse_command_line,
    parse_single_command,
    PlaceOrderCommand,
    ExitCommand,
    ModifyOrderCommand,
    CancelOrderCommand,
    CancelConditionalOrderCommand,
    CancelScheduledOrderCommand,
    PositionSelectCommand,
    AccountSelectCommand,
    OrderSelectCommand,
    ConditionalOrderCommand,
    ConditionalOrdersViewCommand,
    ScheduledOrdersViewCommand,
    MarketDepthCommand,
    HoldingsCommand,
    PositionsCommand,
    OrdersCommand,
    StatusCommand,
)
from cli.live_session import KCLILiveSession


class TestParserSyntaxMatrix(unittest.TestCase):
    """Exhaustive syntax AST tests for all command forms."""

    # --- BUY SYNTAX MATRIX ---
    def test_buy_qty_only(self):
        # b <qty> (uses active selection)
        cmds = parse_command_line("b 100")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].action, "BUY")
        self.assertEqual(cmds[0].symbol_or_id, None)
        self.assertEqual(cmds[0].quantity, "100")
        self.assertEqual(cmds[0].price, None)

    def test_buy_lot_qty(self):
        # b 2L 150.5
        cmds = parse_command_line("b 2L 150.5")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].action, "BUY")
        self.assertEqual(cmds[0].quantity, "2L")
        self.assertEqual(cmds[0].price, 150.5)

    def test_buy_price_only(self):
        # b 1450.5 (omitted qty, limit price on active selection)
        cmds = parse_command_line("b 1450.5")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].action, "BUY")
        self.assertEqual(cmds[0].symbol_or_id, None)
        self.assertEqual(cmds[0].quantity, None)
        self.assertEqual(cmds[0].price, 1450.5)

    def test_buy_symbol_and_qty(self):
        # b INFY 10
        cmds = parse_command_line("b INFY 10")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].action, "BUY")
        self.assertEqual(cmds[0].symbol_or_id, "INFY")
        self.assertEqual(cmds[0].quantity, "10")
        self.assertEqual(cmds[0].price, None)

    def test_buy_symbol_and_price(self):
        # b INFY 1450.5 (omitted qty, limit price)
        cmds = parse_command_line("b INFY 1450.5")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].action, "BUY")
        self.assertEqual(cmds[0].symbol_or_id, "INFY")
        self.assertEqual(cmds[0].quantity, None)
        self.assertEqual(cmds[0].price, 1450.5)

    def test_buy_symbol_qty_price(self):
        # b INFY 10 1450.5
        cmds = parse_command_line("b INFY 10 1450.5")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].action, "BUY")
        self.assertEqual(cmds[0].symbol_or_id, "INFY")
        self.assertEqual(cmds[0].quantity, "10")
        self.assertEqual(cmds[0].price, 1450.5)

    def test_buy_symbol_qty_price_product(self):
        # b INFY 10 1450.5 CNC
        cmds = parse_command_line("b INFY 10 1450.5 CNC")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].action, "BUY")
        self.assertEqual(cmds[0].symbol_or_id, "INFY")
        self.assertEqual(cmds[0].quantity, "10")
        self.assertEqual(cmds[0].price, 1450.5)
        self.assertEqual(cmds[0].product, "CNC")

    def test_buy_position_id_qty_price(self):
        # b 1 50 120.0 (by position id)
        cmds = parse_command_line("b 1 50 120.0")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].action, "BUY")
        self.assertEqual(cmds[0].symbol_or_id, "1")
        self.assertEqual(cmds[0].quantity, "50")
        self.assertEqual(cmds[0].price, 120.0)

    # --- SELL SYNTAX MATRIX ---
    def test_sell_qty_only(self):
        cmds = parse_command_line("s 50")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].action, "SELL")
        self.assertEqual(cmds[0].quantity, "50")

    def test_sell_price_only(self):
        cmds = parse_command_line("s 180.0")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].action, "SELL")
        self.assertEqual(cmds[0].price, 180.0)
        self.assertEqual(cmds[0].quantity, None)

    def test_sell_symbol_qty_price(self):
        cmds = parse_command_line("s NIFTY26JUL22200PE 50 120.0 MIS")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].action, "SELL")
        self.assertEqual(cmds[0].symbol_or_id, "NIFTY26JUL22200PE")
        self.assertEqual(cmds[0].quantity, "50")
        self.assertEqual(cmds[0].price, 120.0)
        self.assertEqual(cmds[0].product, "MIS")

    # --- EXIT SYNTAX MATRIX ---
    def test_exit_selected_market(self):
        cmds = parse_command_line("exit")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].target, "selected")
        self.assertEqual(cmds[0].price, None)

    def test_exit_all_market(self):
        cmds = parse_command_line("exit all")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].target, "all")
        self.assertEqual(cmds[0].price, None)

    def test_exit_position_id_market(self):
        cmds = parse_command_line("exit 1")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].target, "1")
        self.assertEqual(cmds[0].price, None)

    def test_exit_price_only_limit(self):
        # exit 1.40 (float limit price on active selection)
        cmds = parse_command_line("exit 1.40")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].target, "selected")
        self.assertEqual(cmds[0].price, 1.40)

    def test_exit_position_id_limit(self):
        cmds = parse_command_line("exit 1 1.40")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].target, "1")
        self.assertEqual(cmds[0].price, 1.40)

    def test_exit_symbol_market(self):
        cmds = parse_command_line("exit INFY")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].target, "INFY")
        self.assertEqual(cmds[0].price, None)

    def test_exit_symbol_limit(self):
        cmds = parse_command_line("exit INFY 1500.0")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].target, "INFY")
        self.assertEqual(cmds[0].price, 1500.0)

    def test_exit_all_limit(self):
        cmds = parse_command_line("exit all 2.50")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].target, "all")
        self.assertEqual(cmds[0].price, 2.50)

    # --- MODIFY SYNTAX MATRIX ---
    def test_modify_price_only(self):
        for prefix in ("order", "modify", "m"):
            cmds = parse_command_line(f"{prefix} 1500.0")
            self.assertEqual(len(cmds), 1)
            self.assertEqual(cmds[0].order_id, "selected")
            self.assertEqual(cmds[0].quantity, None)
            self.assertEqual(cmds[0].price, 1500.0)

    def test_modify_id_or_sym_price_only(self):
        for prefix in ("order", "modify", "m"):
            cmds = parse_command_line(f"{prefix} 111111 1500.0")
            self.assertEqual(len(cmds), 1)
            self.assertEqual(cmds[0].order_id, "111111")
            self.assertEqual(cmds[0].quantity, None)
            self.assertEqual(cmds[0].price, 1500.0)

            cmds_sym = parse_command_line(f"{prefix} INFY 1500.0")
            self.assertEqual(len(cmds_sym), 1)
            self.assertEqual(cmds_sym[0].order_id, "INFY")
            self.assertEqual(cmds_sym[0].quantity, None)
            self.assertEqual(cmds_sym[0].price, 1500.0)

    def test_modify_id_qty_and_price(self):
        for prefix in ("order", "modify", "m"):
            cmds = parse_command_line(f"{prefix} 111111 10 1500.0")
            self.assertEqual(len(cmds), 1)
            self.assertEqual(cmds[0].order_id, "111111")
            self.assertEqual(cmds[0].quantity, "10")
            self.assertEqual(cmds[0].price, 1500.0)

    # --- SELECTION SYNTAX MATRIX ---
    def test_select_position_shorthand(self):
        cmds = parse_command_line("1")
        self.assertEqual(len(cmds), 1)
        self.assertIsInstance(cmds[0], PositionSelectCommand)
        self.assertEqual(cmds[0].target, "1")

        cmds_kw = parse_command_line("select 1")
        self.assertEqual(len(cmds_kw), 1)
        self.assertEqual(cmds_kw[0].target, "1")

        cmds_sym = parse_command_line("select INFY")
        self.assertEqual(len(cmds_sym), 1)
        self.assertEqual(cmds_sym[0].target, "INFY")

    def test_deselect(self):
        for kw in ("deselect", "select none", "s none"):
            cmds = parse_command_line(kw)
            self.assertEqual(len(cmds), 1)
            self.assertEqual(cmds[0].target, "none")

    def test_select_order(self):
        for kw in ("select order 111111", "s o 111111", "s ord 111111"):
            cmds = parse_command_line(kw)
            self.assertEqual(len(cmds), 1)
            self.assertIsInstance(cmds[0], OrderSelectCommand)
            self.assertEqual(cmds[0].target, "111111")

    def test_select_account(self):
        for kw in ("account Zerodha1", "acct Zerodha1", "a Zerodha1", "select account Zerodha1", "s a Zerodha1"):
            cmds = parse_command_line(kw)
            self.assertEqual(len(cmds), 1)
            self.assertIsInstance(cmds[0], AccountSelectCommand)
            self.assertEqual(cmds[0].account_name, "Zerodha1")

    # --- CANCEL SYNTAX MATRIX ---
    def test_cancel_orders(self):
        self.assertIsInstance(parse_command_line("cancel")[0], CancelOrderCommand)
        self.assertEqual(parse_command_line("cancel 111111")[0].target, "111111")
        self.assertEqual(parse_command_line("c INFY")[0].target, "INFY")

    def test_cancel_conditionals(self):
        for kw in ("cancel co 1", "cancel cond 1", "cancel conditional 1"):
            cmds = parse_command_line(kw)
            self.assertIsInstance(cmds[0], CancelConditionalOrderCommand)
            self.assertEqual(cmds[0].target, "1")

    def test_cancel_scheduled(self):
        for kw in ("cancel sched 1", "cancel scheduled 1", "cancel so 1"):
            cmds = parse_command_line(kw)
            self.assertIsInstance(cmds[0], CancelScheduledOrderCommand)
            self.assertEqual(cmds[0].target, "1")

    # --- SCHEDULED & CONDITIONAL SYNTAX MATRIX ---
    def test_scheduled_order_time_clause(self):
        cmds = parse_command_line("b INFY 10 1450 at 23:50")
        self.assertEqual(cmds[0].trigger_time.hour, 23)
        self.assertEqual(cmds[0].trigger_time.minute, 50)

    def test_conditional_order_chain(self):
        for sep in ("THEN", "then", "->", "=>"):
            cmds = parse_command_line(f"b INFY 10 1450.0 {sep} s 1500.0")
            self.assertEqual(len(cmds), 1)
            self.assertIsInstance(cmds[0], ConditionalOrderCommand)
            self.assertEqual(cmds[0].parent_cmd.action, "BUY")
            self.assertEqual(cmds[0].child_cmd.action, "SELL")


class TestExecutionCommandMatrix(unittest.IsolatedAsyncioTestCase):
    """Tests execution resolution, prompt confirmation, and dispatch across accounts."""

    async def asyncSetUp(self):
        self.client = MagicMock()
        self.accounts = [
            {"name": "Account1", "api_key": "api_1", "broker": "zerodha"},
            {"name": "Account2", "api_key": "api_2", "broker": "zerodha"}
        ]
        self.session = KCLILiveSession(self.client, self.accounts)
        self.session.prompt_control = MagicMock()
        self.session.header_control = MagicMock()
        self.session.log_message = MagicMock()

        self.session.active_positions = [
            {"tradingsymbol": "NIFTY26JUL22200PE", "quantity": 50, "api_key": "api_1", "account_name": "Account1"},
            {"tradingsymbol": "NIFTY26JUL22200PE", "quantity": 100, "api_key": "api_2", "account_name": "Account2"},
        ]
        self.session.position_id_map = {
            1: self.session.active_positions[0],
            2: self.session.active_positions[1]
        }

        self.session.last_orders_response = {
            "accounts": [
                {
                    "name": "Account1",
                    "api_key": "api_1",
                    "orders": [
                        {"order_id": "ORD111", "status": "OPEN", "tradingsymbol": "NIFTY26JUL22200PE", "quantity": 50, "price": 1.4}
                    ]
                },
                {
                    "name": "Account2",
                    "api_key": "api_2",
                    "orders": [
                        {"order_id": "ORD222", "status": "OPEN", "tradingsymbol": "NIFTY26JUL22200PE", "quantity": 100, "price": 1.4}
                    ]
                }
            ]
        }

    async def test_order_price_only_modify_retains_quantities(self):
        # order <sym> <price> modifying price only across all matching orders
        self.session._execute_single_command("order NIFTY26JUL22200PE 1.85")
        self.assertIsNotNone(self.session.pending_order)
        self.assertEqual(self.session.pending_order["type"], "modify_multi")
        self.assertEqual(len(self.session.pending_order["orders"]), 2)

        with patch.object(self.session, "execute_modify", new_callable=AsyncMock) as mock_mod:
            self.session._execute_single_command("y")
            self.assertEqual(mock_mod.call_count, 2)
            mock_mod.assert_any_call("ORD111", "50", "1.85", "api_1")
            mock_mod.assert_any_call("ORD222", "100", "1.85", "api_2")

    async def test_omitted_quantity_multi_account_buy(self):
        # b NIFTY26JUL22200PE 1.50 (omitted quantity across ALL accounts)
        self.session._execute_single_command("b NIFTY26JUL22200PE 1.50")
        self.assertIsNotNone(self.session.pending_order)
        self.assertEqual(self.session.pending_order["type"], "buy")
        self.assertIsNotNone(self.session.pending_order["orders"])
        self.assertEqual(len(self.session.pending_order["orders"]), 2)

        with patch.object(self.session, "execute_order", new_callable=AsyncMock) as mock_exec:
            self.session._execute_single_command("y")
            await asyncio.sleep(0.01)
            self.assertEqual(mock_exec.call_count, 2)
            mock_exec.assert_any_call(
                symbol="NIFTY26JUL22200PE",
                transaction_type="BUY",
                qty_str="50",
                price_str="1.5",
                product="NRML",
                api_keys=["api_1"]
            )
            mock_exec.assert_any_call(
                symbol="NIFTY26JUL22200PE",
                transaction_type="BUY",
                qty_str="100",
                price_str="1.5",
                product="NRML",
                api_keys=["api_2"]
            )

    async def test_exit_position_id_limit_resolution(self):
        # exit 1 1.40 should exit position #1 at price 1.40
        self.session._execute_single_command("exit 1 1.40")
        self.assertIsNotNone(self.session.pending_order)
        self.assertEqual(self.session.pending_order["type"], "exit")
        self.assertEqual(self.session.pending_order["symbol"], "NIFTY26JUL22200PE")
        self.assertEqual(self.session.pending_order["price"], 1.40)

        with patch.object(self.session, "execute_exit", new_callable=AsyncMock) as mock_exit:
            self.session._execute_single_command("y")
            mock_exit.assert_called_once_with("NIFTY26JUL22200PE", ["api_1"], 1.40)

    async def test_exit_all_limit_resolution(self):
        # exit all 2.50 should exit all positions at price 2.50
        self.session._execute_single_command("exit all 2.50")
        self.assertIsNotNone(self.session.pending_order)
        self.assertEqual(self.session.pending_order["type"], "exit")
        self.assertEqual(self.session.pending_order["symbol"], "all")
        self.assertEqual(self.session.pending_order["price"], 2.50)

        with patch.object(self.session, "execute_exit", new_callable=AsyncMock) as mock_exit:
            self.session._execute_single_command("y")
            mock_exit.assert_called_once_with("all", [], 2.50)


if __name__ == "__main__":
    unittest.main()
