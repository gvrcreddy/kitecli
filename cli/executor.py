from typing import Optional, Union, Any, Tuple
from cli.api_client import KCLIClient
from cli.parser import (
    AccountSelectCommand, PlaceOrderCommand, ExitCommand,
    StatusCommand, PositionsCommand, OrdersCommand,
    CancelOrderCommand, ModifyOrderCommand
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
    api_keys = [context.selected_account_key] if context.selected_account_key != "ALL" else [a["api_key"] for a in context.client.accounts]
    
    # Narrow down api_keys if the symbol is found in active positions
    if symbol and context.selected_account_key == "ALL":
        matching_keys = [p["api_key"] for p in context.active_positions if p["tradingsymbol"] == symbol]
        if matching_keys:
            api_keys = matching_keys
            
    return symbol, api_keys


def resolve_account_by_name(accounts: list[dict], name: str) -> dict:
    """Helper to resolve an account dict by its name or api_key."""
    for acct in accounts:
        if acct.get("name") == name or acct.get("api_key") == name:
            return acct
    raise ValueError(f"Account '{name}' not found.")


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

    # 2. Place Order (Buy / Sell)
    elif isinstance(cmd, PlaceOrderCommand):
        symbol = cmd.symbol_or_id
        api_keys = [context.selected_account_key] if context.selected_account_key != "ALL" else [a["api_key"] for a in context.client.accounts]
        
        if symbol:
            if symbol.isdigit():
                symbol, api_keys = resolve_symbol_or_id(context, symbol)
            else:
                symbol = symbol.upper()
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

        exchange = "NFO" if len(symbol) > 6 else "NSE"
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
            api_keys = [context.selected_account_key] if context.selected_account_key != "ALL" else [a["api_key"] for a in context.client.accounts]
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
        target = cmd.target
        api_keys = [context.selected_account_key] if context.selected_account_key != "ALL" else [a["api_key"] for a in context.client.accounts]
        
        orders_to_cancel = []
        
        if target.isdigit() and len(target) > 5:
            orders_to_cancel.append({"order_id": target, "api_key": api_keys[0] if len(api_keys) == 1 else None})
        else:
            orders_res = context.client.get_orders(api_keys)
            for acct in orders_res.get("accounts", []):
                for o in acct.get("orders", []):
                    if o.get("status") in ("OPEN", "TRIGGER PENDING"):
                        match_symbol = target.upper() if target.lower() != "all" else None
                        if match_symbol is None or o.get("tradingsymbol").upper() == match_symbol:
                            orders_to_cancel.append({"order_id": o.get("order_id"), "api_key": acct.get("api_key")})

        if not orders_to_cancel:
            raise ValueError(f"No pending orders found matching target '{target}'.")

        message = f"Confirm CANCEL of {len(orders_to_cancel)} order(s)?"
        if not cmd.confirmed:
            return {
                "status": "pending_confirmation",
                "message": message,
                "command": cmd
            }

        output_lines = [f"Cancelling {len(orders_to_cancel)} order(s)..."]
        for o in orders_to_cancel:
            ak = o["api_key"] or api_keys[0]
            try:
                res = context.client.cancel_order(api_key=ak, order_id=o["order_id"])
                icon = "✅" if res.get("status") == "success" else "❌"
                output_lines.append(f"  {icon} Order {o['order_id']}: {res.get('message', 'Success')}")
            except Exception as e:
                output_lines.append(f"  ❌ Order {o['order_id']}: {str(e)}")
        
        return {
            "status": "executed",
            "message": "\n".join(output_lines)
        }

    # 8. Modify Command
    elif isinstance(cmd, ModifyOrderCommand):
        order_id = cmd.order_id
        resolved_api_key = None
        resolved_symbol = None
        
        orders_res = context.client.get_orders(None)
        for acct in orders_res.get("accounts", []):
            for o in acct.get("orders", []):
                if o.get("order_id") == order_id:
                    resolved_api_key = acct.get("api_key")
                    resolved_symbol = o.get("tradingsymbol")
                    break
        
        if not resolved_api_key:
            raise ValueError(f"Order ID '{order_id}' not found.")

        qty_val, qty_display = resolve_qty(context.client, resolved_symbol, cmd.quantity)

        message = f"Confirm MODIFY of order {order_id} to Qty: {qty_display}, Price: {cmd.price:.2f}?"
        if not cmd.confirmed:
            return {
                "status": "pending_confirmation",
                "message": message,
                "command": cmd
            }

        res = context.client.modify_order(
            api_key=resolved_api_key,
            order_id=order_id,
            quantity=qty_val,
            price=cmd.price
        )

        output_lines = [f"Modifying order {order_id} to Qty={qty_val}, Price={cmd.price:.2f}..."]
        icon = "✅" if res.get("status") == "success" else "❌"
        output_lines.append(f"  {icon} Result: {res.get('message', 'Success')}")
        
        return {
            "status": "executed",
            "message": "\n".join(output_lines)
        }

    raise TypeError(f"Execution not implemented for command type '{type(cmd)}'.")
