from dataclasses import dataclass
from typing import Optional, Union, Any

@dataclass
class AccountSelectCommand:
    account_name: str

@dataclass
class PlaceOrderCommand:
    action: str  # "BUY" or "SELL"
    symbol_or_id: Optional[str]  # e.g. "NIFTY2670722200PE", "3", or None (if using active selection)
    quantity: Optional[str]  # Quantity string to preserve lot syntax (e.g. "2L", "50") or None
    price: Optional[float] = None
    product: str = "NRML"
    confirmed: bool = False

@dataclass
class ExitCommand:
    target: str  # "all", symbol name, index ID, or "selected"
    price: Optional[float] = None
    confirmed: bool = False

@dataclass
class StatusCommand:
    pass

@dataclass
class PositionsCommand:
    pass

@dataclass
class OrdersCommand:
    pass

@dataclass
class CancelOrderCommand:
    target: str  # order ID, symbol name, or "all"
    confirmed: bool = False

@dataclass
class ModifyOrderCommand:
    order_id: str
    quantity: Optional[str]  # Quantity string preserving lot syntax; None = price-only (keep existing qty)
    price: float
    confirmed: bool = False

@dataclass
class MarketDepthCommand:
    symbol: Optional[str] = None

@dataclass
class ConditionalOrderCommand:
    parent_cmd: Union[PlaceOrderCommand, ExitCommand]
    child_cmd: Union[PlaceOrderCommand, ExitCommand]
    child_raw_text: str
    confirmed: bool = False

@dataclass
class CancelConditionalOrderCommand:
    target: str  # "all", ID string, etc.
    confirmed: bool = False

@dataclass
class ConditionalOrdersViewCommand:
    pass



def _is_qty_token(s: str) -> bool:
    """Return True if s looks like a quantity: digits or lot syntax (e.g. 2L)."""
    return s.isdigit() or (s.upper().endswith("L") and s[:-1].isdigit())


def parse_single_command(raw_text: str) -> Any:
    """Parse a single command token string without && chaining."""
    raw_clean = raw_text.strip()
    if not raw_clean:
        return None

    # Check for conditional chaining: "THEN", "->", or space-separated secondary action keyword (e.g. buy ... sell ...)
    import re
    then_match = re.search(r"\s+(?:THEN|then|->)\s+", raw_clean)
    if not then_match:
        # Check if command begins with buy/sell/exit/b/s and contains a second action keyword
        first_word = raw_clean.split()[0].lower() if raw_clean.split() else ""
        if first_word in ("buy", "sell", "b", "s", "exit"):
            # Look for secondary action keyword (sell, buy, exit, s, b) with whitespace around it
            sec_match = re.search(r"\s+(?:sell|buy|exit|s|b)\s+", raw_clean, flags=re.IGNORECASE)
            if sec_match:
                then_match = sec_match

    if then_match:
        idx_start = then_match.start()
        idx_end = then_match.end()
        # If match was a keyword like 'sell', include that keyword in child_raw
        matched_str = raw_clean[idx_start:idx_end].strip()
        if matched_str.lower() in ("sell", "buy", "exit", "s", "b"):
            parent_raw = raw_clean[:idx_start].strip()
            child_raw = raw_clean[idx_start:].strip()
        else:
            parent_raw = raw_clean[:idx_start].strip()
            child_raw = raw_clean[idx_end:].strip()

        if not parent_raw:
            raise ValueError("Conditional order missing primary/parent order.")
        if not child_raw:
            raise ValueError("Conditional order missing secondary/child order.")

        parent_cmd = parse_single_command(parent_raw)
        if not isinstance(parent_cmd, (PlaceOrderCommand, ExitCommand)):
            raise ValueError(f"Primary order in conditional chain must be a BUY, SELL, or EXIT command (got '{parent_raw}').")

        # Child command parsing: could be a full command or partial command (e.g. 'sell 180' or 'exit 180')
        try:
            child_cmd = parse_single_command(child_raw)
        except Exception:
            # Try interpreting child_raw as shorthand e.g. "sell 180" or "exit 180"
            parts_child = child_raw.split()
            if parts_child and parts_child[0].lower() in ("sell", "buy", "exit", "s", "b"):
                act_raw = parts_child[0].upper()
                act = "BUY" if act_raw in ("BUY", "B") else ("SELL" if act_raw in ("SELL", "S") else "EXIT")
                if act == "EXIT":
                    price_val = float(parts_child[1]) if len(parts_child) >= 2 else None
                    child_cmd = ExitCommand(target="selected", price=price_val)
                else:
                    price_val = float(parts_child[1]) if len(parts_child) >= 2 else None
                    child_cmd = PlaceOrderCommand(action=act, symbol_or_id=None, quantity=None, price=price_val)
            else:
                raise

        if not isinstance(child_cmd, (PlaceOrderCommand, ExitCommand)):
            raise ValueError(f"Secondary order in conditional chain must be a BUY, SELL, or EXIT command (got '{child_raw}').")

        # Inherit symbol and quantity from parent if omitted in child
        if isinstance(child_cmd, PlaceOrderCommand) and isinstance(parent_cmd, PlaceOrderCommand):
            if child_cmd.symbol_or_id is None:
                child_cmd.symbol_or_id = parent_cmd.symbol_or_id
            if child_cmd.quantity is None:
                child_cmd.quantity = parent_cmd.quantity

        return ConditionalOrderCommand(
            parent_cmd=parent_cmd,
            child_cmd=child_cmd,
            child_raw_text=child_raw,
        )

    parts = [p.strip() for p in raw_clean.split() if p.strip()]
    if not parts:
        return None
    primary = parts[0].lower()

    # 1. Account select
    if primary in ("account", "acct", "a"):
        if len(parts) < 2:
            raise ValueError("Usage: account <name|all>")
        return AccountSelectCommand(account_name=parts[1])

    # 2. Place Order (Buy/Sell)
    elif primary in ("buy", "sell", "b", "s"):
        if len(parts) < 2:
            raise ValueError(f"Usage: {primary} [symbol|id] [qty] [price] [product]")
        
        act = "BUY" if primary in ("buy", "b") else "SELL"
        arg1 = parts[1]
        symbol_or_id = None
        qty = None
        price = None
        product = "NRML"

        # Case A: buy <qty> ... (symbol omitted, meaning use current selection)
        if _is_qty_token(arg1):
            qty = arg1
            if len(parts) >= 3:
                try:
                    price = float(parts[2])
                except ValueError:
                    product = parts[2]
            if len(parts) >= 4 and price is not None:
                product = parts[3]
        # Case B: buy <price> (only price given, e.g. "sell 180.0")
        elif len(parts) == 2:
            try:
                price = float(arg1)
                qty = None
            except ValueError:
                symbol_or_id = arg1
                qty = None
        # Case C: buy <symbol_or_id> ...
        else:
            symbol_or_id = arg1
            if len(parts) == 3:
                # Could be: buy <symbol> <qty> OR buy <symbol> <price>
                if _is_qty_token(parts[2]):
                    qty = parts[2]
                else:
                    try:
                        price = float(parts[2])
                        qty = None
                    except ValueError:
                        product = parts[2]
            else:
                if len(parts) >= 3:
                    if _is_qty_token(parts[2]):
                        qty = parts[2]
                    else:
                        try:
                            price = float(parts[2])
                            qty = None
                        except ValueError:
                            product = parts[2]
                if len(parts) >= 4 and qty is not None:
                    try:
                        price = float(parts[3])
                    except ValueError:
                        product = parts[3]
                if len(parts) >= 5 and price is not None:
                    product = parts[4]

        return PlaceOrderCommand(
            action=act,
            symbol_or_id=symbol_or_id,
            quantity=qty,
            price=price,
            product=product
        )

    # 3. Exit
    elif primary == "exit":
        if len(parts) < 2:
            target = "selected"
            price = None
        elif len(parts) == 2:
            try:
                price = float(parts[1])
                target = "selected"
            except ValueError:
                target = parts[1]
                price = None
        else:
            target = parts[1]
            try:
                price = float(parts[2])
            except ValueError:
                raise ValueError("Exit price must be a valid number.")

        return ExitCommand(target=target, price=price)

    # 4. Status
    elif primary in ("status", "st", "health"):
        return StatusCommand()

    # 5. Positions / Pos
    elif primary in ("positions", "pos"):
        return PositionsCommand()

    # 6. Orders / Ord
    elif primary in ("orders", "ord"):
        return OrdersCommand()

    # 7. Cancel / Cancel Conditional
    elif primary in ("cancel", "c"):
        if len(parts) < 2:
            raise ValueError("Usage: cancel <order_id|symbol|all>  OR  cancel co <id|all>")
        if parts[1].lower() in ("co", "cond", "conditional", "conditionals"):
            target_id = parts[2] if len(parts) >= 3 else "all"
            return CancelConditionalOrderCommand(target=target_id)
        return CancelOrderCommand(target=parts[1])

    # 8. Conditional Orders View
    elif primary in ("cond", "co", "conditional", "conditionals"):
        return ConditionalOrdersViewCommand()

    # 9. Modify
    elif primary in ("modify", "m", "order"):
        if len(parts) == 3:
            try:
                price_val = float(parts[2])
            except ValueError:
                raise ValueError("Modify price must be a valid number.")
            return ModifyOrderCommand(
                order_id=parts[1],
                quantity=None,
                price=price_val
            )
        elif len(parts) >= 4:
            try:
                price_val = float(parts[3])
            except ValueError:
                raise ValueError("Modify price must be a valid number.")
            return ModifyOrderCommand(
                order_id=parts[1],
                quantity=parts[2],
                price=price_val
            )
        else:
            raise ValueError("Usage: order <id_or_sym> <price>  OR  order <id_or_sym> <qty> <price>")

    # 10. Market Depth
    elif primary in ("md", "depth", "marketdepth"):
        sym = parts[1] if len(parts) >= 2 else None
        return MarketDepthCommand(symbol=sym)

    else:
        raise ValueError(f"Unknown command: '{primary}'")


def parse_command_line(raw_text: str) -> list[Any]:
    """Parse raw text string (potentially chained with &&) into command dataclasses."""
    commands = []
    sub_commands = [c.strip() for c in raw_text.split("&&") if c.strip()]

    for sub in sub_commands:
        cmd_obj = parse_single_command(sub)
        if cmd_obj is not None:
            commands.append(cmd_obj)

    return commands
