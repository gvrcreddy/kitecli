import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is in path
sys.path.append("/Users/vgolugur/Documents/Projects/kitecli")

from cli.telegram_bot import KCLITelegramBot, restrict_user, ALLOWED_CHAT_ID

class TestTelegramBot(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.client = MagicMock()
        self.client.accounts = [
            {"name": "ZK8719", "api_key": "api_zk", "user_id": "ZK8719"}
        ]
        self.bot = KCLITelegramBot(self.client, token="fake_token")

    async def test_security_gate_authorized(self):
        # Create an authorized mock Update
        update = MagicMock()
        update.effective_chat.id = ALLOWED_CHAT_ID
        
        called = False
        @restrict_user
        async def mock_handler(up, ctx):
            nonlocal called
            called = True
            return True

        await mock_handler(update, MagicMock())
        self.assertTrue(called)

    async def test_security_gate_unauthorized(self):
        # Create an unauthorized mock Update
        update = MagicMock()
        update.effective_chat.id = 999999999  # Unauthorized ID
        
        called = False
        @restrict_user
        async def mock_handler(up, ctx):
            nonlocal called
            called = True
            return True

        await mock_handler(update, MagicMock())
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

        update.message.reply_text.assert_called_once()
        args, kwargs = update.message.reply_text.call_args
        self.assertIn("Confirm BUY Order", args[0])
        self.assertIn("MARKET", args[0])
        self.assertIn("do_place:BUY:NIFTY2670722200PE:50:MARKET:NFO", kwargs["reply_markup"].inline_keyboard[0][0].callback_data)

    async def test_buy_command_limit(self):
        # Mock Update
        update = MagicMock()
        update.effective_chat.id = ALLOWED_CHAT_ID
        update.message.reply_text = AsyncMock()

        # Mock Context with command arguments including price
        context = MagicMock()
        context.args = ["NIFTY2670722200PE", "50", "85.20"]

        await self.bot.cmd_buy(update, context)

        update.message.reply_text.assert_called_once()
        args, kwargs = update.message.reply_text.call_args
        self.assertIn("LIMIT", args[0])
        self.assertIn("85.20", args[0])
        self.assertIn("do_place:BUY:NIFTY2670722200PE:50:LIMIT:NFO:85.2", kwargs["reply_markup"].inline_keyboard[0][0].callback_data)

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

if __name__ == "__main__":
    unittest.main()
