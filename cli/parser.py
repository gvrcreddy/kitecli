from dataclasses import dataclass
from datetime import datetime, date, time as dtime, timedelta
import re
from typing import Optional, Union, Any
from lark import Lark, Transformer, LarkError

@dataclass
class AccountSelectCommand:
    account_name: str

@dataclass
class OrderSelectCommand:
    target: str  # order ID suffix, exact ID, or "none" / "clear"

@dataclass
class PositionSelectCommand:
    target: str  # position ID, symbol, or "none" / "clear"

@dataclass
class PlaceOrderCommand:
    action: str  # "BUY" or "SELL"
    symbol_or_id: Optional[str]  # e.g. "NIFTY2670722200PE", "3", or None (if using active selection)
    quantity: Optional[str]  # Quantity string to preserve lot syntax (e.g. "2L", "50") or None
    price: Optional[float] = None
    product: str = "NRML"
    trigger_time: Optional[datetime] = None
    confirmed: bool = False

@dataclass
class ExitCommand:
    target: str  # "all", symbol name, index ID, or "selected"
    price: Optional[float] = None
    trigger_time: Optional[datetime] = None
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
class HoldingsCommand:
    deep: bool = False
    account: Optional[str] = None

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

@dataclass
class CancelScheduledOrderCommand:
    target: str  # "all", ID string, etc.
    confirmed: bool = False

@dataclass
class ScheduledOrdersViewCommand:
    pass


def _parse_date_time(date_str: Optional[str], time_str: str) -> datetime:
    """Parse time and date strings into a concrete datetime object."""
    time_str = str(time_str).strip()
    is_pm = "pm" in time_str.lower()
    is_am = "am" in time_str.lower()
    clean_time = re.sub(r"[a-zA-Z\s]", "", time_str)
    t_parts = clean_time.split(":")

    hr = int(t_parts[0])
    minute = int(t_parts[1]) if len(t_parts) > 1 else 0
    sec = int(t_parts[2]) if len(t_parts) > 2 else 0

    if is_pm and hr < 12:
        hr += 12
    elif is_am and hr == 12:
        hr = 0

    if not (0 <= hr <= 23 and 0 <= minute <= 59 and 0 <= sec <= 59):
        raise ValueError(f"Invalid time format: '{time_str}'")

    if date_str:
        date_str = str(date_str).strip()
        if "-" in date_str:
            d_parts = date_str.split("-")
            if len(d_parts[0]) == 4:  # YYYY-MM-DD
                target_date = date(int(d_parts[0]), int(d_parts[1]), int(d_parts[2]))
            else:  # DD-MM-YYYY
                target_date = date(int(d_parts[2]), int(d_parts[1]), int(d_parts[0]))
        else:
            raise ValueError(f"Invalid date format: '{date_str}'. Expected YYYY-MM-DD or DD-MM-YYYY.")
    else:
        target_date = datetime.now().date()

    target_dt = datetime.combine(target_date, dtime(hr, minute, sec))

    # If date was omitted, check if time has already passed today by > 60s
    if not date_str:
        now = datetime.now()
        if target_dt < now - timedelta(seconds=60):
            raise ValueError(
                f"Specified time '{time_str}' has already passed today ({now.strftime('%H:%M:%S')}). "
                f"Please specify a future date, e.g. 'at {time_str} {(now + timedelta(days=1)).strftime('%Y-%m-%d')}'"
            )

    return target_dt


def _extract_time_clause(raw_text: str) -> tuple[str, Optional[datetime]]:
    """Extract 'at <time> [<date>]' or 'on <date> at <time>' from command text.

    Returns:
        (remaining_command_text, parsed_datetime_or_None)
    """
    m_on = re.search(
        r"\s+on\s+(\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4})\s+(?:at\s+)?(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)",
        raw_text,
        re.IGNORECASE
    )
    if m_on:
        date_str, time_str = m_on.group(1), m_on.group(2)
        dt = _parse_date_time(date_str, time_str)
        cleaned = raw_text[:m_on.start()] + raw_text[m_on.end():]
        return cleaned.strip(), dt

    m_at = re.search(
        r"\s+at\s+(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)(?:\s+(\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4}))?",
        raw_text,
        re.IGNORECASE
    )
    if m_at:
        time_str, date_str = m_at.group(1), m_at.group(2)
        dt = _parse_date_time(date_str, time_str)
        cleaned = raw_text[:m_at.start()] + raw_text[m_at.end():]
        return cleaned.strip(), dt

    return raw_text, None


def _is_qty_token(s: str) -> bool:
    """Return True if s looks like a quantity: digits or lot syntax (e.g. 2L)."""
    return s.isdigit() or (s.upper().endswith("L") and s[:-1].isdigit())


LARK_GRAMMAR = r"""
?start: single_cmd

?single_cmd: select_cmd
           | cancel_cmd
           | modify_cmd
           | exit_cmd
           | status_cmd
           | positions_cmd
           | orders_cmd
           | holdings_cmd
           | cond_view_cmd
           | sched_view_cmd
           | md_cmd
           | place_order

// --- SELECT ---
select_cmd.5: INT                                                   -> select_pos_shorthand
            | SELECT_VERB (DESELECT_KW | "none"i)                   -> select_none
            | DESELECT_KW                                           -> select_none
            | "s"i "none"i                                          -> select_none
            | SELECT_VERB (ORDER_KW | "o"i) (INT | SYMBOL | "none"i) -> select_order
            | "s"i (ORDER_KW | "o"i) (INT | SYMBOL | "none"i)       -> select_order
            | SELECT_VERB? (ACCT_KW | "a"i) ACCT_NAME               -> select_acct
            | "s"i (ACCT_KW | "a"i) ACCT_NAME                       -> select_acct
            | SELECT_VERB (INT | SYMBOL)                            -> select_pos

// --- PLACE ORDER ---
place_order: ACTION INT qty price_val [PRODUCT] [time_clause]       -> place_order_pos_qty_price
           | ACTION SYMBOL qty price_val [PRODUCT] [time_clause]    -> place_order_sym_qty_price
           | ACTION SYMBOL FLOAT_NUM [PRODUCT] [time_clause]        -> place_order_sym_price
           | ACTION SYMBOL qty [PRODUCT] [time_clause]              -> place_order_sym_qty
           | ACTION qty price_val [PRODUCT] [time_clause]           -> place_order_qty_price
           | ACTION qty [PRODUCT] [time_clause]                     -> place_order_qty_only
           | ACTION price_val [PRODUCT] [time_clause]               -> place_order_price_only

ACTION.2: "buy"i | "b"i | "sell"i | "s"i

// --- EXIT ---
exit_cmd: EXIT [time_clause]                           -> exit_selected_market
        | EXIT FLOAT_NUM [time_clause]                 -> exit_selected_limit
        | EXIT ALL [price_val] [time_clause]           -> exit_all
        | EXIT INT [price_val] [time_clause]           -> exit_pos_id
        | EXIT SYMBOL [price_val] [time_clause]        -> exit_symbol

EXIT.4: "exit"i
ALL.4: "all"i

// --- MODIFY ---
modify_cmd: MODIFY_VERB (SYMBOL | INT) qty price_val -> modify_id_qty_price
          | MODIFY_VERB (SYMBOL | INT) price_val     -> modify_id_price
          | MODIFY_VERB price_val                    -> modify_selected_price

MODIFY_VERB.4: "modify"i | "m"i | "order"i

SELECT_VERB.4: "select"i | "sel"i
DESELECT_KW.4: "deselect"i | "clear"i
ORDER_KW.4: "order"i | "ord"i
ACCT_KW.4: "account"i | "acct"i

// --- CANCEL ---
cancel_cmd: CANCEL_VERB COND_KW (INT | SYMBOL | ALL)  -> cancel_cond
          | CANCEL_VERB SCHED_KW (INT | SYMBOL | ALL) -> cancel_sched
          | CANCEL_VERB (INT | SYMBOL | ALL)          -> cancel_order_target
          | CANCEL_VERB                               -> cancel_order_selected

CANCEL_VERB.4: "cancel"i | "c"i
COND_KW.5: "co"i | "cond"i | "conditional"i | "conditionals"i
SCHED_KW.5: "sched"i | "scheduled"i | "schedule"i | "so"i
MD_KW.4: "md"i | "depth"i | "marketdepth"i

// --- VIEWS ---
status_cmd: ("status"i | "st"i | "health"i) -> status_view
positions_cmd: ("positions"i | "pos"i)      -> positions_view
orders_cmd: ("orders"i | "ord"i)            -> orders_view
cond_view_cmd.6: COND_KW                    -> cond_view
sched_view_cmd.6: SCHED_KW                  -> sched_view
md_cmd.5: MD_KW [SYMBOL]                    -> md_view

holdings_cmd: HOLD_KW [HOLD_FLAG] [ACCT_NAME] -> holdings_view
            | HOLD_KW ACCT_NAME HOLD_FLAG      -> holdings_view_acct_first

HOLD_KW.4: "holdings"i | "holding"i | "portfolio"i | "port"i | "hld"i
HOLD_FLAG.4: "deep"i | "detail"i | "details"i | "summary"i | "sum"i

// --- TIME CLAUSE ---
time_clause: "at"i TIME [DATE] -> time_at
           | "on"i DATE ["at"i] TIME -> time_on

TIME: /\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?/
DATE: /\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4}/

// --- TOKENS ---
?qty: LOT_QTY | INT
LOT_QTY.3: /\d+[Ll]/
?price_val: FLOAT_NUM | INT
FLOAT_NUM.2: /\d+\.\d+/
PRODUCT.3: "NRML"i | "MIS"i | "CNC"i
SYMBOL.1: /[A-Za-z][A-Za-z0-9_-]*/
ACCT_NAME.2: /[A-Za-z0-9_-]+/

%import common.INT
%import common.WS
%ignore WS
"""

class KiteCommandTransformer(Transformer):
    def _extract_time(self, items):
        for item in items:
            if isinstance(item, datetime):
                return item
        return None

    def time_at(self, items):
        time_str = str(items[0])
        date_str = str(items[1]) if len(items) > 1 and items[1] is not None else None
        return _parse_date_time(date_str, time_str)

    def time_on(self, items):
        date_str = str(items[0])
        time_str = str(items[1])
        return _parse_date_time(date_str, time_str)

    def _extract_product(self, items):
        for item in items:
            if isinstance(item, str) and item.upper() in ("NRML", "MIS", "CNC"):
                return item.upper()
        return "NRML"

    def place_order_pos_qty_price(self, items):
        act = "BUY" if str(items[0]).upper() in ("BUY", "B") else "SELL"
        pos_id = str(items[1])
        qty = str(items[2])
        price = float(items[3])
        product = self._extract_product(items[4:])
        trig = self._extract_time(items[4:])
        return PlaceOrderCommand(action=act, symbol_or_id=pos_id, quantity=qty, price=price, product=product, trigger_time=trig)

    def place_order_sym_qty_price(self, items):
        act = "BUY" if str(items[0]).upper() in ("BUY", "B") else "SELL"
        sym = str(items[1]).upper()
        qty = str(items[2])
        price = float(items[3])
        product = self._extract_product(items[4:])
        trig = self._extract_time(items[4:])
        return PlaceOrderCommand(action=act, symbol_or_id=sym, quantity=qty, price=price, product=product, trigger_time=trig)

    def place_order_sym_price(self, items):
        act = "BUY" if str(items[0]).upper() in ("BUY", "B") else "SELL"
        sym = str(items[1]).upper()
        price = float(items[2])
        product = self._extract_product(items[3:])
        trig = self._extract_time(items[3:])
        return PlaceOrderCommand(action=act, symbol_or_id=sym, quantity=None, price=price, product=product, trigger_time=trig)

    def place_order_sym_qty(self, items):
        act = "BUY" if str(items[0]).upper() in ("BUY", "B") else "SELL"
        sym = str(items[1]).upper()
        qty = str(items[2])
        product = self._extract_product(items[3:])
        trig = self._extract_time(items[3:])
        return PlaceOrderCommand(action=act, symbol_or_id=sym, quantity=qty, price=None, product=product, trigger_time=trig)

    def place_order_qty_price(self, items):
        act = "BUY" if str(items[0]).upper() in ("BUY", "B") else "SELL"
        qty = str(items[1])
        price = float(items[2])
        product = self._extract_product(items[3:])
        trig = self._extract_time(items[3:])
        return PlaceOrderCommand(action=act, symbol_or_id=None, quantity=qty, price=price, product=product, trigger_time=trig)

    def place_order_qty_only(self, items):
        act = "BUY" if str(items[0]).upper() in ("BUY", "B") else "SELL"
        qty = str(items[1])
        product = self._extract_product(items[2:])
        trig = self._extract_time(items[2:])
        return PlaceOrderCommand(action=act, symbol_or_id=None, quantity=qty, price=None, product=product, trigger_time=trig)

    def place_order_price_only(self, items):
        act = "BUY" if str(items[0]).upper() in ("BUY", "B") else "SELL"
        price = float(items[1])
        product = self._extract_product(items[2:])
        trig = self._extract_time(items[2:])
        return PlaceOrderCommand(action=act, symbol_or_id=None, quantity=None, price=price, product=product, trigger_time=trig)

    def exit_selected_market(self, items):
        trig = self._extract_time(items)
        return ExitCommand(target="selected", price=None, trigger_time=trig)

    def exit_selected_limit(self, items):
        price = float(items[1])
        trig = self._extract_time(items[2:])
        return ExitCommand(target="selected", price=price, trigger_time=trig)

    def exit_all(self, items):
        price = float(items[2]) if len(items) > 2 and items[2] is not None and not isinstance(items[2], datetime) else None
        trig = self._extract_time(items)
        return ExitCommand(target="all", price=price, trigger_time=trig)

    def exit_pos_id(self, items):
        pos_id = str(items[1])
        price = float(items[2]) if len(items) > 2 and items[2] is not None and not isinstance(items[2], datetime) else None
        trig = self._extract_time(items)
        return ExitCommand(target=pos_id, price=price, trigger_time=trig)

    def exit_symbol(self, items):
        sym = str(items[1]).upper()
        price = float(items[2]) if len(items) > 2 and items[2] is not None and not isinstance(items[2], datetime) else None
        trig = self._extract_time(items)
        return ExitCommand(target=sym, price=price, trigger_time=trig)

    def modify_id_qty_price(self, items):
        target = str(items[1])
        qty = str(items[2])
        price = float(items[3])
        return ModifyOrderCommand(order_id=target, quantity=qty, price=price)

    def modify_id_price(self, items):
        target = str(items[1])
        price = float(items[2])
        return ModifyOrderCommand(order_id=target, quantity=None, price=price)

    def modify_selected_price(self, items):
        price = float(items[1])
        return ModifyOrderCommand(order_id="selected", quantity=None, price=price)

    def select_pos_shorthand(self, items):
        return PositionSelectCommand(target=str(items[0]))

    def select_pos(self, items):
        return PositionSelectCommand(target=str(items[1]))

    def select_none(self, items):
        return PositionSelectCommand(target="none")

    def select_order(self, items):
        target = str(items[-1])
        return OrderSelectCommand(target=target)

    def select_acct(self, items):
        acct = str(items[-1])
        return AccountSelectCommand(account_name=acct)

    def cancel_cond(self, items):
        target = str(items[-1])
        return CancelConditionalOrderCommand(target=target)

    def cancel_sched(self, items):
        target = str(items[-1])
        return CancelScheduledOrderCommand(target=target)

    def cancel_order_target(self, items):
        target = str(items[-1])
        return CancelOrderCommand(target=target)

    def cancel_order_selected(self, items):
        return CancelOrderCommand(target="selected")

    def status_view(self, items):
        return StatusCommand()

    def positions_view(self, items):
        return PositionsCommand()

    def orders_view(self, items):
        return OrdersCommand()

    def cond_view(self, items):
        return ConditionalOrdersViewCommand()

    def sched_view(self, items):
        return ScheduledOrdersViewCommand()

    def md_view(self, items):
        sym = None
        for item in items:
            if item is not None and str(item).upper() not in ("MD", "DEPTH", "MARKETDEPTH"):
                sym = str(item)
        return MarketDepthCommand(symbol=sym)

    def holdings_view(self, items):
        deep = False
        target_acct = None
        for item in items:
            if item is None:
                continue
            s = str(item).lower()
            if s in ("holdings", "holding", "portfolio", "port", "hld"):
                continue
            if s in ("deep", "detail", "details", "all"):
                deep = True
            elif s in ("summary", "sum"):
                deep = False
            else:
                target_acct = str(item)
                deep = True
        return HoldingsCommand(deep=deep, account=target_acct)

    def holdings_view_acct_first(self, items):
        target_acct = str(items[1]) if len(items) > 1 and items[1] is not None else None
        deep = True
        if len(items) > 2 and items[2] is not None:
            s = str(items[2]).lower()
            if s in ("summary", "sum"):
                deep = False
        return HoldingsCommand(deep=deep, account=target_acct)


_LARK_PARSER = Lark(LARK_GRAMMAR, parser='earley', start='start')
_TRANSFORMER = KiteCommandTransformer()


def parse_single_command(raw_text: str) -> Any:
    """Parse a single command token string without && chaining."""
    raw_clean = raw_text.strip()
    if not raw_clean:
        return None

    # Check for conditional chaining: "THEN", "->", "=>", or space-separated secondary action keyword
    then_match = re.search(r"\s+(?:THEN|then|->|=>)\s+", raw_clean)
    if not then_match:
        first_word = raw_clean.split()[0].lower() if raw_clean.split() else ""
        if first_word in ("buy", "sell", "b", "s", "exit"):
            sec_match = re.search(r"\s+(?:sell|buy|exit|s|b)\s+", raw_clean, flags=re.IGNORECASE)
            if sec_match:
                then_match = sec_match

    if then_match:
        idx_start = then_match.start()
        idx_end = then_match.end()
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

        try:
            child_cmd = parse_single_command(child_raw)
        except Exception:
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

    try:
        tree = _LARK_PARSER.parse(raw_clean)
        return _TRANSFORMER.transform(tree)
    except LarkError as e:
        raise ValueError(f"Invalid command syntax: '{raw_clean}'") from e


def parse_command_line(raw_text: str) -> list[Any]:
    """Parse raw text string (potentially chained with &&) into command dataclasses."""
    commands = []
    sub_commands = [c.strip() for c in raw_text.split("&&") if c.strip()]
    for sub in sub_commands:
        cmd_obj = parse_single_command(sub)
        if cmd_obj is not None:
            commands.append(cmd_obj)
    return commands
