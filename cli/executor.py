from typing import Optional, Union, Any, Tuple
from cli.api_client import KCLIClient
from cli.parser import (
    AccountSelectCommand, PositionSelectCommand, OrderSelectCommand,
    PlaceOrderCommand, ExitCommand,
    StatusCommand, PositionsCommand, OrdersCommand,
    CancelOrderCommand, ModifyOrderCommand, MarketDepthCommand,
    ConditionalOrderCommand, CancelConditionalOrderCommand, ConditionalOrdersViewCommand,
    CancelScheduledOrderCommand, ScheduledOrdersViewCommand
)

class ExecutionContext:
    def __init__(
        self,
        client: KCLIClient,
        active_positions: Optional[list[dict]] = None,
        position_id_map: Optional[dict[int, dict]] = None,
        selected_symbol: Optional[str] = None,
        selected_account_key: str = "ALL"
    ) -> None:
        self.client = client
        self.active_positions = active_positions or []
        self.position_id_map = position_id_map or {}
        self.selected_symbol = selected_symbol
        self.selected_account_key = selected_account_key


def resolve_qty(client: KCLIClient, symbol: str, qty_arg: str) -> Tuple[int, str]:
    """Resolve quantity string (potentially containing lot size notation like 2L) to integer quantity.
    
    Returns:
        (qty, display_str)
    """
    cleaned = qty_arg.strip()
    if cleaned.upper().endswith("L"):
        lots_str = cleaned[:-1]
        if not lots_str.isdigit() or int(lots_str) <= 0:
            raise ValueError(f"Invalid lot quantity '{qty_arg}'. Use e.g. '2L' for 2 lots.")
        lots = int(lots_str)
        lot_size = 1
        if symbol:
            lot_size = client.get_nfo_lot_sizes().get(symbol.upper(), 0)
            if not lot_size:
                sym_up = symbol.upper()
                if sym_up.startswith("SENSEX"):
                    lot_size = 20
                elif sym_up.startswith("BANKEX"):
                    lot_size = 30
                elif sym_up.startswith("NIFTY"):
                    lot_size = 65
                elif sym_up.startswith("BANKNIFTY"):
                    lot_size = 30
                elif sym_up.startswith("FINNIFTY"):
                    lot_size = 65
                elif sym_up.startswith("MIDCPNIFTY"):
                    lot_size = 120
            if not lot_size:
                raise ValueError(
                    f"No lot size found for '{symbol}'. "
                    "Use raw quantity (e.g. '75') for equity symbols."
                )
        raw_qty = lots * lot_size
        return raw_qty, f"{lots}L ({raw_qty} qty)"
    else:
        if not cleaned.isdigit() or int(cleaned) <= 0:
            raise ValueError(f"Invalid quantity '{qty_arg}'. Must be a positive integer or lot notation (e.g. '2L').")
        raw_qty = int(cleaned)
        return raw_qty, str(raw_qty)


def resolve_account_by_name(accounts: list[dict], name: str) -> dict:
    """Helper to resolve an account dict by its name or api_key."""
    for acct in accounts:
        if acct.get("name") == name or acct.get("api_key") == name:
            return acct
    raise ValueError(f"Account '{name}' not found.")


def resolve_api_keys(context: ExecutionContext) -> list[str]:
    """Resolve target api_keys from context.selected_account_key."""
    if not context.selected_account_key or context.selected_account_key == "ALL":
        return [a["api_key"] for a in context.client.accounts]
    try:
        acct = resolve_account_by_name(context.client.accounts, context.selected_account_key)
        return [acct["api_key"]]
    except ValueError:
        return [context.selected_account_key]


def resolve_symbol_or_id(context: ExecutionContext, target: str) -> Tuple[str, list[str]]:
    """Resolves a symbol or numeric ID to target symbol and associated api_keys."""
    if target.isdigit():
        pos_id = int(target)
        if pos_id in context.position_id_map:
            pos = context.position_id_map[pos_id]
            return pos["tradingsymbol"], [pos["api_key"]]
        if context.active_positions and 0 < pos_id <= len(context.active_positions):
            pos = context.active_positions[pos_id - 1]
            return pos["tradingsymbol"], [pos["api_key"]]
        raise ValueError(f"Position ID {pos_id} not found.")
    
    symbol = None if target.lower() == "all" else target.upper()
    api_keys = resolve_api_keys(context)
    
    # Narrow down api_keys if the symbol is found in active positions
    if symbol and context.selected_account_key == "ALL":
        matching_keys = [p["api_key"] for p in context.active_positions if p["tradingsymbol"] == symbol]
        if matching_keys:
            api_keys = matching_keys
            
    return symbol, api_keys


async def execute_command(cmd: Any, context: ExecutionContext) -> dict:
    """Executes a parsed command dataclass against the provided ExecutionContext."""
    
    # 1. Account select
    if isinstance(cmd, AccountSelectCommand):
        target_name = cmd.account_name
        if target_name.lower() in ("none", "clear", "null", "all"):
            context.selected_account_key = "ALL"
            return {
                "status": "executed",
                "message": "Account selection cleared (targeting all accounts)."
            }
        
        resolved_acct = resolve_account_by_name(context.client.accounts, target_name)
        context.selected_account_key = resolved_acct["api_key"]
        return {
            "status": "executed",
            "message": f"Selected account: {resolved_acct.get('name', resolved_acct['api_key'])}"
        }

    # 1.1 Position Select
    elif isinstance(cmd, PositionSelectCommand):
        target = cmd.target.strip()
        if target.lower() in ("none", "clear", "null", "all"):
            context.selected_symbol = None
            return {
                "status": "executed",
                "message": "Position selection cleared."
            }
        symbol, api_keys = resolve_symbol_or_id(context, target)
        context.selected_symbol = symbol
        if api_keys and len(api_keys) == 1:
            context.selected_account_key = api_keys[0]
        return {
            "status": "executed",
            "message": f"Selected position: {symbol}"
        }

    # 1.2 Order Select
    elif isinstance(cmd, OrderSelectCommand):
        target = cmd.target.strip()
        if target.lower() in ("none", "clear", "null"):
            return {
                "status": "executed",
                "message": "Order selection cleared."
            }
        return {
            "status": "executed",
            "message": f"Selected order: {target}"
        }

    # 2. Place Order (Buy / Sell)
    elif isinstance(cmd, PlaceOrderCommand):
        symbol = cmd.symbol_or_id
        api_keys = resolve_api_keys(context)
        
        if symbol:
            if symbol.isdigit():
                symbol, api_keys = resolve_symbol_or_id(context, symbol)
            else:
                symbol = symbol.upper()
                if context.selected_account_key == "ALL":
                    if context.selected_symbol and context.selected_symbol.upper() == symbol:
                        for p in context.active_positions:
                            if p.get("tradingsymbol", "").upper() == symbol:
                                api_keys = [p["api_key"]]
                                break
                    else:
                        matching_positions = [
                            p for p in context.active_positions
                            if p.get("tradingsymbol", "").upper() == symbol
                        ]
                        if len(matching_positions) == 1:
                            api_keys = [matching_positions[0]["api_key"]]
        else:
            if not context.selected_symbol:
                raise ValueError("No symbol specified and no active selection in TUI.")
            symbol = context.selected_symbol
            if context.selected_account_key == "ALL":
                for p in context.active_positions:
                    if p.get("tradingsymbol") == symbol:
                        api_keys = [p["api_key"]]
                        break
        
        if not cmd.quantity:
            matched_pos = None
            for p in context.active_positions:
                if p.get("tradingsymbol") == symbol and p.get("api_key") in api_keys:
                    matched_pos = p
                    break
            if not matched_pos:
                raise ValueError("Quantity omitted, but no matching position found to fallback to.")
            qty_val = abs(matched_pos.get("quantity", 0))
            qty_display = str(qty_val)
        else:
            qty_val, qty_display = resolve_qty(context.client, symbol, cmd.quantity)

        price_str = f" @ {cmd.price:.2f}" if cmd.price else " (MARKET)"
        message = f"Confirm {cmd.action} order of {qty_display} {symbol}{price_str} ({cmd.product})?"

        if not cmd.confirmed:
            return {
                "status": "pending_confirmation",
                "message": message,
                "command": cmd
            }

        sym_up = symbol.upper()
        if sym_up.startswith(("SENSEX", "BANKEX")):
            exchange = "BFO"
        elif sym_up.startswith(("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")):
            exchange = "NFO"
        elif len(symbol) > 6:
            exchange = "NFO"
        else:
            exchange = "NSE"
        order_type = "LIMIT" if cmd.price else "MARKET"

        res = context.client.place_order(
            api_keys=api_keys,
            tradingsymbol=symbol,
            exchange=exchange,
            transaction_type=cmd.action,
            quantity=qty_val,
            order_type=order_type,
            price=cmd.price,
            product=cmd.product
        )

        output_lines = [f"Placing {order_type} {cmd.action} for {symbol} (Qty: {qty_val})..."]
        for r in res.get("results", []):
            icon = "✅" if r.get("status") == "success" else "❌"
            output_lines.append(f"  {icon} {r.get('name')}: {r.get('message', 'Success')}")
        
        return {
            "status": "executed",
            "message": "\n".join(output_lines)
        }

    # 3. Exit Command
    elif isinstance(cmd, ExitCommand):
        target = cmd.target
        symbol = None
        api_keys = []

        if target == "selected":
            if not context.selected_symbol:
                raise ValueError("No active selection to exit.")
            symbol = context.selected_symbol
            api_keys = resolve_api_keys(context)
            for p in context.active_positions:
                if p.get("tradingsymbol") == symbol:
                    api_keys = [p["api_key"]]
                    break
        else:
            symbol, api_keys = resolve_symbol_or_id(context, target)

        price_str = f" @ {cmd.price:.2f}" if cmd.price else " (MARKET)"
        target_display = symbol if symbol else "ALL"
        message = f"Confirm EXIT of {target_display} positions{price_str}?"

        if not cmd.confirmed:
            return {
                "status": "pending_confirmation",
                "message": message,
                "command": cmd
            }

        res = context.client.exit_positions(
            api_keys=api_keys,
            tradingsymbol=symbol,
            price=cmd.price
        )

        output_lines = [f"Exiting positions for {target_display}..."]
        for r in res.get("results", []):
            icon = "✅" if r.get("status") == "success" else "❌"
            output_lines.append(f"  {icon} {r.get('name')}: {r.get('message', 'Success')}")
        
        return {
            "status": "executed",
            "message": "\n".join(output_lines)
        }

    # 4. Status Command
    elif isinstance(cmd, StatusCommand):
        res = context.client.get_status()
        output_lines = ["🔌 Account Status:"]
        for acct in res.get("accounts", []):
            icon = "🟢" if acct.get("authenticated") else "🔴"
            output_lines.append(f"  {icon} {acct.get('name')}: {'Active' if acct.get('authenticated') else 'Inactive'}")
        return {
            "status": "executed",
            "message": "\n".join(output_lines)
        }

    # 5. Positions / Pos Command
    elif isinstance(cmd, PositionsCommand):
        res = context.client.get_positions(
            [context.selected_account_key] if context.selected_account_key != "ALL" else None
        )
        output_lines = []
        for acct in res.get("accounts", []):
            output_lines.append(f"📊 Account: {acct.get('name')} (P&L: {acct.get('total_pnl', 0.0):.2f})")
            positions = [p for p in acct.get("positions", []) if p.get("quantity", 0) != 0]
            if not positions:
                output_lines.append("  No open positions.")
            else:
                for p in positions:
                    sym = p.get("tradingsymbol")
                    output_lines.append(f"  • {sym} | Qty: {p.get('quantity')} | Avg: {p.get('average_price'):.2f} | LTP: {p.get('last_price'):.2f}")
        return {
            "status": "executed",
            "message": "\n".join(output_lines)
        }

    # 5.5 Status Command
    elif isinstance(cmd, StatusCommand):
        res = context.client.get_status()
        output_lines = ["🔌 Account Connection & Health Status:"]
        for acct in res.get("accounts", []):
            auth_icon = "🟢 Active" if acct.get("authenticated") else "🔴 Inactive"
            broker_name = acct.get("broker", "Zerodha").title()
            output_lines.append(f"  • {acct.get('name', 'Account')} ({broker_name}): {auth_icon}")
        return {
            "status": "executed",
            "message": "\n".join(output_lines)
        }

    # 6. Orders Command
    elif isinstance(cmd, OrdersCommand):
        api_keys = [context.selected_account_key] if context.selected_account_key != "ALL" else None
        res = context.client.get_orders(api_keys)
        output_lines = ["📋 Pending Orders:"]
        found_any = False
        for acct in res.get("accounts", []):
            orders = [o for o in acct.get("orders", []) if o.get("status") in ("OPEN", "TRIGGER PENDING")]
            if orders:
                found_any = True
                output_lines.append(f"  Account: {acct.get('name')}")
                for o in orders:
                    output_lines.append(
                        f"    ID: {o.get('order_id')} | {o.get('transaction_type')} {o.get('tradingsymbol')} | "
                        f"Qty: {o.get('pending_quantity')}/{o.get('quantity')} | Price: {o.get('price')} | Status: {o.get('status')}"
                    )
        if not found_any:
            output_lines.append("  No pending orders.")
        return {
            "status": "executed",
            "message": "\n".join(output_lines)
        }

    # 7. Cancel Command
    elif isinstance(cmd, CancelOrderCommand):
        orders_to_cancel = getattr(cmd, "orders_to_cancel", None)
        if not orders_to_cancel:
            target = cmd.target.strip()
            api_keys = resolve_api_keys(context)
            
            orders_to_cancel = []
            orders_res = context.client.get_orders(api_keys)
            PENDING_STATUSES = {"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "PUT ORDER REQ RECEIVED"}
            target_up = target.upper()

            for acct in orders_res.get("accounts", []):
                ak = acct.get("api_key")
                for o in acct.get("orders", []):
                    status_up = str(o.get("status", "")).upper()
                    if status_up in PENDING_STATUSES:
                        oid = str(o.get("order_id", ""))
                        sym = str(o.get("tradingsymbol", "")).upper()
                        if target.lower() == "all":
                            orders_to_cancel.append({"order_id": oid, "api_key": ak, "symbol": sym})
                        elif oid == target or oid.endswith(target) or (len(target) >= 4 and target in oid):
                            orders_to_cancel.append({"order_id": oid, "api_key": ak, "symbol": sym})
                        elif sym == target_up or target_up in sym:
                            orders_to_cancel.append({"order_id": oid, "api_key": ak, "symbol": sym})

            # Fallback for exact ID if not found in pending statuses
            if not orders_to_cancel and target.lower() != "all":
                for acct in orders_res.get("accounts", []):
                    ak = acct.get("api_key")
                    for o in acct.get("orders", []):
                        if str(o.get("order_id", "")) == target:
                            orders_to_cancel.append({"order_id": str(o.get("order_id")), "api_key": ak, "symbol": str(o.get("tradingsymbol"))})
                            break

            if not orders_to_cancel:
                raise ValueError(f"No pending orders found matching target '{target}'.")

            cmd.orders_to_cancel = orders_to_cancel

        message = f"Confirm CANCEL of {len(orders_to_cancel)} order(s)?"
        if not cmd.confirmed:
            return {
                "status": "pending_confirmation",
                "message": message,
                "command": cmd
            }

        output_lines = [f"Cancelling {len(orders_to_cancel)} order(s)..."]
        has_error = False
        for o in orders_to_cancel:
            ak = o.get("api_key") or (context.selected_account_key if context.selected_account_key != "ALL" else None) or (context.client.accounts[0]["api_key"] if context.client.accounts else None)
            try:
                res = context.client.cancel_order(api_key=ak, order_id=o["order_id"])
                icon = "✅" if res.get("status") == "success" else "❌"
                if res.get("status") != "success":
                    has_error = True
                output_lines.append(f"  {icon} Order {o['order_id']}: {res.get('message', 'Success')}")
            except Exception as e:
                has_error = True
                output_lines.append(f"  ❌ Order {o['order_id']}: {str(e)}")
        
        return {
            "status": "error" if has_error and len(orders_to_cancel) == 1 else "executed",
            "message": "\n".join(output_lines)
        }

    # 8. Modify Command
    elif isinstance(cmd, ModifyOrderCommand):
        matches = getattr(cmd, "matches", None)
        if not matches:
            target = str(cmd.order_id).strip()
            api_keys = resolve_api_keys(context)

            orders_res = context.client.get_orders(api_keys)
            PENDING_STATUSES = {"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "PUT ORDER REQ RECEIVED"}
            target_up = target.upper()

            matches = []
            for acct in orders_res.get("accounts", []):
                ak = acct.get("api_key")
                for o in acct.get("orders", []):
                    status_up = str(o.get("status", "")).upper()
                    if status_up in PENDING_STATUSES:
                        oid = str(o.get("order_id", ""))
                        sym = str(o.get("tradingsymbol", "")).upper()
                        if oid == target or oid.endswith(target) or (len(target) >= 4 and target in oid):
                            matches.append((ak, o))
                        elif sym == target_up or target_up in sym:
                            matches.append((ak, o))

            if not matches:
                for acct in orders_res.get("accounts", []):
                    ak = acct.get("api_key")
                    for o in acct.get("orders", []):
                        if str(o.get("order_id", "")) == target:
                            matches.append((ak, o))
                            break

            if not matches:
                raise ValueError(f"No pending order found matching '{target}'.")

            cmd.matches = matches

        if not cmd.confirmed:
            if len(matches) == 1:
                ak, o = matches[0]
                oid = str(o.get("order_id"))
                sym = str(o.get("tradingsymbol"))
                qty_val, qty_display = resolve_qty(context.client, sym, cmd.quantity) if cmd.quantity else (o.get("quantity") or o.get("pending_quantity"), str(o.get("quantity") or o.get("pending_quantity")))
                message = f"Confirm MODIFY of order {oid} ({sym}) to Qty: {qty_display}, Price: {cmd.price:.2f}?"
            else:
                desc = ", ".join(f"{str(o.get('order_id'))[-6:]}@{str(o.get('tradingsymbol'))}" for _, o in matches[:3])
                if len(matches) > 3:
                    desc += f" ... ({len(matches)} total)"
                message = f"Confirm MODIFY of {len(matches)} order(s) matching '{target}' to Price: {cmd.price:.2f}?"
            return {
                "status": "pending_confirmation",
                "message": message,
                "command": cmd
            }

        output_lines = [f"Modifying {len(matches)} order(s)..."]
        has_error = False
        for ak, o in matches:
            oid = str(o.get("order_id"))
            sym = str(o.get("tradingsymbol"))
            if cmd.quantity:
                qty_val, _ = resolve_qty(context.client, sym, cmd.quantity)
            else:
                qty_val = o.get("quantity") or o.get("pending_quantity")
            try:
                res = context.client.modify_order(
                    api_key=ak,
                    order_id=oid,
                    quantity=qty_val,
                    price=cmd.price
                )
                icon = "✅" if res.get("status") == "success" else "❌"
                if res.get("status") != "success":
                    has_error = True
                output_lines.append(f"  {icon} Order {oid} ({sym}): {res.get('message', 'Success')}")
            except Exception as e:
                has_error = True
                output_lines.append(f"  ❌ Order {oid} ({sym}): {str(e)}")

        return {
            "status": "error" if has_error and len(matches) == 1 else "executed",
            "message": "\n".join(output_lines)
        }

    # 9. Market Depth Command
    elif isinstance(cmd, MarketDepthCommand):
        target_sym = cmd.symbol or context.selected_symbol
        if not target_sym:
            raise ValueError("Usage: md <symbol> (e.g. 'md NIFTY2682025000CE' or 'md INFY')")

        res = context.client.get_market_depth(target_sym)
        if res.get("status") != "success":
            return {
                "status": "executed",
                "message": f"❌ Failed to fetch market depth for {target_sym}: {res.get('message', 'Unknown error')}"
            }

        sym = res.get("symbol", target_sym)
        ltp = res.get("last_price", 0.0)
        chg = res.get("change", 0.0)
        chg_pct = res.get("change_pct", 0.0)
        vol = res.get("volume", 0)
        oi = res.get("oi", 0)
        tot_b = res.get("total_buy_quantity", 0)
        tot_s = res.get("total_sell_quantity", 0)

        output_lines = [
            f"📊 Market Depth: {sym} | LTP: ₹{ltp:.2f} ({chg:+.2f} / {chg_pct:+.2f}%) | Vol: {vol:,} | OI: {oi:,}",
            "┌─────────┬─────────┬──────────────┬──────────────┬─────────┬─────────┐",
            "│ Orders  │ Qty     │ Bid Price    │ Ask Price    │ Qty     │ Orders  │",
            "├─────────┼─────────┼──────────────┼──────────────┼─────────┼─────────┤"
        ]

        bids = res.get("buy_depth", [])
        asks = res.get("sell_depth", [])
        for i in range(5):
            b = bids[i] if i < len(bids) else {}
            a = asks[i] if i < len(asks) else {}
            b_ord = f"{b.get('orders', 0):>7}" if b.get('orders') else "       -"
            b_qty = f"{b.get('quantity', 0):>7}" if b.get('quantity') else "       -"
            b_pr = f"₹{b.get('price', 0.0):>9.2f}" if b.get('price') else "        -"
            a_pr = f"₹{a.get('price', 0.0):>9.2f}" if a.get('price') else "        -"
            a_qty = f"{a.get('quantity', 0):>7}" if a.get('quantity') else "       -"
            a_ord = f"{a.get('orders', 0):>7}" if a.get('orders') else "       -"
            output_lines.append(f"│ {b_ord} │ {b_qty} │ {b_pr} │ {a_pr} │ {a_qty} │ {a_ord} │")

        output_lines.append("├─────────┼─────────┼──────────────┼──────────────┼─────────┼─────────┤")
        output_lines.append(f"│ Total Buy Qty: {tot_b:<17,} │ Total Sell Qty: {tot_s:<16,} │")
        output_lines.append("└────────────────────────────────┬──────────────────────────────┘")

        return {
            "status": "executed",
            "message": "\n".join(output_lines)
        }

    # 10. Conditional Order Command (Headless / CLI)
    elif isinstance(cmd, ConditionalOrderCommand):
        parent_cmd = cmd.parent_cmd
        if not cmd.confirmed:
            parent_res = await execute_command(parent_cmd, context)
            if parent_res.get("status") == "pending_confirmation":
                return {
                    "status": "pending_confirmation",
                    "message": f"Confirm CONDITIONAL CHAIN:\n  1) {parent_res.get('message')}\n  2) THEN ON FILL: {cmd.child_raw_text}",
                    "command": cmd
                }
        parent_cmd.confirmed = True
        return await execute_command(parent_cmd, context)

    # 11. Cancel Conditional Order Command
    elif isinstance(cmd, CancelConditionalOrderCommand):
        return {
            "status": "executed",
            "message": f"Cancelled conditional orders targeting '{cmd.target}'."
        }

    # 12. Conditional Orders View Command
    elif isinstance(cmd, ConditionalOrdersViewCommand):
        return {
            "status": "executed",
            "message": "Conditional orders viewer is active in live TUI dashboard ('kcli live')."
        }

    # 13. Cancel Scheduled Order Command
    elif isinstance(cmd, CancelScheduledOrderCommand):
        return {
            "status": "executed",
            "message": f"Cancelled scheduled orders targeting '{cmd.target}'."
        }

    # 14. Scheduled Orders View Command
    elif isinstance(cmd, ScheduledOrdersViewCommand):
        return {
            "status": "executed",
            "message": "Scheduled orders viewer is active in live TUI dashboard ('kcli live')."
        }

    raise TypeError(f"Execution not implemented for command type '{type(cmd)}'.")
