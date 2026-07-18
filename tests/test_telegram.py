import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is in path
sys.path.append("/Users/vgolugur/Documents/Projects/kitecli")

from cli.telegram_bot import KCLITelegramBot, restrict_user

# Test mock chat ID
ALLOWED_CHAT_ID = 462942994

class TestTelegramBot(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.client = MagicMock()
        self.client.accounts = [
            {"name": "ZK8719", "api_key": "api_zk", "user_id": "ZK8719"}
        ]
        self.bot = KCLITelegramBot(self.client, token="fake_token", chat_id=ALLOWED_CHAT_ID)

    async def test_security_gate_authorized(self):
        # Create an authorized mock Update
        update = MagicMock()
        update.effective_chat.id = ALLOWED_CHAT_ID
        
        called = False
        @restrict_user
        async def mock_handler(self_bot, up, ctx):
            nonlocal called
            called = True
            return True

        await mock_handler(self.bot, update, MagicMock())
        self.assertTrue(called)

    async def test_security_gate_unauthorized(self):
        # Create an unauthorized mock Update
        update = MagicMock()
        update.effective_chat.id = 999999999  # Unauthorized ID
        
        called = False
        @restrict_user
        async def mock_handler(self_bot, up, ctx):
            nonlocal called
            called = True
            return True

        await mock_handler(self.bot, update, MagicMock())
        self.assertFalse(called)

    async def test_positions_command(self):
        # Mock client responses
        self.client.get_positions.return_value = {
            "accounts": [
                {
                    "name": "ZK8719",
                    "total_pnl": 1250.00,
                    "positions": [
                        {
                            "tradingsymbol": "NIFTY2670722200PE",
                            "quantity": 50,
                            "average_price": 80.0,
                            "last_price": 105.0,
                            "pnl": 1250.00
                        }
                    ]
                }
            ]
        }

        # Mock Update and Message
        update = MagicMock()
        update.effective_chat.id = ALLOWED_CHAT_ID
        update.message.reply_text = AsyncMock()

        await self.bot.cmd_positions(update, MagicMock())

        # Verify positions rendering
        update.message.reply_text.assert_called_once()
        args, kwargs = update.message.reply_text.call_args
        self.assertIn("ZK8719", args[0])
        self.assertTrue(kwargs["reply_markup"].inline_keyboard[0][0].text.startswith("07Jul26"))

    async def test_orders_command(self):
        # Mock client responses
        self.client.get_orders.return_value = {
            "accounts": [
                {
                    "name": "ZK8719",
                    "api_key": "api_zk",
                    "orders": [
                        {
                            "order_id": "2406180001",
                            "tradingsymbol": "NIFTY2670722200PE",
                            "quantity": 50,
                            "price": 85.0,
                            "transaction_type": "BUY",
                            "order_type": "LIMIT",
                            "status": "OPEN"
                        }
                    ]
                }
            ]
        }

        # Mock Update and Message
        update = MagicMock()
        update.effective_chat.id = ALLOWED_CHAT_ID
        update.message.reply_text = AsyncMock()

        await self.bot.cmd_orders(update, MagicMock())

        # Verify orders rendering
        update.message.reply_text.assert_called_once()
        args, kwargs = update.message.reply_text.call_args
        self.assertIn("2406180001", args[0])
        self.assertIn("NIFTY2670722200PE", args[0])
        self.assertEqual("Cancel Order", kwargs["reply_markup"].inline_keyboard[0][0].text)

    async def test_buy_command_market(self):
        # Mock Update
        update = MagicMock()
        update.effective_chat.id = ALLOWED_CHAT_ID
        update.message.reply_text = AsyncMock()

        # Mock Context with command arguments
        context = MagicMock()
        context.args = ["NIFTY2670722200PE", "50"]

        await self.bot.cmd_buy(update, context)

        self.assertEqual(update.message.reply_text.call_count, 2)
        # Check confirmation call
        args, kwargs = update.message.reply_text.call_args_list[1]
        self.assertIn("Confirm BUY order", args[0])
        self.assertIn("MARKET", args[0])
        self.assertIn("do_exec_cmd:ALL:buy NIFTY2670722200PE 50", kwargs["reply_markup"].inline_keyboard[0][0].callback_data)

    async def test_buy_command_limit(self):
        # Mock Update
        update = MagicMock()
        update.effective_chat.id = ALLOWED_CHAT_ID
        update.message.reply_text = AsyncMock()

        # Mock Context with command arguments including price
        context = MagicMock()
        context.args = ["NIFTY2670722200PE", "50", "85.20"]

        await self.bot.cmd_buy(update, context)

        self.assertEqual(update.message.reply_text.call_count, 2)
        # Check confirmation call
        args, kwargs = update.message.reply_text.call_args_list[1]
        self.assertIn("Confirm BUY order", args[0])
        self.assertIn("85.20", args[0])
        self.assertIn("do_exec_cmd:ALL:buy NIFTY2670722200PE 50 85.2", kwargs["reply_markup"].inline_keyboard[0][0].callback_data)

    async def test_buy_command_single_account(self):
        # Mock Update
        update = MagicMock()
        update.effective_chat.id = ALLOWED_CHAT_ID
        update.message.reply_text = AsyncMock()

        # Mock target account ZK8719
        self.bot.client.accounts = [
            {"name": "ZK8719", "api_key": "api_zk"}
        ]

        # Mock Context with command arguments including @ZK8719 target account
        context = MagicMock()
        context.args = ["NIFTY2670722200PE", "50", "85.20", "@ZK8719"]

        await self.bot.cmd_buy(update, context)

        self.assertEqual(update.message.reply_text.call_count, 2)
        # Check confirmation call
        args, kwargs = update.message.reply_text.call_args_list[1]
        self.assertIn("Confirm BUY order", args[0])
        self.assertIn("85.20", args[0])
        self.assertIn("do_exec_cmd:0:buy NIFTY2670722200PE 50 85.2", kwargs["reply_markup"].inline_keyboard[0][0].callback_data)

    async def test_init_command_manual_login(self):
        self.client.init_accounts.return_value = {
            "accounts": [
                {
                    "name": "ZK8719",
                    "api_key": "api_zk",
                    "auto_logged_in": False,
                    "message": "Manual login required",
                    "login_url": "http://mock_login_url"
                }
            ]
        }
        update = MagicMock()
        update.effective_chat.id = ALLOWED_CHAT_ID
        update.message.reply_text = AsyncMock()

        await self.bot.cmd_init(update, MagicMock())

        calls = [call[0][0] for call in update.message.reply_text.call_args_list]
        self.assertTrue(any("requires manual authentication" in msg for msg in calls))

    async def test_token_command(self):
        self.bot.client.accounts = [
            {"name": "ZK8719", "api_key": "api_zk"}
        ]
        self.client.complete_callback.return_value = {"status": "success"}

        update = MagicMock()
        update.effective_chat.id = ALLOWED_CHAT_ID
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = ["ZK8719", "mock_token_123"]

        await self.bot.cmd_token(update, context)

        self.client.complete_callback.assert_called_once_with("api_zk", "mock_token_123")
        calls = [call[0][0] for call in update.message.reply_text.call_args_list]
        self.assertTrue(any("authenticated successfully" in msg for msg in calls))

    async def test_cmd_command(self):
        # 1. Test command parsing and confirmation prompt
        update = MagicMock()
        update.effective_chat.id = ALLOWED_CHAT_ID
        update.message.reply_text = AsyncMock()
        
        loading_msg = MagicMock()
        loading_msg.delete = AsyncMock()
        update.message.reply_text.return_value = loading_msg

        context = MagicMock()
        context.args = ["account", "ZK8719", "&&", "sell", "NIFTY2670722200PE", "50"]

        await self.bot.cmd_cmd(update, context)

        # It should send the confirmation message
        update.message.reply_text.assert_any_call(
            "Confirm SELL order of 50 NIFTY2670722200PE (MARKET) (NRML)?",
            reply_markup=unittest.mock.ANY,
            parse_mode="Markdown"
        )

        # 2. Test callback query for execution after confirmation
        query = AsyncMock()
        query.data = "do_exec_cmd:0:sell NIFTY2670722200PE 50"
        query.message = MagicMock()
        query.message.chat_id = ALLOWED_CHAT_ID
        query.edit_message_text = AsyncMock()
        
        update_cb = MagicMock()
        update_cb.callback_query = query
        update_cb.effective_chat.id = ALLOWED_CHAT_ID
        
        # Mock place_order return value
        self.client.place_order.return_value = {
            "results": [
                {"name": "ZK8719", "status": "success", "message": "Order placed successfully"}
            ]
        }

        await self.bot.handle_callback(update_cb, context)
        self.client.place_order.assert_called_once()

    async def test_interactive_limit_exit(self):
        # 1. Test confirmation displays the price selection grid
        query = AsyncMock()
        query.data = f"confirm_exit_single:NIFTY2670722200PE:api_zk:50:105.00"
        query.message = MagicMock()
        query.message.chat_id = ALLOWED_CHAT_ID
        query.edit_message_text = AsyncMock()
        
        update = MagicMock()
        update.callback_query = query
        update.effective_chat.id = ALLOWED_CHAT_ID
        
        await self.bot.handle_callback(update, MagicMock())
        
        # Should edit text with exit selection prompt and buttons
        query.edit_message_text.assert_called_once()
        args, kwargs = query.edit_message_text.call_args
        self.assertIn("Exit Price Selection", args[0])
        self.assertIn("105.00", args[0])
        self.assertEqual(kwargs["parse_mode"], "Markdown")
        
        # Verify button callbacks contain the price (e.g. do_exit_single:NIFTY...:105.00)
        buttons = kwargs["reply_markup"].inline_keyboard
        self.assertIn("Limit @ 105.00 (LTP)", buttons[0][0].text)
        self.assertIn("do_exit_single:NIFTY2670722200PE:0:105.00", buttons[0][0].callback_data)
        
        # 2. Test limit exit execution after callback selection
        query_exec = AsyncMock()
        query_exec.data = f"do_exit_single:NIFTY2670722200PE:0:105.00"
        query_exec.message = MagicMock()
        query_exec.message.chat_id = ALLOWED_CHAT_ID
        query_exec.edit_message_text = AsyncMock()
        
        update_exec = MagicMock()
        update_exec.callback_query = query_exec
        update_exec.effective_chat.id = ALLOWED_CHAT_ID
        
        self.client.exit_positions.return_value = {
            "results": [{"name": "ZK8719", "status": "success", "message": "Order placed successfully"}]
        }
        
        await self.bot.handle_callback(update_exec, MagicMock())
        self.client.exit_positions.assert_called_once_with(["api_zk"], tradingsymbol="NIFTY2670722200PE", price=105.00)
        
        # 3. Test Custom Price Prompt trigger (ForceReply message)
        query_prompt = AsyncMock()
        query_prompt.data = f"prompt_custom_price:NIFTY2670722200PE:0"
        query_prompt.message = MagicMock()
        query_prompt.message.chat_id = ALLOWED_CHAT_ID
        query_prompt.message.reply_text = AsyncMock()
        query_prompt.answer = AsyncMock()
        
        update_prompt = MagicMock()
        update_prompt.callback_query = query_prompt
        update_prompt.effective_chat.id = ALLOWED_CHAT_ID
        
        await self.bot.handle_callback(update_prompt, MagicMock())
        query_prompt.message.reply_text.assert_called_once()
        self.assertIn("Enter custom limit price for `NIFTY2670722200PE`", query_prompt.message.reply_text.call_args[0][0])
        query_prompt.answer.assert_called_once()
        
        # 4. Test replying to ForceReply to execute limit order
        msg_reply = MagicMock()
        msg_reply.reply_to_message = MagicMock()
        msg_reply.reply_to_message.text = "Enter custom limit price for `NIFTY2670722200PE` in reply to this message:"
        msg_reply.text = "104.50"
        msg_reply.reply_text = AsyncMock()
        
        update_msg = MagicMock()
        update_msg.message = msg_reply
        update_msg.effective_chat.id = ALLOWED_CHAT_ID
        
        self.client.get_positions.return_value = {
            "accounts": [
                {
                    "api_key": "api_zk",
                    "positions": [{"tradingsymbol": "NIFTY2670722200PE", "quantity": 50}]
                }
            ]
        }
        self.client.exit_positions.reset_mock()
        self.client.exit_positions.return_value = {
            "results": [{"name": "ZK8719", "status": "success", "message": "Order placed successfully"}]
        }
        
        await self.bot.handle_message(update_msg, MagicMock())
        self.client.exit_positions.assert_called_once_with(["api_zk"], tradingsymbol="NIFTY2670722200PE", price=104.50)

    async def test_interactive_add_more(self):
        # 1. Test confirmation displays the add more price selection grid
        query = AsyncMock()
        query.data = f"confirm_add_more:NIFTY2670722200PE:api_zk:50:105.00"
        query.message = MagicMock()
        query.message.chat_id = ALLOWED_CHAT_ID
        query.edit_message_text = AsyncMock()
        
        update = MagicMock()
        update.callback_query = query
        update.effective_chat.id = ALLOWED_CHAT_ID
        
        await self.bot.handle_callback(update, MagicMock())
        
        # Should edit text with add more selection prompt and buttons
        query.edit_message_text.assert_called_once()
        args, kwargs = query.edit_message_text.call_args
        self.assertIn("Add More Position Price Selection", args[0])
        self.assertIn("105.00", args[0])
        self.assertEqual(kwargs["parse_mode"], "Markdown")
        
        # Verify button callbacks contain the price (e.g. do_place_add:BUY:NIFTY...:50:NFO:0:105.00)
        buttons = kwargs["reply_markup"].inline_keyboard
        self.assertIn("Limit @ 105.00 (LTP)", buttons[0][0].text)
        self.assertIn("do_place_add:BUY:NIFTY2670722200PE:50:NFO:0:105.00", buttons[0][0].callback_data)
        
        # 2. Test limit order execution for add more after callback selection
        query_exec = AsyncMock()
        query_exec.data = f"do_place_add:BUY:NIFTY2670722200PE:50:NFO:0:105.00"
        query_exec.message = MagicMock()
        query_exec.message.chat_id = ALLOWED_CHAT_ID
        query_exec.edit_message_text = AsyncMock()
        
        update_exec = MagicMock()
        update_exec.callback_query = query_exec
        update_exec.effective_chat.id = ALLOWED_CHAT_ID
        
        self.client.place_order.reset_mock()
        self.client.place_order.return_value = {
            "results": [{"name": "ZK8719", "status": "success", "message": "Order placed successfully"}]
        }
        
        await self.bot.handle_callback(update_exec, MagicMock())
        self.client.place_order.assert_called_once_with(
            api_keys=["api_zk"],
            tradingsymbol="NIFTY2670722200PE",
            exchange="NFO",
            transaction_type="BUY",
            quantity=50,
            order_type="LIMIT",
            price=105.00
        )
        
        # 3. Test Custom Price Prompt trigger for Add More (ForceReply message)
        query_prompt = AsyncMock()
        query_prompt.data = f"prompt_custom_price_add:BUY:NIFTY2670722200PE:50:NFO:0"
        query_prompt.message = MagicMock()
        query_prompt.message.chat_id = ALLOWED_CHAT_ID
        query_prompt.message.reply_text = AsyncMock()
        query_prompt.answer = AsyncMock()
        
        update_prompt = MagicMock()
        update_prompt.callback_query = query_prompt
        update_prompt.effective_chat.id = ALLOWED_CHAT_ID
        
        await self.bot.handle_callback(update_prompt, MagicMock())
        query_prompt.message.reply_text.assert_called_once()
        self.assertIn("Enter custom limit price for adding `50` more to `NIFTY2670722200PE`", query_prompt.message.reply_text.call_args[0][0])
        query_prompt.answer.assert_called_once()
        
        # 4. Test replying to ForceReply to execute limit order for Add More
        msg_reply = MagicMock()
        msg_reply.reply_to_message = MagicMock()
        msg_reply.reply_to_message.text = "Enter custom limit price for adding `50` more to `NIFTY2670722200PE` (`BUY` segments: `NFO`):"
        msg_reply.text = "104.50"
        msg_reply.reply_text = AsyncMock()
        
        update_msg = MagicMock()
        update_msg.message = msg_reply
        update_msg.effective_chat.id = ALLOWED_CHAT_ID
        
        self.client.get_positions.return_value = {
            "accounts": [
                {
                    "api_key": "api_zk",
                    "positions": [{"tradingsymbol": "NIFTY2670722200PE", "quantity": 50}]
                }
            ]
        }
        self.client.place_order.reset_mock()
        self.client.place_order.return_value = {
            "results": [{"name": "ZK8719", "status": "success", "message": "Order placed successfully"}]
        }
        
        await self.bot.handle_message(update_msg, MagicMock())
        self.client.place_order.assert_called_once_with(
            api_keys=["api_zk"],
            tradingsymbol="NIFTY2670722200PE",
            exchange="NFO",
            transaction_type="BUY",
            quantity=50,
            order_type="LIMIT",
            price=104.50
        )

    async def test_interactive_limit_exit_with_account_routing(self):
        # Test replying to a custom limit price exit prompt with @account specified
        msg_reply = MagicMock()
        msg_reply.reply_to_message = MagicMock()
        msg_reply.reply_to_message.text = "Enter custom limit price for `NIFTY2670722200PE` under `@WVV135` in reply to this message:"
        msg_reply.text = "104.50"
        msg_reply.reply_text = AsyncMock()
        
        update_msg = MagicMock()
        update_msg.message = msg_reply
        update_msg.effective_chat.id = ALLOWED_CHAT_ID
        
        # Two accounts holding the same position
        self.bot.client.accounts = [
            {"name": "ZK8719", "api_key": "api_zk"},
            {"name": "WVV135", "api_key": "api_wvv"}
        ]
        
        self.client.exit_positions.reset_mock()
        self.client.exit_positions.return_value = {
            "results": [{"name": "WVV135", "status": "success", "message": "Order placed successfully"}]
        }
        
        await self.bot.handle_message(update_msg, MagicMock())
        # Should be routed strictly to WVV135 ("api_wvv") instead of defaulting to ZK8719
        self.client.exit_positions.assert_called_once_with(["api_wvv"], tradingsymbol="NIFTY2670722200PE", price=104.50)

    async def test_interactive_add_more_with_account_routing(self):
        # Test replying to a custom limit price add prompt with @account specified
        msg_reply = MagicMock()
        msg_reply.reply_to_message = MagicMock()
        msg_reply.reply_to_message.text = "Enter custom limit price for adding `50` more to `NIFTY2670722200PE` under `@WVV135` (`BUY` segments: `NFO`):"
        msg_reply.text = "104.50"
        msg_reply.reply_text = AsyncMock()
        
        update_msg = MagicMock()
        update_msg.message = msg_reply
        update_msg.effective_chat.id = ALLOWED_CHAT_ID
        
        # Two accounts holding the same position
        self.bot.client.accounts = [
            {"name": "ZK8719", "api_key": "api_zk"},
            {"name": "WVV135", "api_key": "api_wvv"}
        ]
        
        self.client.place_order.reset_mock()
        self.client.place_order.return_value = {
            "results": [{"name": "WVV135", "status": "success", "message": "Order placed successfully"}]
        }
        
        await self.bot.handle_message(update_msg, MagicMock())
        # Should be routed strictly to WVV135 ("api_wvv") instead of defaulting to ZK8719
        self.client.place_order.assert_called_once_with(
            api_keys=["api_wvv"],
            tradingsymbol="NIFTY2670722200PE",
            exchange="NFO",
            transaction_type="BUY",
            quantity=50,
            order_type="LIMIT",
            price=104.50
        )

if __name__ == "__main__":
    unittest.main()
