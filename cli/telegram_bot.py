import logging
import re
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from cli.api_client import KCLIClient

# Configure logging
logger = logging.getLogger("kcli.bot")

# User ID to strictly restrict access to
ALLOWED_CHAT_ID = 462942994


def restrict_user(func):
    """Decorator to ensure only the allowed user can access the bot commands/actions."""
    async def wrapper(*args, **kwargs):
        # Resolve the Update argument positionally
        # (index 1 for method calls: self, update, context; index 0 for plain handlers: update, context)
        update = None
        if len(args) == 3:
            update = args[1]
        elif len(args) == 2:
            update = args[0]

        if not update or not getattr(update, "effective_chat", None):
            return
        chat_id = update.effective_chat.id
        if chat_id != ALLOWED_CHAT_ID:
            logger.warning(f"Blocked unauthorized message/action from Chat ID: {chat_id}")
            # Silently ignore to avoid exposing the bot to scanners
            return
        return await func(*args, **kwargs)
    return wrapper


class KCLITelegramBot:
    """Telegram Bot wrapper for KCLIClient."""

    def __init__(self, client: KCLIClient, token: str, chat_id: int = ALLOWED_CHAT_ID) -> None:
        self.client = client
        self.token = token
        self.chat_id = chat_id
        self.app = None

    async def start(self) -> None:
        """Start the Telegram bot loop."""
        self.app = ApplicationBuilder().token(self.token).build()

        # Command handlers
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_start))
        self.app.add_handler(CommandHandler("positions", self.cmd_positions))
        self.app.add_handler(CommandHandler("pos", self.cmd_positions))
        self.app.add_handler(CommandHandler("orders", self.cmd_orders))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("buy", self.cmd_buy))
        self.app.add_handler(CommandHandler("sell", self.cmd_sell))
        self.app.add_handler(CommandHandler("modify", self.cmd_modify))

        # Inline button handlers
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))

        logger.info("Initializing Telegram bot application...")
        await self.app.initialize()
        logger.info("Starting Telegram bot polling...")
        await self.app.start()
        await self.app.updater.start_polling()

    async def stop(self) -> None:
        """Stop the Telegram bot loop."""
        if self.app:
            logger.info("Stopping Telegram bot polling...")
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

    @restrict_user
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send welcome message and command list."""
        welcome_text = (
            "🪁 *Welcome to KiteCLI Bot!*\n\n"
            "This bot allows you to manage positions and orders across all authenticated Zerodha accounts.\n\n"
            "*Core Commands:*\n"
            "• `/positions` (or `/pos`) - Display open positions & exit buttons\n"
            "• `/orders` - View pending orders & modify/cancel options\n"
            "• `/status` - Check account authentication status\n\n"
            "*Trade Commands:*\n"
            "• `/buy <symbol> <qty> [price]` - Place a market or limit buy order\n"
            "• `/sell <symbol> <qty> [price]` - Place a market or limit sell order\n"
            "• `/modify <order_id> <qty> <price>` - Modify a pending limit order's price/quantity\n\n"
            "_Examples:_\n"
            "• `/buy NIFTY2670722200PE 50` (Market Buy)\n"
            "• `/buy NIFTY2670722200PE 50 85.20` (Limit Buy)"
        )
        await update.message.reply_text(welcome_text, parse_mode="Markdown")

    @restrict_user
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Render status check for all accounts."""
        try:
            status_resp = self.client.get_status()
            accounts = status_resp.get("accounts", [])
            if not accounts:
                await update.message.reply_text("❌ No accounts configured in config.yaml.")
                return

            msg_lines = ["🔌 *Account Status:*"]
            for acct in accounts:
                name = acct.get("name", "Account")
                auth = acct.get("authenticated", False)
                status_icon = "🟢" if auth else "🔴"
                status_lbl = "Session Active" if auth else "Not Authenticated"
                msg_lines.append(f"{status_icon} *{name}*: {status_lbl}")

            await update.message.reply_text("\n".join(msg_lines), parse_mode="Markdown")
        except Exception as exc:
            await update.message.reply_text(f"❌ Failed to fetch status: {exc}")

    def _format_account_positions(self, api_key: str) -> tuple[str, list[list[InlineKeyboardButton]]]:
        """Fetch and format positions for a specific account into a clean monospaced table and separate matching buttons."""
        pos_resp = self.client.get_positions([api_key])
        accounts = pos_resp.get("accounts", [])
        if not accounts:
            return "Account not found.", []

        acct = accounts[0]
        name = acct.get("name", "Account")
        total_pnl = acct.get("total_pnl", 0.0)
        positions = [p for p in acct.get("positions", []) if p.get("quantity", 0) != 0]

        pnl_sign = "+" if total_pnl >= 0 else ""
        msg_lines = [f"📊 *{name}* (P&L: {pnl_sign}₹{total_pnl:.2f})"]

        if not positions:
            return f"✅ No active open positions for *{name}*.", []

        msg_lines = [
            f"📊 *{name}* (P&L: {pnl_sign}₹{total_pnl:.2f})",
            "👇 _Select a position below to Modify or Exit:_"
        ]

        keyboard_rows = []
        for pos in positions:
            sym = pos.get("tradingsymbol", "")
            qty = pos.get("quantity", 0)
            avg = pos.get("average_price", 0.0)
            ltp = pos.get("last_price", 0.0)

            # The entire row of data is contained in the interactive button
            btn = InlineKeyboardButton(
                f"🔹 {sym}  •  Qty: {qty}  •  Avg: {avg:.2f}  •  LTP: {ltp:.2f}",
                callback_data=f"select_pos:{sym}:{api_key}:{qty}:{avg:.2f}:{ltp:.2f}"
            )
            keyboard_rows.append([btn])
        return "\n".join(msg_lines), keyboard_rows

    @restrict_user
    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Fetch and display open positions across all accounts as separate interactive tables."""
        try:
            api_keys = [acct["api_key"] for acct in self.client.accounts]
            has_any = False
            for api_key in api_keys:
                msg_text, keyboard = self._format_account_positions(api_key)
                if keyboard:  # Only send messages for accounts with active positions
                    has_any = True
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")
            
            if not has_any:
                await update.message.reply_text("✅ No active open positions across any accounts.")
        except Exception as exc:
            await update.message.reply_text(f"❌ Error fetching positions: {exc}")

    @restrict_user
    async def cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Fetch and display active pending orders."""
        try:
            api_keys = [acct["api_key"] for acct in self.client.accounts]
            orders_resp = self.client.get_orders(api_keys)
            accounts_data = orders_resp.get("accounts", [])

            has_any_pending = False
            for acct in accounts_data:
                name = acct.get("name")
                api_key = acct.get("api_key")
                orders = acct.get("orders", [])

                # Filter pending orders
                pending = [
                    o for o in orders 
                    if o.get("status") in ("OPEN", "TRIGGER PENDING", "AMO SUBMITTED")
                ]

                if not pending:
                    continue

                has_any_pending = True
                for o in pending:
                    order_id = o.get("order_id")
                    sym = o.get("tradingsymbol")
                    qty = o.get("quantity")
                    price = o.get("price")
                    tx_type = o.get("transaction_type")
                    ord_type = o.get("order_type")
                    status = o.get("status")

                    msg_text = (
                        f"⏳ *Pending Order* | `{name}`\n"
                        f"• `{sym}`\n"
                        f"  *ID*: `{order_id}`\n"
                        f"  *Type*: `{tx_type}` ({ord_type}) | Qty: `{qty}`\n"
                        f"  *Price*: `{price}` | Status: `{status}`\n"
                    )

                    keyboard = [
                        [
                            InlineKeyboardButton(
                                "Cancel Order",
                                callback_data=f"confirm_cancel:{order_id}:{api_key}:{sym}"
                            ),
                            InlineKeyboardButton(
                                "Modify Info",
                                callback_data=f"prompt_modify:{order_id}:{sym}:{qty}:{price}"
                            )
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")

            if not has_any_pending:
                await update.message.reply_text("✅ No active pending orders found.")

        except Exception as exc:
            await update.message.reply_text(f"❌ Error fetching orders: {exc}")

    @restrict_user
    async def cmd_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Place buy order across all accounts."""
        await self._place_order_helper("BUY", update, context)

    @restrict_user
    async def cmd_sell(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Place sell order across all accounts."""
        await self._place_order_helper("SELL", update, context)

    async def _place_order_helper(
        self,
        transaction_type: str,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Common logic to parse and place buy/sell orders."""
        args = context.args
        if not args or len(args) < 2:
            await update.message.reply_text(
                f"❌ Invalid syntax. Use:\n"
                f"`/{transaction_type.lower()} <SYMBOL> <QTY> [LIMIT_PRICE]`\n\n"
                f"_Examples:_\n"
                f"• `/{transaction_type.lower()} NIFTY2670722200PE 50` (Market)\n"
                f"• `/{transaction_type.lower()} NIFTY2670722200PE 50 85.20` (Limit)",
                parse_mode="Markdown"
            )
            return

        symbol = args[0].upper()
        try:
            qty = int(args[1])
        except ValueError:
            await update.message.reply_text("❌ Quantity must be an integer.")
            return

        price = None
        order_type = "MARKET"
        if len(args) >= 3:
            try:
                # Support both raw float and leading '@' price prefix
                price_str = args[2].lstrip("@")
                price = float(price_str)
                order_type = "LIMIT"
            except ValueError:
                await update.message.reply_text("❌ Price must be a valid number.")
                return

        # Infer exchange (Nifty/Sensex options belong to NFO/BFO, indices/equities NSE)
        exchange = "NFO"
        if len(symbol) <= 6:
            exchange = "NSE"

        confirm_text = (
            f"🛒 *Confirm {transaction_type} Order*\n\n"
            f"• *Symbol*: `{symbol}`\n"
            f"• *Quantity*: `{qty}`\n"
            f"• *Type*: `{order_type}`"
        )
        if price is not None:
            confirm_text += f"\n• *Price*: `{price:.2f}`"

        confirm_text += "\n\nPlace this order across *ALL* authenticated accounts?"

        callback_payload = f"do_place:{transaction_type}:{symbol}:{qty}:{order_type}:{exchange}"
        if price is not None:
            callback_payload += f":{price}"

        keyboard = [
            [
                InlineKeyboardButton("✅ Confirm Order", callback_data=callback_payload),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(confirm_text, reply_markup=reply_markup, parse_mode="Markdown")

    @restrict_user
    async def cmd_modify(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Modify a pending order."""
        args = context.args
        if not args or len(args) < 3:
            await update.message.reply_text(
                "❌ Invalid syntax. Use:\n"
                "`/modify <ORDER_ID> <NEW_QTY> <NEW_PRICE>`\n\n"
                "_Example:_\n"
                "`/modify 240618000123 50 82.50`",
                parse_mode="Markdown"
            )
            return

        order_id = args[0]
        try:
            qty = int(args[1])
        except ValueError:
            await update.message.reply_text("❌ Quantity must be an integer.")
            return

        try:
            price = float(args[2].lstrip("@"))
        except ValueError:
            await update.message.reply_text("❌ Price must be a valid number.")
            return

        # Find the order across accounts to determine api_key
        api_keys = [acct["api_key"] for acct in self.client.accounts]
        orders_resp = self.client.get_orders(api_keys)
        accounts_data = orders_resp.get("accounts", [])

        target_api_key = None
        acct_name = ""
        symbol = ""
        for acct in accounts_data:
            for o in acct.get("orders", []):
                if o.get("order_id") == order_id:
                    target_api_key = acct.get("api_key")
                    acct_name = acct.get("name", "Account")
                    symbol = o.get("tradingsymbol", "")
                    break

        if not target_api_key:
            await update.message.reply_text(f"❌ Order `{order_id}` was not found in active pending orders.")
            return

        confirm_text = (
            f"🔄 *Confirm Order Modification* | `{acct_name}`\n\n"
            f"• *Symbol*: `{symbol}`\n"
            f"• *Order ID*: `{order_id}`\n"
            f"• *New Quantity*: `{qty}`\n"
            f"• *New Price*: `{price:.2f}`\n\n"
            f"Apply this modification?"
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Confirm Modify",
                    callback_data=f"do_modify:{target_api_key}:{order_id}:{qty}:{price}"
                ),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(confirm_text, reply_markup=reply_markup, parse_mode="Markdown")

    @restrict_user
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline button actions and confirmations."""
        query = update.callback_query
        await query.answer()

        data = query.data.split(":")
        action = data[0]

        if action == "select_pos":
            symbol = data[1]
            api_key = data[2]
            qty = int(data[3])
            avg = float(data[4]) if len(data) > 4 else 0.0
            ltp = float(data[5]) if len(data) > 5 else 0.0
            
            acct_name = "Account"
            for acct in self.client.accounts:
                if acct.get("api_key") == api_key:
                    acct_name = acct.get("name", "Account")
                    break
            
            msg = (
                f"🎯 *Selected Position:* `{symbol}`\n"
                f"• *Account:* `{acct_name}`\n"
                f"• *Current Qty:* `{qty}`\n"
                f"• *Average Price:* `{avg:.2f}`\n"
                f"• *LTP:* `{ltp:.2f}`\n\n"
                f"Choose an action:"
            )
            
            keyboard = [
                [
                    InlineKeyboardButton("🚨 Exit Position", callback_data=f"confirm_exit_single:{symbol}:{api_key}:{qty}"),
                    InlineKeyboardButton("➕ Add More", callback_data=f"confirm_add_more:{symbol}:{api_key}:{qty}")
                ],
                [
                    InlineKeyboardButton("🔙 Back to Positions", callback_data=f"back_to_positions:{api_key}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(msg, reply_markup=reply_markup, parse_mode="Markdown")

        elif action == "confirm_exit_single":
            symbol = data[1]
            api_key = data[2]
            qty = int(data[3])
            
            confirm_text = (
                f"🚨 *Market Exit Confirmation*\n\n"
                f"Are you sure you want to exit position `{symbol}` (Qty: `{qty}`) at market price?"
            )
            keyboard = [
                [
                    InlineKeyboardButton("✅ Confirm Market Exit", callback_data=f"do_exit_single:{symbol}:{api_key}"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_reply_markup(reply_markup=reply_markup)

        elif action == "do_exit_single":
            symbol = data[1]
            api_key = data[2]
            await query.edit_message_text(f"⏳ Placing market exit order for `{symbol}`...")
            try:
                res = self.client.exit_positions([api_key], tradingsymbol=symbol)
                res_lines = [f"📊 *Market Exit Result:* `{symbol}`"]
                for r in res.get("results", []):
                    name = r.get("name")
                    status = r.get("status")
                    msg = r.get("message", "")
                    icon = "✅" if status == "success" else "❌"
                    res_lines.append(f"{icon} *{name}*: {msg}")

                await query.edit_message_text("\n".join(res_lines), parse_mode="Markdown")
            except Exception as exc:
                await query.edit_message_text(f"❌ Exit execution failed: {exc}")

        elif action == "confirm_add_more":
            symbol = data[1]
            api_key = data[2]
            qty = int(data[3])
            
            tx_type = "BUY" if qty > 0 else "SELL"
            abs_qty = abs(qty)
            exchange = "NFO" if len(symbol) > 6 else "NSE"
            
            confirm_text = (
                f"➕ *Confirm Add More Position*\n\n"
                f"• *Symbol*: `{symbol}`\n"
                f"• *Current Qty*: `{qty}`\n"
                f"• *Order*: `{tx_type}` `{abs_qty}` (Market)\n\n"
                f"Place market order to increase position size?"
            )
            
            callback_payload = f"do_place_add:{tx_type}:{symbol}:{abs_qty}:{exchange}:{api_key}"
            keyboard = [
                [
                    InlineKeyboardButton("✅ Confirm Add More", callback_data=callback_payload),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_reply_markup(reply_markup=reply_markup)

        elif action == "do_place_add":
            tx_type = data[1]
            symbol = data[2]
            qty = int(data[3])
            exchange = data[4]
            api_key = data[5]
            
            await query.edit_message_text(f"⏳ Placing market order to add to `{symbol}`...")
            try:
                res = self.client.place_order(
                    api_keys=[api_key],
                    tradingsymbol=symbol,
                    exchange=exchange,
                    transaction_type=tx_type,
                    quantity=qty,
                    order_type="MARKET"
                )
                res_lines = [f"📊 *Order Execution Result:* `{symbol}`"]
                for r in res.get("results", []):
                    name = r.get("name")
                    status = r.get("status")
                    msg = r.get("message", "")
                    icon = "✅" if status == "success" else "❌"
                    res_lines.append(f"{icon} *{name}*: {msg}")

                await query.edit_message_text("\n".join(res_lines), parse_mode="Markdown")
            except Exception as exc:
                await query.edit_message_text(f"❌ Order placement failed: {exc}")

        elif action == "back_to_positions":
            api_key = data[1]
            await query.edit_message_text("⏳ Reloading positions...")
            try:
                msg_text, keyboard = self._format_account_positions(api_key)
                reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
                await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")
            except Exception as exc:
                await query.edit_message_text(f"❌ Error reloading positions: {exc}")

        elif action == "confirm_exit":
            symbol = data[1]
            confirm_text = (
                f"🚨 *Market Exit Confirmation*\n\n"
                f"Are you sure you want to exit position `{symbol}` across *ALL* accounts at market price?"
            )
            keyboard = [
                [
                    InlineKeyboardButton("✅ Yes, Market Exit", callback_data=f"do_exit:{symbol}"),
                    InlineKeyboardButton("❌ No, Keep Open", callback_data="cancel_action")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_reply_markup(reply_markup=reply_markup)

        elif action == "do_exit":
            symbol = data[1]
            await query.edit_message_text(f"⏳ Placing market exit orders for `{symbol}`...")
            try:
                api_keys = [acct["api_key"] for acct in self.client.accounts]
                res = self.client.exit_positions(api_keys, tradingsymbol=symbol)
                
                res_lines = [f"📊 *Market Exit Results:* `{symbol}`"]
                for r in res.get("results", []):
                    name = r.get("name")
                    status = r.get("status")
                    msg = r.get("message", "")
                    icon = "✅" if status == "success" else "❌"
                    res_lines.append(f"{icon} *{name}*: {msg}")

                await query.edit_message_text("\n".join(res_lines), parse_mode="Markdown")
            except Exception as exc:
                await query.edit_message_text(f"❌ Exit execution failed: {exc}")

        elif action == "confirm_cancel":
            order_id = data[1]
            api_key = data[2]
            symbol = data[3]
            confirm_text = (
                f"🚨 *Cancel Order Confirmation*\n\n"
                f"Cancel order `{order_id}` (`{symbol}`)?"
            )
            keyboard = [
                [
                    InlineKeyboardButton(
                        "✅ Yes, Cancel Order",
                        callback_data=f"do_cancel:{order_id}:{api_key}"
                    ),
                    InlineKeyboardButton("❌ No, Keep Pending", callback_data="cancel_action")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_reply_markup(reply_markup=reply_markup)

        elif action == "do_cancel":
            order_id = data[1]
            api_key = data[2]
            await query.edit_message_text(f"⏳ Sending cancellation for `{order_id}`...")
            try:
                res = self.client.cancel_order(api_key, order_id)
                icon = "✅" if res.get("status") == "success" else "❌"
                await query.edit_message_text(f"{icon} {res.get('message')}")
            except Exception as exc:
                await query.edit_message_text(f"❌ Cancel execution failed: {exc}")

        elif action == "prompt_modify":
            order_id = data[1]
            sym = data[2]
            qty = data[3]
            price = data[4]
            instr = (
                f"🔄 *Modify Order `{order_id}` (`{sym}`)*\n\n"
                f"Current: Qty `{qty}` @ Price `{price}`\n\n"
                f"To apply modifications, please type:\n"
                f"`/modify {order_id} <qty> <price>`"
            )
            await query.edit_message_text(instr, parse_mode="Markdown")

        elif action == "do_place":
            tx_type = data[1]
            symbol = data[2]
            qty = int(data[3])
            ord_type = data[4]
            exchange = data[5]
            price = float(data[6]) if len(data) > 6 else None

            await query.edit_message_text(f"⏳ Placing `{ord_type}` `{tx_type}` orders for `{symbol}`...")
            try:
                api_keys = [acct["api_key"] for acct in self.client.accounts]
                res = self.client.place_order(
                    api_keys=api_keys,
                    tradingsymbol=symbol,
                    exchange=exchange,
                    transaction_type=tx_type,
                    quantity=qty,
                    order_type=ord_type,
                    price=price
                )
                
                res_lines = [f"📊 *Order Execution Results:* `{symbol}`"]
                for r in res.get("results", []):
                    name = r.get("name")
                    status = r.get("status")
                    msg = r.get("message", "")
                    icon = "✅" if status == "success" else "❌"
                    res_lines.append(f"{icon} *{name}*: {msg}")

                await query.edit_message_text("\n".join(res_lines), parse_mode="Markdown")
            except Exception as exc:
                await query.edit_message_text(f"❌ Order placement failed: {exc}")

        elif action == "do_modify":
            api_key = data[1]
            order_id = data[2]
            qty = int(data[3])
            price = float(data[4])

            await query.edit_message_text(f"⏳ Sending modification for `{order_id}`...")
            try:
                res = self.client.modify_order(
                    api_key=api_key,
                    order_id=order_id,
                    quantity=qty,
                    price=price
                )
                icon = "✅" if res.get("status") == "success" else "❌"
                await query.edit_message_text(f"{icon} {res.get('message')}")
            except Exception as exc:
                await query.edit_message_text(f"❌ Modification failed: {exc}")

        elif action == "cancel_action":
            await query.edit_message_text("❌ Action cancelled.")
