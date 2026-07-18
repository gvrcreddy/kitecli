import logging
import asyncio
import re
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from cli.api_client import KCLIClient
from cli.parser import (
    parse_command_line, PlaceOrderCommand, ExitCommand,
    CancelOrderCommand, ModifyOrderCommand
)
from cli.executor import ExecutionContext, execute_command

# Configure logging
logger = logging.getLogger("kcli.bot")

# User ID to strictly restrict access to
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

        # Resolve the target chat_id from the bot instance (self is the first arg in method decorators)
        bot_instance = args[0]
        allowed_chat_id = getattr(bot_instance, "chat_id", None)

        if not update or not getattr(update, "effective_chat", None):
            return
        chat_id = update.effective_chat.id
        if allowed_chat_id is None or chat_id != allowed_chat_id:
            logger.warning(f"Blocked unauthorized message/action from Chat ID: {chat_id}")
            # Silently ignore to avoid exposing the bot to scanners
            return
        return await func(*args, **kwargs)
    return wrapper


def clean_option_symbol(symbol: str) -> str:
    """Parses and formats option symbols into a compact format, stripping index names like NIFTY."""
    # 1. Weekly option pattern: e.g. NIFTY2670722800PE
    # Group 1: Symbol (e.g. NIFTY)
    # Group 2: Year (26)
    # Group 3: Month character (1-9, O, N, D)
    # Group 4: Date (07)
    # Group 5: Strike (22800)
    # Group 6: Option Type (PE)
    m_weekly = re.match(r"^([A-Z]+)(\d{2})([1-9ONDond])(\d{2})(\d+)(CE|PE)$", symbol)
    if m_weekly:
        _, year, month_char, date, strike, opt_type = m_weekly.groups()
        months_map = {
            "1": "Jan", "2": "Feb", "3": "Mar", "4": "Apr", "5": "May", "6": "Jun",
            "7": "Jul", "8": "Aug", "9": "Sep", "O": "Oct", "N": "Nov", "D": "Dec"
        }
        month_name = months_map.get(month_char.upper(), month_char)
        return f"{date}{month_name}{year} {strike}{opt_type}"

    # 2. Monthly option pattern: e.g. NIFTY26JUL21100CE
    # Group 1: Symbol (NIFTY)
    # Group 2: Year (26)
    # Group 3: Month name (JUL)
    # Group 4: Strike (21100)
    # Group 5: Option Type (CE)
    m_monthly = re.match(r"^([A-Z]+)(\d{2})([A-Z]{3})(\d+)(CE|PE)$", symbol)
    if m_monthly:
        _, year, month_name, strike, opt_type = m_monthly.groups()
        month_cap = month_name.capitalize()
        return f"{month_cap}{year} {strike}{opt_type}"

    return symbol


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Returns a persistent ReplyKeyboardMarkup containing the main slash commands."""
    keyboard = [
        ["/positions", "/orders"],
        ["/status", "/init"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)



class PrettyTable:
    """ASCII table generator mimicking PrettyTable."""
    def __init__(self) -> None:
        self.field_names: list[str] = []
        self.rows: list[list[str]] = []

    def add_row(self, row: list[str]) -> None:
        self.rows.append(row)

    def get_string(self) -> str:
        if not self.field_names:
            return ""
        # Calculate max widths
        col_widths = [len(h) for h in self.field_names]
        for row in self.rows:
            for i, val in enumerate(row):
                col_widths[i] = max(col_widths[i], len(str(val)))

        # Border separator
        border = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
        
        lines = [border]
        # Centered header row
        header_row = "|" + "|".join(f" {h:^{col_widths[i]}} " for i, h in enumerate(self.field_names)) + "|"
        lines.append(header_row)
        lines.append(border)

        # Left-align symbol (col 0), right-align other numeric columns
        for row in self.rows:
            parts = []
            for i, val in enumerate(row):
                val_str = str(val)
                if i == 0:
                    parts.append(f" {val_str:<{col_widths[i]}} ")
                else:
                    parts.append(f" {val_str:>{col_widths[i]}} ")
            lines.append("|" + "|".join(parts) + "|")

        lines.append(border)
        return "\n".join(lines)


class KCLITelegramBot:
    """Telegram Bot wrapper for KCLIClient."""

    def __init__(self, client: KCLIClient, token: str, chat_id: int) -> None:
        self.client = client
        self.token = token
        self.chat_id = chat_id
        self.app = None

    def _resolve_api_key(self, key_or_idx: str) -> str:
        """Resolve a 32-character api_key from either a mock key, index, or the key itself."""
        if key_or_idx.isdigit():
            idx = int(key_or_idx)
            if 0 <= idx < len(self.client.accounts):
                return self.client.accounts[idx]["api_key"]
        for acct in self.client.accounts:
            if acct["api_key"] == key_or_idx or acct.get("name") == key_or_idx or acct.get("user_id") == key_or_idx:
                return acct["api_key"]
        return key_or_idx

    def _get_acct_ref(self, api_key: str) -> str:
        """Return the account index as a string if found, otherwise the api_key itself."""
        for i, acct in enumerate(self.client.accounts):
            if acct.get("api_key") == api_key:
                return str(i)
        return api_key

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
        self.app.add_handler(CommandHandler("init", self.cmd_init))
        self.app.add_handler(CommandHandler("token", self.cmd_token))
        self.app.add_handler(CommandHandler("kcli", self.cmd_cmd))
        self.app.add_handler(CommandHandler("cmd", self.cmd_cmd))
        self.app.add_handler(CommandHandler("cmd_kcli", self.cmd_cmd))

        # Inline button handlers
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        self.app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.handle_message))

        # Register slash commands in the Bot command menu
        await self.app.bot.set_my_commands([
            BotCommand("positions", "View and manage active positions"),
            BotCommand("orders", "View and cancel pending orders"),
            BotCommand("status", "Check account connection status"),
            BotCommand("init", "Initialize account sessions and get login links"),
            BotCommand("token", "Complete login: /token <account_name> <token>"),
            BotCommand("cmd", "Execute any TUI command: /cmd <args>"),
            BotCommand("buy", "Place buy order: /buy <symbol> <qty> [price]"),
            BotCommand("sell", "Place sell order: /sell <symbol> <qty> [price]"),
            BotCommand("modify", "Modify pending order: /modify <order_id> <qty> <price>"),
        ])

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
            "• `/status` - Check account authentication status\n"
            "• `/init` - Initialize account sessions & get login links\n"
            "• `/token <account> <token>` - Complete manual login\n\n"
            "*Trade Commands:*\n"
            "• `/buy <symbol> <qty> [price]` - Place a market or limit buy order\n"
            "• `/sell <symbol> <qty> [price]` - Place a market or limit sell order\n"
            "• `/modify <order_id> <qty> <price>` - Modify a pending limit order's price/quantity\n\n"
            "_Examples:_\n"
            "• `/buy NIFTY2670722200PE 50` (Market Buy)\n"
            "• `/buy NIFTY2670722200PE 50 85.20` (Limit Buy)"
        )
        await update.message.reply_text(welcome_text, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")

    @restrict_user
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Render status check for all accounts."""
        try:
            status_resp = self.client.get_status()
            accounts = status_resp.get("accounts", [])
            if not accounts:
                await update.message.reply_text("❌ No accounts configured in config.yaml.", reply_markup=get_main_menu_keyboard())
                return

            msg_lines = ["🔌 *Account Status:*"]
            for acct in accounts:
                name = acct.get("name", "Account")
                auth = acct.get("authenticated", False)
                status_icon = "🟢" if auth else "🔴"
                status_lbl = "Session Active" if auth else "Not Authenticated"
                msg_lines.append(f"{status_icon} *{name}*: {status_lbl}")

            await update.message.reply_text("\n".join(msg_lines), reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")
        except Exception as exc:
            await update.message.reply_text(f"❌ Failed to fetch status: {exc}")

    @restrict_user
    async def cmd_init(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Initialise accounts and attempt auto-login or provide manual login links."""
        try:
            loading_msg = await update.message.reply_text("⏳ Initialising accounts and checking session status...")
            
            # Auto-login or request logins
            result = self.client.init_accounts(self.client.accounts)
            accounts = result.get("accounts", [])
            
            for acct in accounts:
                name = acct.get("name", "Account")
                api_key = acct.get("api_key", "")
                auto_logged_in = acct.get("auto_logged_in", False)
                msg = acct.get("message", "")
                login_url = acct.get("login_url", "")
                
                if auto_logged_in:
                    await update.message.reply_text(
                        f"✅ *{name}*: Session active / Auto-login successful!\n`{msg}`",
                        reply_markup=get_main_menu_keyboard(),
                        parse_mode="Markdown"
                    )
                else:
                    # Manual login needed
                    instruction = (
                        f"🔑 *{name}* requires manual authentication:\n\n"
                        f"1. [Click here to Login]({login_url})\n"
                        f"2. Copy the `request_token` from the redirect URL.\n"
                        f"3. Send it back to the bot by replying with:\n"
                        f"`/token {name} <token>`"
                    )
                    await update.message.reply_text(
                        instruction,
                        reply_markup=get_main_menu_keyboard(),
                        parse_mode="Markdown",
                        disable_web_page_preview=True
                    )
            await loading_msg.delete()
        except Exception as exc:
            await update.message.reply_text(f"❌ Initialization failed: {exc}", reply_markup=get_main_menu_keyboard())

    @restrict_user
    async def cmd_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle request_token callback completion."""
        args = context.args
        if len(args) < 2:
            await update.message.reply_text(
                "❌ Invalid format. Please use:\n`/token <account_name_or_api_key> <request_token>`",
                parse_mode="Markdown"
            )
            return
            
        target_name = args[0]
        request_token = args[1]
        
        target_account = None
        for acct in self.client.accounts:
            if acct.get("name") == target_name or acct.get("api_key") == target_name:
                target_account = acct
                break
                
        if not target_account:
            await update.message.reply_text(
                f"❌ Account '{target_name}' not found in configuration.",
                parse_mode="Markdown"
            )
            return
            
        api_key = target_account.get("api_key")
        name = target_account.get("name", api_key)
        
        loading_msg = await update.message.reply_text(f"⏳ Completing login for *{name}*...", parse_mode="Markdown")
        
        try:
            resp = self.client.complete_callback(api_key, request_token.strip())
            await loading_msg.delete()
            if resp.get("status") == "error":
                await update.message.reply_text(
                    f"❌ *{name}* authentication failed: {resp.get('message', 'Callback failed')}",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(
                    f"✅ *{name}* authenticated successfully! Session is now active.",
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode="Markdown"
                )
        except Exception as exc:
            if 'loading_msg' in locals():
                try:
                    await loading_msg.delete()
                except Exception:
                    pass
            await update.message.reply_text(
                f"❌ *{name}* login failed: {exc}",
                parse_mode="Markdown"
            )

    def serialize_cmd_to_text(self, cmd) -> str:
        """Helper to serialize a Command object back to its string representation."""
        if isinstance(cmd, PlaceOrderCommand):
            parts = [cmd.action.lower(), cmd.symbol_or_id or "", cmd.quantity or ""]
            if cmd.price is not None:
                parts.append(str(cmd.price))
            if cmd.product and cmd.product != "NRML":
                if cmd.price is None:
                    parts.append("0.0")  # Placeholder price
                parts.append(cmd.product)
            return " ".join([p for p in parts if p])
        elif isinstance(cmd, ExitCommand):
            parts = ["exit", cmd.target]
            if cmd.price is not None:
                parts.append(str(cmd.price))
            return " ".join(parts)
        elif isinstance(cmd, CancelOrderCommand):
            return f"cancel {cmd.target}"
        elif isinstance(cmd, ModifyOrderCommand):
            return f"modify {cmd.order_id} {cmd.quantity} {cmd.price}"
        return ""

    @restrict_user
    async def cmd_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Run a TUI-like command (e.g. 'account ZK8719 && sell NIFTY2670722200PE 50') and return output."""
        raw_text = " ".join(context.args).strip()
        if not raw_text:
            await update.message.reply_text("❌ Usage: `/cmd <TUI_commands>`\nExample: `/cmd account ZK8719 && sell NIFTY2670722200PE 50`", parse_mode="Markdown")
            return
            
        loading_msg = await update.message.reply_text(f"⏳ Executing command: `{raw_text}`...")
        
        try:
            # Parse command(s)
            parsed_cmds = parse_command_line(raw_text)
            
            # Fetch active positions to resolve position IDs
            pos_resp = self.client.get_positions(None)
            active_positions = []
            position_id_map = {}
            idx = 1
            for acct in pos_resp.get("accounts", []):
                for p in acct.get("positions", []):
                    if p.get("quantity", 0) != 0:
                        p["api_key"] = acct.get("api_key")
                        p["account_name"] = acct.get("name")
                        active_positions.append(p)
                        position_id_map[idx] = p
                        idx += 1
                        
            # Execute commands sequentially in-process
            exec_ctx = ExecutionContext(
                client=self.client,
                active_positions=active_positions,
                position_id_map=position_id_map,
                selected_account_key="ALL"
            )
            
            output_lines = []
            for cmd in parsed_cmds:
                res = await execute_command(cmd, exec_ctx)
                if res["status"] == "pending_confirmation":
                    cmd_text = self.serialize_cmd_to_text(cmd)
                    acct_ref = self._get_acct_ref(exec_ctx.selected_account_key)
                    keyboard = [
                        [
                            InlineKeyboardButton("✅ Confirm", callback_data=f"do_exec_cmd:{acct_ref}:{cmd_text}"),
                            InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await loading_msg.delete()
                    await update.message.reply_text(res["message"], reply_markup=reply_markup, parse_mode="Markdown")
                    return
                else:
                    output_lines.append(res["message"])
                    
            await loading_msg.delete()
            final_output = "\n".join(output_lines)
            if len(final_output) > 4000:
                final_output = final_output[:3900] + "\n... (truncated)"
            await update.message.reply_text(f"💻 *cmd output:*\n```\n{final_output}\n```", parse_mode="Markdown")
            
        except Exception as exc:
            if 'loading_msg' in locals():
                try:
                    await loading_msg.delete()
                except Exception:
                    pass
            await update.message.reply_text(f"❌ Execution failed: {exc}")

    def _format_account_positions(self, api_key: str) -> tuple[str, list[list[InlineKeyboardButton]]]:
        """Fetch and format positions for a specific account into a clean monospaced table and separate matching buttons."""
        pos_resp = self.client.get_positions([api_key])
        accounts = pos_resp.get("accounts", [])
        if not accounts:
            return "Account not found.", []

        acct = accounts[0]
        name = acct.get("name", "Account")
        status = acct.get("status", "success")
        if status == "unauthenticated":
            return f"🔴 *{name}*: Not authenticated. Run `/init` to log in.", []
        if isinstance(status, str) and status.startswith("error"):
            err_msg = status.split("error: ", 1)[-1]
            return f"⚠️ *{name}*: Failed to fetch positions ({err_msg}).", []

        total_pnl = acct.get("total_pnl", 0.0)
        positions = [p for p in acct.get("positions", []) if p.get("quantity", 0) != 0]

        pnl_sign = "+" if total_pnl >= 0 else ""
        if not positions:
            return f"✅ No active open positions for *{name}*.", []

        # Construct beautiful ASCII PrettyTable
        table = PrettyTable()
        table.field_names = ["Symbol", "Qty", "Avg", "LTP"]

        keyboard_rows = []
        current_row = []
        for pos in positions:
            sym = pos.get("tradingsymbol", "")
            qty = pos.get("quantity", 0)
            avg = pos.get("average_price", 0.0)
            ltp = pos.get("last_price", 0.0)

            display_sym = clean_option_symbol(sym)
            table.add_row([display_sym, str(qty), f"{avg:.2f}", f"{ltp:.2f}"])

            acct_ref = self._get_acct_ref(api_key)
            btn = InlineKeyboardButton(
                display_sym,
                callback_data=f"select_pos:{sym}:{acct_ref}:{qty}:{avg:.2f}:{ltp:.2f}"
            )
            current_row.append(btn)
            if len(current_row) == 2:
                keyboard_rows.append(current_row)
                current_row = []

        if current_row:
            keyboard_rows.append(current_row)

        msg_lines = [
            f"📊 *{name}* (P&L: {pnl_sign}₹{total_pnl:.2f})",
            f"```\n{table.get_string()}\n```",
            "👇 _Select a position to Modify or Exit:_"
        ]
        return "\n".join(msg_lines), keyboard_rows

    @restrict_user
    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Fetch and display open positions across all accounts as separate interactive tables."""
        try:
            api_keys = [acct["api_key"] for acct in self.client.accounts]
            has_any = False
            for api_key in api_keys:
                msg_text, keyboard = self._format_account_positions(api_key)
                if keyboard or msg_text.startswith("🔴") or msg_text.startswith("⚠️"):
                    has_any = True
                    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
                    await update.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")
            
            if not has_any:
                await update.message.reply_text("✅ No active open positions across any accounts.", reply_markup=get_main_menu_keyboard())
        except Exception as exc:
            import traceback
            traceback.print_exc()
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
                    if o.get("status") in ("OPEN", "TRIGGER PENDING", "AMO SUBMITTED", "AMO REQ RECEIVED", "PUT ORDER REQ RECEIVED")
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

                    acct_ref = self._get_acct_ref(api_key)
                    keyboard = [
                        [
                            InlineKeyboardButton(
                                "Cancel Order",
                                callback_data=f"confirm_cancel:{order_id}:{acct_ref}:{sym}"
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
                await update.message.reply_text("✅ No active pending orders found.", reply_markup=get_main_menu_keyboard())

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
        """Common logic to parse and place buy/sell orders, supporting optional @account routing."""
        args = context.args
        if not args or len(args) < 2:
            await update.message.reply_text(
                f"❌ Invalid syntax. Use:\n"
                f"`/{transaction_type.lower()} <SYMBOL> <QTY> [LIMIT_PRICE] [@ACCOUNT_NAME]`\n\n"
                f"_Examples:_\n"
                f"• `/{transaction_type.lower()} NIFTY2670722200PE 50` (Market, All Accounts)\n"
                f"• `/{transaction_type.lower()} NIFTY2670722200PE 50 85.20 @ZK8719` (Limit, ZK8719 only)",
                parse_mode="Markdown"
            )
            return

        # Check for optional target account prefixed with '@'
        target_account_name = None
        for arg in args:
            if arg.startswith("@") and len(arg) > 1:
                target_account_name = arg[1:]
                args = [a for a in args if a != arg]
                break

        symbol = args[0]
        qty = args[1]
        price = args[2] if len(args) >= 3 else None

        cmd_parts = [transaction_type.lower(), symbol, qty]
        if price:
            cmd_parts.append(price)

        cmd_str = " ".join(cmd_parts)
        if target_account_name:
            cmd_str = f"account {target_account_name} && {cmd_str}"

        # Delegate execution
        context.args = cmd_str.split()
        await self.cmd_cmd(update, context)

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

        cmd_str = f"modify {args[0]} {args[1]} {args[2]}"
        context.args = cmd_str.split()
        await self.cmd_cmd(update, context)

    @restrict_user
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline button actions and confirmations."""
        query = update.callback_query
        await query.answer()

        data = query.data.split(":")
        action = data[0]

        if action == "do_exec_cmd":
            acct_ref = data[1]
            cmd_text = ":".join(data[2:])
            await query.edit_message_text(f"⏳ Executing confirmed command: `{cmd_text}`...")
            try:
                parsed_cmds = parse_command_line(cmd_text)
                for c in parsed_cmds:
                    if hasattr(c, "confirmed"):
                        c.confirmed = True
                        
                pos_resp = self.client.get_positions(None)
                active_positions = []
                position_id_map = {}
                idx = 1
                for acct in pos_resp.get("accounts", []):
                    for p in acct.get("positions", []):
                        if p.get("quantity", 0) != 0:
                            p["api_key"] = acct.get("api_key")
                            p["account_name"] = acct.get("name")
                            active_positions.append(p)
                            position_id_map[idx] = p
                            idx += 1
                            
                selected_account_key = self._resolve_api_key(acct_ref)
                exec_ctx = ExecutionContext(
                    client=self.client,
                    active_positions=active_positions,
                    position_id_map=position_id_map,
                    selected_account_key=selected_account_key
                )
                
                output_lines = []
                for c in parsed_cmds:
                    res = await execute_command(c, exec_ctx)
                    output_lines.append(res["message"])
                    
                await query.edit_message_text(f"💻 *Command Output:*\n```\n{'/'.join(output_lines)}\n```", parse_mode="Markdown")
            except Exception as exc:
                await query.edit_message_text(f"❌ Execution failed: {exc}")

        elif action == "select_pos":
            symbol = data[1]
            api_key = self._resolve_api_key(data[2])
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
            
            acct_ref = self._get_acct_ref(api_key)
            keyboard = [
                [
                    InlineKeyboardButton("🚨 Exit Position", callback_data=f"confirm_exit_single:{symbol}:{acct_ref}:{qty}:{ltp:.2f}"),
                    InlineKeyboardButton("➕ Add More", callback_data=f"confirm_add_more:{symbol}:{acct_ref}:{qty}:{ltp:.2f}")
                ],
                [
                    InlineKeyboardButton("🔙 Back to Positions", callback_data=f"back_to_positions:{acct_ref}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(msg, reply_markup=reply_markup, parse_mode="Markdown")

        elif action == "confirm_exit_single":
            symbol = data[1]
            api_key = self._resolve_api_key(data[2])
            qty = int(data[3])
            ltp = float(data[4]) if len(data) > 4 else 0.0
            
            ltp_plus_0_1 = ltp * 1.001
            ltp_minus_0_1 = ltp * 0.999
            ltp_plus_0_5 = ltp * 1.005
            ltp_minus_0_5 = ltp * 0.995

            confirm_text = (
                f"🚨 *Exit Price Selection*\n\n"
                f"Select an exit limit price or market price for `{symbol}` (Qty: `{qty}`):\n"
                f"• *LTP:* `{ltp:.2f}`"
            )
            acct_ref = self._get_acct_ref(api_key)
            keyboard = [
                [
                    InlineKeyboardButton(f"Limit @ {ltp:.2f} (LTP)", callback_data=f"do_exit_single:{symbol}:{acct_ref}:{ltp:.2f}"),
                ],
                [
                    InlineKeyboardButton(f"Limit @ {ltp_plus_0_1:.2f} (+0.1%)", callback_data=f"do_exit_single:{symbol}:{acct_ref}:{ltp_plus_0_1:.2f}"),
                    InlineKeyboardButton(f"Limit @ {ltp_minus_0_1:.2f} (-0.1%)", callback_data=f"do_exit_single:{symbol}:{acct_ref}:{ltp_minus_0_1:.2f}"),
                ],
                [
                    InlineKeyboardButton(f"Limit @ {ltp_plus_0_5:.2f} (+0.5%)", callback_data=f"do_exit_single:{symbol}:{acct_ref}:{ltp_plus_0_5:.2f}"),
                    InlineKeyboardButton(f"Limit @ {ltp_minus_0_5:.2f} (-0.5%)", callback_data=f"do_exit_single:{symbol}:{acct_ref}:{ltp_minus_0_5:.2f}"),
                ],
                [
                    InlineKeyboardButton("💬 Custom Price", callback_data=f"prompt_custom_price:{symbol}:{acct_ref}"),
                    InlineKeyboardButton("🚨 Market Exit", callback_data=f"do_exit_single:{symbol}:{acct_ref}:market")
                ],
                [
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(confirm_text, reply_markup=reply_markup, parse_mode="Markdown")

        elif action == "prompt_custom_price":
            symbol = data[1]
            api_key = self._resolve_api_key(data[2])
            acct_name = "Account"
            for acct in self.client.accounts:
                if acct.get("api_key") == api_key:
                    acct_name = acct.get("name", "Account")
                    break
            
            from telegram import ForceReply
            await query.message.reply_text(
                f"Enter custom limit price for `{symbol}` under `@{acct_name}` in reply to this message:",
                reply_markup=ForceReply(selective=True)
            )

        elif action == "do_exit_single":
            symbol = data[1]
            api_key = self._resolve_api_key(data[2])
            price = None
            if len(data) > 3:
                price_str = data[3]
                if price_str != "market":
                    try:
                        price = float(price_str)
                    except ValueError:
                        price = None

            if price is not None:
                await query.edit_message_text(f"⏳ Placing limit exit order for `{symbol}` at `{price:.2f}`...")
            else:
                await query.edit_message_text(f"⏳ Placing market exit order for `{symbol}`...")
                
            try:
                res = self.client.exit_positions([api_key], tradingsymbol=symbol, price=price)
                res_lines = [f"📊 *Exit Result:* `{symbol}`" + (f" @ `{price:.2f}`" if price is not None else " (Market)")]
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
            api_key = self._resolve_api_key(data[2])
            qty = int(data[3])
            ltp = float(data[4]) if len(data) > 4 else 0.0
            
            tx_type = "BUY" if qty > 0 else "SELL"
            abs_qty = abs(qty)
            exchange = "NFO" if len(symbol) > 6 else "NSE"
            
            ltp_plus_0_1 = ltp * 1.001
            ltp_minus_0_1 = ltp * 0.999
            ltp_plus_0_5 = ltp * 1.005
            ltp_minus_0_5 = ltp * 0.995

            confirm_text = (
                f"➕ *Add More Position Price Selection*\n\n"
                f"Select order price to add `{abs_qty}` more to `{symbol}` (Current Qty: `{qty}`):\n"
                f"• *LTP:* `{ltp:.2f}`"
            )
            acct_ref = self._get_acct_ref(api_key)
            keyboard = [
                [
                    InlineKeyboardButton(f"Limit @ {ltp:.2f} (LTP)", callback_data=f"do_place_add:{tx_type}:{symbol}:{abs_qty}:{exchange}:{acct_ref}:{ltp:.2f}"),
                ],
                [
                    InlineKeyboardButton(f"Limit @ {ltp_plus_0_1:.2f} (+0.1%)", callback_data=f"do_place_add:{tx_type}:{symbol}:{abs_qty}:{exchange}:{acct_ref}:{ltp_plus_0_1:.2f}"),
                    InlineKeyboardButton(f"Limit @ {ltp_minus_0_1:.2f} (-0.1%)", callback_data=f"do_place_add:{tx_type}:{symbol}:{abs_qty}:{exchange}:{acct_ref}:{ltp_minus_0_1:.2f}"),
                ],
                [
                    InlineKeyboardButton(f"Limit @ {ltp_plus_0_5:.2f} (+0.5%)", callback_data=f"do_place_add:{tx_type}:{symbol}:{abs_qty}:{exchange}:{acct_ref}:{ltp_plus_0_5:.2f}"),
                    InlineKeyboardButton(f"Limit @ {ltp_minus_0_5:.2f} (-0.5%)", callback_data=f"do_place_add:{tx_type}:{symbol}:{abs_qty}:{exchange}:{acct_ref}:{ltp_minus_0_5:.2f}"),
                ],
                [
                    InlineKeyboardButton("💬 Custom Price", callback_data=f"prompt_custom_price_add:{tx_type}:{symbol}:{abs_qty}:{exchange}:{acct_ref}"),
                    InlineKeyboardButton("🚨 Market Order", callback_data=f"do_place_add:{tx_type}:{symbol}:{abs_qty}:{exchange}:{acct_ref}:market")
                ],
                [
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(confirm_text, reply_markup=reply_markup, parse_mode="Markdown")

        elif action == "prompt_custom_price_add":
            tx_type = data[1]
            symbol = data[2]
            abs_qty = data[3]
            exchange = data[4]
            acct_ref = data[5]
            api_key = self._resolve_api_key(acct_ref)
            acct_name = "Account"
            for acct in self.client.accounts:
                if acct.get("api_key") == api_key:
                    acct_name = acct.get("name", "Account")
                    break
            
            from telegram import ForceReply
            await query.message.reply_text(
                f"Enter custom limit price for adding `{abs_qty}` more to `{symbol}` under `@{acct_name}` (`{tx_type}` segments: `{exchange}`):",
                reply_markup=ForceReply(selective=True)
            )

        elif action == "do_place_add":
            tx_type = data[1]
            symbol = data[2]
            qty = int(data[3])
            exchange = data[4]
            api_key = self._resolve_api_key(data[5])
            
            price = None
            if len(data) > 6:
                price_str = data[6]
                if price_str != "market":
                    try:
                        price = float(price_str)
                    except ValueError:
                        price = None

            if price is not None:
                await query.edit_message_text(f"⏳ Placing limit order to add to `{symbol}` at `{price:.2f}`...")
            else:
                await query.edit_message_text(f"⏳ Placing market order to add to `{symbol}`...")
                
            try:
                res = self.client.place_order(
                    api_keys=[api_key],
                    tradingsymbol=symbol,
                    exchange=exchange,
                    transaction_type=tx_type,
                    quantity=qty,
                    order_type="LIMIT" if price is not None else "MARKET",
                    price=price
                )
                res_lines = [f"📊 *Order Placement Result:* `{symbol}`" + (f" @ `{price:.2f}`" if price is not None else " (Market)")]
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
            api_key = self._resolve_api_key(data[1])
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
            api_key = self._resolve_api_key(data[2])
            symbol = data[3]
            confirm_text = (
                f"🚨 *Cancel Order Confirmation*\n\n"
                f"Cancel order `{order_id}` (`{symbol}`)?"
            )
            acct_ref = self._get_acct_ref(api_key)
            keyboard = [
                [
                    InlineKeyboardButton(
                        "✅ Yes, Cancel Order",
                        callback_data=f"do_cancel:{order_id}:{acct_ref}"
                    ),
                    InlineKeyboardButton("❌ No, Keep Pending", callback_data="cancel_action")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_reply_markup(reply_markup=reply_markup)

        elif action == "do_cancel":
            order_id = data[1]
            api_key = self._resolve_api_key(data[2])
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
            target_key = self._resolve_api_key(data[6])
            price = float(data[7]) if len(data) > 7 else None

            await query.edit_message_text(f"⏳ Placing `{ord_type}` `{tx_type}` orders for `{symbol}`...")
            try:
                if target_key == "ALL":
                    api_keys = [acct["api_key"] for acct in self.client.accounts]
                else:
                    api_keys = [target_key]

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

    @restrict_user
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming text messages (e.g. replies to custom price prompts)."""
        if not update.message or not update.message.reply_to_message:
            return
        
        reply_text = update.message.reply_to_message.text
        if not reply_text:
            return
            
        # 1. Match custom exit price prompt
        match_exit = re.search(r"Enter custom limit price for `([^`]+)`(?: under `@([^`]+)`)?", reply_text)
        if match_exit and "reply to this message:" in reply_text:
            symbol = match_exit.group(1)
            acct_name = match_exit.group(2)
            price_input = update.message.text.strip()
            
            try:
                price = float(price_input)
                if price <= 0:
                    raise ValueError("Price must be positive")
            except Exception:
                await update.message.reply_text(f"❌ Invalid price: `{price_input}`. Please reply with a valid number.")
                return

            # Resolve api_key using acct_name, fallback to positions lookup if name is not present
            api_key = None
            if acct_name:
                for acct in self.client.accounts:
                    if acct.get("name") == acct_name or acct.get("api_key") == acct_name:
                        api_key = acct.get("api_key")
                        break
            else:
                pos_resp = self.client.get_positions(None)
                for acct in pos_resp.get("accounts", []):
                    for p in acct.get("positions", []):
                        if p.get("tradingsymbol") == symbol and p.get("quantity", 0) != 0:
                            api_key = p.get("api_key") or acct.get("api_key")
                            break
                    if api_key:
                        break
                    
            if not api_key:
                if self.client.accounts:
                    api_key = self.client.accounts[0].get("api_key")
                    
            if not api_key:
                await update.message.reply_text("❌ Failed to resolve account for this position.")
                return
                
            await update.message.reply_text(f"⏳ Placing limit exit order for `{symbol}` at `{price:.2f}`...")
            try:
                res = self.client.exit_positions([api_key], tradingsymbol=symbol, price=price)
                res_lines = [f"📊 *Limit Exit Result:* `{symbol}` @ `{price:.2f}`"]
                for r in res.get("results", []):
                    name = r.get("name")
                    status = r.get("status")
                    msg = r.get("message", "")
                    icon = "✅" if status == "success" else "❌"
                    res_lines.append(f"{icon} *{name}*: {msg}")

                await update.message.reply_text("\n".join(res_lines), parse_mode="Markdown")
            except Exception as exc:
                await update.message.reply_text(f"❌ Limit exit execution failed: {exc}")
            return

        # 2. Match custom add more price prompt
        match_add = re.search(r"Enter custom limit price for adding `([^`]+)` more to `([^`]+)`(?: under `@([^`]+)`)? \(`([^`]+)` segments: `([^`]+)`\)", reply_text)
        if match_add:
            qty_str = match_add.group(1)
            symbol = match_add.group(2)
            acct_name = match_add.group(3)
            tx_type = match_add.group(4)
            exchange = match_add.group(5)
            price_input = update.message.text.strip()
            
            try:
                price = float(price_input)
                if price <= 0:
                    raise ValueError("Price must be positive")
            except Exception:
                await update.message.reply_text(f"❌ Invalid price: `{price_input}`. Please reply with a valid number.")
                return

            # Resolve api_key using acct_name, fallback to positions lookup if name is not present
            api_key = None
            if acct_name:
                for acct in self.client.accounts:
                    if acct.get("name") == acct_name or acct.get("api_key") == acct_name:
                        api_key = acct.get("api_key")
                        break
            else:
                pos_resp = self.client.get_positions(None)
                for acct in pos_resp.get("accounts", []):
                    for p in acct.get("positions", []):
                        if p.get("tradingsymbol") == symbol and p.get("quantity", 0) != 0:
                            api_key = p.get("api_key") or acct.get("api_key")
                            break
                    if api_key:
                        break
            if not api_key and self.client.accounts:
                api_key = self.client.accounts[0].get("api_key")
            if not api_key:
                await update.message.reply_text("❌ Failed to resolve account for this position.")
                return

            await update.message.reply_text(f"⏳ Placing limit order to add to `{symbol}` at `{price:.2f}`...")
            try:
                res = self.client.place_order(
                    api_keys=[api_key],
                    tradingsymbol=symbol,
                    exchange=exchange,
                    transaction_type=tx_type,
                    quantity=int(qty_str),
                    order_type="LIMIT",
                    price=price
                )
                res_lines = [f"📊 *Limit Order Result:* `{symbol}` @ `{price:.2f}`"]
                for r in res.get("results", []):
                    name = r.get("name")
                    status = r.get("status")
                    msg = r.get("message", "")
                    icon = "✅" if status == "success" else "❌"
                    res_lines.append(f"{icon} *{name}*: {msg}")

                await update.message.reply_text("\n".join(res_lines), parse_mode="Markdown")
            except Exception as exc:
                await update.message.reply_text(f"❌ Limit order placement failed: {exc}")
            return
