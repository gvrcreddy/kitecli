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
from cli.recorder import DataRecorder
import json

logger = logging.getLogger(__name__)


def _silence_websocket_loggers() -> None:
    """Route noisy third-party and CLI loggers to a file instead of the
    terminal.

    kiteconnect/autobahn/twisted/urllib3 and our own CLI module emit log warnings/errors
    on their module-level loggers. The root logger has no handlers, so Python's
    "last resort" handler writes them to stderr — which corrupts this
    full-screen prompt_toolkit TUI. We attach a dedicated file handler to those
    loggers and disable propagation so nothing reaches the terminal.
    """
    from pathlib import Path

    log_dir = Path.home() / ".kcli"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "websocket.log"

    file_handler = logging.FileHandler(str(log_file))
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    )

    for name in ("kiteconnect", "kiteconnect.ticker", "autobahn", "twisted", "cli", "urllib3"):
        noisy = logging.getLogger(name)
        # Avoid stacking duplicate handlers if run() is called more than once.
        if not any(isinstance(h, logging.FileHandler) for h in noisy.handlers):
            noisy.addHandler(file_handler)
        noisy.propagate = False
        if name == "cli":
            noisy.setLevel(logging.INFO)
        else:
            noisy.setLevel(logging.WARNING)


def probe_ws_auth(api_key: str, access_token: str, proxy_str: str = None,
                  timeout: float = 12.0) -> tuple[str, str]:
    """Probe Zerodha's WebSocket endpoint to check whether streaming auth works.

    This performs a single, lightweight WebSocket *upgrade* handshake against
    ``wss://ws.kite.trade`` (optionally through the account's HTTP proxy) and
    inspects the HTTP status of the response.

    Why this exists: a token can authenticate REST (``/user/profile`` returns
    200) yet still be rejected by the WebSocket with ``403 Authentication
    failed`` — this happens per api_key/app (e.g. an app without an active
    streaming subscription, or a token not generated via the api_secret
    ``generate_session`` flow). A REST-only check (``kite.profile()``) cannot
    detect this, so we probe the actual streaming handshake to decide whether
    to start the ticker and avoid an endless 403 reconnect storm.

    Returns a ``(status, detail)`` tuple where status is one of:
      - ``"ok"``            → server returned 101 Switching Protocols.
      - ``"auth_failed"``   → server returned 403/401 (token rejected for streaming).
      - ``"proxy_blocked"`` → proxy refused the CONNECT tunnel.
      - ``"error"``         → network/other failure (inconclusive).
    """
    import socket
    import base64
    import ssl
    import os

    host = "ws.kite.trade"
    port = 443

    # Build TLS context. The production kiteconnect ticker does not verify the
    # certificate either, but prefer a verified context when certifi is present.
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    raw_sock = None
    try:
        if proxy_str:
            p_str = proxy_str if "://" in proxy_str else f"http://{proxy_str}"
            pr = urlparse(p_str)
            raw_sock = socket.create_connection((pr.hostname, pr.port), timeout=timeout)
            connect_req = (
                f"CONNECT {host}:{port} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
            )
            if pr.username and pr.password:
                auth = base64.b64encode(
                    f"{pr.username}:{pr.password}".encode()
                ).decode("ascii")
                connect_req += f"Proxy-Authorization: Basic {auth}\r\n"
            connect_req += "\r\n"
            raw_sock.sendall(connect_req.encode())
            connect_resp = raw_sock.recv(4096).decode(errors="replace")
            first = connect_resp.splitlines()[0] if connect_resp else ""
            if " 200" not in first:
                return "proxy_blocked", first.strip() or "proxy CONNECT failed"
        else:
            raw_sock = socket.create_connection((host, port), timeout=timeout)

        try:
            tls = ctx.wrap_socket(raw_sock, server_hostname=host)
        except ssl.SSLError:
            # Fall back to an unverified handshake (matches ticker behaviour).
            ctx2 = ssl.create_default_context()
            ctx2.check_hostname = False
            ctx2.verify_mode = ssl.CERT_NONE
            tls = ctx2.wrap_socket(raw_sock, server_hostname=host)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        upgrade = (
            f"GET /?api_key={api_key}&access_token={access_token} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"X-Kite-Version: 3\r\n"
            f"User-Agent: kite3-python\r\n\r\n"
        )
        tls.sendall(upgrade.encode())
        resp = tls.recv(8192).decode(errors="replace")
        try:
            tls.close()
        except Exception:
            pass

        status_line = resp.splitlines()[0] if resp else ""
        if "101" in status_line:
            return "ok", ""
        if "403" in status_line or "401" in status_line:
            msg = "Authentication failed."
            if '"message"' in resp:
                try:
                    import json as _json
                    body = resp.split("\r\n\r\n", 1)[1]
                    msg = _json.loads(body).get("message", msg)
                except Exception:
                    pass
            return "auth_failed", msg
        return "error", status_line.strip() or "unexpected response"
    except Exception as exc:
        return "error", str(exc)
    finally:
        if raw_sock is not None:
            try:
                raw_sock.close()
            except Exception:
                pass


# ── Monkeypatch Autobahn to support Proxy Authentication ───────────
try:
    import base64
    import txaio
    
    # Safely select framework for txaio if not already selected
    if not getattr(txaio, "_explicit_framework", None):
        try:
            txaio.use_twisted()
        except Exception:
            try:
                txaio.use_asyncio()
            except Exception:
                pass

    from autobahn.websocket.protocol import WebSocketClientProtocol

    def custom_startProxyConnect(self):
        """Autobahn startProxyConnect override to inject Proxy-Authorization."""
        request = f"CONNECT {self.factory.host}:{self.factory.port} HTTP/1.1\x0d\x0a"
        request += f"Host: {self.factory.host}:{self.factory.port}\x0d\x0a"

        # Check if proxy dict contains username/password
        if (
            hasattr(self, "factory")
            and getattr(self.factory, "proxy", None)
            and "username" in self.factory.proxy
            and "password" in self.factory.proxy
        ):
            usr = self.factory.proxy["username"]
            pwd = self.factory.proxy["password"]
            auth_str = f"{usr}:{pwd}"
            auth_b64 = base64.b64encode(auth_str.encode("utf-8")).decode("ascii")
            request += f"Proxy-Authorization: Basic {auth_b64}\x0d\x0a"

        request += "\x0d\x0a"
        self.sendData(request.encode("utf-8"))

    WebSocketClientProtocol.startProxyConnect = custom_startProxyConnect
except Exception as patch_exc:
    logger.warning("Failed to monkeypatch Autobahn proxy connect: %s", patch_exc)


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
        line_count = ui_content.line_count or 1
        self.vertical_scroll_position = max(0, min(self.vertical_scroll_position, line_count - 1))
        ui_content.cursor_position = Point(x=0, y=self.vertical_scroll_position)
        return ui_content


class ScrollableBufferControl(BufferControl):
    """BufferControl that supports mouse wheel and keyboard scrolling via cursor synchronization."""
    pass


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

    def __init__(self, client: KCLIClient, accounts: list[dict]) -> None:
        self.client = client
        self.accounts = accounts
        self.running = True
        self.tickers = {}
        self.websocket_connected = {a["api_key"]: False for a in accounts if a.get("api_key")}
        self.subscribed_tokens = set()
        # Throttle state for noisy per-account WebSocket close/error logging.
        self._ws_log_throttle = {}

        # Market index instrument tokens (constant on Kite). These are streamed
        # over the WebSocket alongside position tokens so NIFTY/SENSEX/INDIA VIX
        # update live rather than only on REST refresh. Verified via quote/ltp:
        #   NSE:NIFTY 50 -> 256265, BSE:SENSEX -> 265, NSE:INDIA VIX -> 264969
        self.index_tokens = {256265: "nifty", 265: "sensex", 264969: "vix"}
        self.index_values = {"nifty": None, "sensex": None, "vix": None}

        # Designated primary streaming account. Index and option-chain ticks are
        # subscribed only on this account's ticker (chosen in
        # _initial_fetch_and_connect) so we don't redundantly stream the same
        # instruments on every account.
        self.primary_api_key = None
        # Option-chain streaming state.
        self.oc_data = None            # structured {underlying, expiry, strikes, ...}
        self.oc_token_map = {}         # instrument_token -> (strike_value, "ce"|"pe")
        self.oc_subscribed_tokens = set()
        
        # In-memory store of active positions used to resolve partial symbols
        self.active_positions = []
        self.position_id_map = {}
        self.last_positions_response = None
        self.last_orders_response = None
        # Margin data keyed by api_key; fetched on order fill.
        self.margins_by_api_key: dict = {}
        # NFO tradingsymbol → lot_size; fetched once by kite_manager, cached there.
        
        # Log message list (plain text for Buffer-based display)
        self.logs = ["Type 'help' to see commands. Scroll logs using Mouse Wheel."]
        
        self.selected_symbol = None
        self.selected_account_name = None
        self.selected_account_api_key = None
        self.pending_order = None
        self.selected_order = None

        # Build account api_key -> user_id mapping
        self.api_key_to_user_id = {
            a.get("api_key"): a.get("user_id", "UNKNOWN")
            for a in accounts
            if a.get("api_key")
        }

        # Initialize and start SQLite recorder
        self.recorder = DataRecorder()
        self.recorder.start()

        # Info pane state
        # mode: "orders_pending" | "orders_executed" | "oc" | "advisor"
        self.info_mode: str = "orders_pending"
        self._last_oc_text: str = "Press F3 or run 'oc <UNDERLYING>' to fetch option chain."
        self._last_pending_text: str = "Fetching pending orders..."
        self._last_executed_text: str = "Fetching executed orders..."
        self._last_advisor_text: str = "Press F4 to view Tuesday Option Strangle Advisor plan."
        self._advisor_alerted_today: str | None = None
        self._last_advisor_time_check: float = 0.0

        # Load Gemini API key
        import os
        from cli.config import load_config
        cfg = load_config() or {}
        self.gemini_api_key = cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
        self._skip_confirmation: bool = False

        # Pane resize state (adjusted via Ctrl+arrows)
        self.left_width_pct: int = 50   # % of terminal width for left pane (20–80)
        self.log_height_lines: int = 10  # rows for the log sub-pane in the left half
        self.dragging_vertical = False
        self.dragging_horizontal = False
        
        # Style definition for UI elements (professional dark theme)
        self.style = Style.from_dict({
            "header": "bg:#21262d fg:#e6edf3 bold",
            "prompt_label": "fg:#58a6ff bold",
            "input_text": "fg:#ffffff",
            "log_title": "fg:#d29922 bold",
            "selected_row": "bg:#1f6feb fg:#ffffff bold",
            "info_header": "fg:#56d364 bold",
            "divider": "bg:#21262d fg:#30363d",
            "divider.dragging": "bg:#0969da fg:#ffffff bold",
            "market_indices": "bg:#161b22",
            # Quick action bar
            "quickaction": "bg:#161b22 fg:#8b949e",
            "quickaction.hint": "bg:#161b22 fg:#484f58 italic",
            # Buttons (Soft professional colors)
            "btn.buy": "bg:#1b4a2d fg:#56d364 bold",
            "btn.sell": "bg:#5e1c18 fg:#ff7b72 bold",
            "btn.exit": "bg:#4a1b4d fg:#e85ffd bold",
            "btn.modify": "bg:#18315e fg:#79c0ff bold",
            "btn.modify_matching": "bg:#005f73 fg:#94d2bd bold",
            "btn.cancel_matching": "bg:#8b263e fg:#ff8b94 bold",
            "btn.cancel": "bg:#30363d fg:#8b949e bold",
            "btn.refresh": "bg:#0f3542 fg:#39c5bb bold",
            
            # Frame and Borders (Focused Container Highlight)
            "frame.border": "fg:#30363d",
            "frame.label": "fg:#8b949e bold",
            "focused_frame.border": "fg:#58a6ff bold",
            "focused_frame.label": "fg:#58a6ff bold",
        })

    def _strip_rich_markup(self, text: str) -> str:
        """Strip Rich-style markup tags from text for plain display."""
        import re
        return re.sub(r"\[/?[^\[\]]*\]", "", text)

    @staticmethod
    def _clean_error(text: str) -> str:
        """Extract a short human-readable message from a verbose exception string.

        Strips nested Python exception chains, long URLs, and connection pool
        boilerplate so the logs pane shows concise, actionable messages.
        """
        import re
        s = str(text).strip()

        # 1. Pull the innermost meaningful message from chained exceptions.
        #    e.g. "...Failed to place order: The instrument..." → "The instrument..."
        for sep in (": ", " — "):
            parts = s.split(sep)
            # Walk from the end, pick the last part that looks like a real message
            for part in reversed(parts):
                part = part.strip()
                if part and not part.startswith("HTTPSConnectionPool") and not part.startswith("Max retries"):
                    s = part
                    break

        # 2. Simplify common connection error patterns
        if "407 Proxy Authentication Required" in s:
            return "Proxy authentication failed (407) — check proxy credentials in config"
        if "ProxyError" in s or "Tunnel connection failed" in s:
            return "Proxy connection failed — check proxy settings"
        if "ConnectionError" in s or "Max retries exceeded" in s:
            return "Network error — connection failed"
        if "TimeoutError" in s or "timed out" in s.lower():
            return "Connection timed out"
        if "TokenException" in s or "Invalid token" in s:
            return "Token expired — run 'kcli init' to re-login"
        if "PermissionException" in s or "403" in s:
            return "Permission denied (403) — check account entitlements"

        # 3. Strip trailing context like "(Caused by ...)" and "url: /..."
        s = re.sub(r'\s*\(Caused by.*', '', s)
        s = re.sub(r'\s*with url:.*', '', s)
        s = re.sub(r'\s*HTTPSConnectionPool\(.*?\)', '', s)

        return s.strip() or str(text)[:80]

    def log_message(self, message: str) -> None:
        """Add a timestamped message to the logs and update logs pane."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        plain = self._strip_rich_markup(message)
        plain = self._clean_error(plain)

        # Compute max usable width for the logs pane so long lines don't wrap
        # and spill outside the Frame. Frame border = 2, padding = 2, safety = 2.
        try:
            total_cols = self.app.output.get_size().columns if (hasattr(self, "app") and self.app and self.app.output) else 80
            left_width = int(total_cols * (self.left_width_pct / 100.0))
            max_line = max(40, left_width - 8)
        except Exception:
            max_line = 120

        line = f"[{timestamp}] {plain}"
        if len(line) > max_line:
            line = line[:max_line - 1] + "…"

        self.logs.append(line)
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
        if self.selected_order:
            self.prompt_control.text = f" kcli [@{self.selected_order.get('account_name')}:{self.selected_order.get('order_id')[-6:]}]> "
        elif self.selected_account_name and self.selected_symbol:
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
        order = getattr(self, "selected_order", None)

        # Find matching active position to determine default qty
        matched_pos = None
        if sym:
            for pos in getattr(self, "active_positions", []):
                if self.selected_account_api_key and pos.get("api_key") != self.selected_account_api_key:
                    continue
                if pos.get("tradingsymbol", "").upper() == sym.upper():
                    matched_pos = pos
                    break
        qty = abs(matched_pos.get("quantity", 1)) if matched_pos else 1

        # Get live price (LTP) from matched position to pre-fill price
        price = matched_pos.get("last_price") if matched_pos else None
        if price is None:
            price = 0.0
        else:
            try:
                price = float(price)
            except (ValueError, TypeError):
                price = 0.0
        price_str = f" {price:.2f}" if price > 0 else ""

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

                if snippet == "refresh":
                    self.log_message("Triggering manual refresh via action bar button...")
                    if hasattr(self, "app") and self.app and self.app.loop:
                        asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)
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

        # Determine snippets based on context
        if order:
            o_id = order.get("order_id")
            o_qty = order.get("quantity", 1)
            o_price = order.get("price", 0.0)
            o_sym = order.get("tradingsymbol")
            
            modify_snippet = f"order {o_id} {o_qty} {o_price:.2f}"
            cancel_snippet = f"cancel {o_id}"
            
            matching_orders = self._find_all_matching_pending_orders(o_sym) if o_sym else []
            
            buttons = [
                ("  MODIFY  ", "class:btn.modify", "bg:#1a1a1a fg:#444444", modify_snippet),
            ]
            
            if len(matching_orders) > 1:
                modify_matching_snippet = f"order {o_sym} {o_qty} {o_price:.2f}"
                cancel_matching_snippet = f"cancel {o_sym}"
                buttons.append(("  MODIFY MATCHING  ", "class:btn.modify_matching", "bg:#1a1a1a fg:#444444", modify_matching_snippet))
                buttons.append(("  CANCEL MATCHING  ", "class:btn.cancel_matching", "bg:#1a1a1a fg:#444444", cancel_matching_snippet))
                
            buttons.extend([
                ("  CANCEL  ", "class:btn.cancel", "bg:#1a1a1a fg:#444444", cancel_snippet),
                ("  REFRESH  ", "class:btn.refresh", "bg:#1a1a1a fg:#444444", "refresh"),
            ])
        else:
            if sym:
                # Context 3: If account and symbol are selected
                buy_snippet = f"buy {qty}{price_str} "
                sell_snippet = f"sell {qty}{price_str} "
            elif acct:
                # Context 2: If account is selected (but no symbol)
                buy_snippet = "buy <symbol> <qty> [price] [product]"
                sell_snippet = "sell <symbol> <qty> [price] [product]"
            else:
                # Context 1: If nothing is selected
                buy_snippet = "account <name> && buy <symbol> <qty> [price] [product]"
                sell_snippet = "account <name> && sell <symbol> <qty> [price] [product]"

            # Resolve near-week option symbols to build explicit exit command
            squareoff_parts = []
            try:
                from cli.advisor import get_nifty_options
                import datetime

                ref_key = None
                for a in self.accounts:
                    if self.client.is_authenticated(a["api_key"]):
                        ref_key = a["api_key"]
                        break

                if ref_key:
                    options = get_nifty_options(self.client, ref_key)
                    if options:
                        today = datetime.date.today()
                        underlying_near_expiry = {}
                        for inst in options:
                            name = inst.get("name")
                            expiry = inst.get("expiry")
                            if name and isinstance(expiry, datetime.date) and expiry >= today:
                                if name not in underlying_near_expiry:
                                    underlying_near_expiry[name] = expiry
                                else:
                                    underlying_near_expiry[name] = min(underlying_near_expiry[name], expiry)

                        # Determine targeted accounts
                        target_key = self.selected_account_api_key
                        target_accounts = [a for a in self.accounts if a["api_key"] == target_key] if target_key else self.accounts

                        accounts_data = self.last_positions_response.get("accounts", []) if self.last_positions_response else []

                        for a_cfg in target_accounts:
                            a_key = a_cfg["api_key"]
                            a_name = a_cfg["name"]
                            
                            # Find positions for this specific account
                            acct_data = next((x for x in accounts_data if x.get("api_key") == a_key), None)
                            if acct_data:
                                acct_exits = []
                                for pos in acct_data.get("positions", []):
                                    if pos.get("quantity", 0) == 0:
                                        continue
                                    sym_name = pos.get("tradingsymbol", "")
                                    inst = next((x for x in options if x.get("tradingsymbol") == sym_name), None)
                                    if inst:
                                        name = inst.get("name")
                                        exp = inst.get("expiry")
                                        if exp == underlying_near_expiry.get(name):
                                            lp = pos.get("last_price")
                                            try:
                                                p_val = float(lp) if lp is not None else 0.0
                                                p_str = f" {p_val:.2f}" if p_val > 0 else ""
                                            except (ValueError, TypeError):
                                                p_str = ""
                                            acct_exits.append((sym_name, p_str))
                                
                                if acct_exits:
                                    if not target_key:
                                        squareoff_parts.append(f"account {a_name}")
                                    for sym_name, p_str in acct_exits:
                                        squareoff_parts.append(f"exit {sym_name}{p_str}")
            except Exception:
                pass

            if sym:
                # Target selected position with limit price
                squareoff_snippet = f"exit {sym}{price_str}"
            else:
                squareoff_snippet = " && ".join(squareoff_parts) if squareoff_parts else "exit near-week"

            # ── button definitions: (label, active_style, dim_style, snippet) ──
            buttons = [
                ("  BUY  ", "bg:#005f00 fg:#afffaf bold", "bg:#1a1a1a fg:#444444", buy_snippet),
                ("  SELL  ", "bg:#5f0000 fg:#ffafaf bold", "bg:#1a1a1a fg:#444444", sell_snippet),
                ("  SQUAREOFF  ", "bg:#800080 fg:#ffcfff bold", "bg:#1a1a1a fg:#444444", squareoff_snippet),
                ("  REFRESH  ", "class:btn.refresh", "bg:#1a1a1a fg:#444444", "refresh"),
            ]

        frags = []

        # Left label — show selected context or neutral hint
        if order:
            ctx = f"Order:{order.get('order_id')[-6:]} @{order.get('account_name')}"
            frags.append(("bg:#262626 fg:#00afaf bold", f"  [{ctx}] "))
        elif sym:
            ctx = sym + (f" @{acct}" if acct else "")
            frags.append(("bg:#262626 fg:#00afaf bold", f"  [{ctx}] "))
        elif acct:
            frags.append(("bg:#262626 fg:#00afaf bold", f"  [@{acct}] "))
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
                        self.selected_order = None
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
                    self.selected_order = None
                    self.update_prompt_label()
                    self.log_message(f"Selected account [bold]@{self.selected_account_name}[/bold] via click.")
                    if hasattr(self, "app"):
                        self.app.invalidate()
        return handler

    def _update_positions_display(self) -> None:
        """Render positions to a formatted string aligned to the left container width."""
        if not getattr(self, "last_positions_response", None):
            self.positions_control.text = "Fetching positions, please wait..."
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

        # Filter accounts' positions to only include non-zero quantity for the TUI rendering
        filtered_accounts = []
        for acct in self.last_positions_response.get("accounts", []):
            filtered_acct = dict(acct)
            filtered_acct["positions"] = [p for p in acct.get("positions", []) if p.get("quantity", 0) != 0]
            filtered_accounts.append(filtered_acct)

        rendered = render_positions_to_string(
            filtered_accounts,
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
        self._update_header_display()

    def _update_header_display(self) -> None:
        """Update the header text and style based on WebSocket status."""
        total = len(self.accounts)
        connected = sum(1 for k in getattr(self, "websocket_connected", {}).values() if k)
        
        def _header_click_handler(*args, **kwargs):
            from prompt_toolkit.mouse_events import MouseEventType
            mouse_event = None
            if len(args) == 2:
                mouse_event = args[1]
            elif len(args) == 1:
                mouse_event = args[0]
            
            if mouse_event and mouse_event.event_type == MouseEventType.MOUSE_UP:
                self.log_message("Triggering manual WebSocket reconnection...")
                if hasattr(self, "app") and self.app and self.app.loop:
                    asyncio.run_coroutine_threadsafe(self.reconnect_websockets(), self.app.loop)

        if connected == total:
            status_style = "fg:#00ff00 bold"
            status_label = "WebSockets Active"
        elif connected > 0:
            status_style = "fg:#ff8700 bold"
            status_label = f"WebSockets Partial ({connected}/{total})"
        else:
            status_style = "fg:#ff0000 bold"
            status_label = "WebSockets Inactive"

        now = datetime.now().strftime("%H:%M:%S")
        
        frags = [
            ("", "🪁 KiteCLI Live │ "),
            (status_style, status_label, _header_click_handler),
            ("", f" │ Last Update: {now} │ Accounts: {total} │ Ctrl+C: Quit │ Escape: Deselect"),
        ]
        self.header_control.text = frags
        if hasattr(self, "app") and self.app:
            self.app.invalidate()

    async def reconnect_websockets(self) -> None:
        """Close existing WebSocket connections and start fresh ones, updating the UI."""
        self.log_message("[#58a6ff]Closing existing WebSocket connections...[/#]")
        
        # 1. Nullify callbacks on the old tickers before closing to prevent async race conditions
        for api_key, ticker in list(self.tickers.items()):
            try:
                ticker.on_connect = None
                ticker.on_close = None
                ticker.on_error = None
                ticker.on_ticks = None
                ticker.close()
            except Exception:
                pass
        self.tickers.clear()
        
        # 2. Reset connection flags locally and redraw the header status immediately
        for api_key in list(self.websocket_connected.keys()):
            self.websocket_connected[api_key] = False
        self._update_header_display()
        
        # 3. Establish the new connection (triggers REST fetch & connects WS)
        await self._initial_fetch_and_connect()
        
        # 4. Redraw positions and invalidate TUI to reflect new values
        self._update_display_and_invalidate()
        self.log_message("[#00ff00]✓ WebSockets reconnected successfully.[/#]")

    async def _diagnose_tokens(self) -> dict[str, str]:
        """Check each account's token validity and report it in the Status Logs.

        Returns a mapping of api_key -> status so the caller can skip or warn
        about accounts whose WebSocket handshake will be rejected (e.g. 403).
        """
        self.log_message("Running token diagnostics...")
        statuses: dict[str, str] = {}
        any_bad = False
        for acct in self.accounts:
            api_key = acct.get("api_key")
            if not api_key:
                continue
            try:
                result = await self._run_api_call(self.client.check_token, api_key)
            except Exception as exc:
                result = {"name": acct.get("name", api_key), "status": "error",
                          "detail": str(exc)}
            status = result.get("status", "error")
            statuses[api_key] = status
            name = result.get("name", api_key)
            detail = result.get("detail", "")
            if status == "valid":
                # REST works, but that does NOT guarantee streaming works: the
                # WebSocket can still reject this token with 403 "Authentication
                # failed" (per api_key/app — e.g. no active streaming
                # subscription). Probe the actual WS handshake so we only start
                # tickers that will succeed and avoid a 403 reconnect storm.
                access_token = self.client.get_access_token(api_key)
                proxy_str = acct.get("proxy")
                ws_status, ws_detail = await self._run_api_call(
                    probe_ws_auth, api_key, access_token, proxy_str
                )
                if ws_status == "ok":
                    self.log_message(f"[#00ff00]✓ Streaming OK:[/#] @{name}")
                elif ws_status == "auth_failed":
                    any_bad = True
                    statuses[api_key] = "stream_forbidden"
                    self.log_message(
                        f"[#ff0000]✗ Streaming rejected:[/#] @{name} — "
                        f"WebSocket auth failed ({ws_detail}). REST works, so the "
                        f"token is valid but this api_key lacks streaming access. "
                        f"Check the app's subscription on the Kite Connect dashboard."
                    )
                elif ws_status == "proxy_blocked":
                    any_bad = True
                    statuses[api_key] = "proxy_blocked"
                    self.log_message(
                        f"[#ff8700]⚠ Proxy blocked streaming:[/#] @{name} — "
                        f"proxy refused the WebSocket tunnel ({ws_detail})."
                    )
                else:
                    # Inconclusive probe — let the ticker try anyway.
                    self.log_message(
                        f"[#ff8700]⚠ Streaming check inconclusive:[/#] @{name} "
                        f"({ws_detail}). Will attempt to connect."
                    )
            elif status == "no_token":
                any_bad = True
                self.log_message(f"[#ff8700]⚠ No token:[/#] @{name} — login required (run 'kcli init').")
            elif status == "expired":
                any_bad = True
                self.log_message(f"[#ff8700]⚠ Token expired:[/#] @{name} — re-login (run 'kcli init'). {detail}")
            elif status == "forbidden":
                any_bad = True
                self.log_message(f"[#ff0000]✗ Forbidden:[/#] @{name} — token rejected / no streaming entitlement. {detail}")
            else:
                any_bad = True
                self.log_message(f"[#ff0000]✗ Token check failed:[/#] @{name} — {detail}")

        if any_bad:
            self.log_message(
                "[#ff8700]Note:[/#] Accounts flagged above are skipped for "
                "streaming to avoid 403/1006 reconnect storms. 'Streaming "
                "rejected' with working REST usually means that api_key's app "
                "has no active streaming subscription — re-init won't fix it."
            )
        return statuses

    def _select_primary_account(self, token_statuses: dict[str, str]) -> str | None:
        """Pick the primary streaming account.

        Preference order:
          1. A config account explicitly flagged ``primary: true`` — but only if
             it is stream-capable (token status ``valid``).
          2. Otherwise the first stream-capable account in config order.

        Returns the api_key, or ``None`` if no account can stream.
        """
        def streamable(acct) -> bool:
            return token_statuses.get(acct.get("api_key")) == "valid"

        flagged = [a for a in self.accounts if a.get("primary")]
        for acct in flagged:
            if streamable(acct):
                return acct.get("api_key")
        if flagged:
            self.log_message(
                f"[#ff8700]Configured primary @{flagged[0].get('name')} cannot "
                f"stream;[/#] falling back to another stream-capable account."
            )

        for acct in self.accounts:
            if streamable(acct):
                return acct.get("api_key")
        return None

    async def _initial_fetch_and_connect(self) -> None:
        """Initial fetch of data and connect all WebSockets."""
        self.log_message("Initializing connections and fetching data...")
        try:
            await self._trigger_immediate_refresh()
        except Exception as exc:
            self.log_message(f"[#ff0000]Initial fetch failed:[/#] {exc}")

        # Diagnose token validity up front. The same api_key/access_token pair is
        # used for the WebSocket ticker, so a REST 403/expired here explains the
        # "1006 / 403 Forbidden" handshake failures.
        token_statuses = await self._diagnose_tokens()

        # Choose the primary streaming account (used for index + option-chain
        # streaming). Index/OC ticks are subscribed only on this account.
        self.primary_api_key = self._select_primary_account(token_statuses)
        if self.primary_api_key:
            self.log_message(
                f"[#58a6ff]Primary streaming account:[/#] "
                f"@{self._get_account_name(self.primary_api_key)}"
            )
        else:
            self.log_message(
                "[#ff8700]No stream-capable account:[/#] indices and option "
                "chain will use REST snapshots only (no live streaming)."
            )

        # Connect WebSockets for all accounts
        from kiteconnect import KiteTicker
        for acct in self.accounts:
            api_key = acct.get("api_key")
            access_token = self.client.get_access_token(api_key)
            # Skip accounts whose token we already know is bad — avoids the
            # reconnect storm of 403 handshakes.
            if token_statuses.get(api_key) not in (None, "valid"):
                self.log_message(
                    f"[#ff8700]Skipping WebSocket for @{acct.get('name')}:[/#] "
                    f"token not valid ({token_statuses.get(api_key)})."
                )
                continue
            if api_key and access_token:
                try:
                    # reconnect=True/reconnect_max_tries belong on KiteTicker()
                    # constructor, not connect(). Max 50 tries (library default max
                    # is 300; 50 gives ~15+ mins of exponential-backoff recovery).
                    ticker = KiteTicker(
                        api_key, access_token,
                        reconnect=True,
                        reconnect_max_tries=5,
                    )

                    ticker.on_connect = self._make_on_connect(api_key, ticker)
                    ticker.on_ticks = self._on_ticks
                    ticker.on_order_update = self._make_on_order_update(api_key)
                    ticker.on_close = self._make_on_close(api_key)
                    ticker.on_error = self._make_on_error(api_key)

                    # Parse proxy if configured for this account
                    proxy_str = acct.get("proxy")
                    proxy_dict = None
                    if proxy_str:
                        from urllib.parse import urlparse
                        try:
                            p_str = proxy_str if "://" in proxy_str else f"http://{proxy_str}"
                            parsed = urlparse(p_str)
                            if parsed.hostname and parsed.port:
                                proxy_dict = {
                                    "host": parsed.hostname,
                                    "port": int(parsed.port),
                                }
                                if parsed.username and parsed.password:
                                    proxy_dict["username"] = parsed.username
                                    proxy_dict["password"] = parsed.password
                        except Exception as p_err:
                            self.log_message(f"[#ff8700]Failed to parse proxy for {acct.get('name')}:[/#] {p_err}")

                    # connect() only accepts threaded + proxy
                    connect_kwargs = dict(threaded=True)
                    if proxy_dict:
                        connect_kwargs["proxy"] = proxy_dict
                    ticker.connect(**connect_kwargs)
                    self.tickers[api_key] = ticker
                except Exception as exc:
                    self.log_message(f"[#ff0000]Failed to connect WebSocket for {acct.get('name')}:[/#] {exc}")

    def _get_account_name(self, api_key: str) -> str:
        """Helper to get account name by api_key."""
        for acct in self.accounts:
            if acct.get("api_key") == api_key:
                return acct.get("name", api_key)
        return api_key

    def _make_on_connect(self, api_key: str, ticker):
        def on_connect(ws, response):
            self.websocket_connected[api_key] = True
            if hasattr(self, "app") and self.app and self.app.loop:
                self.app.loop.call_soon_threadsafe(self._update_header_display)
            name = self._get_account_name(api_key)
            self.log_message(f"[#00ff00]WebSocket connected:[/#] @{name}")
            # All market-data (position prices, indices, option chain) streams on
            # the primary ticker only — instrument prices are global, so there's
            # no need to subscribe them on every account. Non-primary tickers
            # stay connected purely for their own order-update postbacks, which
            # require no subscription.
            is_primary = api_key == self.primary_api_key
            # If no primary was selected, fall back to the first connected ticker
            # so position prices still stream somewhere.
            if not self.primary_api_key and self.tickers:
                is_primary = api_key == next(iter(self.tickers.keys()))
            if not is_primary:
                return
            tokens = set(self.subscribed_tokens)
            tokens |= set(self.index_tokens)
            tokens |= set(self.oc_subscribed_tokens)
            if tokens:
                token_list = list(tokens)
                ticker.subscribe(token_list)
                ticker.set_mode(ticker.MODE_LTP, token_list)
        return on_connect

    def _ws_should_log(self, key: str, min_interval: float = 10.0) -> bool:
        """Return True if a throttled WebSocket message for ``key`` should be
        logged now (rate-limited to one message per ``min_interval`` seconds).

        Prevents reconnect storms (e.g. repeated 1006 closures) from flooding
        the Status Logs pane.
        """
        import time

        now = time.monotonic()
        last = self._ws_log_throttle.get(key, 0.0)
        if now - last >= min_interval:
            self._ws_log_throttle[key] = now
            return True
        return False

    def _make_on_close(self, api_key: str):
        def on_close(ws, code, reason):
            self.websocket_connected[api_key] = False
            if hasattr(self, "app") and self.app and self.app.loop:
                self.app.loop.call_soon_threadsafe(self._update_header_display)
            name = self._get_account_name(api_key)
            if self._ws_should_log(f"close:{api_key}"):
                self.log_message(
                    f"[#ff8700]WebSocket closed:[/#] @{name} ({code} {reason}) "
                    f"— reconnecting..."
                )
        return on_close

    def _make_on_error(self, api_key: str):
        def on_error(ws, code, reason):
            self.websocket_connected[api_key] = False
            if hasattr(self, "app") and self.app and self.app.loop:
                self.app.loop.call_soon_threadsafe(self._update_header_display)
            name = self._get_account_name(api_key)
            reason_str = str(reason).lower()
            # Detect permanent auth failures (403, token expired) — stop reconnecting
            # immediately to avoid hammering Zerodha and getting rate-limited.
            is_auth_failure = (
                code == 403
                or "403" in reason_str
                or "auth_failed" in reason_str
                or "token" in reason_str
            )
            if is_auth_failure:
                self.log_message(
                    f"[#ff0000]WebSocket auth failed:[/#] @{name} — token expired or invalid. "
                    f"Run [bold]kcli init[/bold] to re-authenticate."
                )
                # Stop reconnecting — close the ticker cleanly
                ticker = self.tickers.get(api_key)
                if ticker:
                    try:
                        ticker.close()
                    except Exception:
                        pass
            elif self._ws_should_log(f"error:{api_key}"):
                self.log_message(f"[#ff0000]WebSocket error:[/#] @{name} ({code} {reason})")
        return on_error

    def _make_on_order_update(self, api_key: str):
        def on_order_update(ws, data):
            status = data.get("status")
            symbol = data.get("tradingsymbol")
            qty = data.get("quantity")
            filled = data.get("filled_quantity", 0)
            ord_type = data.get("transaction_type")
            name = self._get_account_name(api_key)
            qty_desc = f"{filled}/{qty}"
            self.log_message(f"[#00afaf]Order update [@{name}]:[/#] {ord_type} {qty_desc} {symbol} -> {status}")
            
            # Record order update to database
            if hasattr(self, "recorder"):
                user_id = self.api_key_to_user_id.get(api_key, "UNKNOWN")
                self.recorder.enqueue_order(data, self.index_values, user_id)

            # Trigger immediate refresh of positions and orders
            if hasattr(self, "app") and self.app and self.app.loop:
                asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)
        return on_order_update

    def _on_ticks(self, ws, ticks):
        updated = False
        indices_updated = False
        oc_updated = False
        for tick in ticks:
            token = tick.get("instrument_token")
            ltp = tick.get("last_price")
            if not token or ltp is None:
                continue

            # Market index tick → update the header values.
            if token in self.index_tokens:
                self.index_values[self.index_tokens[token]] = ltp
                indices_updated = True
                continue

            # Option-chain tick → update the matching strike's CE/PE LTP.
            if token in self.oc_token_map and self.oc_data:
                strike_val, side = self.oc_token_map[token]
                for s in self.oc_data.get("strikes", []):
                    if s.get("strike") == strike_val:
                        s[f"{side}_ltp"] = ltp
                        oc_updated = True
                        break
                continue

            if ltp:
                resp = getattr(self, "last_positions_response", None)
                if resp:
                    for acct in resp.get("accounts", []):
                        for pos in acct.get("positions", []):
                            pos_token = pos.get("instrument_token")
                            if pos_token is not None and int(pos_token) == int(token):
                                pos["last_price"] = ltp
                                # Recalculate P&L only for open positions (quantity != 0)
                                qty = pos.get("quantity", 0)
                                if qty != 0:
                                    avg_price = pos.get("average_price", 0.0)
                                    pos["pnl"] = (ltp - avg_price) * qty
                                    if avg_price > 0:
                                        pos["pnl_pct"] = (pos["pnl"] / (avg_price * abs(qty))) * 100
                                    else:
                                        pos["pnl_pct"] = 0.0
                                # For closed positions, realised P&L remains unchanged and is already set.
                                updated = True
                        
                        # Recalculate total P&L for account
                        acct["total_pnl"] = sum(p.get("pnl", 0.0) for p in acct.get("positions", []))
        
        if indices_updated and hasattr(self, "app") and self.app and self.app.loop:
            self.app.loop.call_soon_threadsafe(self._refresh_indices_header)

        if oc_updated and hasattr(self, "app") and self.app and self.app.loop:
            self.app.loop.call_soon_threadsafe(self._update_oc_and_invalidate)

        if updated:
            self._positions_version = getattr(self, "_positions_version", 0) + 1
            if hasattr(self, "app") and self.app and self.app.loop:
                self.app.loop.call_soon_threadsafe(self._update_display_and_invalidate)

    def _update_display_and_invalidate(self) -> None:
        """Helper to re-render positions and invalidate TUI."""
        self._update_positions_display()
        if hasattr(self, "app") and self.app:
            self.app.invalidate()

    def _refresh_indices_header(self) -> None:
        """Re-render the NIFTY/SENSEX/INDIA VIX header from ``self.index_values``
        and invalidate the TUI. Safe to call from the event loop thread."""
        self.market_indices_control.text = self._render_indices_html()
        if hasattr(self, "app") and self.app:
            self.app.invalidate()

    def _render_indices_html(self) -> HTML:
        """Build the formatted market-index header from the current values."""
        def fmt(v):
            return f"{v:,.2f}" if v else "N/A"

        nifty_str = fmt(self.index_values.get("nifty"))
        sensex_str = fmt(self.index_values.get("sensex"))
        vix_str = fmt(self.index_values.get("vix"))
        return HTML(
            f"  <ansicyan><b>NIFTY 50:</b></ansicyan> <style fg='#ffffff'>{nifty_str}</style>   "
            f"│   <ansiyellow><b>SENSEX:</b></ansiyellow> <style fg='#ffffff'>{sensex_str}</style>   "
            f"│   <ansired><b>INDIA VIX:</b></ansired> <style fg='#ffffff'>{vix_str}</style>"
        )

    def _get_active_ticker(self):
        """Get the primary ticker if it is connected, else fallback to any connected ticker."""
        ticker = None
        if self.primary_api_key and self.websocket_connected.get(self.primary_api_key):
            ticker = self.tickers.get(self.primary_api_key)
        
        if ticker is None:
            connected_keys = [k for k, conn in self.websocket_connected.items() if conn]
            for key in connected_keys:
                if key in self.tickers:
                    ticker = self.tickers[key]
                    break
        
        if ticker is None and self.tickers:
            ticker = next(iter(self.tickers.values()))
        return ticker

    def _update_subscriptions(self, new_tokens: set[int]) -> None:
        """Subscribe to position instrument tokens and unsubscribe inactive ones.

        Instrument prices are global (the same LTP regardless of which account
        holds the position), so we stream each token on the **primary** ticker
        only — not on every account — to avoid receiving the same price tick
        once per account. The tick handler (`_on_ticks`) then applies each
        price to all accounts' matching positions, so per-account positions and
        P&L remain fully independent.
        """
        to_subscribe = new_tokens - self.subscribed_tokens
        to_unsubscribe = self.subscribed_tokens - new_tokens
        # Never unsubscribe the market index tokens — they must keep streaming.
        to_unsubscribe -= set(self.index_tokens)
        # Keep tokens still needed by the live option chain subscribed.
        to_unsubscribe -= set(self.oc_subscribed_tokens)

        # Stream position prices on the active ticker
        ticker = self._get_active_ticker()

        if to_subscribe and ticker is not None:
            try:
                ticker.subscribe(list(to_subscribe))
                ticker.set_mode(ticker.MODE_LTP, list(to_subscribe))
            except Exception:
                pass
        # Track intent even if no ticker is connected yet; on_connect re-subscribes.
        self.subscribed_tokens.update(to_subscribe)

        if to_unsubscribe:
            if ticker is not None:
                try:
                    ticker.unsubscribe(list(to_unsubscribe))
                except Exception:
                    pass
            self.subscribed_tokens.difference_update(to_unsubscribe)

    async def _trigger_immediate_refresh(self) -> None:
        """Trigger an immediate fetch of positions, orders, margins, and indices from Zerodha."""
        api_keys = [acct["api_key"] for acct in self.accounts]

        # Fetch positions, margins, orders, and indices concurrently.
        positions_task = self._run_api_call(self.client.get_positions, api_keys)
        margins_task   = self._run_api_call(self.client.get_margins, api_keys)
        orders_task    = self._run_api_call(self.client.get_orders, api_keys)
        indices_task   = self._run_api_call(self.client.get_market_indices)

        response, margins_resp, orders_resp, indices_resp = await asyncio.gather(
            positions_task, margins_task, orders_task, indices_task,
            return_exceptions=True,
        )

        # Merge margin data into the positions response so the renderer
        # can display it without a separate lookup.
        if isinstance(margins_resp, dict):
            margin_map = {m["api_key"]: m for m in margins_resp.get("accounts", [])}
            self.margins_by_api_key = margin_map
            if isinstance(response, dict):
                for acct in response.get("accounts", []):
                    key = acct.get("api_key", "")
                    m = margin_map.get(key, {})
                    acct["margin_net"]  = m.get("net")
                    acct["margin_cash"] = m.get("cash")

        if isinstance(response, dict):
            self.last_positions_response = response
            self._positions_version = getattr(self, "_positions_version", 0) + 1

            self.active_positions = []
            self.position_id_map = {}
            pos_idx = 1
            new_tokens = set()
            for acct in response.get("accounts", []):
                for pos in acct.get("positions", []):
                    if pos.get("quantity", 0) != 0:
                        pos["api_key"] = acct.get("api_key")
                        pos["account_name"] = acct.get("name")
                        # Annotate with lot size so the display layer can show lots.
                        # get_nfo_lot_sizes() returns the kite_manager internal cache
                        # after the first fetch — no repeated API calls.
                        sym = pos.get("tradingsymbol", "")
                        lot_size = self.client.get_nfo_lot_sizes().get(sym, 1)
                        pos["lot_size"] = lot_size
                        pos["lots"] = pos.get("quantity", 0) / lot_size
                        self.active_positions.append(pos)
                        self.position_id_map[pos_idx] = pos
                        pos_idx += 1
                        if pos.get("instrument_token"):
                            new_tokens.add(int(pos["instrument_token"]))

            self._update_subscriptions(new_tokens)

            # Record position snapshots to database
            if hasattr(self, "recorder"):
                for acct in response.get("accounts", []):
                    api_key = acct.get("api_key", "")
                    user_id = self.api_key_to_user_id.get(api_key, "UNKNOWN")
                    positions = acct.get("positions", [])
                    self.recorder.enqueue_positions(positions, self.index_values, user_id)

        if isinstance(orders_resp, dict):
            self.last_orders_response = orders_resp
            self._last_pending_text  = self._render_orders_pane(orders_resp, "orders_pending")
            self._last_executed_text = self._render_orders_pane(orders_resp, "orders_executed")
            if self.info_mode in ("orders_pending", "orders_executed"):
                self._update_info_buffer(reset_scroll=False)

        # Indices REST snapshot (WebSocket ticks keep these live thereafter).
        try:
            if isinstance(indices_resp, dict) and indices_resp.get("status") == "success":
                self.index_values["nifty"]  = indices_resp.get("nifty")
                self.index_values["sensex"] = indices_resp.get("sensex")
                self.index_values["vix"]    = indices_resp.get("vix")
                self.market_indices_control.text = self._render_indices_html()
            elif isinstance(indices_resp, dict):
                msg = indices_resp.get("message", "Unknown error")
                self.market_indices_control.text = HTML(f"  <style fg='#ff5f5f'>Indices: {msg}</style>")
        except Exception as exc:
            self.market_indices_control.text = HTML(f"  <style fg='#ff5f5f'>Indices Error: {exc}</style>")

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
            if self.selected_account_api_key and pos.get("api_key") != self.selected_account_api_key:
                continue
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

    def _find_all_matching_pending_orders(self, query: str) -> list[tuple[dict, dict]]:
        """Find all pending orders in self.last_orders_response matching an ID, suffix, or symbol."""
        if not getattr(self, "last_orders_response", None):
            return []

        PENDING_STATUSES = {"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "PUT ORDER REQ RECEIVED"}
        matches = []
        
        normalized_query = query.strip().upper()
        if not normalized_query:
            return []

        for acct in self.last_orders_response.get("accounts", []):
            for o in acct.get("orders", []):
                if o.get("status", "").upper() in PENDING_STATUSES:
                    order_id = str(o.get("order_id", ""))
                    symbol = str(o.get("tradingsymbol", "")).upper()
                    
                    # 1. Exact or suffix match on order ID
                    if order_id == query or order_id.endswith(query) or (len(query) >= 4 and query in order_id):
                        matches.append((acct, o))
                    # 2. Match on trading symbol (exact match or query is substring of symbol)
                    elif symbol == normalized_query or normalized_query in symbol:
                        matches.append((acct, o))
                        
        return matches

    def _find_pending_order(self, query: str) -> tuple[tuple[dict, dict] | None, str | None]:
        """Find a pending order in self.last_orders_response by ID or suffix.

        Returns:
            ((account, order), None) if a unique match is found,
            (None, error_message) otherwise.
        """
        if not getattr(self, "last_orders_response", None):
            return None, "No orders cache available. Please wait for a refresh."

        PENDING_STATUSES = {"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "PUT ORDER REQ RECEIVED"}
        matches = []
        
        # Normalize the query (strip whitespace)
        normalized_query = query.strip()
        if not normalized_query:
            return None, "Empty order search query."

        for acct in self.last_orders_response.get("accounts", []):
            for o in acct.get("orders", []):
                if o.get("status", "").upper() in PENDING_STATUSES:
                    order_id = str(o.get("order_id", ""))
                    # Check exact match, ends with match, or general substring
                    if order_id == normalized_query or order_id.endswith(normalized_query) or (len(normalized_query) >= 4 and normalized_query in order_id):
                        matches.append((acct, o))

        if not matches:
            return None, f"No pending order matches '{query}'."

        if len(matches) > 1:
            # Check if there is an exact endswith match with the suffix
            exact_matches = [m for m in matches if m[1].get("order_id", "").endswith(normalized_query)]
            if len(exact_matches) == 1:
                return exact_matches[0], None
            
            desc = ", ".join(m[1].get("order_id") for m in matches)
            return None, f"Multiple pending orders match '{query}': {desc}. Please be more specific."

        return matches[0], None

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




    def _resolve_qty(self, qty_arg: str, symbol: str | None) -> tuple[str, str, str | None]:
        """Resolve a quantity argument that may use the lot suffix (e.g. '2L').

        Args:
            qty_arg: Raw qty string from the command line, e.g. '2L', '75', '1L'.
            symbol:  Trading symbol used to look up lot_size from the cache.

        Returns:
            (raw_qty_str, display_str, error)
            - raw_qty_str: plain integer string ready for execute_order, e.g. '150'
            - display_str: human-friendly string shown in confirmation, e.g. '2L (150 qty)'
            - error: non-None string if the input is invalid.
        """
        cleaned = qty_arg.strip()
        if cleaned.upper().endswith("L"):
            # Lot-based quantity
            lots_str = cleaned[:-1]
            if not lots_str.isdigit() or int(lots_str) <= 0:
                return "", "", f"Invalid lot quantity '{qty_arg}'. Use e.g. '2L' for 2 lots."
            lots = int(lots_str)
            lot_size = 1
            if symbol:
                lot_size = self.client.get_nfo_lot_sizes().get(symbol.upper(), 0)
                if not lot_size:
                    return "", "", (
                        f"No lot size found for '{symbol}'. "
                        "Use raw quantity (e.g. '75') for equity symbols."
                    )
            raw_qty = lots * lot_size
            return str(raw_qty), f"{lots}L ({raw_qty} qty)", None
        else:
            # Plain raw quantity — backward compatible
            if not cleaned.isdigit() or int(cleaned) <= 0:
                return "", "", f"Invalid quantity '{qty_arg}'. Must be a positive integer or lot notation (e.g. '2L')."
            return cleaned, cleaned, None

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
                legs = res.get("legs", 1)
                ord_id = res.get("order_id")
                if status == "success":
                    if legs > 1:
                        self.log_message(f"[#00ff00]✓ {name}:[/#] Split into {legs} legs. IDs: {ord_id}")
                    else:
                        self.log_message(f"[#00ff00]✓ {name}:[/#] Placed. ID: {ord_id}")
                else:
                    self.log_message(f"[#ff0000]✗ {name}:[/#] Failed — {msg}")

        except KCLIClientError as exc:
            self.log_message(f"[#ff0000]Order Execution Failed:[/#] {exc}")
        except Exception as exc:
            self.log_message(f"[#ff0000]Unexpected Error:[/#] {exc}")

    async def execute_exit(self, symbol: str = None, api_keys: list[str] = [], price: float | None = None) -> None:
        """Execute exit of positions across specified accounts in executor."""
        if symbol and symbol.lower() == "all":
            symbol = None
        accts_desc = f"account {self.selected_account_name}" if (api_keys and hasattr(self, "selected_account_name") and self.selected_account_name) else "all accounts"
        price_desc = f" @{price:.2f}" if price is not None else ""
        if symbol:
            self.log_message(f"Exiting open positions for {symbol} across {accts_desc}{price_desc}...")
        else:
            self.log_message(f"Exiting ALL open positions across {accts_desc}{price_desc}...")

        try:
            # Place exit request on server
            response = await self._run_api_call(
                self.client.exit_positions,
                api_keys=api_keys,
                tradingsymbol=symbol,
                price=price,
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

    async def execute_modify(self, order_id: str, qty_str: str, price_str: str, api_key: str) -> None:
        """Modify open order in executor."""
        # 1. Validate quantity
        try:
            qty = int(qty_str)
            if qty <= 0:
                raise ValueError("Quantity must be positive.")
        except ValueError:
            self.log_message(f"[#ff0000]Error:[/#] Invalid quantity '{qty_str}'. Must be a positive integer.")
            return

        # 2. Parse price
        price = None
        order_type = "LIMIT"
        if price_str:
            try:
                price = float(price_str)
                if price <= 0:
                    price = None
                    order_type = "MARKET"
            except ValueError:
                self.log_message(f"[#ff0000]Error:[/#] Invalid price '{price_str}'. Must be a positive float.")
                return

        self.log_message(f"Modifying order {order_id} to quantity {qty} @ {price or 'MARKET'}...")

        try:
            response = await self._run_api_call(
                self.client.modify_order,
                api_key=api_key,
                order_id=order_id,
                quantity=qty,
                price=price,
                order_type=order_type,
            )
            
            status = response.get("status", "error")
            msg = response.get("message", "")
            if status == "success":
                self.log_message(f"[#00ff00]✓ Order modified successfully.[/#] ID: {order_id}")
                # Clear order selection after successful modification
                if self.selected_order and self.selected_order.get("order_id") == order_id:
                    self.selected_order = None
                    self.update_prompt_label()
                # Trigger a refresh to show updated orders/positions
                if hasattr(self, "app") and self.app and self.app.loop:
                    asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)
            else:
                self.log_message(f"[#ff0000]✗ Modify failed:[/#] {msg}")

        except Exception as exc:
            self.log_message(f"[#ff0000]Modify Execution Failed:[/#] {exc}")

    async def execute_cancel(self, order_id: str, api_key: str) -> None:
        """Cancel open order in executor."""
        self.log_message(f"Cancelling order {order_id}...")

        try:
            response = await self._run_api_call(
                self.client.cancel_order,
                api_key=api_key,
                order_id=order_id,
            )
            
            status = response.get("status", "error")
            msg = response.get("message", "")
            if status == "success":
                self.log_message(f"[#00ff00]✓ Order cancelled successfully.[/#] ID: {order_id}")
                # Clear order selection after successful cancellation
                if self.selected_order and self.selected_order.get("order_id") == order_id:
                    self.selected_order = None
                    self.update_prompt_label()
                # Trigger a refresh to show updated orders/positions
                if hasattr(self, "app") and self.app and self.app.loop:
                    asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)
            else:
                self.log_message(f"[#ff0000]✗ Cancel failed:[/#] {msg}")

        except Exception as exc:
            self.log_message(f"[#ff0000]Cancel Execution Failed:[/#] {exc}")

    def _log_command(self, cmd_text: str, action: dict | str, status: str, result: str | None = None, api_key: str | None = None) -> None:
        if not hasattr(self, "recorder"):
            return
        user_id = self.api_key_to_user_id.get(api_key) if api_key else None
        parsed_str = json.dumps(action) if isinstance(action, dict) else str(action)
        self.recorder.enqueue_command(
            command_text=cmd_text,
            parsed_action=parsed_str,
            status=status,
            result_message=result,
            index_values=self.index_values,
            user_id=user_id,
        )

    def handle_input(self, buffer) -> None:
        """Process entered command line with error safety wrapper."""
        try:
            return self._handle_input_core(buffer)
        except Exception as exc:
            self.log_message(f"[#ff0000]Error processing input:[/#] {exc}")
            logger.error("Error processing input: %s", exc, exc_info=True)

    def _handle_input_core(self, buffer) -> None:
        """Process entered command line, supporting command chaining via &&."""
        raw_text = buffer.text.strip()
        if not raw_text:
            return

        # Support command chaining via '&&'
        commands = [c.strip() for c in raw_text.split("&&") if c.strip()]
        for cmd in commands:
            self._execute_single_command(cmd)

    def _execute_single_command(self, cmd: str) -> None:
        """Process a single command line."""
        if not cmd:
            return

        # Check if we are waiting for confirmation of a pending order
        if hasattr(self, "pending_order") and self.pending_order:
            ans = cmd.lower().strip()
            p = self.pending_order
            
            # Reset confirmation state upfront so nested commands are not intercepted
            self.pending_order = None
            self.update_prompt_label()

            if ans in ("y", "yes"):
                self.log_message("[#00ff00]Order Confirmed.[/#]")
                self._log_command(
                    cmd_text=f"confirm: {ans}",
                    action={"confirmed_action": p},
                    status="success",
                    api_key=p.get("api_key") if p.get("api_key") else (p.get("api_keys")[0] if p.get("api_keys") else None),
                )
                if p["type"] == "exit":
                    asyncio.create_task(self.execute_exit(p["symbol"], p.get("api_keys", []), p.get("price")))
                elif p["type"] == "exit_near_week":
                    for sym in p["symbols"]:
                        asyncio.create_task(self.execute_exit(sym, p.get("api_keys", []), p.get("price")))
                elif p["type"] == "modify":
                    asyncio.create_task(
                        self.execute_modify(
                            p["order_id"],
                            p["qty"],
                            p["price"],
                            p["api_key"],
                        )
                    )
                elif p["type"] == "modify_multi":
                    for order_info in p["orders"]:
                        asyncio.create_task(
                            self.execute_modify(
                                order_info["order_id"],
                                p["qty"],
                                p["price"],
                                order_info["api_key"],
                            )
                        )
                elif p["type"] == "cancel":
                    asyncio.create_task(
                        self.execute_cancel(
                            p["order_id"],
                            p["api_key"],
                        )
                    )
                elif p["type"] == "cancel_multi":
                    for order_info in p["orders"]:
                        asyncio.create_task(
                            self.execute_cancel(
                                order_info["order_id"],
                                order_info["api_key"],
                            )
                        )
                elif p["type"] == "nli_command":
                    self.log_message(f"Executing NLI Command: [bold]{p['command']}[/bold]")
                    self._skip_confirmation = True
                    try:
                        for sub_cmd in p["command"].split(" && "):
                            sub_cmd = sub_cmd.strip()
                            if sub_cmd:
                                self._execute_single_command(sub_cmd)
                    finally:
                        self._skip_confirmation = False
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
                self._log_command(
                    cmd_text=f"confirm: {ans}",
                    action={"cancelled_action": p},
                    status="cancelled",
                    api_key=p.get("api_key") if p.get("api_key") else (p.get("api_keys")[0] if p.get("api_keys") else None),
                )
            return

        if cmd.startswith("/"):
            nli_text = cmd[1:].strip()
            if nli_text:
                asyncio.create_task(self.resolve_nli_command(nli_text))
            return

        parts = cmd.split()
        primary_cmd = parts[0].lower()

        if primary_cmd == "quit" and len(parts) == 1:
            self._log_command(cmd, "quit", "success")
            self.running = False
            self.app.exit()
            return

        if primary_cmd == "exit" and len(parts) == 1:
            if hasattr(self, "selected_symbol") and self.selected_symbol:
                target_keys = [self.selected_account_api_key] if self.selected_account_api_key else []
                self._log_command(
                    cmd_text=cmd,
                    action={"action": "exit_selected_symbol", "symbol": self.selected_symbol},
                    status="success",
                    api_key=self.selected_account_api_key,
                )
                asyncio.create_task(self.execute_exit(self.selected_symbol, target_keys))
                return
            else:
                self._log_command(cmd, "quit_via_exit", "success")
                self.running = False
                self.app.exit()
                return

        if primary_cmd == "clear":
            self._log_command(cmd, "clear", "success")
            self.logs = []
            self.logs_control.text = ANSI("")
            return

        if primary_cmd == "help":
            self._log_command(cmd, "help", "success")
            self.log_message("[#00afaf]Available Commands:[/#]")
            self.log_message("  [bold]buy / sell [symbol|id] <qty|lotsL> [price] [product][/bold]")
            self.log_message("    e.g. [bold]sell 2L[/bold]               (2 lots of selected position)")
            self.log_message("    e.g. [bold]sell 3 2L[/bold]             (2 lots of position ID 3)")
            self.log_message("    e.g. [bold]sell NIFTY25JUN24000CE 1L[/bold]  (1 lot by symbol)")
            self.log_message("    e.g. [bold]buy 75[/bold]                (75 raw qty, backward-compatible)")
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
            self.log_message("  [bold]refresh[/bold] - Trigger manual sync of positions/orders")
            self.log_message("  [bold]reconnect[/bold] - Restart all WebSocket connections")
            self.log_message("  [bold]clear[/bold] - Clear logs screen")
            self.log_message("  [bold]quit / exit[/bold] - Close dashboard")
            self.log_message("[#00afaf]Right Pane (Info Panel):[/#]")
            self.log_message("  [bold]F1[/bold] — Pending Orders (per account, real-time)")
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
            self._log_command(cmd, "refresh", "success")
            self.log_message("Triggering manual refresh...")
            if hasattr(self, "app") and self.app and self.app.loop:
                asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)
            return

        if primary_cmd == "reconnect":
            self._log_command(cmd, "reconnect", "success")
            if hasattr(self, "app") and self.app and self.app.loop:
                asyncio.run_coroutine_threadsafe(self.reconnect_websockets(), self.app.loop)
            return

        # Deselect Command
        if primary_cmd == "deselect":
            self._log_command(cmd, "deselect", "success")
            self.selected_symbol = None
            self.selected_account_name = None
            self.selected_account_api_key = None
            self.selected_order = None
            self.update_prompt_label()
            self.log_message("Selection cleared.")
            return

        # Select Position / Account / Order Command
        if primary_cmd in ("select", "s"):
            if len(parts) < 2:
                self.log_message("[#ff0000]Usage:[/#] select <id|none> OR select order <id_suffix|none> OR select account <name|index|none>")
                return
            
            raw_id = parts[1]
            
            # Check if selecting an account: select account <query> or s a <query>
            if raw_id.lower() in ("account", "acct", "a"):
                if len(parts) < 3:
                    self.log_message("[#ff0000]Usage:[/#] select account <name|index|none>")
                    return
                raw_acc = " ".join(parts[2:])
                if raw_acc.lower() in ("none", "clear", "null", "empty", "all"):
                    self._log_command(cmd, {"action": "clear_account_selection"}, "success")
                    self.selected_account_name = None
                    self.selected_account_api_key = None
                    self.selected_order = None
                    self.update_prompt_label()
                    self.log_message("Account selection cleared (orders will target all accounts).")
                    return
                
                acct, err = self.resolve_account(raw_acc)
                if err:
                    self._log_command(cmd, {"action": "select_account", "query": raw_acc}, "error", err)
                    self.log_message(f"[#ff0000]Error:[/#] {err}")
                    return
                
                self.selected_account_name = acct.get("name")
                self.selected_account_api_key = acct.get("api_key")
                self.selected_order = None
                self.update_prompt_label()
                self._log_command(cmd, {"action": "select_account", "name": self.selected_account_name}, "success", api_key=self.selected_account_api_key)
                self.log_message(f"Selected account: [bold]{self.selected_account_name}[/bold]. Orders will target this account only.")
                return

            # Check if selecting an order: select order <id_suffix> or s o <id_suffix>
            if raw_id.lower() in ("order", "ord", "o"):
                if len(parts) < 3:
                    self.log_message("[#ff0000]Usage:[/#] select order <id_suffix|none>")
                    return
                raw_ord = parts[2]
                if raw_ord.lower() in ("none", "clear", "null", "empty"):
                    self._log_command(cmd, {"action": "clear_order_selection"}, "success")
                    self.selected_order = None
                    self.update_prompt_label()
                    self.log_message("Order selection cleared.")
                    return
                
                acct_and_order, err = self._find_pending_order(raw_ord)
                if err:
                    self._log_command(cmd, {"action": "select_order", "query": raw_ord}, "error", err)
                    self.log_message(f"[#ff0000]Error:[/#] {err}")
                    return
                
                acct, order = acct_and_order
                self.selected_order = {
                    "order_id": order.get("order_id"),
                    "tradingsymbol": order.get("tradingsymbol"),
                    "transaction_type": order.get("transaction_type"),
                    "quantity": order.get("quantity"),
                    "price": order.get("price"),
                    "order_type": order.get("order_type"),
                    "product": order.get("product"),
                    "api_key": acct.get("api_key"),
                    "account_name": acct.get("name")
                }
                
                # Clear active symbol selection to avoid confusion
                self.selected_symbol = None
                
                self.update_prompt_label()
                self._log_command(cmd, {"action": "select_order", "order": self.selected_order}, "success", api_key=self.selected_order["api_key"])
                self.log_message(f"Selected pending order [bold]{self.selected_order['order_id']}[/bold] ({self.selected_order['tradingsymbol']} | {self.selected_order['transaction_type']} | {self.selected_order['quantity']} @ {self.selected_order['price']:.2f}) on @{self.selected_order['account_name']}.")
                return
                
            if raw_id.lower() in ("none", "clear", "null", "empty"):
                self._log_command(cmd, {"action": "clear_position_selection"}, "success")
                self.selected_symbol = None
                self.selected_account_name = None
                self.selected_account_api_key = None
                self.selected_order = None
                self.update_prompt_label()
                self.log_message("Selection cleared.")
                return

            symbol, api_key, err = self.resolve_symbol(raw_id)
            if err:
                self._log_command(cmd, {"action": "select_position", "query": raw_id}, "error", err)
                self.log_message(f"[#ff0000]Error:[/#] {err}")
                return
            
            self.selected_symbol = symbol
            self.selected_order = None
            if api_key:
                self.selected_account_api_key = api_key
                for acct in self.accounts:
                    if acct.get("api_key") == api_key:
                        self.selected_account_name = acct.get("name")
                        break
                
            self.update_prompt_label()
            self._log_command(
                cmd_text=cmd,
                action={"action": "select_position", "symbol": symbol, "api_key": api_key},
                status="success",
                api_key=api_key,
            )
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
                self.selected_order = None
                self.update_prompt_label()
                self.log_message("Account selection cleared (orders will target all accounts).")
                return
            
            acct, err = self.resolve_account(raw_acc)
            if err:
                self.log_message(f"[#ff0000]Error:[/#] {err}")
                return
            
            self.selected_account_name = acct.get("name")
            self.selected_account_api_key = acct.get("api_key")
            self.selected_order = None
            self.update_prompt_label()
            self.log_message(f"Selected account: [bold]{self.selected_account_name}[/bold]. Orders will target this account only.")
            return

        # Modify Order Command: order <id_suffix|full_id|symbol> <quantity|lotsL> <price>
        if primary_cmd == "order":
            if len(parts) < 4:
                self.log_message("[#ff0000]Usage:[/#] order <id_suffix|full_id|symbol> <quantity|lotsL> <price>")
                return
            
            raw_id_or_sym = parts[1]
            qty_str = parts[2]
            price_str = parts[3]
            
            # Find all matching pending orders
            matches = self._find_all_matching_pending_orders(raw_id_or_sym)
            if not matches:
                self.log_message(f"[#ff0000]Error:[/#] No pending order matches ID/suffix/symbol '{raw_id_or_sym}'.")
                return

            # Resolve quantity with lot size if it has L
            # Use the first matched order's symbol for lot-size resolution helper
            first_acct, first_order = matches[0]
            first_sym = first_order.get("tradingsymbol")
            raw_qty_str, qty_display, qty_err = self._resolve_qty(qty_str, first_sym)
            if qty_err:
                self.log_message(f"[#ff0000]Error:[/#] {qty_err}")
                return
                
            # Parse price
            try:
                price_val = float(price_str)
            except ValueError:
                self.log_message(f"[#ff0000]Error:[/#] Price '{price_str}' must be a float.")
                return
                
            price_desc = f"@{price_val:.2f}" if price_val > 0 else "MARKET"

            # If there is exactly 1 match, run standard single order modify
            if len(matches) == 1:
                acct, order = matches[0]
                order_id = order.get("order_id")
                symbol = order.get("tradingsymbol")
                api_key = acct.get("api_key")
                self.pending_order = {
                    "type": "modify",
                    "order_id": order_id,
                    "qty": raw_qty_str,
                    "price": price_str,
                    "api_key": api_key,
                    "symbol": symbol
                }
                self._log_command(
                    cmd_text=cmd,
                    action=self.pending_order,
                    status="pending_confirmation",
                    api_key=api_key,
                )
                self.prompt_control.text = f" Confirm MODIFY order {order_id[-6:]} to {qty_display} {symbol} {price_desc}? (y/n)> "
                self.log_message(f"[#ff8700]Pending Confirmation:[/#] MODIFY order {order_id} ({symbol}) to {qty_display} {price_desc}. Press [bold]y[/bold] to confirm, any other key to cancel.")
            else:
                # Multi-order modify
                self.pending_order = {
                    "type": "modify_multi",
                    "orders": [
                        {
                            "order_id": o.get("order_id"),
                            "api_key": a.get("api_key"),
                            "account_name": a.get("name"),
                            "symbol": o.get("tradingsymbol")
                        }
                        for a, o in matches
                    ],
                    "qty": raw_qty_str,
                    "price": price_str
                }
                self._log_command(
                    cmd_text=cmd,
                    action=self.pending_order,
                    status="pending_confirmation",
                    api_key=None,
                )
                accts_list = ", ".join(f"@{o['account_name']}" for o in self.pending_order["orders"])
                self.prompt_control.text = f" Confirm MODIFY {len(matches)} orders ({first_sym}) to {qty_display} {price_desc} on {accts_list}? (y/n)> "
                self.log_message(
                    f"[#ff8700]Pending Confirmation:[/#] MODIFY {len(matches)} pending orders for {first_sym} "
                    f"to {qty_display} {price_desc} on accounts {accts_list}. Press [bold]y[/bold] to confirm, any other key to cancel."
                )
            return

        # Cancel Order Command: cancel [id_suffix|full_id|symbol]
        if primary_cmd == "cancel":
            target_id_or_sym = None
            if len(parts) >= 2:
                target_id_or_sym = parts[1]
            elif self.selected_order:
                target_id_or_sym = self.selected_order.get("order_id")
                
            if not target_id_or_sym:
                self.log_message("[#ff0000]Usage:[/#] cancel <id_suffix|full_id|symbol> (or select a pending order first)")
                return
                
            matches = self._find_all_matching_pending_orders(target_id_or_sym)
            if not matches:
                self.log_message(f"[#ff0000]Error:[/#] No pending order matches ID/suffix/symbol '{target_id_or_sym}'.")
                return
                
            if len(matches) == 1:
                acct, order = matches[0]
                order_id = order.get("order_id")
                symbol = order.get("tradingsymbol")
                api_key = acct.get("api_key")
                qty = order.get("quantity")
                price = order.get("price")
                tx_type = order.get("transaction_type")
                price_desc = f"@{price:.2f}" if price else "MARKET"
                
                self.pending_order = {
                    "type": "cancel",
                    "order_id": order_id,
                    "api_key": api_key,
                    "symbol": symbol
                }
                self._log_command(
                    cmd_text=cmd,
                    action=self.pending_order,
                    status="pending_confirmation",
                    api_key=api_key,
                )
                self.prompt_control.text = f" Confirm CANCEL order {order_id[-6:]} ({tx_type} {qty} {symbol} {price_desc})? (y/n)> "
                self.log_message(f"[#ff8700]Pending Confirmation:[/#] CANCEL order {order_id} ({tx_type} {qty} {symbol} {price_desc}). Press [bold]y[/bold] to confirm, any other key to cancel.")
            else:
                self.pending_order = {
                    "type": "cancel_multi",
                    "orders": [
                        {
                            "order_id": o.get("order_id"),
                            "api_key": a.get("api_key"),
                            "account_name": a.get("name"),
                            "symbol": o.get("tradingsymbol")
                        }
                        for a, o in matches
                    ]
                }
                self._log_command(
                    cmd_text=cmd,
                    action=self.pending_order,
                    status="pending_confirmation",
                    api_key=None,
                )
                first_sym = matches[0][1].get("tradingsymbol")
                accts_list = ", ".join(f"@{o['account_name']}" for o in self.pending_order["orders"])
                self.prompt_control.text = f" Confirm CANCEL {len(matches)} orders ({first_sym}) on {accts_list}? (y/n)> "
                self.log_message(
                    f"[#ff8700]Pending Confirmation:[/#] CANCEL {len(matches)} pending orders for {first_sym} "
                    f"on accounts {accts_list}. Press [bold]y[/bold] to confirm, any other key to cancel."
                )
            return

        # Direct Buy/Sell Commands
        if primary_cmd in ("buy", "sell"):
            args = parts[1:]
            if not args:
                self.log_message(f"[#ff0000]Usage:[/#] {primary_cmd} [symbol|id] <qty|lotsL> [price] [product]")
                self.log_message(f"  e.g. [bold]{primary_cmd} 2L[/bold]                  — 2 lots of selected position")
                self.log_message(f"  e.g. [bold]{primary_cmd} 3 2L[/bold]                — 2 lots of position #3")
                self.log_message(f"  e.g. [bold]{primary_cmd} NIFTY24DEC24000CE 1L[/bold] — 1 lot by symbol")
                return

            symbol = None
            api_key = None
            qty_str = None
            price_str = None
            product = "NRML"

            def _is_qty_token(s):
                """Return True if s looks like a qty: plain int or NL notation."""
                return s.isdigit() or (s.upper().endswith("L") and s[:-1].isdigit())

            # Case 1: First argument is a valid position ID (plain integer < 100,
            #         exists in position_id_map). Must be plain digit, not e.g. '2L'.
            if args[0].isdigit() and int(args[0]) < 100 and hasattr(self, "position_id_map") and int(args[0]) in self.position_id_map:
                symbol, api_key, err = self.resolve_symbol(args[0])
                pos = self.position_id_map.get(int(args[0]))
                if len(args) < 2:
                    # Default to full position size, expressed in lots when possible
                    if pos:
                        lot_size = pos.get("lot_size", 1) or 1
                        raw_qty = abs(pos.get("quantity", 0))
                        qty_str = f"{raw_qty // lot_size}L" if lot_size > 1 and raw_qty % lot_size == 0 else str(raw_qty)
                        self.log_message(f"Omitted quantity. Defaulting to position size {qty_str}.")
                    else:
                        self.log_message(f"[#ff0000]Usage:[/#] {primary_cmd} <id> <qty|lotsL> [price] [product]")
                        return
                else:
                    qty_str = args[1]
                    if len(args) > 2:
                        price_str = args[2]
                    if len(args) > 3:
                        product = args[3]

            # Case 1b: Active selection exists and first argument is a qty token
            elif _is_qty_token(args[0]) and hasattr(self, "selected_symbol") and self.selected_symbol:
                symbol = self.selected_symbol
                api_key = self.selected_account_api_key
                qty_str = args[0]
                if len(args) > 1:
                    price_str = args[1]
                if len(args) > 2:
                    product = args[2]

            else:
                # Case 2: Parse symbol and quantity.
                # Scan right-to-left for a qty token to avoid strike prices/dates.
                # IMPORTANT: NL tokens (e.g. '2L') take priority over plain integers
                # to their right (which are likely prices). So first try to find an NL
                # token; only fall back to plain-integer scan if none found.
                qty_idx = -1

                # Pass 1: look for rightmost NL token (e.g. '2L', '1L')
                for i in range(len(args) - 1, -1, -1):
                    arg = args[i]
                    if arg.upper().endswith("L") and arg[:-1].isdigit():
                        qty_idx = i
                        break

                # Pass 2: if no NL token, fall back to rightmost plain integer
                if qty_idx == -1:
                    for i in range(len(args) - 1, -1, -1):
                        arg = args[i]
                        if arg.isdigit():
                            # Skip strike prices: followed by CE/PE/FUT
                            if i + 1 < len(args) and args[i+1].upper() in ("CE", "PE", "FUT"):
                                continue
                            qty_idx = i
                            break

                if qty_idx != -1:
                    qty_str = args[qty_idx]

                    if qty_idx == 0:
                        # No symbol specified — use currently selected symbol
                        if hasattr(self, "selected_symbol") and self.selected_symbol:
                            symbol = self.selected_symbol
                            api_key = self.selected_account_api_key
                        else:
                            self.log_message(f"[#ff0000]Error:[/#] No position selected. Type 'select <id>' first or specify a symbol.")
                            return
                    else:
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
                    # No qty token — check if entire args is a symbol with a known position
                    raw_sym = " ".join(args)
                    symbol, api_key, err = self.resolve_symbol(raw_sym)
                    if err:
                        self.log_message(f"[#ff0000]Error:[/#] {err}")
                        return

                    matched_pos = None
                    normalized_sym = symbol.replace(" ", "").upper()
                    for pos in getattr(self, "active_positions", []):
                        if pos.get("tradingsymbol", "").replace(" ", "").upper() == normalized_sym:
                            matched_pos = pos
                            break

                    if matched_pos:
                        lot_size = matched_pos.get("lot_size", 1) or 1
                        raw_qty = abs(matched_pos.get("quantity", 0))
                        qty_str = f"{raw_qty // lot_size}L" if lot_size > 1 and raw_qty % lot_size == 0 else str(raw_qty)
                        api_key = matched_pos.get("api_key")
                        self.log_message(f"Omitted quantity. Defaulting to position size {qty_str}.")
                    else:
                        self.log_message(f"[#ff0000]Error:[/#] Missing quantity. Usage: {primary_cmd} [symbol|id] <qty|lotsL> [price] [product]")
                        return

            # Resolve lot notation → raw qty
            raw_qty_str, qty_display, qty_err = self._resolve_qty(qty_str, symbol)
            if qty_err:
                self.log_message(f"[#ff0000]Error:[/#] {qty_err}")
                return

            target_key = api_key or self.selected_account_api_key
            target_keys = [target_key] if target_key else []

            if getattr(self, "_skip_confirmation", False):
                asyncio.create_task(
                    self.execute_order(
                        symbol,
                        primary_cmd,
                        raw_qty_str,
                        price_str,
                        product,
                        api_keys=target_keys
                    )
                )
                return

            # Set pending order for confirmation (store resolved raw qty)
            self.pending_order = {
                "symbol": symbol,
                "type": primary_cmd,
                "qty": raw_qty_str,
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

            # Log pending buy/sell command
            self._log_command(
                cmd_text=cmd,
                action=self.pending_order,
                status="pending_confirmation",
                api_key=target_keys[0] if len(target_keys) == 1 else None,
            )

            price_desc = f"at price {price_str}" if price_str else "at MARKET"
            self.prompt_control.text = f" Confirm {primary_cmd.upper()} {qty_display} {symbol} ({product}) {price_desc} on {accts_desc}? (y/n)> "
            self.log_message(f"[#ff8700]Pending Confirmation:[/#] {primary_cmd.upper()} {qty_display} {symbol} ({product}) {price_desc} on {accts_desc}. Press [bold]y[/bold] to confirm, any other key to cancel.")
            return
            return

        # Exit Positions Command
        if primary_cmd == "exit":
            price_val = None
            if len(parts) < 2:
                if hasattr(self, "selected_symbol") and self.selected_symbol:
                    raw_symbol = self.selected_symbol
                else:
                    self.log_message("[#ff0000]Usage:[/#] exit <symbol|id> [price] OR exit all [price]")
                    return
            else:
                last_part = parts[-1]
                is_numeric = False
                try:
                    if last_part.replace(".", "", 1).isdigit() and not last_part.isalpha():
                        is_numeric = True
                except Exception:
                    pass

                if is_numeric:
                    if len(parts) == 2 and hasattr(self, "selected_symbol") and self.selected_symbol:
                        price_val = float(last_part)
                        raw_symbol = self.selected_symbol
                    elif len(parts) >= 3:
                        price_val = float(last_part)
                        raw_symbol = " ".join(parts[1:-1])
                    else:
                        raw_symbol = " ".join(parts[1:])
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

            if raw_symbol.lower() in ("near-week", "near"):
                # Handle near-week option exit
                target_key = self.selected_account_api_key
                target_keys = [target_key] if target_key else [a["api_key"] for a in self.accounts]
                
                # Retrieve options database to resolve expiries
                from cli.advisor import get_nifty_options
                import datetime
                
                ref_key = None
                for a in self.accounts:
                    if self.client.is_authenticated(a["api_key"]):
                        ref_key = a["api_key"]
                        break
                        
                if not ref_key:
                    self.log_message("[#ff0000]Error:[/#] No authenticated accounts to resolve expiries.")
                    return
                    
                options = get_nifty_options(self.client, ref_key)
                if not options:
                    self.log_message("[#ff0000]Error:[/#] Failed to load options database.")
                    return
                    
                today = datetime.date.today()
                underlying_near_expiry = {}
                for inst in options:
                    name = inst.get("name")
                    expiry = inst.get("expiry")
                    if name and isinstance(expiry, datetime.date) and expiry >= today:
                        if name not in underlying_near_expiry:
                            underlying_near_expiry[name] = expiry
                        else:
                            underlying_near_expiry[name] = min(underlying_near_expiry[name], expiry)
                            
                # Scan active positions matching near expiry
                target_symbols = set()
                accounts_data = []
                if self.last_positions_response:
                    accounts_data = self.last_positions_response.get("accounts", [])
                    
                for acct in accounts_data:
                    if acct.get("api_key") not in target_keys:
                        continue
                    for pos in acct.get("positions", []):
                        if pos.get("quantity", 0) == 0:
                            continue
                        sym = pos.get("tradingsymbol", "")
                        inst = next((x for x in options if x.get("tradingsymbol") == sym), None)
                        if inst:
                            name = inst.get("name")
                            exp = inst.get("expiry")
                            if exp == underlying_near_expiry.get(name):
                                target_symbols.add(sym)
                                
                if not target_symbols:
                    self.log_message("No open near-week option positions found.")
                    return
                    
                symbols_list = sorted(list(target_symbols))
                if getattr(self, "_skip_confirmation", False):
                    for sym in symbols_list:
                        asyncio.create_task(self.execute_exit(sym, target_keys, price_val))
                    return

                self.pending_order = {
                    "type": "exit_near_week",
                    "symbols": symbols_list,
                    "api_keys": target_keys,
                    "price": price_val,
                }
                
                if self.selected_account_name:
                    accts_desc = f"@{self.selected_account_name}"
                else:
                    accts_desc = "All Accounts"
                    
                price_desc = f" @{price_val:.2f}" if price_val is not None else ""
                
                # Log pending command
                self._log_command(
                    cmd_text=cmd,
                    action=self.pending_order,
                    status="pending_confirmation",
                    api_key=target_keys[0] if len(target_keys) == 1 else None,
                )
                
                self.prompt_control.text = f" Confirm SQUAREOFF {', '.join(symbols_list)} on {accts_desc}{price_desc}? (y/n)> "
                self.log_message(f"[#ff8700]Pending Confirmation:[/#] SQUAREOFF {', '.join(symbols_list)} on {accts_desc}{price_desc}. Press [bold]y[/bold] to confirm.")
                return

            if getattr(self, "_skip_confirmation", False):
                target_key = self.selected_account_api_key
                if raw_symbol and raw_symbol.lower() != "all":
                    symbol, api_key, err = self.resolve_symbol(raw_symbol)
                    if err:
                        self.log_message(f"[#ff0000]Error:[/#] {err}")
                        return
                    target_key = api_key or self.selected_account_api_key
                    asyncio.create_task(self.execute_exit(symbol, [target_key] if target_key else [], price_val))
                else:
                    asyncio.create_task(self.execute_exit(None, [target_key] if target_key else [], price_val))
                return

            # Set pending exit for confirmation
            self.pending_order = {
                "symbol": raw_symbol,
                "type": "exit",
                "qty": "",
                "price": price_val,
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

            # Log pending exit command
            self._log_command(
                cmd_text=cmd,
                action=self.pending_order,
                status="pending_confirmation",
                api_key=target_keys[0] if len(target_keys) == 1 else None,
            )

            price_desc = f" @{price_val:.2f}" if price_val is not None else ""
            if raw_symbol and raw_symbol.lower() != "all":
                symbol = self.pending_order["symbol"]
                self.prompt_control.text = f" Confirm EXIT of {symbol} on {accts_desc}{price_desc}? (y/n)> "
                self.log_message(f"[#ff8700]Pending Confirmation:[/#] EXIT open positions for {symbol} on {accts_desc}{price_desc}. Press [bold]y[/bold] to confirm, any other key to cancel.")
            else:
                self.prompt_control.text = f" Confirm EXIT of ALL positions on {accts_desc}{price_desc}? (y/n)> "
                self.log_message(f"[#ff8700]Pending Confirmation:[/#] EXIT ALL open positions on {accts_desc}{price_desc}. Press [bold]y[/bold] to confirm, any other key to cancel.")
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

            # Use the primary streaming account so the option chain can stream;
            # fall back to the first configured account for the REST fetch.
            if not self.accounts:
                self.log_message("[#ff0000]Error:[/#] No accounts configured.")
                return
            api_key = self.primary_api_key or self.accounts[0]["api_key"]

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

        # Store structured data so streaming ticks can update LTPs in place and
        # the pane can be re-rendered without another REST fetch.
        self.oc_data = {
            "underlying": underlying,
            "expiry": expiry,
            "strikes": strikes,
            "available_expiries": expiries,
        }

        # Subscribe the option tokens on the primary ticker so the LTPs stream.
        self._update_oc_subscriptions(strikes)

        self._last_oc_text = self._render_oc_text()
        self.info_mode = "oc"
        self._update_info_buffer()
        stream_note = " (streaming)" if self.primary_api_key else ""
        self.log_message(
            f"Option chain loaded for {underlying} expiry {expiry}{stream_note}. "
            f"Press F3 to view in right pane."
        )

    def _render_oc_text(self) -> str:
        """Render the current ``self.oc_data`` into the option-chain pane text."""
        data = self.oc_data
        if not data:
            return self._last_oc_text

        underlying = data.get("underlying", "")
        expiry = data.get("expiry", "")
        strikes = data.get("strikes", [])
        expiries = data.get("available_expiries", [])

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
        return "\n".join(lines)

    def _update_oc_subscriptions(self, strikes: list[dict]) -> None:
        """Subscribe the option-chain instrument tokens on the primary ticker
        (unsubscribing any tokens from a previously displayed chain).

        Also rebuilds ``self.oc_token_map`` so incoming ticks can be routed to
        the correct strike/side for live LTP updates.
        """
        new_tokens: set[int] = set()
        token_map: dict[int, tuple] = {}
        for s in strikes:
            ce_tok = s.get("ce_token")
            pe_tok = s.get("pe_token")
            if ce_tok:
                new_tokens.add(int(ce_tok))
                token_map[int(ce_tok)] = (s.get("strike"), "ce")
            if pe_tok:
                new_tokens.add(int(pe_tok))
                token_map[int(pe_tok)] = (s.get("strike"), "pe")

        self.oc_token_map = token_map

        ticker = self._get_active_ticker()
        old_tokens = self.oc_subscribed_tokens

        # Don't unsubscribe tokens that are also position/index tokens.
        protected = set(self.subscribed_tokens) | set(self.index_tokens)
        to_unsub = (old_tokens - new_tokens) - protected
        to_sub = new_tokens - protected

        if ticker:
            if to_unsub:
                try:
                    ticker.unsubscribe(list(to_unsub))
                except Exception:
                    pass
            if to_sub:
                try:
                    ticker.subscribe(list(to_sub))
                    ticker.set_mode(ticker.MODE_LTP, list(to_sub))
                except Exception:
                    pass

        self.oc_subscribed_tokens = new_tokens

    def _update_oc_and_invalidate(self) -> None:
        """Re-render the option-chain pane from streamed values and refresh TUI."""
        self._last_oc_text = self._render_oc_text()
        if self.info_mode == "oc":
            self._update_info_buffer(reset_scroll=False)
        if hasattr(self, "app") and self.app:
            self.app.invalidate()

    def _render_advisor_text(self) -> str:
        """Render Tuesday strangle advisor plan into plain text."""
        import datetime
        
        today = datetime.date.today()
        is_tuesday = today.weekday() == 1
        
        if not is_tuesday:
            return "=== Tuesday Expiry Option Strangle Advisor ===\n\n[INFO] Today is not Tuesday. Expiry strangle planning is only active on Tuesdays."

        from cli.advisor import generate_tuesday_plan

        # Fetch live Nifty spot price
        nifty_spot = self.index_values.get("nifty")

        accounts_positions = []
        if self.last_positions_response:
            accounts_positions = self.last_positions_response.get("accounts", [])

        try:
            plan = generate_tuesday_plan(
                client=self.client,
                accounts_positions=accounts_positions,
                margins_by_api_key=self.margins_by_api_key,
                api_key_to_user_id=self.api_key_to_user_id,
                nifty_spot=nifty_spot
            )
        except Exception as exc:
            return f"=== Tuesday Expiry Advisor ===\n\nFailed to plan: {exc}"

        if plan.get("status") == "error":
            return f"=== Tuesday Expiry Advisor ===\n\nError: {plan.get('message')}"

        lines = []

        lines.append("=== Tuesday Expiry Option Strangle Advisor ===")
        spot_desc = f"{plan.get('nifty_spot'):,.2f}" if plan.get('nifty_spot') else "N/A"
        lines.append(f"NIFTY Index Spot: {spot_desc}")

        exp = plan.get("expiries", {})
        lines.append(f"Expiries: E0={exp.get('E0')} | E1={exp.get('E1')} | E2={exp.get('E2')}")

        strikes = plan.get("strikes", {})
        symbols = plan.get("symbols", {})
        lines.append("Target Strikes:")
        lines.append(f"  - E1 (5% OTM) CE: {strikes.get('E1_CE')} ({symbols.get('E1_CE') or 'NOT FOUND'})")
        lines.append(f"  - E1 (5% OTM) PE: {strikes.get('E1_PE')} ({symbols.get('E1_PE') or 'NOT FOUND'})")
        lines.append(f"  - E2 (7% OTM) CE: {strikes.get('E2_CE')} ({symbols.get('E2_CE') or 'NOT FOUND'})")
        lines.append(f"  - E2 (7% OTM) PE: {strikes.get('E2_PE')} ({symbols.get('E2_PE') or 'NOT FOUND'})")
        lines.append("-" * 75)
        lines.append("")

        for acct in plan.get("accounts", []):
            lines.append(f"Account: {acct.get('name')} ({acct.get('user_id')})")
            lines.append(f"  Margin: Cash={acct.get('cash')/100000:.2f}L | Collateral={acct.get('collateral')/100000:.2f}L | Total={acct.get('total_capital')/100000:.2f}L")
            lines.append(f"  Allocated (50/50): E1={acct.get('lots_e1')} Lots (~{acct.get('lots_e1')*1.3:.1f}L) | E2={acct.get('lots_e2')} Lots (~{acct.get('lots_e2')*1.3:.1f}L)")

            ex_e0 = acct.get("exits_e0", [])
            ex_e1 = acct.get("exits_e1", [])
            lines.append(f"  Exits to Execute: E0={ex_e0 or '(none)'} | E1={ex_e1 or '(none)'}")

            lines.append("")
            lines.append("  [Stage 1 Command (E0/E1 Exits + E1 Entry - 5% OTM)]")
            if acct.get("stage_1_cmd"):
                lines.append(f"  > {acct.get('stage_1_cmd')}")
            else:
                lines.append("  > (no action needed or insufficient capital)")

            lines.append("")
            lines.append("  [Stage 2 Command (E2 Entry - 7% OTM - ONLY if margin > 3L after Stage 1)]")
            if acct.get("stage_2_cmd"):
                lines.append(f"  > {acct.get('stage_2_cmd')}")
            else:
                lines.append("  > (no action or insufficient capital)")

            lines.append("-" * 75)
            lines.append("")

        return "\n".join(lines)

    async def resolve_nli_command(self, user_text: str) -> None:
        """Call Gemini NLI to translate natural language string, then stage confirmation."""
        if not getattr(self, "gemini_api_key", None):
            self.log_message("[#ff0000]Error:[/#] Gemini API Key is missing. Please add 'gemini_api_key' to ~/.kcli/config.yaml or set GEMINI_API_KEY env var.")
            return

        self.log_message(f"[#d787ff]Translating NLI command via Gemini: \"{user_text}\"...[/#]")

        # Build context
        selected_acct = self.selected_account_name
        accounts_list = [a.get("name") for a in self.accounts]

        # Get open positions (from active_positions list)
        open_positions = getattr(self, "active_positions", [])
        nifty_spot = self.index_values.get("nifty")

        # Resolve nearest active weekly expiry for each underlying and compile option list fallback
        nearest_expiries_str = {}
        available_options_list = []
        try:
            from cli.advisor import get_nifty_options
            import datetime
            
            ref_key = None
            for a in self.accounts:
                if self.client.is_authenticated(a["api_key"]):
                    ref_key = a["api_key"]
                    break
                    
            if ref_key:
                options = get_nifty_options(self.client, ref_key)
                if options:
                    today = datetime.date.today()
                    nearest_expiries = {}
                    for inst in options:
                        name = inst.get("name")
                        expiry = inst.get("expiry")
                        if name and isinstance(expiry, datetime.date) and expiry >= today:
                            if name not in nearest_expiries:
                                nearest_expiries[name] = expiry
                            else:
                                nearest_expiries[name] = min(nearest_expiries[name], expiry)
                    
                    nearest_expiries_str = {k: v.strftime("%Y-%m-%d") for k, v in nearest_expiries.items()}

                    spot = nifty_spot or 23500.0  # fallback
                    for opt in options:
                        try:
                            strike = float(opt.get("strike", 0))
                            if abs(strike - spot) / spot <= 0.15:
                                available_options_list.append({
                                    "symbol": opt.get("tradingsymbol"),
                                    "expiry": opt.get("expiry").strftime("%Y-%m-%d") if isinstance(opt.get("expiry"), datetime.date) else str(opt.get("expiry")),
                                    "strike": strike,
                                    "type": opt.get("instrument_type")
                                })
                        except (ValueError, TypeError):
                            pass
        except Exception:
            pass

        from cli.nli import parse_natural_language

        try:
            result = await parse_natural_language(
                api_key=self.gemini_api_key,
                user_input=user_text,
                selected_account=selected_acct,
                accounts_list=accounts_list,
                open_positions=open_positions,
                nifty_spot=nifty_spot,
                nearest_expiries=nearest_expiries_str,
                available_options=available_options_list
            )
        except Exception as exc:
            self.log_message(f"[#ff0000]NLI Translation Failed:[/#] {exc}")
            return

        translated_cmd = result.get("command", "").strip()
        explanation = result.get("explanation", "").strip()
        confidence = result.get("confidence", 0.0)

        if not translated_cmd:
            self.log_message(f"[#ff5555]NLI Translation Failed:[/#] Could not interpret query. ({explanation or 'Low confidence'})")
            return

        # Stage confirmation
        self.pending_order = {
            "type": "nli_command",
            "command": translated_cmd,
            "explanation": explanation
        }

        # Log pending command status
        self._log_command(
            cmd_text=f"/{user_text}",
            action=self.pending_order,
            status="pending_confirmation",
            api_key=self.selected_account_api_key
        )

        self.prompt_control.text = f" Confirm NLI Action: {translated_cmd}? (y/n)> "
        self.log_message(f"[#d787ff]Pending Translation:[/#] \"{translated_cmd}\" ({explanation}, conf={confidence:.2f}). Press [bold]y[/bold] to confirm.")


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

                    # Always show filled/total format (e.g. 0/910, 130/910, 910/910)
                    qty_desc = f"{filled}/{qty}"
                    lines.append(
                        f"  [{oid[-6:]}] {tx} {sym} | {qty_desc} | "
                        f"{otype} {price_desc} | {product} | {status}"
                    )
            lines.append("")

        if not any_orders and mode == "orders_pending":
            lines.append("No pending orders across all accounts.")
        return "\n".join(lines)

    def _update_info_buffer(self, reset_scroll: bool = True) -> None:
        """Push current info_mode text into the info_buffer.

        Args:
            reset_scroll: When True (default) the pane scrolls back to the top.
                          Pass False for live tick/data refreshes where the user
                          may have scrolled down — preserves their position.
        """
        if not hasattr(self, "info_buffer"):
            return
        if self.info_mode == "orders_pending":
            text = self._last_pending_text
        elif self.info_mode == "orders_executed":
            text = self._last_executed_text
        elif self.info_mode == "advisor":
            try:
                self._last_advisor_text = self._render_advisor_text()
            except Exception as exc:
                self._last_advisor_text = f"=== Tuesday Expiry Advisor ===\n\nFailed to plan: {exc}"
            text = self._last_advisor_text
        else:
            text = self._last_oc_text
        if reset_scroll:
            new_cursor = 0
        else:
            old_doc = self.info_buffer.document
            old_row = old_doc.cursor_position_row
            old_col = old_doc.cursor_position_col
            temp_doc = Document(text=text)
            target_row = min(old_row, max(0, len(temp_doc.lines) - 1))
            new_cursor = temp_doc.translate_row_col_to_index(target_row, old_col)

        self.info_buffer.set_document(
            Document(text=text, cursor_position=new_cursor),
            bypass_readonly=True,
        )
        if reset_scroll and hasattr(self, "_info_control"):
            self._info_control.vertical_scroll_position = 0
        if hasattr(self, "app"):
            self.app.invalidate()

    def _get_frame_style(self, body_window) -> str:
        """Return focused_frame style if the window or its content has focus."""
        if hasattr(self, "app") and self.app:
            if self.app.layout.has_focus(body_window):
                return "class:focused_frame"
            if hasattr(body_window, "content") and self.app.layout.has_focus(body_window.content):
                return "class:focused_frame"
        return "class:frame"

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
        # Keep noisy third-party WebSocket loggers off the full-screen TUI.
        _silence_websocket_loggers()
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
            text="Fetching positions, please wait...",
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
            get_vertical_scroll=lambda win: self._logs_control.buffer.document.cursor_position_row,
        )
        self.info_window = ScrollableWindow(
            content=self._info_control,
            wrap_lines=True,
            allow_scroll_beyond_bottom=True,
            get_vertical_scroll=lambda win: self._info_control.buffer.document.cursor_position_row,
        )

        def make_scroll_handler(window, control):
            original_handler = control.mouse_handler
            def scroll_mouse_handler(mouse_event):
                from prompt_toolkit.mouse_events import MouseEventType
                if mouse_event.event_type == MouseEventType.SCROLL_UP:
                    if hasattr(control, "buffer"):
                        for _ in range(3):
                            control.buffer.cursor_up()
                    else:
                        control.vertical_scroll_position = max(0, control.vertical_scroll_position - 3)
                    self.app.invalidate()
                    return None
                elif mouse_event.event_type == MouseEventType.SCROLL_DOWN:
                    if hasattr(control, "buffer"):
                        for _ in range(3):
                            control.buffer.cursor_down()
                    else:
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
                or (hasattr(self, "selected_order") and self.selected_order)
            ):
                self.selected_symbol = None
                self.selected_account_name = None
                self.selected_account_api_key = None
                self.selected_order = None
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
            elif hasattr(cur, "buffer"):
                cur.buffer.cursor_up()

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
            elif hasattr(cur, "buffer"):
                cur.buffer.cursor_down()

        @kb.add("pageup", filter=~has_focus(self.input_field))
        def _page_up_kb(event):
            cur = event.app.layout.current_control
            if cur == self.positions_control:
                self.positions_control.vertical_scroll_position = max(0, self.positions_control.vertical_scroll_position - 10)
            elif hasattr(cur, "buffer"):
                for _ in range(10):
                    cur.buffer.cursor_up()

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
            elif hasattr(cur, "buffer"):
                for _ in range(10):
                    cur.buffer.cursor_down()

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

        @kb.add("f4")
        def _f4(event):
            self.info_mode = "advisor"
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
                        title="Active Positions (Live Ticks) [Tab: focus]",
                        body=self.positions_window,
                        style=self._get_frame_style(self.positions_window),
                    ),
                    self.horizontal_divider,
                    Frame(
                        title="Status Logs [Tab: focus] [Ctrl+↑↓: resize]",
                        body=self.logs_window,
                        style=self._get_frame_style(self.logs_window),
                    ),
                ], width=D(weight=lw)),
                self.vertical_divider,
                # ── RIGHT half: Info Pane (scrollable) ──
                Frame(
                    title="[F1] Pending  [F2] Executed  [F3] Option Chain  [F4] Advisor  [Ctrl+←→: resize]",
                    body=HSplit([
                        Window(
                            content=self.market_indices_control,
                            height=1,
                            style="class:market_indices",
                        ),
                        Window(height=1, char="─", style="class:divider"),
                        self.info_window,
                    ]),
                    style=self._get_frame_style(self.info_window),
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
                        wrap_lines=True,
                    ),
                    self.input_field,
                ], height=3),
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

        asyncio.create_task(self._initial_fetch_and_connect())

        try:
            await self.app.run_async()
        finally:
            self.running = False
            if hasattr(self, "recorder"):
                try:
                    self.recorder.stop()
                except Exception as exc:
                    logger.error("Error stopping recorder: %s", exc, exc_info=True)
            for api_key, ticker in list(self.tickers.items()):
                try:
                    ticker.close()
                except Exception as exc:
                    logger.warning("Failed to close ticker for %s: %s", api_key[:8], exc)
