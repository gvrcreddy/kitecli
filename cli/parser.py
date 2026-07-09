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


def _is_qty_token(s: str) -> bool:
    """Return True if s looks like a quantity: digits or lot syntax (e.g. 2L)."""
    return s.isdigit() or (s.upper().endswith("L") and s[:-1].isdigit())


def parse_command_line(raw_text: str) -> list[Any]:
    """Parse raw text string (potentially chained with &&) into command dataclasses."""
    commands = []
    sub_commands = [c.strip() for c in raw_text.split("&&") if c.strip()]

    for sub in sub_commands:
        parts = [p.strip() for p in sub.split() if p.strip()]
        if not parts:
            continue
        primary = parts[0].lower()

        # 1. Account select
        if primary in ("account", "acct", "a"):
            if len(parts) < 2:
                raise ValueError("Usage: account <name|all>")
            commands.append(AccountSelectCommand(account_name=parts[1]))

        # 2. Buy / Sell
        elif primary in ("buy", "sell"):
            if len(parts) < 2:
                raise ValueError(f"Usage: {primary} [symbol|id] <qty> [price] [product]")
            
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
            # Case B: buy <symbol_or_id> <qty> ...
            else:
                symbol_or_id = arg1
                if len(parts) < 3:
                    qty = None
                else:
                    qty = parts[2]
                if len(parts) >= 4 and qty is not None:
                    try:
                        price = float(parts[3])
                    except ValueError:
                        product = parts[3]
                if len(parts) >= 5 and price is not None:
                    product = parts[4]

            commands.append(PlaceOrderCommand(
                action=primary.upper(),
                symbol_or_id=symbol_or_id,
                quantity=qty,
                price=price,
                product=product
            ))

        # 3. Exit
        elif primary == "exit":
            if len(parts) < 2:
                target = "selected"
            else:
                target = parts[1]

            price = None
            if len(parts) >= 3:
                try:
                    price = float(parts[2])
                except ValueError:
                    raise ValueError("Exit price must be a valid number.")

            commands.append(ExitCommand(target=target, price=price))

        # 4. Status
        elif primary == "status":
            commands.append(StatusCommand())

        # 5. Positions / Pos
        elif primary in ("positions", "pos"):
            commands.append(PositionsCommand())

        # 6. Orders / Ord
        elif primary in ("orders", "ord"):
            commands.append(OrdersCommand())

        # 7. Cancel
        elif primary in ("cancel", "c"):
            if len(parts) < 2:
                raise ValueError("Usage: cancel <order_id|symbol|all>")
            commands.append(CancelOrderCommand(target=parts[1]))

        # 8. Modify
        # Supports two forms:
        #   order <id_or_sym> <price>           → price-only (qty=None, keeps each order's own qty)
        #   order <id_or_sym> <qty> <price>     → full modify (qty + price)
        elif primary in ("modify", "m", "order"):
            if len(parts) == 3:
                # Price-only form: order <id_or_sym> <price>
                try:
                    price_val = float(parts[2])
                except ValueError:
                    raise ValueError("Modify price must be a valid number.")
                commands.append(ModifyOrderCommand(
                    order_id=parts[1],
                    quantity=None,
                    price=price_val
                ))
            elif len(parts) >= 4:
                # Full form: order <id_or_sym> <qty> <price>
                try:
                    price_val = float(parts[3])
                except ValueError:
                    raise ValueError("Modify price must be a valid number.")
                commands.append(ModifyOrderCommand(
                    order_id=parts[1],
                    quantity=parts[2],
                    price=price_val
                ))
            else:
                raise ValueError("Usage: order <id_or_sym> <price>  OR  order <id_or_sym> <qty> <price>")

        else:
            raise ValueError(f"Unknown command: '{primary}'")

    return commands
