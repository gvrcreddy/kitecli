import asyncio
from datetime import datetime
import logging
from urllib.parse import urlparse

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import DynamicContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.history import InMemoryHistory, FileHistory
from collections import UserDict
from prompt_toolkit.filters import has_focus
from prompt_toolkit.data_structures import Point

from cli.api_client import KCLIClient, KCLIClientError
from cli.display import render_positions_to_string

logger = logging.getLogger(__name__)


class DragInterceptDict(UserDict):
    def __init__(self, session, original_defaultdict):
        super().__init__()
        self.session = session
        self.data = original_defaultdict

    def __getitem__(self, y):
        row = self.data[y]
        return DragInterceptRow(self.session, row, y)

class DragInterceptRow(UserDict):
    def __init__(self, session, original_defaultdict, y):
        super().__init__()
        self.session = session
        self.data = original_defaultdict
        self.y = y

    def __getitem__(self, x):
        if getattr(self.session, "dragging_vertical", False) or getattr(self.session, "dragging_horizontal", False):
            return lambda mouse_event: self.session.handle_global_drag(mouse_event, x, self.y)
        return self.data[x]


class ScrollableFormattedTextControl(FormattedTextControl):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.vertical_scroll_position = 0

    def create_content(self, width, height):
        ui_content = super().create_content(width, height)
        ui_content.cursor_position = Point(x=0, y=self.vertical_scroll_position)
        return ui_content


class ScrollableBufferControl(BufferControl):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.vertical_scroll_position = 0

    def create_content(self, width, height):
        ui_content = super().create_content(width, height)
        ui_content.cursor_position = Point(x=0, y=self.vertical_scroll_position)
        return ui_content


class ScrollableWindow(Window):
    """Window that honours get_vertical_scroll even when wrap_lines=True."""

    def _scroll_when_linewrapping(self, ui_content, width, height):
        if self.get_vertical_scroll:
            desired = self.get_vertical_scroll(self)
            line_count = ui_content.line_count or 1
            self.vertical_scroll = max(0, min(desired, line_count - 1))
            self.vertical_scroll_2 = 0
            return
        super()._scroll_when_linewrapping(ui_content, width, height)


class KCLILiveSession:
    """Manages the interactive live positions dashboard and trading command line."""

    def __init__(self, client: KCLIClient, accounts: list[dict], refresh_interval: int = 5) -> None:
        self.client = client
        self.accounts = accounts
        self.refresh_interval = refresh_interval
        self.running = True
        
        # In-memory store of active positions used to resolve partial symbols
        self.active_positions = []
        self.position_id_map = {}
        self.last_positions_response = None
        
        # Log message list (plain text for Buffer-based display)
        self.logs = ["Type 'help' to see commands. Scroll logs using Mouse Wheel."]
        
        self.selected_symbol = None
        self.selected_account_name = None
        self.selected_account_api_key = None
        self.pending_order = None

        # Info pane state
        # mode: "orders_pending" | "orders_executed" | "oc"
        self.info_mode: str = "orders_pending"
        self._last_oc_text: str = "Press F3 or run 'oc <UNDERLYING>' to fetch option chain."
        self._last_pending_text: str = "Fetching pending orders..."
        self._last_executed_text: str = "Fetching executed orders..."

        # Pane resize state (adjusted via Ctrl+arrows)
        self.left_width_pct: int = 50   # % of terminal width for left pane (20–80)
        self.log_height_lines: int = 10  # rows for the log sub-pane in the left half
        self.dragging_vertical = False
        self.dragging_horizontal = False
        
        # Style definition for UI elements
        self.style = Style.from_dict({
            "header": "bg:#005f87 #ffffff bold",
            "prompt_label": "fg:#00afaf bold",
            "input_text": "fg:#ffffff",
            "log_title": "fg:#ff8700 bold",
            "selected_row": "bg:ansiblue fg:ansiwhite bold",
            "info_header": "fg:#87ff87 bold",
            "divider": "bg:#2b2b2b fg:#5a5a5a",
            "divider.dragging": "bg:#0087af fg:#ffffff bold",
            "market_indices": "bg:#121212",
            # Quick action bar
            "quickaction": "bg:#1c1c1c fg:#888888",
            "quickaction.hint": "bg:#1c1c1c fg:#555555 italic",
            "btn.buy": "bg:#005f00 fg:#afffaf bold",
            "btn.sell": "bg:#5f0000 fg:#ffafaf bold",
            "btn.exit": "bg:#5f005f fg:#ffafff bold",
            "btn.modify": "bg:#00005f fg:#afafff bold",
            "btn.cancel": "bg:#3a3a3a fg:#d0d0d0 bold",
        })

    def _strip_rich_markup(self, text: str) -> str:
        """Strip Rich-style markup tags from text for plain display."""
        import re
        return re.sub(r"\[/?[^\[\]]*\]", "", text)

    def log_message(self, message: str) -> None:
        """Add a timestamped message to the logs and update logs pane."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        plain = self._strip_rich_markup(message)
        self.logs.append(f"[{timestamp}] {plain}")
        if len(self.logs) > 500:
            self.logs.pop(0)

        if hasattr(self, "logs_buffer"):
            full_text = "\n".join(self.logs)
            # Move cursor to end to auto-scroll to latest
            self.logs_buffer.set_document(
                Document(text=full_text, cursor_position=len(full_text)),
                bypass_readonly=True,
            )
            if hasattr(self, "_logs_control"):
                self._logs_control.vertical_scroll_position = 999999

        if hasattr(self, "app"):
            self.app.invalidate()

    async def _run_api_call(self, func, *args, **kwargs):
        """Helper to run blocking client operations in an executor to keep TUI responsive."""
        loop = asyncio.get_running_loop()
        from functools import partial
        fn = partial(func, *args, **kwargs)
        return await loop.run_in_executor(None, fn)

    def update_prompt_label(self) -> None:
        """Update prompt label based on selected symbol and account contexts."""
        if self.selected_account_name and self.selected_symbol:
            self.prompt_control.text = f" kcli [@{self.selected_account_name}:{self.selected_symbol}]> "
        elif self.selected_account_name:
            self.prompt_control.text = f" kcli [@{self.selected_account_name}]> "
        elif self.selected_symbol:
            self.prompt_control.text = f" kcli [{self.selected_symbol}]> "
        else:
            self.prompt_control.text = " kcli> "
        # Invalidate so the action bar re-renders with fresh fragments
        if hasattr(self, "app"):
            self.app.invalidate()


    def _build_action_bar_text(self):
        """Build the formatted fragment list for the quick-action bar.

        Always shows all 5 buttons.  When no symbol is selected the buttons
        appear dimmed and carry no handler — clicking them does nothing.
        When a symbol is selected each button inserts the matching command
        template into the input field and shifts focus there.
        """
        from prompt_toolkit.mouse_events import MouseEventType

        sym  = getattr(self, "selected_symbol", None)
        acct = getattr(self, "selected_account_name", None)

        # Find matching active position to determine default qty
        matched_pos = None
        if sym:
            for pos in getattr(self, "active_positions", []):
                if pos.get("tradingsymbol", "").upper() == sym.upper():
                    matched_pos = pos
                    break
        qty = abs(matched_pos.get("quantity", 1)) if matched_pos else 1

        def _fill(snippet):
            """Return a mouse handler that pre-fills the input with snippet."""
            def _handler(*args, **kwargs):
                self.log_message("DEBUG: Quickaction button _handler called!")
                if len(args) == 2:
                    mouse_event = args[1]
                elif len(args) == 1:
                    mouse_event = args[0]
                else:
                    self.log_message("DEBUG: Quickaction button: no mouse_event found in args")
                    return

                self.log_message(f"DEBUG: Quickaction mouse event type: {mouse_event.event_type}")
                if mouse_event.event_type not in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP):
                    return
                from prompt_toolkit.document import Document
                buf = self.input_field.buffer
                buf.set_document(
                    Document(text=snippet, cursor_position=len(snippet)),
                    bypass_readonly=False,
                )
                if hasattr(self, "app"):
                    self.app.layout.focus(self.input_field)
                    self.app.invalidate()
            return _handler

        # ── button definitions: (label, active_style, dim_style, snippet) ──
        buttons = [
            ("  BUY  ", "bg:#005f00 fg:#afffaf bold", "bg:#1a1a1a fg:#444444",
             f"buy {sym or '<symbol>'} {qty} " if sym else None),
            ("  SELL  ", "bg:#5f0000 fg:#ffafaf bold", "bg:#1a1a1a fg:#444444",
             f"sell {sym or '<symbol>'} {qty} " if sym else None),
            ("  EXIT  ", "bg:#5f005f fg:#ffafff bold", "bg:#1a1a1a fg:#444444",
             f"exit {sym}" if sym else None),
            ("  MODIFY  ", "bg:#00005f fg:#afafff bold", "bg:#1a1a1a fg:#444444",
             f"order {sym} sell {qty} " if sym else None),
            ("  CANCEL  ", "bg:#3a3a3a fg:#d0d0d0 bold", "bg:#1a1a1a fg:#444444",
             f"cancel {sym}" if sym else None),
        ]

        frags = []

        # Left label — show selected context or neutral hint
        if sym:
            ctx = sym + (f" @{acct}" if acct else "")
            frags.append(("bg:#262626 fg:#00afaf bold", f"  [{ctx}] "))
        else:
            frags.append(("bg:#1a1a1a fg:#555555", "  Actions: "))

        for label, active_style, dim_style, snippet in buttons:
            frags.append(("bg:#1a1a1a fg:#333333", " "))  # spacer
            if snippet is not None:
                frags.append((active_style, label, _fill(snippet)))
            else:
                frags.append((dim_style, label))

        frags.append(("bg:#1a1a1a fg:#333333", "  "))  # trailing pad
        return frags


    def _make_click_handler(self, idx: int):
        """Create a mouse handler for a specific position ID."""
        def handler(*args, **kwargs):
            from prompt_toolkit.mouse_events import MouseEventType
            # Compatible with handler(mouse_event) and handler(app, mouse_event)
            if len(args) == 2:
                mouse_event = args[1]
            elif len(args) == 1:
                mouse_event = args[0]
            else:
                return

            if mouse_event.event_type in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP):
                pos = self.position_id_map.get(idx)
                if pos:
                    if self.selected_symbol != pos.get("tradingsymbol") or self.selected_account_api_key != pos.get("api_key"):
                        self.selected_symbol = pos.get("tradingsymbol")
                        self.selected_account_api_key = pos.get("api_key")
                        self.selected_account_name = pos.get("account_name")
                        self.update_prompt_label()
                        self.log_message(f"Selected [bold]{self.selected_symbol}[/bold] (@{self.selected_account_name}) via click.")
                        if hasattr(self, "app"):
                            self.app.invalidate()
        return handler

    def _make_account_click_handler(self, acct: dict):
        """Create a mouse handler for a specific account."""
        def handler(*args, **kwargs):
            from prompt_toolkit.mouse_events import MouseEventType
            if len(args) == 2:
                mouse_event = args[1]
            elif len(args) == 1:
                mouse_event = args[0]
            else:
                return

            if mouse_event.event_type in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP):
                if self.selected_account_api_key != acct.get("api_key"):
                    self.selected_account_name = acct.get("name")
                    self.selected_account_api_key = acct.get("api_key")
                    self.update_prompt_label()
                    self.log_message(f"Selected account [bold]@{self.selected_account_name}[/bold] via click.")
                    if hasattr(self, "app"):
                        self.app.invalidate()
        return handler

    def _update_positions_display(self) -> None:
        """Render positions to a formatted string aligned to the left container width."""
        if not getattr(self, "last_positions_response", None):
            self.positions_control.text = "Fetching positions from server, please wait..."
            return

        # Calculate width of the positions container dynamically
        total_cols = 80
        if hasattr(self, "app") and self.app and self.app.output:
            total_cols = self.app.output.get_size().columns

        # left_width matches width=D(weight=lw) where lw = self.left_width_pct
        # the vertical divider takes 1 char.
        left_width = int((total_cols - 1) * (self.left_width_pct / 100.0))
        # Frame has borders (2 chars) and padding (2 chars) and a safety margin (2 chars)
        width = max(40, left_width - 6)

        # Memoize: only re-render when an input that affects output changes.
        # _build_body() runs this on every invalidate (mouse move/scroll), so
        # without this guard we'd re-parse ANSI and rebuild handlers constantly.
        cache_key = (
            getattr(self, "_positions_version", 0),
            width,
            self.selected_symbol,
            self.selected_account_api_key,
            self.selected_account_name,
        )
        if getattr(self, "_positions_cache_key", None) == cache_key:
            return
        self._positions_cache_key = cache_key

        rendered = render_positions_to_string(
            self.last_positions_response.get("accounts", []),
            width=width,
            show_indices=True
        )
        
        # Parse ANSI into formatted text and attach click handlers
        lines = rendered.split("\n")
        all_line_frags = []
        for line in lines:
            line_frags = list(ANSI(line).__pt_formatted_text__())
            plain_line = "".join(frag[1] for frag in line_frags)
            
            matched_idx = None
            for idx in self.position_id_map.keys():
                if f"[{idx}]" in plain_line:
                    matched_idx = idx
                    break

            matched_acct = None
            for acct in self.accounts:
                name = acct.get("name")
                if name and name in plain_line:
                    matched_acct = acct
                    break
                    
            if matched_idx is not None:
                selected_pos = self.position_id_map.get(matched_idx)
                is_selected = (
                    selected_pos
                    and self.selected_symbol
                    and selected_pos.get("tradingsymbol") == self.selected_symbol
                    and (self.selected_account_api_key is None or selected_pos.get("api_key") == self.selected_account_api_key)
                )
                click_handler = self._make_click_handler(matched_idx)
                
                updated_frags = []
                for frag in line_frags:
                    style = frag[0]
                    text = frag[1]
                    if is_selected:
                        style = "class:selected_row"
                    updated_frags.append((style, text, click_handler))
                line_frags = updated_frags

            if matched_acct is not None:
                acct_click_handler = self._make_account_click_handler(matched_acct)
                is_acct_selected = self.selected_account_api_key == matched_acct.get("api_key")
                updated_frags = []
                for frag in line_frags:
                    style = frag[0]
                    text = frag[1]
                    if is_acct_selected:
                        style = "class:selected_row"
                    updated_frags.append((style, text, acct_click_handler))
                line_frags = updated_frags
                
            all_line_frags.append(line_frags)
            
        # Reconstruct full list of formatted text fragments with newlines
        all_fragments = []
        for i, line_frags in enumerate(all_line_frags):
            all_fragments.extend(line_frags)
            if i < len(all_line_frags) - 1:
                all_fragments.append(("", "\n"))

        self.positions_control.text = all_fragments

        # Update title bar info
        now = datetime.now().strftime("%H:%M:%S")
        self.header_control.text = (
            f"🪁 KiteCLI Live │ Refresh: {self.refresh_interval}s │ "
            f"Last Update: {now} │ Accounts: {len(self.accounts)} │ "
            f"Ctrl+C: Quit │ Escape: Deselect"
        )

    async def _update_loop(self) -> None:
        """Coroutine to fetch positions and update the UI periodically."""
        while self.running:
            try:
                # Fetch positions in executor
                api_keys = [acct["api_key"] for acct in self.accounts]
                response = await self._run_api_call(self.client.get_positions, api_keys)

                # Check if any account failed with "Account not found"
                has_unregistered = False
                for acct in response.get("accounts", []):
                    if "Account not found" in acct.get("status", ""):
                        has_unregistered = True
                        break

                if has_unregistered:
                    self.log_message("[#ffaa00]Server lost account session. Re-initializing...[/#]")
                    await self._run_api_call(self.client.init_accounts, self.accounts)
                    # Fetch positions again immediately
                    response = await self._run_api_call(self.client.get_positions, api_keys)

                # Store response for resizing and dynamic rendering
                self.last_positions_response = response
                self._positions_version = getattr(self, "_positions_version", 0) + 1

                # Store active positions for auto-resolution
                self.active_positions = []
                self.position_id_map = {}
                pos_idx = 1
                for acct in response.get("accounts", []):
                    # Filter only non-zero quantity open positions
                    for pos in acct.get("positions", []):
                        if pos.get("quantity", 0) != 0:
                            pos["api_key"] = acct.get("api_key")
                            pos["account_name"] = acct.get("name")
                            self.active_positions.append(pos)
                            self.position_id_map[pos_idx] = pos
                            pos_idx += 1

                self._update_positions_display()

                # Fetch market indices
                try:
                    indices_resp = await self._run_api_call(self.client.get_market_indices)
                    if indices_resp.get("status") == "success":
                        nifty = indices_resp.get("nifty")
                        sensex = indices_resp.get("sensex")
                        vix = indices_resp.get("vix")

                        nifty_str = f"{nifty:,.2f}" if nifty else "N/A"
                        sensex_str = f"{sensex:,.2f}" if sensex else "N/A"
                        vix_str = f"{vix:,.2f}" if vix else "N/A"

                        self.market_indices_control.text = HTML(
                            f"  <ansicyan><b>NIFTY 50:</b></ansicyan> <style fg='#ffffff'>{nifty_str}</style>   "
                            f"│   <ansiyellow><b>SENSEX:</b></ansiyellow> <style fg='#ffffff'>{sensex_str}</style>   "
                            f"│   <ansired><b>INDIA VIX:</b></ansired> <style fg='#ffffff'>{vix_str}</style>"
                        )
                    else:
                        msg = indices_resp.get("message", "Unknown error")
                        self.market_indices_control.text = HTML(f"  <style fg='#ff5f5f'>Indices: {msg}</style>")
                except Exception as exc:
                    self.market_indices_control.text = HTML(f"  <style fg='#ff5f5f'>Indices Error: {exc}</style>")

                self.app.invalidate()
            except KCLIClientError as exc:
                self.log_message(f"[#ff0000]API Error:[/#] {exc}")
            except Exception as exc:
                self.log_message(f"[#ff0000]Unexpected error:[/#] {exc}")

            # Sleep in small increments to check running flag and exit quickly if needed
            for _ in range(int(self.refresh_interval * 10)):
                if not self.running:
                    break
                await asyncio.sleep(0.1)

    def resolve_symbol(self, input_sym: str) -> tuple[str | None, str | None, str | None]:
        """Resolve a symbol against current active positions.

        Accepts either:
          - A numeric position ID  (e.g. "1", "3")
          - An exact trading symbol (case-insensitive, spaces ignored)

        No fuzzy or pattern matching is performed. If the symbol doesn't
        match any active position it is returned as-is (upper-cased) so
        the server can validate it when placing the order.

        Returns:
            (resolved_symbol, api_key, error_message)
        """
        normalized_input = input_sym.replace(" ", "").upper()
        if not normalized_input:
            return None, None, "Empty symbol."

        # Numeric input → position ID lookup (only for small integers < 100 to avoid strike price collision)
        if normalized_input.isdigit():
            idx = int(normalized_input)
            if idx < 100:
                if hasattr(self, "position_id_map") and idx in self.position_id_map:
                    pos = self.position_id_map[idx]
                    return pos.get("tradingsymbol"), pos.get("api_key"), None
                else:
                    return None, None, f"Invalid position ID '{input_sym}'."

        # Exact match against active positions (case-insensitive, spaces stripped)
        matched_pos = None
        for pos in getattr(self, "active_positions", []):
            sym = pos.get("tradingsymbol", "")
            if sym.replace(" ", "").upper() == normalized_input:
                if matched_pos is None:
                    matched_pos = pos
                else:
                    # Same symbol in multiple accounts — don't pin api_key
                    return sym, None, None

        if matched_pos:
            return matched_pos.get("tradingsymbol"), matched_pos.get("api_key"), None

        # No match in active positions — pass through as-is (for new orders)
        return normalized_input, None, None

    def resolve_account(self, input_acc: str) -> tuple[dict | None, str | None]:
        """Resolve an account name, 1-based index, or API key.
        
        Returns:
            (resolved_account_dict, error_message)
        """
        normalized = input_acc.strip().lower()
        if not normalized:
            return None, "Empty account query."
            
        # 1. Check if index (1-based)
        if normalized.isdigit():
            idx = int(normalized) - 1
            if 0 <= idx < len(self.accounts):
                return self.accounts[idx], None
            return None, f"Invalid account index '{input_acc}'. Available: 1 to {len(self.accounts)}"

        # 2. Match name (exact case-insensitive)
        for acct in self.accounts:
            if acct.get("name", "").lower() == normalized:
                return acct, None
                
        # 3. Match api_key (exact)
        for acct in self.accounts:
            if acct.get("api_key", "").lower() == normalized:
                return acct, None

        # 4. Match prefix of name
        matches = [acct for acct in self.accounts if acct.get("name", "").lower().startswith(normalized)]
        if len(matches) == 1:
            return matches[0], None
        elif len(matches) > 1:
            names = [m.get("name") for m in matches]
            return None, f"Ambiguous account '{input_acc}'. Matches: {', '.join(names)}"

        return None, f"No account found matching '{input_acc}'"

    def _select_symbol_from_text(self, text: str) -> None:
        """Helper to validate and set the selected symbol from clicked/highlighted text."""
        if not text:
            return
        
        symbol = text.strip().upper()
        if not symbol:
            return
        if not symbol[0].isalpha():
            return
            
        excluded = {
            "CE", "PE", "LTP", "STRIKE", "SYMBOL", "LOT", "N/A", "WEEK", "OPTION",
            "CHAIN", "EXPIRY", "TIP", "TO", "BUY", "TYPE", "AVAILABLE", "EXPIRIES",
            "USE", "OR", "ORDERS", "PENDING", "EXECUTED", "ACCOUNTS", "ACCOUNT",
            "CONFIRM", "EXIT", "ALL", "POSITIONS", "REFRESH", "SECONDS", "CLEAR",
            "QUIT", "HELP", "STATUS", "LOGS", "AND", "THE", "FOR", "IN", "ON",
            "OF", "AT", "BY", "WITH", "IS", "ARE", "WAS", "WERE", "BE", "BEEN",
            "E.G.", "E.G", "I.E.", "I.E", "LIMIT", "MARKET", "CNC", "MIS",
            "NRML", "CO", "BO", "OPEN", "COMPLETE", "NO", "ERROR"
        }
        if symbol in excluded:
            return
            
        if not all(c.isalnum() or c in "-." for c in symbol):
            return
            
        resolved_sym, api_key, err = self.resolve_symbol(symbol)
        if resolved_sym:
            self.selected_symbol = resolved_sym
            if api_key:
                self.selected_account_api_key = api_key
                for acct in self.accounts:
                    if acct.get("api_key") == api_key:
                        self.selected_account_name = acct.get("name")
                        break
        else:
            self.selected_symbol = symbol
            
        self.update_prompt_label()
        self.log_message(f"Selected symbol [bold]{self.selected_symbol}[/bold] via click/selection.")
        if hasattr(self, "app"):
            self.app.invalidate()




    async def execute_order(self, symbol: str, transaction_type: str, qty_str: str, price_str: str = None, product: str = "NRML", api_keys: list[str] = []) -> None:
        """Place order across specified accounts in executor."""
        # 1. Validate quantity
        try:
            qty = int(qty_str)
            if qty <= 0:
                raise ValueError("Quantity must be positive.")
        except ValueError:
            self.log_message(f"[#ff0000]Error:[/#] Invalid quantity '{qty_str}'. Must be a positive integer.")
            return

        # 2. Parse price and order type
        price = None
        order_type = "MARKET"
        if price_str:
            try:
                price = float(price_str)
                if price > 0:
                    order_type = "LIMIT"
                else:
                    price = None
            except ValueError:
                self.log_message(f"[#ff5555]Warning:[/#] Price '{price_str}' not a float, placing as MARKET order.")

        # 3. Auto-detect exchange
        exchange = "NFO"
        if symbol.endswith("CE") or symbol.endswith("PE"):
            exchange = "NFO"
        elif any(symbol.endswith(exp) for exp in ("FUT", "CE", "PE")):
            exchange = "NFO"
        else:
            exchange = "NSE"

        # For NSE, NRML is not allowed. If product is still default NRML, auto-switch to CNC
        if exchange == "NSE" and product.upper() == "NRML":
            product = "CNC"

        accts_desc = f"account {self.selected_account_name}" if (api_keys and hasattr(self, "selected_account_name") and self.selected_account_name) else "all accounts"
        self.log_message(f"Placing {order_type} {transaction_type.upper()} order for {qty} {symbol} ({product.upper()}) at price {price or 'MARKET'} on {accts_desc}...")

        # 4. Place order
        try:
            response = await self._run_api_call(
                self.client.place_order,
                api_keys=api_keys,
                tradingsymbol=symbol,
                exchange=exchange,
                transaction_type=transaction_type.upper(),
                quantity=qty,
                order_type=order_type,
                price=price,
                product=product.upper(),
            )
            
            # Print individual results
            results = response.get("results", [])
            for res in results:
                name = res.get("name", "Unknown")
                status = res.get("status", "error")
                msg = res.get("message", "")
                ord_id = res.get("order_id")
                if status == "success":
                    self.log_message(f"[#00ff00]✓ {name}:[/#] Placed. ID: {ord_id}")
                else:
                    self.log_message(f"[#ff0000]✗ {name}:[/#] Failed — {msg}")

        except KCLIClientError as exc:
            self.log_message(f"[#ff0000]Order Execution Failed:[/#] {exc}")
        except Exception as exc:
            self.log_message(f"[#ff0000]Unexpected Error:[/#] {exc}")

    async def execute_exit(self, symbol: str = None, api_keys: list[str] = []) -> None:
        """Execute exit of positions across specified accounts in executor."""
        accts_desc = f"account {self.selected_account_name}" if (api_keys and hasattr(self, "selected_account_name") and self.selected_account_name) else "all accounts"
        if symbol:
            self.log_message(f"Exiting open positions for {symbol} across {accts_desc}...")
        else:
            self.log_message(f"Exiting ALL open positions across {accts_desc}...")

        try:
            # Place exit request on server
            response = await self._run_api_call(
                self.client.exit_positions,
                api_keys=api_keys,
                tradingsymbol=symbol,
            )

            # Print exit results
            results = response.get("results", [])
            for res in results:
                name = res.get("name", "Unknown")
                status = res.get("status", "error")
                msg = res.get("message", "")
                placed = res.get("orders_placed", [])
                
                if status == "success":
                    if placed:
                        symbols_exited = ", ".join(f"{item.get('tradingsymbol')} ({item.get('quantity')})" for item in placed)
                        self.log_message(f"[#00ff00]✓ {name}:[/#] Exited {symbols_exited}")
                    else:
                        self.log_message(f"[#87afaf]~ {name}:[/#] {msg}")
                else:
                    self.log_message(f"[#ff0000]✗ {name}:[/#] Failed — {msg}")

        except KCLIClientError as exc:
            self.log_message(f"[#ff0000]Exit Execution Failed:[/#] {exc}")
        except Exception as exc:
            self.log_message(f"[#ff0000]Unexpected Error:[/#] {exc}")

    def handle_input(self, buffer) -> None:
        """Process entered command line."""
        cmd = buffer.text.strip()
        if not cmd:
            return

        # Check if we are waiting for confirmation of a pending order
        if hasattr(self, "pending_order") and self.pending_order:
            ans = cmd.lower().strip()
            if ans in ("y", "yes"):
                self.log_message("[#00ff00]Order Confirmed.[/#]")
                p = self.pending_order
                if p["type"] == "exit":
                    asyncio.create_task(self.execute_exit(p["symbol"], p.get("api_keys", [])))
                else:
                    asyncio.create_task(
                        self.execute_order(
                            p["symbol"],
                            p["type"],
                            p["qty"],
                            p["price"],
                            p["product"],
                            api_keys=p.get("api_keys", []),
                        )
                    )
            else:
                self.log_message("[#ff5555]Order Cancelled.[/#]")
            
            # Reset confirmation state
            self.pending_order = None
            # Restore prompt
            self.update_prompt_label()
            return

        parts = cmd.split()
        primary_cmd = parts[0].lower()

        if primary_cmd == "quit" and len(parts) == 1:
            self.running = False
            self.app.exit()
            return

        if primary_cmd == "exit" and len(parts) == 1:
            if hasattr(self, "selected_symbol") and self.selected_symbol:
                target_keys = [self.selected_account_api_key] if self.selected_account_api_key else []
                asyncio.create_task(self.execute_exit(self.selected_symbol, target_keys))
                return
            else:
                self.running = False
                self.app.exit()
                return

        if primary_cmd == "clear":
            self.logs = []
            self.logs_control.text = ANSI("")
            return

        if primary_cmd == "help":
            self.log_message("[#00afaf]Available Commands:[/#]")
            self.log_message("  [bold]buy / sell [symbol|id] <qty> [price] [product][/bold]")
            self.log_message("    e.g. [bold]buy 50[/bold] (buys 50 qty of selected position)")
            self.log_message("    e.g. [bold]buy 1 50[/bold] (buys 50 qty of position ID 1)")
            self.log_message("    e.g. [bold]buy NIFTY25JUN24000CE 50[/bold] (exact symbol)")
            self.log_message("  [bold]oc <UNDERLYING> [week <N> | <YYYY-MM-DD>][/bold] - Show option chain (right pane)")
            self.log_message("    e.g. [bold]oc NIFTY[/bold]              — current week strikes")
            self.log_message("    e.g. [bold]oc NIFTY week 1[/bold]       — next week strikes")
            self.log_message("    e.g. [bold]oc BANKNIFTY 2024-06-27[/bold] — specific expiry")
            self.log_message("  [bold]select <id|none> / s <id|none>[/bold] - Select active position (or 'none' to clear)")
            self.log_message("  [bold]select account <name|index|none> / s a <name|index|none>[/bold]")
            self.log_message("  [bold]account <name|index|none> / acct <...>/ a <...>[/bold] - Select specific account context")
            self.log_message("  [bold]deselect[/bold] - Clear current active selection")
            self.log_message("  [bold]exit [symbol|id][/bold] - Exit position (current selection if symbol omitted)")
            self.log_message("  [bold]exit all[/bold] - Exit ALL open positions across all/selected accounts")
            self.log_message("  [bold]refresh <seconds>[/bold] - Change auto-refresh interval")
            self.log_message("  [bold]clear[/bold] - Clear logs screen")
            self.log_message("  [bold]quit / exit[/bold] - Close dashboard")
            self.log_message("[#00afaf]Right Pane (Info Panel):[/#]")
            self.log_message("  [bold]F1[/bold] — Pending Orders (per account, auto-refresh every 10s)")
            self.log_message("  [bold]F2[/bold] — Executed Orders (per account, today)")
            self.log_message("  [bold]F3[/bold] — Option Chain (after running 'oc' command)")
            self.log_message("[#00afaf]Navigation:[/#]")
            self.log_message("  [bold]Tab[/bold]        — Cycle focus: Input → Positions → Logs → Info Pane")
            self.log_message("  [bold]Mouse Drag[/bold] — Click & hold/drag pane borders to resize")
            self.log_message("  [bold]Ctrl+←→[/bold]   — Resize left/right split (5% per press)")
            self.log_message("  [bold]Ctrl+↑↓[/bold]   — Resize Status Logs height (2 rows per press)")
            self.log_message("  [bold]Escape[/bold]     — Clear/deselect the current selection")
            return

        if primary_cmd == "refresh":
            if len(parts) != 2:
                self.log_message("[#ff0000]Usage:[/#] refresh <seconds>")
                return
            try:
                seconds = int(parts[1])
                if seconds < 1:
                    raise ValueError()
                self.refresh_interval = seconds
                self.log_message(f"Refresh interval set to {seconds} seconds.")
            except ValueError:
                self.log_message("[#ff0000]Error:[/#] Refresh interval must be a positive integer.")
            return

        # Deselect Command
        if primary_cmd == "deselect":
            self.selected_symbol = None
            self.selected_account_name = None
            self.selected_account_api_key = None
            self.update_prompt_label()
            self.log_message("Selection cleared.")
            return

        # Select Position / Account Command
        if primary_cmd in ("select", "s"):
            if len(parts) < 2:
                self.log_message("[#ff0000]Usage:[/#] select <id|none> OR select account <name|index|none>")
                return
            
            raw_id = parts[1]
            
            # Check if selecting an account: select account <query> or s a <query>
            if raw_id.lower() in ("account", "acct", "a"):
                if len(parts) < 3:
                    self.log_message("[#ff0000]Usage:[/#] select account <name|index|none>")
                    return
                raw_acc = " ".join(parts[2:])
                if raw_acc.lower() in ("none", "clear", "null", "empty", "all"):
                    self.selected_account_name = None
                    self.selected_account_api_key = None
                    self.update_prompt_label()
                    self.log_message("Account selection cleared (orders will target all accounts).")
                    return
                
                acct, err = self.resolve_account(raw_acc)
                if err:
                    self.log_message(f"[#ff0000]Error:[/#] {err}")
                    return
                
                self.selected_account_name = acct.get("name")
                self.selected_account_api_key = acct.get("api_key")
                self.update_prompt_label()
                self.log_message(f"Selected account: [bold]{self.selected_account_name}[/bold]. Orders will target this account only.")
                return
                
            if raw_id.lower() in ("none", "clear", "null", "empty"):
                self.selected_symbol = None
                self.selected_account_name = None
                self.selected_account_api_key = None
                self.update_prompt_label()
                self.log_message("Selection cleared.")
                return

            symbol, api_key, err = self.resolve_symbol(raw_id)
            if err:
                self.log_message(f"[#ff0000]Error:[/#] {err}")
                return
            
            self.selected_symbol = symbol
            if api_key:
                self.selected_account_api_key = api_key
                for acct in self.accounts:
                    if acct.get("api_key") == api_key:
                        self.selected_account_name = acct.get("name")
                        break
                
            self.update_prompt_label()
            self.log_message(f"Selected {symbol} (@{self.selected_account_name or 'All Accounts'}). You can now type: [bold]buy|sell <qty> [price] [product][/bold] directly!")
            return

        # Direct Account Selection Command
        if primary_cmd in ("account", "acct", "a"):
            if len(parts) < 2:
                self.log_message("[#ff0000]Usage:[/#] account <name|index|none>")
                return
            raw_acc = " ".join(parts[1:])
            if raw_acc.lower() in ("none", "clear", "null", "empty", "all"):
                self.selected_account_name = None
                self.selected_account_api_key = None
                self.update_prompt_label()
                self.log_message("Account selection cleared (orders will target all accounts).")
                return
            
            acct, err = self.resolve_account(raw_acc)
            if err:
                self.log_message(f"[#ff0000]Error:[/#] {err}")
                return
            
            self.selected_account_name = acct.get("name")
            self.selected_account_api_key = acct.get("api_key")
            self.update_prompt_label()
            self.log_message(f"Selected account: [bold]{self.selected_account_name}[/bold]. Orders will target this account only.")
            return

        # Place Order Command (Legacy/Alternative)
        if primary_cmd == "order":
            # Find the transaction type (BUY/SELL) index to handle multi-word symbols like "23500 CE"
            tx_type_idx = -1
            for idx, part in enumerate(parts):
                if part.lower() in ("buy", "sell"):
                    tx_type_idx = idx
                    break
            
            if tx_type_idx == -1 or tx_type_idx < 2 or len(parts) <= tx_type_idx + 1:
                self.log_message("[#ff0000]Usage:[/#] order <symbol> <buy|sell> <qty> [price] [product]")
                return

            # Parse parameters
            raw_symbol = " ".join(parts[1:tx_type_idx])
            transaction_type = parts[tx_type_idx].lower()
            qty_str = parts[tx_type_idx + 1]
            
            price_str = None
            if len(parts) > tx_type_idx + 2:
                price_str = parts[tx_type_idx + 2]
                
            product = "NRML"
            if len(parts) > tx_type_idx + 3:
                product = parts[tx_type_idx + 3]

            symbol, api_key, err = self.resolve_symbol(raw_symbol)
            if err:
                self.log_message(f"[#ff0000]Error:[/#] {err}")
                return

            target_key = api_key or self.selected_account_api_key
            target_keys = [target_key] if target_key else []

            # Set pending order for confirmation
            self.pending_order = {
                "symbol": symbol,
                "type": transaction_type,
                "qty": qty_str,
                "price": price_str,
                "product": product,
                "api_keys": target_keys
            }

            if target_keys:
                names = []
                for k in target_keys:
                    for acct in self.accounts:
                        if acct.get("api_key") == k:
                            names.append(f"@{acct.get('name')}")
                            break
                accts_desc = ", ".join(names)
            else:
                accts_desc = "All Accounts"

            price_desc = f"at price {price_str}" if price_str else "at MARKET"
            self.prompt_control.text = f" Confirm {transaction_type.upper()} {qty_str} {symbol} ({product}) {price_desc} on {accts_desc}? (y/n)> "
            self.log_message(f"[#ff8700]Pending Confirmation:[/#] {transaction_type.upper()} {qty_str} {symbol} ({product}) {price_desc} on {accts_desc}. Press [bold]y[/bold] to confirm, any other key to cancel.")
            return

        # Direct Buy/Sell Commands
        if primary_cmd in ("buy", "sell"):
            args = parts[1:]
            if not args:
                self.log_message(f"[#ff0000]Usage:[/#] {primary_cmd} [symbol|id] <qty> [price] [product]")
                return

            symbol = None
            api_key = None
            qty_str = None
            price_str = None
            product = "NRML"

            # Case 1: First argument is a valid position ID (under 100)
            if args[0].isdigit() and int(args[0]) < 100 and hasattr(self, "position_id_map") and int(args[0]) in self.position_id_map:
                symbol, api_key, err = self.resolve_symbol(args[0])
                if len(args) < 2:
                    # Quantity not specified. Default to the position's quantity.
                    pos = self.position_id_map.get(int(args[0]))
                    if pos:
                        qty_str = str(abs(pos.get("quantity", 0)))
                        self.log_message(f"Omitted quantity. Defaulting to position quantity {qty_str}.")
                    else:
                        self.log_message(f"[#ff0000]Usage:[/#] {primary_cmd} <id> <qty> [price] [product]")
                        return
                else:
                    qty_str = args[1]
                    if len(args) > 2:
                        price_str = args[2]
                    if len(args) > 3:
                        product = args[3]

            # Case 1b: Active selection exists, and first argument is acting as quantity
            elif args[0].isdigit() and hasattr(self, "selected_symbol") and self.selected_symbol:
                symbol = self.selected_symbol
                api_key = self.selected_account_api_key
                qty_str = args[0]
                if len(args) > 1:
                    price_str = args[1]
                if len(args) > 2:
                    product = args[2]

            else:
                # Case 2: Parse symbol and quantity
                # Search from right to left to locate quantity (to avoid strike prices or dates)
                qty_idx = -1
                for i in range(len(args) - 1, -1, -1):
                    arg = args[i]
                    if arg.isdigit():
                        # A strike price (e.g. 23500) is followed by CE/PE/FUT
                        if i + 1 < len(args) and args[i+1].upper() in ("CE", "PE", "FUT"):
                            continue
                        # A large digit (like >= 1000) is likely a strike price, not a quantity
                        if int(arg) >= 1000:
                            continue
                        qty_idx = i
                        break

                if qty_idx != -1:
                    qty_str = args[qty_idx]
                    
                    if qty_idx == 0:
                        # No symbol specified. Use currently selected symbol.
                        if hasattr(self, "selected_symbol") and self.selected_symbol:
                            symbol = self.selected_symbol
                            api_key = self.selected_account_api_key
                        else:
                            self.log_message(f"[#ff0000]Error:[/#] No position selected. Type 'select <id>' first or specify a symbol.")
                            return
                    else:
                        # Symbol is everything before the quantity
                        raw_sym = " ".join(args[:qty_idx])
                        symbol, api_key, err = self.resolve_symbol(raw_sym)
                        if err:
                            self.log_message(f"[#ff0000]Error:[/#] {err}")
                            return

                    if len(args) > qty_idx + 1:
                        price_str = args[qty_idx + 1]
                    if len(args) > qty_idx + 2:
                        product = args[qty_idx + 2]
                else:
                    # No quantity specified in arguments.
                    # Check if the entire arguments string represents a symbol of an active position.
                    raw_sym = " ".join(args)
                    symbol, api_key, err = self.resolve_symbol(raw_sym)
                    if err:
                        self.log_message(f"[#ff0000]Error:[/#] {err}")
                        return
                    
                    # Try to find a matching active position to default its quantity
                    matched_pos = None
                    normalized_sym = symbol.replace(" ", "").upper()
                    for pos in getattr(self, "active_positions", []):
                        if pos.get("tradingsymbol", "").replace(" ", "").upper() == normalized_sym:
                            matched_pos = pos
                            break
                            
                    if matched_pos:
                        qty_str = str(abs(matched_pos.get("quantity", 0)))
                        api_key = matched_pos.get("api_key")
                        self.log_message(f"Omitted quantity. Defaulting to position quantity {qty_str}.")
                    else:
                        self.log_message(f"[#ff0000]Error:[/#] Missing quantity. Usage: {primary_cmd} [symbol|id] <qty> [price] [product]")
                        return

            target_key = api_key or self.selected_account_api_key
            target_keys = [target_key] if target_key else []

            # Set pending order for confirmation
            self.pending_order = {
                "symbol": symbol,
                "type": primary_cmd,
                "qty": qty_str,
                "price": price_str,
                "product": product,
                "api_keys": target_keys
            }

            if target_keys:
                names = []
                for k in target_keys:
                    for acct in self.accounts:
                        if acct.get("api_key") == k:
                            names.append(f"@{acct.get('name')}")
                            break
                accts_desc = ", ".join(names)
            else:
                accts_desc = "All Accounts"

            price_desc = f"at price {price_str}" if price_str else "at MARKET"
            self.prompt_control.text = f" Confirm {primary_cmd.upper()} {qty_str} {symbol} ({product}) {price_desc} on {accts_desc}? (y/n)> "
            self.log_message(f"[#ff8700]Pending Confirmation:[/#] {primary_cmd.upper()} {qty_str} {symbol} ({product}) {price_desc} on {accts_desc}. Press [bold]y[/bold] to confirm, any other key to cancel.")
            return

        # Exit Positions Command
        if primary_cmd == "exit":
            if len(parts) < 2:
                if hasattr(self, "selected_symbol") and self.selected_symbol:
                    raw_symbol = self.selected_symbol
                else:
                    self.log_message("[#ff0000]Usage:[/#] exit <symbol|id> OR exit all")
                    return
            else:
                raw_symbol = " ".join(parts[1:])

            # If the user typed "exit none" or "exit clear", they want to clear/exit the selection context
            if raw_symbol.lower() in ("none", "clear"):
                self.selected_symbol = None
                self.selected_account_name = None
                self.selected_account_api_key = None
                self.update_prompt_label()
                self.log_message("Selection cleared.")
                return

            # Set pending exit for confirmation
            self.pending_order = {
                "symbol": raw_symbol,
                "type": "exit",
                "qty": "",
                "price": "",
                "product": ""
            }

            target_key = None
            if raw_symbol and raw_symbol.lower() != "all":
                symbol, api_key, err = self.resolve_symbol(raw_symbol)
                if err:
                    self.log_message(f"[#ff0000]Error:[/#] {err}")
                    self.pending_order = None
                    return
                self.pending_order["symbol"] = symbol
                target_key = api_key or self.selected_account_api_key
            else:
                target_key = self.selected_account_api_key

            target_keys = [target_key] if target_key else []
            self.pending_order["api_keys"] = target_keys

            if target_keys:
                names = []
                for k in target_keys:
                    for acct in self.accounts:
                        if acct.get("api_key") == k:
                            names.append(f"@{acct.get('name')}")
                            break
                accts_desc = ", ".join(names)
            else:
                accts_desc = "All Accounts"

            if raw_symbol and raw_symbol.lower() != "all":
                symbol = self.pending_order["symbol"]
                self.prompt_control.text = f" Confirm EXIT of {symbol} on {accts_desc}? (y/n)> "
                self.log_message(f"[#ff8700]Pending Confirmation:[/#] EXIT open positions for {symbol} on {accts_desc}. Press [bold]y[/bold] to confirm, any other key to cancel.")
            else:
                self.prompt_control.text = f" Confirm EXIT of ALL positions on {accts_desc}? (y/n)> "
                self.log_message(f"[#ff8700]Pending Confirmation:[/#] EXIT ALL open positions on {accts_desc}. Press [bold]y[/bold] to confirm, any other key to cancel.")
            return

        # Option Chain Command
        # Usage:
        #   oc <UNDERLYING>                  → current week
        #   oc <UNDERLYING> week <N>         → N-th week (0=current, 1=next, ...)
        #   oc <UNDERLYING> <YYYY-MM-DD>     → specific expiry date
        if primary_cmd in ("oc", "optionchain", "chain"):
            if len(parts) < 2:
                self.log_message("[#ff0000]Usage:[/#] oc <UNDERLYING> [week <N> | <YYYY-MM-DD>]")
                self.log_message("  e.g. [bold]oc NIFTY[/bold]              — current week expiry")
                self.log_message("  e.g. [bold]oc NIFTY week 1[/bold]       — next week expiry")
                self.log_message("  e.g. [bold]oc BANKNIFTY 2024-06-27[/bold] — specific date")
                return

            underlying = parts[1].upper()
            expiry_week = 0
            expiry_date = None

            if len(parts) >= 3:
                # 'week N' suffix
                if parts[2].lower() == "week" and len(parts) >= 4:
                    try:
                        expiry_week = int(parts[3])
                    except ValueError:
                        self.log_message(f"[#ff0000]Error:[/#] Invalid week number '{parts[3]}'.")
                        return
                else:
                    # Try to parse as a date
                    import re as _re
                    if _re.match(r"\d{4}-\d{2}-\d{2}", parts[2]):
                        expiry_date = parts[2]
                    else:
                        self.log_message(f"[#ff0000]Error:[/#] Unknown option '{parts[2]}'. Use 'week <N>' or 'YYYY-MM-DD'.")
                        return

            # Need an api_key to call the server — use first available account
            if not self.accounts:
                self.log_message("[#ff0000]Error:[/#] No accounts configured.")
                return
            api_key = self.accounts[0]["api_key"]

            self.log_message(f"Fetching option chain for [bold]{underlying}[/bold] (expiry_week={expiry_week if not expiry_date else expiry_date})...")
            asyncio.create_task(self._fetch_and_display_option_chain(api_key, underlying, expiry_week, expiry_date))
            return


        self.log_message(f"[#ff0000]Unknown command:[/#] '{primary_cmd}'. Type 'help' for options.")

    async def _fetch_and_display_option_chain(
        self,
        api_key: str,
        underlying: str,
        expiry_week: int = 0,
        expiry_date: str | None = None,
    ) -> None:
        """Fetch and display the option chain in the info pane (right panel)."""
        try:
            response = await self._run_api_call(
                self.client.get_option_chain,
                api_key=api_key,
                underlying=underlying,
                expiry_week=expiry_week,
                expiry_date=expiry_date,
            )
        except Exception as exc:
            self.log_message(f"Option chain error: {exc}")
            return

        if response.get("status") != "success":
            self.log_message(f"Option chain error: {response.get('message', 'Unknown error')}")
            expiries = response.get("available_expiries", [])
            if expiries:
                self.log_message("Available expiries:")
                for e in expiries:
                    self.log_message(f"  {e['week_label']}: {e['expiry']}")
            return

        expiry = response.get("expiry", "")
        strikes = response.get("strikes", [])
        expiries = response.get("available_expiries", [])

        # Build rich text for the info pane
        lines = [
            f"=== Option Chain: {underlying}  |  Expiry: {expiry} ===",
            "",
            f"{'CE LTP':>8}  {'CE Symbol':<24}  {'Strike':>8}  {'PE Symbol':<24}  {'PE LTP':>8}",
            "-" * 80,
        ]
        for s in strikes:
            strike = s.get("strike", 0)
            ce_sym = s.get("ce_symbol") or "-"
            pe_sym = s.get("pe_symbol") or "-"
            ce_ltp = s.get("ce_ltp")
            pe_ltp = s.get("pe_ltp")
            ce_ltp_str = f"{ce_ltp:>8.2f}" if ce_ltp is not None else f"{'N/A':>8}"
            pe_ltp_str = f"{pe_ltp:>8.2f}" if pe_ltp is not None else f"{'N/A':>8}"
            lines.append(
                f"{ce_ltp_str}  {ce_sym:<24}  {strike:>8.0f}  {pe_sym:<24}  {pe_ltp_str}"
            )

        lines += [
            "-" * 80,
            "",
            "Available expiries (use 'oc <UNDERLYING> week <N>' or 'oc <UNDERLYING> YYYY-MM-DD'):",
        ]
        for e in expiries:
            lines.append(f"  [{e['week_label']}]  {e['expiry']}")
        lines.append("")
        lines.append("Tip: To buy CE, type: buy <CE_SYMBOL> <qty>")

        self._last_oc_text = "\n".join(lines)
        self.info_mode = "oc"
        self._update_info_buffer()
        self.log_message(f"Option chain loaded for {underlying} expiry {expiry}. Press F3 to view in right pane.")

    def _render_orders_pane(self, response: dict, mode: str) -> str:
        """Render pending or executed orders from a /api/orders response into plain text."""
        PENDING_STATUSES = {"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "PUT ORDER REQ RECEIVED"}
        EXECUTED_STATUSES = {"COMPLETE"}
        filter_statuses = PENDING_STATUSES if mode == "orders_pending" else EXECUTED_STATUSES
        label = "PENDING ORDERS" if mode == "orders_pending" else "EXECUTED ORDERS (TODAY)"

        lines = [f"=== {label} ===", ""]
        any_orders = False

        for acct in response.get("accounts", []):
            name = acct.get("name", "?")
            orders = [
                o for o in acct.get("orders", [])
                if o.get("status", "").upper() in filter_statuses
            ]

            lines.append(f"Account: {name}")
            lines.append("-" * 60)

            if acct.get("status", "").startswith("error"):
                lines.append(f"  Error: {acct['status']}")
            elif not orders:
                lines.append("  (no orders)")
            else:
                any_orders = True
                for o in orders:
                    sym = o.get("tradingsymbol", "?")
                    tx = o.get("transaction_type", "?")
                    qty = o.get("quantity", 0)
                    filled = o.get("filled_quantity", 0)
                    otype = o.get("order_type", "?")
                    product = o.get("product", "?")
                    status = o.get("status", "?")
                    price = o.get("price", 0.0)
                    avg = o.get("average_price", 0.0)
                    oid = o.get("order_id", "?")

                    if mode == "orders_executed":
                        price_desc = f"avg={avg:.2f}" if avg else "MARKET"
                    else:
                        price_desc = f"@ {price:.2f}" if price else "MARKET"

                    qty_desc = f"{filled}/{qty}" if mode == "orders_executed" else str(qty)
                    lines.append(
                        f"  [{oid[-6:]}] {tx} {sym} | {qty_desc} | "
                        f"{otype} {price_desc} | {product} | {status}"
                    )
            lines.append("")

        if not any_orders and mode == "orders_pending":
            lines.append("No pending orders across all accounts.")
        return "\n".join(lines)

    def _update_info_buffer(self) -> None:
        """Push current info_mode text into the info_buffer."""
        if not hasattr(self, "info_buffer"):
            return
        if self.info_mode == "orders_pending":
            text = self._last_pending_text
        elif self.info_mode == "orders_executed":
            text = self._last_executed_text
        else:
            text = self._last_oc_text
        self.info_buffer.set_document(
            Document(text=text, cursor_position=0),
            bypass_readonly=True,
        )
        if hasattr(self, "_info_control"):
            self._info_control.vertical_scroll_position = 0
        if hasattr(self, "app"):
            self.app.invalidate()

    async def _update_orders_loop(self) -> None:
        """Periodically fetch orders from the server (every 10 seconds)."""
        while self.running:
            try:
                api_keys = [acct["api_key"] for acct in self.accounts]
                response = await self._run_api_call(self.client.get_orders, api_keys)
                self._last_pending_text = self._render_orders_pane(response, "orders_pending")
                self._last_executed_text = self._render_orders_pane(response, "orders_executed")
                if self.info_mode in ("orders_pending", "orders_executed"):
                    self._update_info_buffer()
            except Exception as exc:
                self.log_message(f"[#ff0000]Orders fetch error:[/#] {exc}")

            for _ in range(100):  # 10-second intervals
                if not self.running:
                    break
                await asyncio.sleep(0.1)

    def handle_global_drag(self, mouse_event, x, y) -> None:
        """Handle global mouse drag events to resize panes."""
        from prompt_toolkit.mouse_events import MouseEventType

        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            self.dragging_vertical = False
            self.dragging_horizontal = False
            if hasattr(self, "vertical_divider"):
                self.vertical_divider.style = "class:divider"
            if hasattr(self, "horizontal_divider"):
                self.horizontal_divider.style = "class:divider"
            self.app.invalidate()
            return

        if mouse_event.event_type == MouseEventType.MOUSE_MOVE:
            if self.dragging_vertical:
                total_cols = self.app.output.get_size().columns
                if total_cols > 0:
                    pct = int((x / total_cols) * 100)
                    self.left_width_pct = max(20, min(80, pct))
                    self.app.invalidate()
            elif self.dragging_horizontal:
                total_rows = self.app.output.get_size().rows
                if total_rows > 0:
                    # y is absolute screen row (0-based)
                    # Workspace is total_rows - 2 rows high (excluding header and input)
                    # Logs pane height should be rows from y to bottom of workspace (total_rows - 2)
                    log_height = (total_rows - 2) - y
                    self.log_height_lines = max(4, min(30, log_height))
                    self.app.invalidate()

    async def run(self) -> None:
        """Launch the interactive prompt_toolkit dashboard."""
        # ── UI Controls ─────────────────────────────────────────────
        self.header_control = FormattedTextControl(
            text="🪁 KiteCLI Live │ Loading dashboard...",
            style="class:header",
        )
        self.market_indices_control = FormattedTextControl(
            text=" NIFTY 50: Loading... │ SENSEX: Loading... │ INDIA VIX: Loading...",
            focusable=False,
            show_cursor=False,
        )

        # Active Positions — ScrollableFormattedTextControl with focusable=True so
        # the Window can receive keyboard scroll (Page Up/Down, arrow keys)
        # and mouse-wheel scroll when focused.
        self.positions_control = ScrollableFormattedTextControl(
            text="Fetching positions from server, please wait...",
            focusable=True,
            show_cursor=False,
        )

        # Status Logs — Buffer-backed: scroll, selection, auto-scroll to end
        initial_log_text = "\n".join(self.logs)
        self.logs_buffer = Buffer(
            name="logs_buffer",
            read_only=True,
            document=Document(text=initial_log_text, cursor_position=len(initial_log_text)),
        )
        # Enable focus_on_click=True so clicking the logs pane focuses it and allows selection
        self._logs_control = ScrollableBufferControl(buffer=self.logs_buffer, focusable=True, focus_on_click=True)

        # Info Pane (right half) — Buffer-backed: scroll, selection
        self.info_buffer = Buffer(
            name="info_buffer",
            read_only=True,
            document=Document(text=self._last_pending_text, cursor_position=0),
        )
        # Enable focus_on_click=True so clicking the info pane focuses it and allows selection
        self._info_control = ScrollableBufferControl(buffer=self.info_buffer, focusable=True, focus_on_click=True)

        # Command input
        # Pass FileHistory to support persistent command line history
        from pathlib import Path
        history_dir = Path.home() / ".kcli"
        history_dir.mkdir(parents=True, exist_ok=True)
        history_file = history_dir / "history.txt"

        self.input_field = TextArea(
            multiline=False,
            prompt="",
            style="class:input_text",
            accept_handler=self.handle_input,
            history=FileHistory(str(history_file)),
            focus_on_click=True,
        )
        self.prompt_control = FormattedTextControl(text=" kcli> ")

        # Quick-action bar — use a *callable* so fragments are freshly
        # generated on every render (avoids stale _fragment_cache bugs).
        self.quickaction_control = FormattedTextControl(
            text=self._build_action_bar_text,  # callable, not a list
            focusable=False,
            show_cursor=False,
        )

        # Custom mouse handlers to copy selected text to macOS system clipboard on mouse release
        def _get_word_at_pos(text, pos):
            if not text or pos < 0 or pos >= len(text):
                return ""
            start = pos
            while start > 0 and (text[start-1].isalnum() or text[start-1] in "_-."):
                start -= 1
            end = pos
            while end < len(text) and (text[end].isalnum() or text[end] in "_-."):
                end += 1
            if start == end and pos > 0 and (text[pos-1].isalnum() or text[pos-1] in "_-."):
                start = pos - 1
                while start > 0 and (text[start-1].isalnum() or text[start-1] in "_-."):
                    start -= 1
                end = pos
            return text[start:end]

        original_info_mouse_handler = self._info_control.mouse_handler
        def new_info_mouse_handler(mouse_event):
            res = original_info_mouse_handler(mouse_event)
            from prompt_toolkit.mouse_events import MouseEventType
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                buf = self._info_control.buffer
                selected_text = None
                if buf.selection_state is not None:
                    from_, to_ = buf.document.selection_range()
                    selected_text = buf.document.text[from_:to_]
                else:
                    pos = buf.document.cursor_position
                    text = buf.document.text
                    selected_text = _get_word_at_pos(text, pos)
                
                if selected_text:
                    selected_text = selected_text.strip()
                    import subprocess
                    try:
                        proc = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
                        proc.communicate(selected_text.encode('utf-8'))
                    except Exception:
                        pass
                    self._select_symbol_from_text(selected_text)
            return res
        self._info_control.mouse_handler = new_info_mouse_handler

        original_logs_mouse_handler = self._logs_control.mouse_handler
        def new_logs_mouse_handler(mouse_event):
            res = original_logs_mouse_handler(mouse_event)
            from prompt_toolkit.mouse_events import MouseEventType
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                buf = self._logs_control.buffer
                selected_text = None
                if buf.selection_state is not None:
                    from_, to_ = buf.document.selection_range()
                    selected_text = buf.document.text[from_:to_]
                else:
                    pos = buf.document.cursor_position
                    text = buf.document.text
                    selected_text = _get_word_at_pos(text, pos)
                
                if selected_text:
                    selected_text = selected_text.strip()
                    import subprocess
                    try:
                        proc = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
                        proc.communicate(selected_text.encode('utf-8'))
                    except Exception:
                        pass
                    self._select_symbol_from_text(selected_text)
            return res
        self._logs_control.mouse_handler = new_logs_mouse_handler

        # Define persistent Windows to preserve scroll state
        self.positions_window = ScrollableWindow(
            content=self.positions_control,
            wrap_lines=True,
            allow_scroll_beyond_bottom=True,
            get_vertical_scroll=lambda win: self.positions_control.vertical_scroll_position,
        )
        self.logs_window = ScrollableWindow(
            content=self._logs_control,
            wrap_lines=True,
            height=lambda: self.log_height_lines,
            get_vertical_scroll=lambda win: self._logs_control.vertical_scroll_position,
        )
        self.info_window = ScrollableWindow(
            content=self._info_control,
            wrap_lines=True,
            allow_scroll_beyond_bottom=True,
            get_vertical_scroll=lambda win: self._info_control.vertical_scroll_position,
        )

        def make_scroll_handler(window, control):
            original_handler = control.mouse_handler
            def scroll_mouse_handler(mouse_event):
                from prompt_toolkit.mouse_events import MouseEventType
                if mouse_event.event_type == MouseEventType.SCROLL_UP:
                    control.vertical_scroll_position = max(0, control.vertical_scroll_position - 3)
                    self.app.invalidate()
                    return None
                elif mouse_event.event_type == MouseEventType.SCROLL_DOWN:
                    if window.render_info:
                        max_scroll = max(0, window.render_info.content_height - window.render_info.window_height)
                        control.vertical_scroll_position = min(max_scroll, control.vertical_scroll_position + 3)
                    else:
                        control.vertical_scroll_position += 3
                    self.app.invalidate()
                    return None
                if original_handler:
                    return original_handler(mouse_event)
                return NotImplemented
            return scroll_mouse_handler

        self.positions_control.mouse_handler = make_scroll_handler(self.positions_window, self.positions_control)
        self._logs_control.mouse_handler = make_scroll_handler(self.logs_window, self._logs_control)
        self._info_control.mouse_handler = make_scroll_handler(self.info_window, self._info_control)

        # ── Key Bindings ───────────────────────────────────────────
        kb = KeyBindings()

        @kb.add("c-c")
        def _quit(event):
            self.running = False
            event.app.exit()

        @kb.add("escape")
        def _deselect(event):
            if (
                (hasattr(self, "selected_symbol") and self.selected_symbol)
                or (hasattr(self, "selected_account_name") and self.selected_account_name)
            ):
                self.selected_symbol = None
                self.selected_account_name = None
                self.selected_account_api_key = None
                self.update_prompt_label()
                self.log_message("Selection cleared.")

        @kb.add("up", filter=has_focus(self.input_field))
        def _input_up(event):
            event.current_buffer.history_backward()

        @kb.add("down", filter=has_focus(self.input_field))
        def _input_down(event):
            event.current_buffer.history_forward()

        @kb.add("up", filter=~has_focus(self.input_field))
        def _scroll_up_kb(event):
            cur = event.app.layout.current_control
            if cur == self.positions_control:
                self.positions_control.vertical_scroll_position = max(0, self.positions_control.vertical_scroll_position - 1)
            elif cur == self._logs_control:
                self._logs_control.vertical_scroll_position = max(0, self._logs_control.vertical_scroll_position - 1)
            elif cur == self._info_control:
                self._info_control.vertical_scroll_position = max(0, self._info_control.vertical_scroll_position - 1)

        @kb.add("down", filter=~has_focus(self.input_field))
        def _scroll_down_kb(event):
            cur = event.app.layout.current_control
            if cur == self.positions_control:
                w = self.positions_window
                c = self.positions_control
                if w.render_info:
                    max_scroll = max(0, w.render_info.content_height - w.render_info.window_height)
                    c.vertical_scroll_position = min(max_scroll, c.vertical_scroll_position + 1)
                else:
                    c.vertical_scroll_position += 1
            elif cur == self._logs_control:
                w = self.logs_window
                c = self._logs_control
                if w.render_info:
                    max_scroll = max(0, w.render_info.content_height - w.render_info.window_height)
                    c.vertical_scroll_position = min(max_scroll, c.vertical_scroll_position + 1)
                else:
                    c.vertical_scroll_position += 1
            elif cur == self._info_control:
                w = self.info_window
                c = self._info_control
                if w.render_info:
                    max_scroll = max(0, w.render_info.content_height - w.render_info.window_height)
                    c.vertical_scroll_position = min(max_scroll, c.vertical_scroll_position + 1)
                else:
                    c.vertical_scroll_position += 1

        @kb.add("pageup", filter=~has_focus(self.input_field))
        def _page_up_kb(event):
            cur = event.app.layout.current_control
            if cur == self.positions_control:
                self.positions_control.vertical_scroll_position = max(0, self.positions_control.vertical_scroll_position - 10)
            elif cur == self._logs_control:
                self._logs_control.vertical_scroll_position = max(0, self._logs_control.vertical_scroll_position - 10)
            elif cur == self._info_control:
                self._info_control.vertical_scroll_position = max(0, self._info_control.vertical_scroll_position - 10)

        @kb.add("pagedown", filter=~has_focus(self.input_field))
        def _page_down_kb(event):
            cur = event.app.layout.current_control
            if cur == self.positions_control:
                w = self.positions_window
                c = self.positions_control
                if w.render_info:
                    max_scroll = max(0, w.render_info.content_height - w.render_info.window_height)
                    c.vertical_scroll_position = min(max_scroll, c.vertical_scroll_position + 10)
                else:
                    c.vertical_scroll_position += 10
            elif cur == self._logs_control:
                w = self.logs_window
                c = self._logs_control
                if w.render_info:
                    max_scroll = max(0, w.render_info.content_height - w.render_info.window_height)
                    c.vertical_scroll_position = min(max_scroll, c.vertical_scroll_position + 10)
                else:
                    c.vertical_scroll_position += 10
            elif cur == self._info_control:
                w = self.info_window
                c = self._info_control
                if w.render_info:
                    max_scroll = max(0, w.render_info.content_height - w.render_info.window_height)
                    c.vertical_scroll_position = min(max_scroll, c.vertical_scroll_position + 10)
                else:
                    c.vertical_scroll_position += 10

        @kb.add("tab")
        def _tab(event):
            """Cycle focus: input -> positions -> logs -> info pane -> input."""
            cur = event.app.layout.current_control
            cycle = [
                self.input_field.control,
                self.positions_control,
                self._logs_control,
                self._info_control,
            ]
            try:
                idx = cycle.index(cur)
                nxt = cycle[(idx + 1) % len(cycle)]
            except ValueError:
                nxt = self.input_field.control
            event.app.layout.focus(nxt)

        # F1/F2/F3 — switch info pane content
        @kb.add("f1")
        def _f1(event):
            self.info_mode = "orders_pending"
            self._update_info_buffer()

        @kb.add("f2")
        def _f2(event):
            self.info_mode = "orders_executed"
            self._update_info_buffer()

        @kb.add("f3")
        def _f3(event):
            self.info_mode = "oc"
            self._update_info_buffer()

        # Ctrl+Left/Right — adjust left/right pane split
        @kb.add("c-left")
        def _narrow_left(event):
            self.left_width_pct = max(20, self.left_width_pct - 5)
            event.app.invalidate()

        @kb.add("c-right")
        def _widen_left(event):
            self.left_width_pct = min(80, self.left_width_pct + 5)
            event.app.invalidate()

        # Ctrl+Up/Down — adjust log pane height within left half
        @kb.add("c-up")
        def _shrink_log(event):
            self.log_height_lines = max(4, self.log_height_lines - 2)
            event.app.invalidate()

        @kb.add("c-down")
        def _grow_log(event):
            self.log_height_lines = min(30, self.log_height_lines + 2)
            event.app.invalidate()

        # Build dividers
        self.vertical_divider = Window(
            content=FormattedTextControl(
                text=[("", "┃\n")] * 120,
                focusable=False,
            ),
            width=1,
            style="class:divider",
        )

        self.horizontal_divider = Window(
            content=FormattedTextControl(
                text="━" * 250,
                focusable=False,
            ),
            height=1,
            style="class:divider",
        )

        # Divider mouse handlers
        def v_divider_mouse_handler(mouse_event):
            from prompt_toolkit.mouse_events import MouseEventType
            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                self.dragging_vertical = True
                self.vertical_divider.style = "class:divider.dragging"
                self.app.invalidate()

        def h_divider_mouse_handler(mouse_event):
            from prompt_toolkit.mouse_events import MouseEventType
            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                self.dragging_horizontal = True
                self.horizontal_divider.style = "class:divider.dragging"
                self.app.invalidate()

        self.vertical_divider.content.mouse_handler = v_divider_mouse_handler
        self.horizontal_divider.content.mouse_handler = h_divider_mouse_handler

        # Monkey-patch MouseHandlers.__init__ to wrap with DragInterceptDict
        original_init = MouseHandlers.__init__
        def new_init(lh_self, *args, **kwargs):
            original_init(lh_self, *args, **kwargs)
            lh_self.mouse_handlers = DragInterceptDict(self, lh_self.mouse_handlers)
        MouseHandlers.__init__ = new_init

        # ── Dynamic Layout ─────────────────────────────────────────
        # DynamicContainer rebuilds the body on each render using current
        # self.left_width_pct and self.log_height_lines values.
        def _build_body():
            self._update_positions_display()
            lw = self.left_width_pct           # left weight (20-80)
            rw = 100 - lw                      # right weight

            return VSplit([
                # ── LEFT half: Positions (scrollable) + Logs (scrollable) ──
                HSplit([
                    Frame(
                        title="Active Positions (Auto-refreshing) [Tab: focus]",
                        body=self.positions_window,
                    ),
                    self.horizontal_divider,
                    Frame(
                        title="Status Logs [Tab: focus] [Ctrl+↑↓: resize]",
                        body=self.logs_window,
                    ),
                ], width=D(weight=lw)),
                self.vertical_divider,
                # ── RIGHT half: Info Pane (scrollable) ──
                Frame(
                    title="[F1] Pending  [F2] Executed  [F3] Option Chain [Ctrl+←→: resize]",
                    body=HSplit([
                        Window(
                            content=self.market_indices_control,
                            height=1,
                            style="class:market_indices",
                        ),
                        Window(height=1, char="─", style="class:divider"),
                        self.info_window,
                    ]),
                    width=D(weight=rw),
                ),
            ])

        layout = Layout(
            HSplit([
                Window(content=self.header_control, height=1, style="class:header"),
                DynamicContainer(lambda: _build_body()),
                # ── Quick-action button bar ──
                Window(
                    content=self.quickaction_control,
                    height=1,
                    style="class:quickaction",
                ),
                # ── Command input row ──
                VSplit([
                    Window(
                        content=self.prompt_control,
                        dont_extend_width=True,
                        style="class:prompt_label",
                        height=1,
                    ),
                    self.input_field,
                ], height=1),
            ]),
            focused_element=self.input_field,
        )

        self.app = Application(
            layout=layout,
            key_bindings=kb,
            style=self.style,
            full_screen=True,
            mouse_support=True,
        )

        asyncio.create_task(self._update_loop())
        asyncio.create_task(self._update_orders_loop())

        try:
            await self.app.run_async()
        finally:
            self.running = False
