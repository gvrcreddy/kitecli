import asyncio
from datetime import datetime, timedelta, time
import logging
from typing import Any, Optional, Union
from urllib.parse import urlparse
import threading

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI, HTML, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import DynamicContainer, HSplit, VSplit, Window, WindowAlign
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.history import InMemoryHistory, FileHistory
from collections import UserDict, defaultdict
from prompt_toolkit.widgets import SearchToolbar
from prompt_toolkit.search import start_search, is_searching, do_incremental_search, SearchDirection
from prompt_toolkit.filters import has_focus
from prompt_toolkit.data_structures import Point

from cli.api_client import KCLIClient, KCLIClientError
from cli.display import render_positions_to_string, render_holdings_to_string
from cli.recorder import DataRecorder
from cli.parser import (
    parse_command_line, parse_single_command, AccountSelectCommand, OrderSelectCommand, PositionSelectCommand, PlaceOrderCommand, ExitCommand,
    StatusCommand, PositionsCommand, OrdersCommand, CancelOrderCommand, ModifyOrderCommand,
    MarketDepthCommand, HoldingsCommand, ConditionalOrderCommand, CancelConditionalOrderCommand,
    ConditionalOrdersViewCommand, CancelScheduledOrderCommand, ScheduledOrdersViewCommand
)
from cli.executor import ExecutionContext, resolve_symbol_or_id
import json

logger = logging.getLogger(__name__)

import re
_ANSI_REGEX = re.compile(r'\x1b\[[0-9;]*m')

def _visible_len(text: str) -> int:
    return len(_ANSI_REGEX.sub('', text))

def _pad_ansi(text: str, width: int, align: str = '<') -> str:
    v_len = _visible_len(text)
    pad_needed = max(0, width - v_len)
    if align == '>':
        return ' ' * pad_needed + text
    elif align == '^':
        left = pad_needed // 2
        right = pad_needed - left
        return ' ' * left + text + ' ' * right
    return text + ' ' * pad_needed


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
            infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
            # Prioritize IPv6 if available on current network
            infos = sorted(infos, key=lambda x: 0 if x[0] == socket.AF_INET6 else 1)
            err = None
            for info in infos:
                family, socktype, proto, _, sockaddr = info
                try:
                    s = socket.socket(family, socktype, proto)
                    s.settimeout(timeout)
                    s.connect(sockaddr)
                    raw_sock = s
                    break
                except Exception as e:
                    err = e
                    continue
            if not raw_sock:
                raise err or socket.error(f"Failed to connect to {host}:{port}")

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
        self._main_thread_id = threading.get_ident()
        self.client = client
        self.accounts = accounts
        self.running = True
        self.tickers = {}
        self.websocket_connected = {a["api_key"]: False for a in accounts if a.get("api_key")}
        self.subscribed_tokens = set()
        self.ws_ltp_cache: dict[int, float] = {}
        # Symbol-based LTP cache for brokers without instrument_token (e.g. Kotak).
        # Populated in _on_ticks when we match a tick's token to a known tradingsymbol.
        self.ws_symbol_ltp_cache: dict[str, float] = {}
        # Reverse map: instrument_token → tradingsymbol, built during _trigger_immediate_refresh
        self.ws_token_to_symbol: dict[int, str] = {}
        # Forward map: tradingsymbol → instrument_token
        self.ws_symbol_to_token: dict[str, int] = {}
        self._refresh_in_progress = False
        self._refresh_pending = False
        self.rest_error_count = 0
        self.account_rest_failed = {a["api_key"]: False for a in accounts if a.get("api_key")}
        # Throttle state for noisy per-account WebSocket close/error logging.
        self._ws_log_throttle = {}

        # Market index instrument tokens (constant on Kite). These are streamed
        # over the WebSocket alongside position tokens so NIFTY/SENSEX/INDIA VIX
        # update live rather than only on REST refresh. Verified via quote/ltp:
        #   NSE:NIFTY 50 -> 256265, BSE:SENSEX -> 265, NSE:INDIA VIX -> 264969
        self.index_tokens = {256265: "nifty", 265: "sensex", 264969: "vix"}
        self.index_values = {"nifty": None, "sensex": None, "vix": None}
        self.index_close_prices = {}

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

        # Build account api_key -> user_id mapping (supporting Zerodha & Kotak)
        self.api_key_to_user_id = {}
        for a in accounts:
            key = a.get("api_key") or a.get("consumer_key")
            uid = a.get("user_id") or a.get("ucc") or a.get("name") or "UNKNOWN"
            if key:
                self.api_key_to_user_id[key] = uid

        # Track recorded executed order IDs to avoid duplicate DB insertions
        self._executed_order_ids_recorded: set[str] = set()
        self._orders_seeded_on_startup: bool = False

        # Initialize and start SQLite recorder
        self.recorder = DataRecorder()
        self.recorder.start()
        self.record_context_enabled = True
        try:
            import sqlite3
            if self.recorder.db_path.exists():
                conn = sqlite3.connect(str(self.recorder.db_path))
                cursor = conn.cursor()
                cursor.execute("SELECT DISTINCT order_id FROM executed_order_context WHERE order_id IS NOT NULL;")
                for row in cursor.fetchall():
                    if row[0]:
                        self._executed_order_ids_recorded.add(str(row[0]))
                conn.close()
        except Exception as exc:
            logger.warning("Failed to load existing executed_order_context IDs: %s", exc)

        # Conditional orders in-memory lookup: parent_order_id -> cond_dict
        self.conditional_orders_map: dict[str, dict] = {}
        self._init_conditional_orders()

        # Scheduled orders in-memory lookup: sched_id -> sched_dict
        self.scheduled_orders_map: dict[int, dict] = {}
        self._init_scheduled_orders()

        # Info pane state
        # mode: "orders_pending" | "orders_executed" | "oc" | "advisor" | "pnl" | "md" | "holdings" | "help"
        self.info_mode: str = "orders_pending"
        self.pnl_period: str = "week"  # "week" | "last_week" | "month" | "year" | "custom"
        self.md_symbol: str = ""
        self.holdings_view_mode: str = "summary"  # "summary" | "deep"
        self.holdings_target_account: str | None = None
        self.last_holdings_response: dict | None = None
        self._last_oc_text: str = "Press F3 or run 'oc <UNDERLYING>' to fetch option chain."
        self.oc_dual_data: dict | None = None
        self.oc_dual_dropdown_1: bool = False
        self.oc_dual_dropdown_2: bool = False
        self.oc_dual_exp1_idx: int = 0
        self.oc_dual_exp2_idx: int = 1
        self._last_oc_dual_text: str = "Press F3 or run 'oc comp <UNDERLYING>' to compare option chains."
        self._last_pending_text: str = "Fetching pending orders..."
        self._last_executed_text: str = "Fetching executed orders..."
        self._last_advisor_text: str = "Press F4 to view Tuesday Option Strangle Advisor plan."
        self._last_pnl_text: str = "Fetching P&L analytics..."
        self._last_md_text: str = "Press F6 or run 'md <SYMBOL>' to fetch market depth."
        self._last_holdings_text: str = "Press F7 or run 'holdings' to view portfolio."
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
            "tab_bar": "bg:#161b22",
            "tab.active": "bg:#1f6feb fg:#ffffff bold",
            "tab.inactive": "bg:#21262d fg:#8b949e",
            "tab.separator": "fg:#30363d",
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

            # Option Chain Matrix styles
            "oc.border": "fg:#30363d",
            "oc.header.title": "fg:#58a6ff bold",
            "oc.header.spot": "fg:#f1e05a bold",
            "oc.header.expiry": "fg:#d29922 bold",
            "oc.header.pcr": "fg:#56d364 bold",
            "oc.header.straddle": "fg:#bc8cff bold",
            "oc.header.pain": "fg:#ff7b72 bold",
            "oc.table.header": "fg:#8b949e bold",
            "oc.table.cols": "fg:#79c0ff bold",
            "oc.ce": "fg:#56d364",
            "oc.pe": "fg:#ff7b72",
            "oc.ce.itm": "bg:#161b22 fg:#56d364",
            "oc.pe.itm": "bg:#161b22 fg:#ff7b72",
            "oc.bar.call": "fg:#ff7b72",
            "oc.bar.put": "fg:#56d364",
            "oc.strike": "fg:#e6edf3 bold",
            "oc.atm": "bg:#388bfd fg:#ffffff bold",
            "oc.wall.call": "bg:#490202 fg:#ff7b72 bold",
            "oc.wall.put": "bg:#033a16 fg:#56d364 bold",
            "oc.maxpain": "bg:#2e1065 fg:#d8b4fe bold",
            "oc.selected": "bg:#1f6feb fg:#ffffff bold",
            "oc.expiry.header": "fg:#8b949e bold",
            "oc.expiry.active": "bg:#238636 fg:#ffffff bold",
            "oc.expiry.inactive": "bg:#21262d fg:#58a6ff",
            "oc.dropdown.btn": "bg:#21262d fg:#58a6ff bold",
            "oc.dropdown.active": "bg:#1f6feb fg:#ffffff bold",
            "oc.dropdown.item": "fg:#e6edf3",
            "oc.dropdown.close": "fg:#8b949e italic",
            "oc.tip": "fg:#8b949e italic",

            # Dual Option Chain Comparison (Set 1 vs Set 2) styles
            "oc.set1.ce": "fg:#56d364",           # Set 1 Calls (Emerald Green)
            "oc.set1.pe": "fg:#ff7b72",           # Set 1 Puts (Coral Red)
            "oc.set1.hdr": "fg:#56d364 bold",      # Set 1 Header
            "oc.set1.btn": "bg:#0d3318 fg:#56d364 bold", # Set 1 Expiry Button
            "oc.set2.ce": "fg:#79c0ff",           # Set 2 Calls (Sky Blue / Cyan)
            "oc.set2.pe": "fg:#f1e05a",           # Set 2 Puts (Gold / Amber)
            "oc.set2.hdr": "fg:#79c0ff bold",      # Set 2 Header
            "oc.set2.btn": "bg:#0c2d48 fg:#79c0ff bold", # Set 2 Expiry Button
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
        if hasattr(self, "app") and self.app and self.app.loop:
            if threading.get_ident() != getattr(self, "_main_thread_id", None):
                self.app.loop.call_soon_threadsafe(self.log_message, message)
                return

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

    def _run_coroutine_threadsafe(self, coro) -> None:
        """Safely schedule a coroutine onto the main asyncio loop from any thread (e.g. Twisted WebSocket thread)."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            target_loop = getattr(self, "_main_loop", None)
            if not target_loop and hasattr(self, "app") and self.app and self.app.loop:
                target_loop = self.app.loop
            if target_loop and target_loop.is_running():
                asyncio.run_coroutine_threadsafe(coro, target_loop)
            else:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.run_coroutine_threadsafe(coro, loop)
                    else:
                        loop.create_task(coro)
                except Exception as exc:
                    logger.error("Failed to schedule coroutine threadsafe: %s", exc, exc_info=True)

    def update_prompt_label(self) -> None:
        """Update prompt label based on selected symbol and account contexts."""
        if not hasattr(self, "prompt_control"):
            return
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
                modify_matching_snippet = f"order {o_sym} {o_price:.2f}"
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
        if not hasattr(self, "positions_control"):
            return
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
        """Update the header by invalidating TUI redraw state."""
        if hasattr(self, "app") and self.app:
            self.app.invalidate()

    def _get_left_header_text(self) -> list[tuple[str, str]]:
        """Build the left-aligned header text dynamically based on columns."""
        total = len(self.accounts)
        now = datetime.now().strftime("%H:%M:%S")

        total_cols = 100
        if hasattr(self, "app") and self.app and hasattr(self.app, "output") and self.app.output:
            try:
                cols = self.app.output.get_size().columns
                if isinstance(cols, int):
                    total_cols = cols
            except Exception:
                pass

        if total_cols >= 120:
            left_str = f" 🪁 KiteCLI Live │ Last Update: {now} │ Accounts: {total} │ Ctrl+D: Quit │ Ctrl+C: Cancel │ Escape: Deselect"
        elif total_cols >= 95:
            left_str = f" 🪁 KiteCLI Live │ {now} │ Accounts: {total}"
        else:
            left_str = f" 🪁 Live │ {now}"

        return [("", left_str)]

    def _get_right_header_text(self) -> list[tuple[str, str] | tuple[str, str, callable]]:
        """Build the right-aligned health indicator text."""
        total = len(self.accounts)
        good_count = sum(1 for acct in self.accounts if self.is_account_healthy(acct.get("api_key")))

        def _header_click_handler(*args, **kwargs):
            from prompt_toolkit.mouse_events import MouseEventType
            mouse_event = None
            if len(args) == 2:
                mouse_event = args[1]
            elif len(args) == 1:
                mouse_event = args[0]
            
            if mouse_event and mouse_event.event_type == MouseEventType.MOUSE_UP:
                self.log_message("Triggering manual WebSocket and REST reconnection...")
                if hasattr(self, "app") and self.app and self.app.loop:
                    asyncio.run_coroutine_threadsafe(self.reconnect_websockets(), self.app.loop)

        if good_count == total:
            status_style = "fg:#00ff00 bold"
            status_label = f"● Health: Good ({total}/{total}) "
            return [(status_style, status_label, _header_click_handler)]

        fragments = []
        status_style = "fg:#ff8700 bold" if good_count > 0 else "fg:#ff0000 bold"
        fragments.append((status_style, f"● Health: ({good_count}/{total}) [", _header_click_handler))

        for i, acct in enumerate(self.accounts):
            key = acct.get("api_key")
            name = acct.get("name", key)
            healthy = self.is_account_healthy(key)

            if healthy:
                fragments.append(("fg:#00ff00", f"{name}:✓", _header_click_handler))
            else:
                fragments.append(("fg:#ff0000 bold", f"{name}:✗", _header_click_handler))

            if i < total - 1:
                fragments.append(("", " ", _header_click_handler))

        fragments.append((status_style, "] ", _header_click_handler))
        return fragments

    def _get_tab_bar_text(self):
        """Build clickable, styled text fragments for the info pane tabs."""
        def make_click_handler(mode):
            def handler(*args, **kwargs):
                from prompt_toolkit.mouse_events import MouseEventType
                mouse_event = None
                if len(args) == 2:
                    mouse_event = args[1]
                elif len(args) == 1:
                    mouse_event = args[0]
                
                if mouse_event and mouse_event.event_type == MouseEventType.MOUSE_UP:
                    if mode == "pnl":
                        self.pnl_period = "week"
                    elif mode == "oc":
                        if getattr(self, "oc_data", None):
                            self._update_oc_subscriptions(self.oc_data.get("strikes", []))
                    self.info_mode = mode
                    self._update_info_buffer()
                    if hasattr(self, "app") and self.app:
                        self.app.invalidate()
            return handler

        fragments = []
        
        # Tiny spacing for visual margin
        fragments.append(("", " "))
        
        # Tab 1: Orders
        fragments.append((
            "class:tab.active" if self.info_mode == "orders_pending" else "class:tab.inactive",
            " Orders ",
            make_click_handler("orders_pending")
        ))
        fragments.append(("", " "))
        
        # Tab 2: Executed
        fragments.append((
            "class:tab.active" if self.info_mode == "orders_executed" else "class:tab.inactive",
            " Executed ",
            make_click_handler("orders_executed")
        ))
        fragments.append(("", " "))
        
        # Tab 3: OC (Option Chain)
        fragments.append((
            "class:tab.active" if self.info_mode == "oc" else "class:tab.inactive",
            " OC ",
            make_click_handler("oc")
        ))
        fragments.append(("", " "))

        # Tab 3b: OC-Comp (Side-by-Side Dual Option Chain)
        def make_oc_dual_click():
            def handler(*args):
                mouse_event = None
                if len(args) == 2:
                    mouse_event = args[1]
                elif len(args) == 1:
                    mouse_event = args[0]
                if mouse_event and mouse_event.event_type == MouseEventType.MOUSE_UP:
                    if not getattr(self, "oc_dual_data", None):
                        api_key = self.primary_api_key or (self.accounts[0]["api_key"] if self.accounts else "")
                        und = getattr(self, "oc_data", {}).get("underlying", "NIFTY") if getattr(self, "oc_data", None) else "NIFTY"
                        if api_key:
                            asyncio.create_task(self._fetch_and_display_dual_option_chain(api_key, und, 0, 1))
                    else:
                        strikes1 = self.oc_dual_data.get("oc1", {}).get("strikes", [])
                        strikes2 = self.oc_dual_data.get("oc2", {}).get("strikes", [])
                        if strikes1 or strikes2:
                            self._update_oc_subscriptions(strikes1 + strikes2)
                    self.info_mode = "oc_dual"
                    self._update_info_buffer()
                    if hasattr(self, "app") and self.app:
                        self.app.invalidate()
            return handler

        fragments.append((
            "class:tab.active" if self.info_mode == "oc_dual" else "class:tab.inactive",
            " OC-Comp ",
            make_oc_dual_click()
        ))
        fragments.append(("", " "))
        
        # Tab 4: Advisor
        fragments.append((
            "class:tab.active" if self.info_mode == "advisor" else "class:tab.inactive",
            " Advisor ",
            make_click_handler("advisor")
        ))
        fragments.append(("", " "))

        # Tab 5: P&L
        fragments.append((
            "class:tab.active" if self.info_mode == "pnl" else "class:tab.inactive",
            " P&L ",
            make_click_handler("pnl")
        ))
        fragments.append(("", " "))

        # Tab 6: MD (Market Depth)
        fragments.append((
            "class:tab.active" if self.info_mode == "md" else "class:tab.inactive",
            " MD ",
            make_click_handler("md")
        ))
        fragments.append(("", " "))

        # Tab 7: Holdings
        fragments.append((
            "class:tab.active" if self.info_mode == "holdings" else "class:tab.inactive",
            " Holdings ",
            make_click_handler("holdings")
        ))
        fragments.append(("", " "))

        # Tab 8: Help
        fragments.append((
            "class:tab.active" if self.info_mode == "help" else "class:tab.inactive",
            " Help ",
            make_click_handler("help")
        ))
        
        return fragments

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

        async def diagnose_account(acct):
            api_key = acct.get("api_key")
            name = acct.get("name", api_key)
            if not api_key:
                return api_key, "no_token", name, "", acct, [], False

            logs = []
            is_bad = False
            broker = acct.get("broker", "zerodha").lower()
            
            # Fast path for Zerodha: try WebSocket auth first
            if broker == "zerodha":
                access_token = self.client.get_access_token(api_key)
                if not access_token:
                    logs.append(f"[#ff8700]⚠ No token:[/#] @{name} — login required (run 'kcli init').")
                    return api_key, "no_token", name, "no access token", acct, logs, True
                
                ws_status, ws_detail = await self._run_api_call(
                    probe_ws_auth, api_key, access_token, None
                )
                
                if ws_status == "ok":
                    logs.append(f"[#00ff00]✓ Streaming OK:[/#] @{name}")
                    return api_key, "valid", name, "", acct, logs, False
                elif ws_status == "proxy_blocked":
                    logs.append(f"[#ff8700]⚠ Proxy blocked streaming:[/#] @{name} — proxy refused the WebSocket tunnel ({ws_detail}).")
                    return api_key, "proxy_blocked", name, ws_detail, acct, logs, True
            
            # If we are here, either it's Kotak, or Zerodha WS failed. 
            # Fallback to the full REST check_token to find out exactly why.
            try:
                result = await self._run_api_call(self.client.check_token, api_key)
            except Exception as exc:
                result = {"name": name, "status": "error", "detail": str(exc)}
            
            status = result.get("status", "error")
            detail = result.get("detail", "")
            
            if status == "valid":
                if broker != "zerodha":
                    logs.append(f"[#00ff00]✓ Token OK:[/#] @{name}")
                else:
                    # Zerodha REST is valid, but we already know WS failed from the fast-path above!
                    is_bad = True
                    status = "stream_forbidden"
                    logs.append(
                        f"[#ff0000]✗ Streaming rejected:[/#] @{name} — "
                        f"WebSocket auth failed. REST works, so the "
                        f"token is valid but this api_key lacks streaming access. "
                        f"Check the app's subscription on the Kite Connect dashboard."
                    )
            elif status == "no_token":
                is_bad = True
                logs.append(f"[#ff8700]⚠ No token:[/#] @{name} — login required (run 'kcli init').")
            elif status == "expired":
                is_bad = True
                logs.append(f"[#ff8700]⚠ Token expired:[/#] @{name} — re-login (run 'kcli init'). {detail}")
            elif status == "forbidden":
                is_bad = True
                logs.append(f"[#ff0000]✗ Forbidden:[/#] @{name} — token rejected / no streaming entitlement. {detail}")
            else:
                is_bad = True
                logs.append(f"[#ff0000]✗ Token check failed:[/#] @{name} — {detail}")

            return api_key, status, name, detail, acct, logs, is_bad

        tasks = [diagnose_account(acct) for acct in self.accounts]
        results = await asyncio.gather(*tasks)

        for api_key, status, name, detail, acct, logs, is_bad in results:
            if not api_key:
                continue
            statuses[api_key] = status
            for log_msg in logs:
                self.log_message(log_msg)
            if is_bad:
                any_bad = True

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

        # Ensure accounts are validated & auto-logged in
        try:
            await self._run_api_call(self.client.init_accounts, self.accounts)
        except Exception as e:
            self.log_message(f"[#ff8700]Account init warning:[/#] {e}")
        
        # Fire off REST data fetch in the background so it doesn't block WebSockets
        if hasattr(self, "app") and self.app and self.app.loop:
            asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)
        
        # Start scheduled orders poller
        asyncio.create_task(self._poll_scheduled_orders())
        
        try:
            token_statuses = await self._diagnose_tokens()
        except Exception as e:
            self.log_message(f"[#ff0000]Initial task failed:[/#] {e}")
            token_statuses = {}

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

        # Connect WebSockets for accounts that support it (Zerodha and Kotak).
        for acct in self.accounts:
            api_key = acct.get("api_key")
            broker = acct.get("broker", "zerodha").lower()
            access_token = self.client.get_access_token(api_key)

            # Skip accounts whose token we already know is bad
            if token_statuses.get(api_key) not in (None, "valid"):
                self.log_message(
                    f"[#ff8700]Skipping WebSocket for @{acct.get('name')}:[/#] "
                    f"token not valid ({token_statuses.get(api_key)})."
                )
                continue

            if api_key and (access_token or broker == "kotak"):
                try:
                    if broker == "kotak":
                        ticker = self.client.create_ticker(api_key)
                        if not ticker:
                            self.log_message(f"[#ff8700]Failed to create Kotak WebSocket ticker for @{acct.get('name')}[/#]")
                            continue
                    else:
                        from kiteconnect import KiteTicker
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

                    # connect() only accepts threaded
                    connect_kwargs = dict(threaded=True)
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

    def is_account_healthy(self, api_key: str) -> bool:
        """Return True if the account REST fetches are passing and WebSocket ticker is connected."""
        rest_ok = not getattr(self, "account_rest_failed", {}).get(api_key, False)
        ws_ok = getattr(self, "websocket_connected", {}).get(api_key, False)
        return rest_ok and ws_ok

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
                or "invalid token" in reason_str
                or "token expired" in reason_str
                or "unauthorized" in reason_str
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
                self._handle_executed_order_context(api_key, data)

            # Check and trigger any linked conditional orders
            oid = str(data.get("order_id", "")).strip()
            if oid and hasattr(self, "conditional_orders_map") and oid in self.conditional_orders_map:
                self._check_conditional_triggers(oid, str(status or ""), int(filled or 0), data)

            # Trigger immediate refresh of positions and orders
            if hasattr(self, "app") and self.app and self.app.loop:
                asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)
        return on_order_update

    def _on_ticks(self, ws, ticks):
        updated = False
        orders_updated = False
        indices_updated = False
        oc_updated = False
        for tick in ticks:
            token = tick.get("instrument_token")
            ltp = tick.get("last_price")
            if not token or ltp is None:
                continue

            # Market index tick → update the header values & streaming change.
            if token in self.index_tokens:
                key = self.index_tokens[token]
                self.index_values[key] = ltp

                # Extract close from tick OHLC if available, or use cached REST close price
                tick_close = None
                if isinstance(tick.get("ohlc"), dict):
                    tick_close = tick["ohlc"].get("close")
                if tick_close:
                    self.index_close_prices[key] = tick_close

                close_val = self.index_close_prices.get(key)
                if close_val is not None:
                    self.index_values[f"{key}_change"] = ltp - close_val

                # Propagate live index spot price to active option chains
                if hasattr(self, "oc_data") and self.oc_data and str(self.oc_data.get("underlying", "")).upper() == key.upper():
                    self.oc_data["spot_price"] = ltp
                    oc_updated = True
                if hasattr(self, "oc_dual_data") and self.oc_dual_data and str(self.oc_dual_data.get("underlying", "")).upper() == key.upper():
                    for oc_k in ("oc1", "oc2"):
                        if self.oc_dual_data.get(oc_k):
                            self.oc_dual_data[oc_k]["spot_price"] = ltp
                    oc_updated = True

                indices_updated = True
                continue

            # Option-chain tick → update matching strike(s) CE/PE LTP in O(1).
            if token in self.oc_token_map:
                for strike_dict, side in self.oc_token_map[token]:
                    strike_dict[f"{side}_ltp"] = ltp
                oc_updated = True

            # Cache the latest LTP.
            self.ws_ltp_cache[int(token)] = ltp
            # Also cache by symbol for Kotak positions and orders
            sym = self.ws_token_to_symbol.get(int(token))
            if sym:
                self.ws_symbol_ltp_cache[sym.strip().upper()] = ltp

            # Check if this tick affects displayed orders
            if self.info_mode in ("orders_pending", "orders_executed"):
                orders_updated = True

            # Position tick → update the matching positions across all accounts.
            # Zerodha positions: matched by instrument_token.
            # Kotak positions: matched by tradingsymbol (via ws_token_to_symbol map).
            resp = getattr(self, "last_positions_response", None)
            if resp:
                for acct in resp.get("accounts", []):
                    for pos in acct.get("positions", []):
                        pos_token = pos.get("instrument_token")
                        token_match = (
                            pos_token is not None and int(pos_token) == int(token)
                        )
                        symbol_match = (
                            pos_token is None
                            and sym is not None
                            and pos.get("tradingsymbol", "").upper() == sym.upper()
                        )
                        if token_match or symbol_match:
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

        if updated or orders_updated:
            if updated:
                self._positions_version = getattr(self, "_positions_version", 0) + 1
            if hasattr(self, "app") and self.app and self.app.loop:
                self.app.loop.call_soon_threadsafe(self._update_display_and_invalidate)

    def _update_display_and_invalidate(self) -> None:
        """Helper to re-render positions and orders and invalidate TUI."""
        self._update_positions_display()
        if self.info_mode in ("orders_pending", "orders_executed"):
            self._update_info_buffer(reset_scroll=False)
        if hasattr(self, "app") and self.app:
            self.app.invalidate()

    def _refresh_indices_header(self) -> None:
        """Re-render the NIFTY/SENSEX/INDIA VIX header from ``self.index_values``
        and invalidate the TUI. Safe to call from the event loop thread."""
        self.market_indices_control.text = self._render_indices_html()
        if hasattr(self, "app") and self.app:
            self.app.invalidate()

    def _render_indices_html(self) -> HTML:
        """Build formatted market-index header tailored to the Right Pane width."""
        total_cols = 100
        if hasattr(self, "app") and self.app and hasattr(self.app, "output") and self.app.output:
            try:
                cols = self.app.output.get_size().columns
                if isinstance(cols, int):
                    total_cols = cols
            except Exception:
                pass

        # The indices bar is rendered inside the Right Pane, so compute right pane width
        lw = getattr(self, "left_pane_weight", 50)
        rw = getattr(self, "right_pane_weight", 50)
        pane_cols = int(total_cols * (rw / max(1, lw + rw)))

        def fmt_item(val, chg, is_vix: bool = False, compact: bool = False):
            if val is None:
                return "<style fg='#ffffff'>N/A</style>"
            val_str = f"{val:,.2f}"
            if chg is None:
                return f"<style fg='#ffffff'>{val_str}</style>"

            # For VIX: negative change (volatility drop) is green, positive (volatility spike) is red
            # For NIFTY/SENSEX: positive is green, negative is red
            if is_vix:
                color = "#00ff00" if chg <= 0 else "#ff5f5f"
            else:
                color = "#00ff00" if chg >= 0 else "#ff5f5f"

            sign = "+" if chg >= 0 else ""
            if compact:
                # In tight right-pane spaces, round large changes to integers e.g. (+80) instead of (+80.50)
                chg_str = f"{sign}{int(round(chg)):,d}" if not is_vix else f"{sign}{chg:.2f}"
            else:
                chg_str = f"{sign}{chg:,.2f}"
            return f"<style fg='{color}'>{val_str} ({chg_str})</style>"

        if pane_cols >= 75:
            # Full width: NIFTY 50: 24,234.85 (-99.80) │ SENSEX: 77,708.24 (-441.41) │ IV: 13.45 (+0.25)
            nifty_html = fmt_item(self.index_values.get("nifty"), self.index_values.get("nifty_change"))
            sensex_html = fmt_item(self.index_values.get("sensex"), self.index_values.get("sensex_change"))
            vix_html = fmt_item(self.index_values.get("vix"), self.index_values.get("vix_change"), is_vix=True)
            return HTML(
                f" <ansicyan><b>NIFTY 50:</b></ansicyan> {nifty_html} "
                f"│ <ansiyellow><b>SENSEX:</b></ansiyellow> {sensex_html} "
                f"│ <ansired><b>IV:</b></ansired> {vix_html}"
            )
        elif pane_cols >= 55:
            # Medium width: NIFTY: 24,234.85 (-100) │ SENSEX: 77,708.24 (-441) │ IV: 13.45 (+0.25)
            nifty_html = fmt_item(self.index_values.get("nifty"), self.index_values.get("nifty_change"), compact=True)
            sensex_html = fmt_item(self.index_values.get("sensex"), self.index_values.get("sensex_change"), compact=True)
            vix_html = fmt_item(self.index_values.get("vix"), self.index_values.get("vix_change"), is_vix=True, compact=True)
            return HTML(
                f"<ansicyan><b>NIFTY:</b></ansicyan> {nifty_html} "
                f"│ <ansiyellow><b>SENSEX:</b></ansiyellow> {sensex_html} "
                f"│ <ansired><b>IV:</b></ansired> {vix_html}"
            )
        else:
            # Ultra compact for narrow right pane (< 55 cols): N: 24,234.85 │ S: 77,708.24 │ IV: 13.45
            def fmt_val_only(val, chg, is_vix=False):
                if val is None:
                    return "<style fg='#ffffff'>N/A</style>"
                val_str = f"{val:,.2f}"
                if chg is None:
                    return f"<style fg='#ffffff'>{val_str}</style>"
                color = ("#00ff00" if chg <= 0 else "#ff5f5f") if is_vix else ("#00ff00" if chg >= 0 else "#ff5f5f")
                return f"<style fg='{color}'>{val_str}</style>"

            nifty_html = fmt_val_only(self.index_values.get("nifty"), self.index_values.get("nifty_change"))
            sensex_html = fmt_val_only(self.index_values.get("sensex"), self.index_values.get("sensex_change"))
            vix_html = fmt_val_only(self.index_values.get("vix"), self.index_values.get("vix_change"), is_vix=True)
            return HTML(
                f"<ansicyan><b>N:</b></ansicyan> {nifty_html} "
                f"│ <ansiyellow><b>S:</b></ansiyellow> {sensex_html} "
                f"│ <ansired><b>IV:</b></ansired> {vix_html}"
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
        """Trigger an immediate fetch of positions, orders, margins, and indices from Zerodha.
        Uses a debounced non-overlapping loop to prevent concurrent API execution.
        """
        if getattr(self, "_refresh_in_progress", False):
            self._refresh_pending = True
            return

        self._refresh_in_progress = True
        self._refresh_pending = False
        try:
            while True:
                api_keys = [acct["api_key"] for acct in self.accounts]
                errors_this_cycle = 0
                cycle_failed_keys = set()

                # Fetch positions, margins, orders, indices (and holdings) concurrently.
                positions_task = self._run_api_call(self.client.get_positions, api_keys)
                margins_task   = self._run_api_call(self.client.get_margins, api_keys)
                orders_task    = self._run_api_call(self.client.get_orders, api_keys)
                indices_task   = self._run_api_call(self.client.get_market_indices)
                
                fetch_holdings = (self.info_mode == "holdings" or getattr(self, "last_holdings_response", None) is None)
                holdings_task  = self._run_api_call(self.client.get_holdings, api_keys) if fetch_holdings else None

                tasks = [positions_task, margins_task, orders_task, indices_task]
                if holdings_task:
                    tasks.append(holdings_task)

                gather_results = await asyncio.gather(*tasks, return_exceptions=True)
                response = gather_results[0]
                margins_resp = gather_results[1]
                orders_resp = gather_results[2]
                indices_resp = gather_results[3]
                holdings_resp = gather_results[4] if holdings_task and len(gather_results) > 4 else None

                if holdings_resp is not None and not isinstance(holdings_resp, BaseException):
                    self.last_holdings_response = holdings_resp
                    if self.info_mode == "holdings":
                        self._update_info_buffer(reset_scroll=False)

                # 1. Process positions response
                if isinstance(response, BaseException):
                    errors_this_cycle += 1
                    for k in api_keys:
                        cycle_failed_keys.add(k)
                    if self._ws_should_log("rest_err:positions", min_interval=30.0):
                        self.log_message(f"[#ff8700]Positions fetch failed:[/#] {self._clean_error(str(response))}")
                elif isinstance(response, dict) and "accounts" in response:
                    old_response = getattr(self, "last_positions_response", None)
                    old_accounts_map = {}
                    if isinstance(old_response, dict):
                        old_accounts_map = {
                            acct.get("api_key"): acct
                            for acct in old_response.get("accounts", [])
                        }
                    
                    for acct in response.get("accounts", []):
                        api_key = acct.get("api_key")
                        status = acct.get("status", "")
                        # If this specific account failed REST fetch, carry forward its previous positions
                        if status.startswith("error") or status == "unauthenticated":
                            errors_this_cycle += 1
                            cycle_failed_keys.add(api_key)
                            if self._ws_should_log(f"rest_err:positions:{api_key}", min_interval=30.0):
                                err_detail = status.split("error:")[-1].strip() if "error:" in status else status
                                self.log_message(f"[#ff8700]@{acct.get('name')} positions fetch failed:[/#] {self._clean_error(err_detail)}")
                            old_acct = old_accounts_map.get(api_key)
                            if old_acct:
                                acct["positions"] = old_acct.get("positions", [])
                                acct["total_pnl"] = old_acct.get("total_pnl", 0.0)
                                # Override the error status to keep it from clearing
                                acct["status"] = old_acct.get("status", "success")

                # 2. Process margins response
                if isinstance(margins_resp, BaseException):
                    errors_this_cycle += 1
                    for k in api_keys:
                        cycle_failed_keys.add(k)
                    if self._ws_should_log("rest_err:margins", min_interval=30.0):
                        self.log_message(f"[#ff8700]Margins fetch failed:[/#] {self._clean_error(str(margins_resp))}")
                elif isinstance(margins_resp, dict):
                    margin_map = {m["api_key"]: m for m in margins_resp.get("accounts", [])}
                    # Check for individual account errors in margins response
                    for m in margins_resp.get("accounts", []):
                        api_key = m.get("api_key")
                        status = m.get("status", "")
                        if status.startswith("error") or status == "unauthenticated":
                            errors_this_cycle += 1
                            cycle_failed_keys.add(api_key)
                            if self._ws_should_log(f"rest_err:margins:{api_key}", min_interval=30.0):
                                err_detail = status.split("error:")[-1].strip() if "error:" in status else status
                                self.log_message(f"[#ff8700]@{self._get_account_name(api_key)} margins fetch failed:[/#] {self._clean_error(err_detail)}")
                    
                    # Keep old margin data if the current one has error or is missing
                    for k, old_m in getattr(self, "margins_by_api_key", {}).items():
                        if k not in margin_map or margin_map[k].get("status", "").startswith("error"):
                            margin_map[k] = old_m
                    self.margins_by_api_key = margin_map
                    if isinstance(response, dict):
                        for acct in response.get("accounts", []):
                            key = acct.get("api_key", "")
                            m = margin_map.get(key, {})
                            acct["margin_net"]  = m.get("net")
                            acct["margin_cash"] = m.get("cash")

                if isinstance(response, dict):
                    # Re-apply cached real-time WebSocket LTPs to prevent regressing to stale REST data.
                    for acct in response.get("accounts", []):
                        for pos in acct.get("positions", []):
                            pos_token = pos.get("instrument_token")
                            sym = pos.get("tradingsymbol", "")
                            if pos_token is not None:
                                t_int = int(pos_token)
                                # Build reverse map for symbol-based tick matching (Kotak)
                                if sym:
                                    self.ws_token_to_symbol[t_int] = sym
                                if t_int in self.ws_ltp_cache:
                                    cached_ltp = self.ws_ltp_cache[t_int]
                                    pos["last_price"] = cached_ltp
                                    qty = pos.get("quantity", 0)
                                    if qty != 0:
                                        avg_price = pos.get("average_price", 0.0)
                                        pos["pnl"] = (cached_ltp - avg_price) * qty
                                        if avg_price > 0:
                                            pos["pnl_pct"] = (pos["pnl"] / (avg_price * abs(qty))) * 100
                                        else:
                                            pos["pnl_pct"] = 0.0
                            elif sym and sym in self.ws_symbol_ltp_cache:
                                # Kotak position: apply cached LTP by symbol
                                cached_ltp = self.ws_symbol_ltp_cache[sym]
                                pos["last_price"] = cached_ltp
                                qty = pos.get("quantity", 0)
                                if qty != 0:
                                    avg_price = pos.get("average_price", 0.0)
                                    pos["pnl"] = (cached_ltp - avg_price) * qty
                                    if avg_price > 0:
                                        pos["pnl_pct"] = (pos["pnl"] / (avg_price * abs(qty))) * 100
                                    else:
                                        pos["pnl_pct"] = 0.0
                        acct["total_pnl"] = sum(p.get("pnl", 0.0) for p in acct.get("positions", []))

                    self.last_positions_response = response
                    self._positions_version = getattr(self, "_positions_version", 0) + 1

                    from cli.greeks import get_position_expiry_info

                    self.active_positions = []
                    self.position_id_map = {}
                    pos_idx = 1
                    new_tokens = set()
                    for acct in response.get("accounts", []):
                        acct_positions = [p for p in acct.get("positions", []) if p.get("quantity", 0) != 0]
                        # Group by expiry
                        groups = {}
                        for pos in acct_positions:
                            info = get_position_expiry_info(pos)
                            exp_date = info.get("expiry_date")
                            groups.setdefault(exp_date, []).append((info, pos))

                        sorted_exp_keys = sorted([k for k in groups.keys() if k is not None])
                        if None in groups:
                            sorted_exp_keys.append(None)

                        for exp_k in sorted_exp_keys:
                            items = groups[exp_k]
                            items.sort(
                                key=lambda x: (
                                    x[0].get("strike", 0.0),
                                    0 if x[0].get("option_type") == "CE" else (1 if x[0].get("option_type") == "PE" else 2),
                                    str(x[1].get("tradingsymbol", "")),
                                )
                            )
                            for info, pos in items:
                                pos["api_key"] = acct.get("api_key")
                                pos["account_name"] = acct.get("name")
                                # Annotate with lot size so the display layer can show lots.
                                sym = pos.get("tradingsymbol", "")
                                lot_size = self.client.get_nfo_lot_sizes().get(sym, 1)
                                pos["lot_size"] = lot_size
                                pos["lots"] = pos.get("quantity", 0) / lot_size
                                pos["expiry_date"] = exp_k.isoformat() if exp_k else None
                                self.active_positions.append(pos)
                                self.position_id_map[pos_idx] = pos
                                pos_idx += 1
                                if pos.get("instrument_token"):
                                    t_int = int(pos["instrument_token"])
                                    new_tokens.add(t_int)
                                    if sym:
                                        self.ws_token_to_symbol[t_int] = sym
                                        self.ws_symbol_to_token[sym.strip().upper()] = t_int

                # 3. Process orders response
                if isinstance(orders_resp, BaseException):
                    errors_this_cycle += 1
                    for k in api_keys:
                        cycle_failed_keys.add(k)
                    if self._ws_should_log("rest_err:orders", min_interval=30.0):
                        self.log_message(f"[#ff8700]Orders fetch failed:[/#] {self._clean_error(str(orders_resp))}")
                elif isinstance(orders_resp, dict) and "accounts" in orders_resp:
                    old_orders_response = getattr(self, "last_orders_response", None)
                    old_orders_map = {}
                    if isinstance(old_orders_response, dict):
                        old_orders_map = {
                            acct.get("api_key"): acct
                            for acct in old_orders_response.get("accounts", [])
                        }
                    
                    nfo_token_map = self.client.get_nfo_symbol_token_map() if hasattr(self.client, "get_nfo_symbol_token_map") else {}

                    for acct in orders_resp.get("accounts", []):
                        api_key = acct.get("api_key")
                        status = acct.get("status", "")
                        # If this specific account failed REST fetch, carry forward its previous orders
                        if status.startswith("error") or status == "unauthenticated":
                            errors_this_cycle += 1
                            cycle_failed_keys.add(api_key)
                            if self._ws_should_log(f"rest_err:orders:{api_key}", min_interval=30.0):
                                err_detail = status.split("error:")[-1].strip() if "error:" in status else status
                                self.log_message(f"[#ff8700]@{acct.get('name')} orders fetch failed:[/#] {self._clean_error(err_detail)}")
                            old_acct = old_orders_map.get(api_key)
                            if old_acct:
                                acct["orders"] = old_acct.get("orders", [])
                                acct["status"] = old_acct.get("status", "success")

                        # Extract instrument tokens for all orders (especially open/pending limit orders)
                        for ord_item in acct.get("orders", []):
                            sym = ord_item.get("tradingsymbol", "")
                            sym_clean = sym.strip().upper()
                            tok = ord_item.get("instrument_token")
                            if not tok and sym_clean and sym_clean in nfo_token_map:
                                tok = nfo_token_map[sym_clean]
                                ord_item["instrument_token"] = tok
                            if tok:
                                t_int = int(tok)
                                new_tokens.add(t_int)
                                if sym:
                                    self.ws_token_to_symbol[t_int] = sym
                                    self.ws_symbol_to_token[sym_clean] = t_int

                    # Subscribe all position and order instrument tokens together on WebSocket
                    self._update_subscriptions(new_tokens)

                    self.last_orders_response = orders_resp
                    self._last_pending_text  = self._render_orders_pane(orders_resp, "orders_pending")
                    self._last_executed_text = self._render_orders_pane(orders_resp, "orders_executed")
                    if self.info_mode in ("orders_pending", "orders_executed"):
                        self._update_info_buffer(reset_scroll=False)

                    # Trigger context recording for completed orders or partial fills returned via REST (e.g. Kotak / Zerodha)
                    if hasattr(self, "recorder") and self.recorder:
                        for acct in orders_resp.get("accounts", []):
                            key = acct.get("api_key", "")
                            for ord_item in acct.get("orders", []):
                                if str(ord_item.get("status", "")).upper() == "COMPLETE" or int(ord_item.get("filled_quantity", 0)) > 0:
                                    self._handle_executed_order_context(key, ord_item)

                    # Trigger conditional orders returned via REST
                    if hasattr(self, "conditional_orders_map") and self.conditional_orders_map:
                        for acct in orders_resp.get("accounts", []):
                            for ord_item in acct.get("orders", []):
                                oid = str(ord_item.get("order_id", "")).strip()
                                st = str(ord_item.get("status", "")).upper()
                                filled = int(ord_item.get("filled_quantity", 0))
                                if oid in self.conditional_orders_map:
                                    self._check_conditional_triggers(oid, st, filled, ord_item)

                # Indices REST snapshot (WebSocket ticks keep these live thereafter).
                try:
                    if isinstance(indices_resp, BaseException):
                        errors_this_cycle += 1
                        if self._ws_should_log("rest_err:indices", min_interval=30.0):
                            self.log_message(f"[#ff8700]Indices fetch failed:[/#] {self._clean_error(str(indices_resp))}")
                    elif isinstance(indices_resp, dict) and indices_resp.get("status") == "success":
                        self.index_values["nifty"]         = indices_resp.get("nifty")
                        self.index_values["nifty_change"]  = indices_resp.get("nifty_change")
                        self.index_values["sensex"]        = indices_resp.get("sensex")
                        self.index_values["sensex_change"] = indices_resp.get("sensex_change")
                        self.index_values["vix"]           = indices_resp.get("vix")
                        self.index_values["vix_change"]    = indices_resp.get("vix_change")

                        # Cache close prices (last - change) so live WebSocket ticks can dynamically recalculate change
                        for key in ["nifty", "sensex", "vix"]:
                            last_val = self.index_values.get(key)
                            chg_val = self.index_values.get(f"{key}_change")
                            if last_val is not None and chg_val is not None:
                                self.index_close_prices[key] = last_val - chg_val

                        self.market_indices_control.text = self._render_indices_html()
                    elif isinstance(indices_resp, dict):
                        errors_this_cycle += 1
                        msg = indices_resp.get("message", "Unknown error")
                        self.market_indices_control.text = HTML(f"  <style fg='#ff5f5f'>Indices: {msg}</style>")
                except Exception as exc:
                    self.market_indices_control.text = HTML(f"  <style fg='#ff5f5f'>Indices Error: {exc}</style>")

                # Update failed REST keys status
                for k in api_keys:
                    self.account_rest_failed[k] = (k in cycle_failed_keys)

                # Update count of REST errors
                self.rest_error_count = errors_this_cycle

                # Force UI rendering updates on main thread
                if hasattr(self, "app") and self.app and self.app.loop:
                    self.app.loop.call_soon_threadsafe(self._update_display_and_invalidate)
                    self.app.loop.call_soon_threadsafe(self._update_header_display)

                # Debounce: check if a new request was queued during our run
                if getattr(self, "_refresh_pending", False):
                    self._refresh_pending = False
                    continue
                else:
                    break
        finally:
            self._refresh_in_progress = False

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




    def _format_curr(self, val: float | None) -> str:
        if val is None:
            return "N/A"
        abs_v = abs(val)
        if abs_v >= 100000:
            return f"₹{val / 100000:.2f}L"
        elif abs_v >= 1000:
            return f"₹{val / 1000:.2f}k"
        else:
            return f"₹{val:,.2f}"

    def _get_order_margin_summary(
        self,
        api_keys: list[str],
        symbol: str,
        transaction_type: str,
        qty: int,
        price: float | None = None,
        product: str = "NRML",
    ) -> str:
        """Fetch margin required and available margins for the given accounts."""
        if not api_keys or not symbol:
            return ""

        summary_parts = []
        for key in api_keys:
            name = self._get_account_name(key)
            avail_margin = None
            m_dict = getattr(self, "margins_by_api_key", {}).get(key, {})
            if isinstance(m_dict, dict):
                avail_margin = m_dict.get("net")
                if avail_margin is None:
                    avail_margin = m_dict.get("cash")
                if avail_margin is None and isinstance(m_dict.get("available"), dict):
                    avail_margin = m_dict["available"].get("live_balance")

            try:
                m_res = self.client.get_order_margin(
                    api_key=key,
                    tradingsymbol=symbol,
                    transaction_type=transaction_type,
                    quantity=qty,
                    price=price,
                    product=product,
                )
                if isinstance(m_res, dict) and m_res.get("status") == "success":
                    req_margin = m_res.get("total", 0.0)
                    req_str = f"Needed {self._format_curr(req_margin)}"
                    avail_str = f"Avail {self._format_curr(avail_margin)}" if avail_margin is not None else ""
                    if avail_str:
                        summary_parts.append(f"@{name}: {req_str} │ {avail_str}")
                    else:
                        summary_parts.append(f"@{name}: {req_str}")
            except Exception as exc:
                pass

        if summary_parts:
            if len(summary_parts) == 1:
                return f"\n  👉 [bold]{summary_parts[0]}[/bold]"
            else:
                lines = [f"\n  • [bold]{s}[/bold]" for s in summary_parts]
                return "".join(lines)
        return ""

    def _get_order_margin_summary_inline(
        self,
        api_keys: list[str],
        symbol: str,
        transaction_type: str,
        qty: int,
        price: float | None = None,
        product: str = "NRML",
    ) -> str:
        """Fetch margin required and available margins as a concise inline string for prompt line."""
        if not api_keys or not symbol:
            return ""

        summary_parts = []
        for key in api_keys:
            name = self._get_account_name(key)
            avail_margin = None
            m_dict = getattr(self, "margins_by_api_key", {}).get(key, {})
            if isinstance(m_dict, dict):
                avail_margin = m_dict.get("net")
                if avail_margin is None:
                    avail_margin = m_dict.get("cash")
                if avail_margin is None and isinstance(m_dict.get("available"), dict):
                    avail_margin = m_dict["available"].get("live_balance")

            try:
                m_res = self.client.get_order_margin(
                    api_key=key,
                    tradingsymbol=symbol,
                    transaction_type=transaction_type,
                    quantity=qty,
                    price=price,
                    product=product,
                )
                if isinstance(m_res, dict) and m_res.get("status") == "success":
                    req_margin = m_res.get("total", 0.0)
                    req_str = f"Need: {self._format_curr(req_margin)}"
                    avail_str = f"Avail: {self._format_curr(avail_margin)}" if avail_margin is not None else ""
                    if len(api_keys) > 1:
                        summary_parts.append(f"@{name}: {req_str}" + (f"/{avail_str}" if avail_str else ""))
                    else:
                        summary_parts.append(f"{req_str}" + (f" │ {avail_str}" if avail_str else ""))
            except Exception:
                pass

        if summary_parts:
            return " [" + " │ ".join(summary_parts) + "]"
        return ""

    def _handle_executed_order_context(self, api_key: str, data: dict) -> None:
        """Record enriched context when an order executes successfully (`COMPLETE`)."""
        if not getattr(self, "record_context_enabled", False):
            return

        status = str(data.get("status", "")).upper()
        raw_filled = data.get("filled_quantity")
        if raw_filled is not None and int(raw_filled) > 0:
            filled_qty = int(raw_filled)
        elif status == "COMPLETE":
            filled_qty = int(data.get("quantity") or 0)
        else:
            filled_qty = 0

        if status != "COMPLETE" and filled_qty == 0:
            return

        order_id = str(data.get("order_id", "")).strip()
        if not order_id:
            import time as _t
            order_id = f"LIVE_{api_key}_{data.get('tradingsymbol')}_{_t.time_ns()}"

        recorded_dict = getattr(self, "_executed_order_ids_recorded_dict", {})
        last_recorded_qty = recorded_dict.get(order_id, 0)
        last_status = getattr(self, "_executed_order_status_dict", {}).get(order_id, "")

        if filled_qty <= last_recorded_qty and order_id in recorded_dict and last_status == status:
            return

        recorded_dict[order_id] = filled_qty
        self._executed_order_ids_recorded_dict = recorded_dict

        if not hasattr(self, "_executed_order_status_dict"):
            self._executed_order_status_dict = {}
        self._executed_order_status_dict[order_id] = status

        executed_set = getattr(self, "_executed_order_ids_recorded", set())
        executed_set.add(order_id)
        self._executed_order_ids_recorded = executed_set

        # Use filled_quantity as effective order quantity for DB and charges calculation
        data_copy = dict(data)
        data_copy["quantity"] = filled_qty
        if filled_qty > 0:
            data_copy["status"] = "COMPLETE"

        user_id = self.api_key_to_user_id.get(api_key, "UNKNOWN")
        account_name = self._get_account_name(api_key)
        m_dict = getattr(self, "margins_by_api_key", {}).get(api_key, {})
        margin_net = m_dict.get("net")
        margin_cash = m_dict.get("cash")
        margins = {"margin_net": margin_net, "margin_cash": margin_cash}

        # Calculate Option Greeks if option contract
        symbol = data_copy.get("tradingsymbol", "")
        greeks = {"delta": None, "gamma": None, "theta": None, "vega": None, "iv": None}
        if any(x in symbol for x in ["CE", "PE", "CALL", "PUT"]):
            try:
                from cli.greeks import calculate_greeks_for_symbol
                spot = self.index_values.get("nifty") or 24500.0
                vix = self.index_values.get("vix") or 15.0
                greeks = calculate_greeks_for_symbol(symbol=symbol, spot_price=spot, vix_pct=vix)
            except Exception as exc:
                logger.warning("Greeks calculation skipped for %s: %s", symbol, exc)

        # Get Daily Indicators (1st order of day lazy fetch + cached math)
        indicators = {"nifty_ema_9": None, "nifty_sma_20": None, "nifty_sma_50": None, "nifty_rsi_14": None}
        try:
            from cli.indicators import calculate_daily_indicators
            spot = self.index_values.get("nifty") or 0.0
            if spot > 0:
                indicators = calculate_daily_indicators(self.client, spot)
        except Exception as exc:
            logger.warning("Daily indicators calculation failed for %s: %s", symbol, exc)

        # Collect positions (includes both open and closed positions for realised P&L calculation)
        all_positions = []
        if isinstance(getattr(self, "last_positions_response", None), dict):
            for acct in self.last_positions_response.get("accounts", []):
                for p in acct.get("positions", []):
                    all_positions.append(p)
        if not all_positions:
            all_positions = getattr(self, "active_positions", [])

        # Calculate estimated order charges
        from cli.charges import calculate_option_charges
        o_price = float(data_copy.get("average_price") or data_copy.get("price") or 0.0)
        o_qty = filled_qty
        tt = str(data_copy.get("transaction_type", "")).upper()
        exch = str(data_copy.get("exchange") or "NFO")
        if tt in ["B", "BUY"]:
            chg_dict = calculate_option_charges(o_price, 0.0, o_qty, 1, 0, exch)
        else:
            chg_dict = calculate_option_charges(0.0, o_price, o_qty, 0, 1, exch)

        # Determine position intent (OPEN_LONG, OPEN_SHORT, CLOSE_LONG, CLOSE_SHORT)
        pos_intent = None
        sym_pos_qty = 0
        for p in all_positions:
            if p.get("tradingsymbol") == symbol:
                sym_pos_qty = int(p.get("quantity", 0))
                break

        if tt in ["B", "BUY"]:
            pos_intent = "OPEN_LONG" if sym_pos_qty >= 0 else "CLOSE_SHORT"
        else:
            pos_intent = "OPEN_SHORT" if sym_pos_qty <= 0 else "CLOSE_LONG"

        if hasattr(self, "recorder") and self.recorder:
            self.recorder.enqueue_executed_order_context(
                user_id=user_id,
                order_data=data_copy,
                margins=margins,
                greeks=greeks,
                indicators=indicators,
                index_values=self.index_values,
                positions=all_positions,
                account_name=account_name,
                charges=chg_dict,
                pos_intent=pos_intent,
            )

    def _init_conditional_orders(self) -> None:
        """Re-hydrate active pending conditional orders from SQLite and expire previous sessions."""
        if not getattr(self, "recorder", None):
            return
        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            # 1. Expire orders from previous trading sessions
            expired_cnt = self.recorder.expire_old_conditional_orders(today_str)
            if expired_cnt > 0:
                logger.info("Expired %d stale conditional order(s) from previous days.", expired_cnt)

            # 2. Re-hydrate pending conditional orders for today
            pending = self.recorder.get_pending_conditional_orders(today_str)
            for cond in pending:
                p_ids = cond.get("parent_order_ids", [])
                for oid in p_ids:
                    self.conditional_orders_map[str(oid)] = cond
            if pending:
                logger.info("Re-hydrated %d active conditional order(s) for today.", len(pending))
        except Exception as exc:
            logger.warning("Failed to initialize conditional orders: %s", exc)

    def _init_scheduled_orders(self) -> None:
        """Re-hydrate active pending scheduled orders from SQLite and expire old ones."""
        if not getattr(self, "recorder", None):
            return
        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            # 1. Expire past scheduled orders older than today
            expired_cnt = self.recorder.expire_old_scheduled_orders(today_str)
            if expired_cnt > 0:
                logger.info("Expired %d stale scheduled order(s) from past dates.", expired_cnt)

            # 2. Re-hydrate pending scheduled orders
            pending = self.recorder.get_pending_scheduled_orders()
            self.scheduled_orders_map.clear()
            for sched in pending:
                sid = sched.get("id")
                if sid:
                    self.scheduled_orders_map[sid] = sched
            if pending:
                logger.info("Re-hydrated %d active scheduled order(s).", len(pending))
        except Exception as exc:
            logger.warning("Failed to initialize scheduled orders: %s", exc)

    def register_scheduled_order(self, cmd_obj: Any, command_raw: str = "", api_keys: list[str] = []) -> int:
        """Register a scheduled standalone order or conditional chain into SQLite and timer map."""
        is_cond = isinstance(cmd_obj, ConditionalOrderCommand)
        if is_cond:
            parent = cmd_obj.parent_cmd
            child_raw = cmd_obj.child_raw_text
            trig_dt = getattr(parent, "trigger_time", None)
            act = getattr(parent, "action", "EXIT")
            sym = getattr(parent, "symbol_or_id", getattr(parent, "target", None))
            qty = getattr(parent, "quantity", None)
            price = getattr(parent, "price", None)
            prod = getattr(parent, "product", "NRML")
        else:
            trig_dt = getattr(cmd_obj, "trigger_time", None)
            act = getattr(cmd_obj, "action", "EXIT" if isinstance(cmd_obj, ExitCommand) else "BUY")
            sym = getattr(cmd_obj, "symbol_or_id", getattr(cmd_obj, "target", None))
            qty = getattr(cmd_obj, "quantity", None)
            price = getattr(cmd_obj, "price", None)
            prod = getattr(cmd_obj, "product", "NRML")
            child_raw = None

        if not trig_dt:
            trig_dt = datetime.now()

        trig_str = trig_dt.strftime("%Y-%m-%d %H:%M:%S")
        target_acct_key = api_keys[0] if api_keys else (self.selected_account_api_key or "ALL")
        target_acct_name = self._get_account_name(target_acct_key) if target_acct_key != "ALL" else "ALL"

        sched_id = self.recorder.save_scheduled_order(
            trigger_time=trig_str,
            command_raw=command_raw,
            action=act,
            symbol_or_id=sym,
            quantity=qty,
            price=price,
            product=prod,
            account_key=target_acct_key,
            account_name=target_acct_name,
            is_conditional=is_cond,
            child_command_raw=child_raw,
        )

        sched_entry = {
            "id": sched_id,
            "trigger_time": trig_str,
            "command_raw": command_raw,
            "action": act,
            "symbol_or_id": sym,
            "quantity": qty,
            "price": price,
            "product": prod,
            "account_key": target_acct_key,
            "account_name": target_acct_name,
            "is_conditional": 1 if is_cond else 0,
            "child_command_raw": child_raw,
            "status": "PENDING",
        }
        self.scheduled_orders_map[sched_id] = sched_entry

        cond_suffix = f" -> [THEN {child_raw}]" if is_cond else ""
        price_disp = f"@{price:.2f}" if price else "@MKT"
        self.log_message(
            f"[#00e5ff]⏰ Scheduled Order #{sched_id} Registered [@{target_acct_name}]:[/#] "
            f"{act} {qty or ''} {sym or ''} {price_disp} at {trig_str}{cond_suffix}"
        )
        if hasattr(self, "app") and self.app and self.app.loop:
            asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)
        return sched_id

    async def _poll_scheduled_orders(self) -> None:
        """Background poller checking pending scheduled orders every 500ms."""
        while self.running:
            try:
                if getattr(self, "scheduled_orders_map", None):
                    now = datetime.now()
                    for sched_id, sched in list(self.scheduled_orders_map.items()):
                        if sched.get("status") != "PENDING":
                            continue

                        trig_str = sched.get("trigger_time")
                        if not trig_str:
                            continue

                        try:
                            if "T" in trig_str:
                                trig_dt = datetime.fromisoformat(trig_str)
                            else:
                                trig_dt = datetime.strptime(trig_str, "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            try:
                                trig_dt = datetime.fromisoformat(trig_str)
                            except Exception:
                                continue

                        if now >= trig_dt:
                            drift_sec = (now - trig_dt).total_seconds()
                            if drift_sec > 60.0:
                                sched["status"] = "EXPIRED"
                                if getattr(self, "recorder", None):
                                    self.recorder.update_scheduled_order_status(
                                        sched_id, "EXPIRED", failure_reason=f"Trigger window missed (drift: {int(drift_sec)}s)"
                                    )
                                self.scheduled_orders_map.pop(sched_id, None)
                                self.log_message(
                                    f"[#ff0000]⏰ Scheduled Order #{sched_id} Expired:[/#] "
                                    f"Missed trigger window by {int(drift_sec)}s (system sleep or clock jump)."
                                )
                                continue

                            sched["status"] = "TRIGGERED"
                            self.scheduled_orders_map.pop(sched_id, None)
                            asyncio.create_task(self._execute_scheduled_trigger(sched_id, sched))
            except Exception as exc:
                logger.error("Error in _poll_scheduled_orders loop: %s", exc)

            await asyncio.sleep(0.5)

    async def _execute_scheduled_trigger(self, sched_id: int, sched: dict) -> None:
        """Execute a scheduled order or conditional chain upon trigger time arrival."""
        is_cond = bool(sched.get("is_conditional"))
        action = str(sched.get("action", "")).upper()
        symbol_or_id = sched.get("symbol_or_id")
        qty = sched.get("quantity")
        price = sched.get("price")
        product = sched.get("product") or "NRML"
        acct_key = sched.get("account_key")

        price_str = f"@{price:.2f}" if price else "@MKT"
        self.log_message(
            f"[#00ff00]⏰ Scheduled Trigger Fired (#{sched_id}):[/#] "
            f"{action} {qty or ''} {symbol_or_id or ''} {price_str}"
        )

        try:
            api_keys = [acct_key] if (acct_key and acct_key != "ALL") else []
            if is_cond:
                child_raw = sched.get("child_command_raw") or ""
                parent_cmd = PlaceOrderCommand(
                    action=action,
                    symbol_or_id=symbol_or_id,
                    quantity=qty,
                    price=price,
                    product=product,
                )
                try:
                    child_cmd = parse_single_command(child_raw)
                except Exception:
                    parts_child = child_raw.split()
                    if parts_child:
                        act_raw = parts_child[0].upper()
                        act = "BUY" if act_raw in ("BUY", "B") else ("SELL" if act_raw in ("SELL", "S") else "EXIT")
                        price_val = float(parts_child[1]) if len(parts_child) >= 2 else None
                        child_cmd = PlaceOrderCommand(action=act, symbol_or_id=None, quantity=None, price=price_val)
                    else:
                        child_cmd = ExitCommand(target="selected")

                cond_cmd = ConditionalOrderCommand(
                    parent_cmd=parent_cmd,
                    child_cmd=child_cmd,
                    child_raw_text=child_raw,
                )
                await self.execute_conditional_order(cond_cmd, api_keys=api_keys)
                if getattr(self, "recorder", None):
                    self.recorder.update_scheduled_order_status(sched_id, "TRIGGERED", executed_at=datetime.now().isoformat())

            elif action == "EXIT":
                target = symbol_or_id or "selected"
                await self.execute_exit(symbol=target, api_keys=api_keys, price=price)
                if getattr(self, "recorder", None):
                    self.recorder.update_scheduled_order_status(sched_id, "TRIGGERED", executed_at=datetime.now().isoformat())

            else:
                # Resolve symbol if ID
                sym = symbol_or_id
                if sym and sym.isdigit():
                    sym, api_keys = resolve_symbol_or_id(
                        ExecutionContext(self.client, self.active_positions, self.position_id_map, self.selected_symbol, self.selected_account_api_key or "ALL"),
                        sym
                    )
                elif sym:
                    sym = sym.upper()
                else:
                    sym = self.selected_symbol

                if not sym:
                    raise ValueError("No symbol specified and no active selection.")

                if not qty:
                    matching_positions = [
                        p for p in self.active_positions
                        if p.get("tradingsymbol") == sym and (not api_keys or p.get("api_key") in api_keys) and abs(p.get("quantity", 0)) > 0
                    ]
                    if not matching_positions:
                        raise ValueError(f"Omitted quantity for {sym}, but no matching open position found.")
                    for p in matching_positions:
                        p_key = p["api_key"]
                        p_qty = str(abs(p.get("quantity", 0)))
                        await self.execute_order(
                            symbol=sym,
                            transaction_type=action,
                            qty_str=p_qty,
                            price_str=str(price) if price else None,
                            product=product,
                            api_keys=[p_key],
                        )
                else:
                    qty_val, _, qty_err = self._resolve_qty(qty, sym)
                    if qty_err:
                        raise ValueError(qty_err)
                    await self.execute_order(
                        symbol=sym,
                        transaction_type=action,
                        qty_str=qty_val,
                        price_str=str(price) if price else None,
                        product=product,
                        api_keys=api_keys,
                    )
                if getattr(self, "recorder", None):
                    self.recorder.update_scheduled_order_status(sched_id, "TRIGGERED", executed_at=datetime.now().isoformat())

        except Exception as exc:
            logger.error("Failed to execute scheduled order #%d: %s", sched_id, exc, exc_info=True)
            self.log_message(f"[#ff0000]✗ Scheduled Order #{sched_id} Execution Failed:[/#] {exc}")
            if getattr(self, "recorder", None):
                self.recorder.update_scheduled_order_status(sched_id, "FAILED", failure_reason=str(exc))
        finally:
            if hasattr(self, "app") and self.app and self.app.loop:
                asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)

    def _check_conditional_triggers(self, order_id: str, status: str, filled_qty: int = 0, ord_data: dict | None = None) -> None:
        """Check if an order update completes or cancels any pending conditional child orders."""
        oid = str(order_id).strip()
        if not oid or not hasattr(self, "conditional_orders_map") or oid not in self.conditional_orders_map:
            return

        cond = self.conditional_orders_map[oid]
        if cond.get("status") != "PENDING":
            return

        status_upper = str(status).upper()
        p_ids = [str(x) for x in cond.get("parent_order_ids", [])]

        if status_upper == "COMPLETE":
            completed_legs = cond.setdefault("_completed_legs", set())
            completed_legs.add(oid)

            # Trigger only when all parent legs are complete (if parent was sliced across multiple orders)
            if all(p in completed_legs for p in p_ids):
                cond["status"] = "TRIGGERED"
                cond_id = cond.get("id")

                # Update SQLite status
                if getattr(self, "recorder", None) and cond_id:
                    self.recorder.update_conditional_order_status(cond_id, "TRIGGERED")

                # Remove from active watch map
                for p in p_ids:
                    self.conditional_orders_map.pop(p, None)

                # Prepare child order execution
                child_action = str(cond.get("child_action", "SELL")).upper()
                child_symbol = cond.get("child_symbol") or cond.get("parent_symbol")
                child_qty_raw = cond.get("child_qty") or str(cond.get("parent_qty", 1))
                child_price = cond.get("child_price")
                child_product = cond.get("child_product", "NRML")
                acct_key = cond.get("account_key")
                target_keys = [acct_key] if (acct_key and acct_key != "ALL") else []

                self.log_message(
                    f"[#00ff00]⚡ CONDITIONAL TRIGGERED:[/#] Parent order {oid} is COMPLETE. "
                    f"Placing secondary order: [bold]{cond.get('child_command_raw')}[/bold]..."
                )

                if child_action == "EXIT":
                    self._run_coroutine_threadsafe(
                        self.execute_exit(
                            child_symbol,
                            api_keys=target_keys,
                            price=child_price,
                        )
                    )
                else:
                    self._run_coroutine_threadsafe(
                        self.execute_order(
                            child_symbol,
                            child_action,
                            str(child_qty_raw),
                            str(child_price) if child_price else None,
                            child_product,
                            api_keys=target_keys,
                        )
                    )

                if hasattr(self, "app") and self.app and self.app.loop:
                    asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)

        elif status_upper in ("CANCELLED", "REJECTED"):
            cond["status"] = "CANCELLED"
            cond_id = cond.get("id")
            if getattr(self, "recorder", None) and cond_id:
                self.recorder.update_conditional_order_status(
                    cond_id,
                    "CANCELLED",
                    failure_reason=f"Parent order {oid} was {status_upper}",
                )

            for p in p_ids:
                self.conditional_orders_map.pop(p, None)

            self.log_message(
                f"[#ff5555]⚠️ CONDITIONAL CANCELLED:[/#] Parent order {oid} was {status_upper}. "
                f"Child order '{cond.get('child_command_raw')}' will not execute."
            )
            if hasattr(self, "app") and self.app and self.app.loop:
                asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)

    async def execute_conditional_order(self, cond_cmd: ConditionalOrderCommand, api_keys: list[str] = []) -> None:
        """Place primary order and register secondary conditional order in SQLite upon success."""
        parent_cmd = cond_cmd.parent_cmd
        child_cmd = cond_cmd.child_cmd
        child_raw = cond_cmd.child_raw_text

        # 1. Resolve parent symbol & target keys
        if isinstance(parent_cmd, PlaceOrderCommand):
            symbol = parent_cmd.symbol_or_id
            if symbol:
                if symbol.isdigit():
                    symbol, api_keys = resolve_symbol_or_id(
                        ExecutionContext(self.client, self.active_positions, self.position_id_map, self.selected_symbol, self.selected_account_api_key or "ALL"),
                        symbol
                    )
                else:
                    symbol = symbol.upper()
            else:
                if not self.selected_symbol:
                    self.log_message("[#ff0000]Error:[/#] No symbol specified for primary order.")
                    return
                symbol = self.selected_symbol
                if not api_keys and self.selected_account_api_key:
                    api_keys = [self.selected_account_api_key]

            price = parent_cmd.price
            order_type = "LIMIT" if price else "MARKET"
            product = parent_cmd.product or "NRML"
            exchange = "NFO" if (symbol.endswith("CE") or symbol.endswith("PE") or any(symbol.endswith(e) for e in ("FUT", "CE", "PE"))) else "NSE"
            if exchange == "NSE" and product.upper() == "NRML":
                product = "CNC"

            target_keys = api_keys or [a.get("api_key") for a in self.accounts if a.get("api_key")]

            # Resolve per-account quantity if omitted
            account_orders = []
            if not parent_cmd.quantity:
                for k in target_keys:
                    pos = next((p for p in self.active_positions if p.get("tradingsymbol") == symbol and p.get("api_key") == k and p.get("quantity", 0) != 0), None)
                    if not pos:
                        pos = next((p for p in self.active_positions if p.get("tradingsymbol") == symbol and p.get("quantity", 0) != 0), None)
                    if not pos:
                        continue
                    account_orders.append((k, abs(pos.get("quantity", 0))))
                if not account_orders:
                    self.log_message("[#ff0000]Error:[/#] Omitted quantity for primary order, but no matching open position found.")
                    return
            else:
                qty_val, qty_display, qty_err = self._resolve_qty(parent_cmd.quantity, symbol)
                if qty_err:
                    self.log_message(f"[#ff0000]Error in primary order:[/#] {qty_err}")
                    return
                for k in target_keys:
                    account_orders.append((k, int(qty_val)))

            # Execute parent orders
            results = []
            for k, q_int in account_orders:
                name = self._get_account_name(k)
                self.log_message(f"Placing primary {order_type} {parent_cmd.action} order for {q_int} {symbol} on account {name}...")
                try:
                    res = await self._run_api_call(
                        self.client.place_order,
                        api_keys=[k],
                        tradingsymbol=symbol,
                        exchange=exchange,
                        transaction_type=parent_cmd.action.upper(),
                        quantity=q_int,
                        order_type=order_type,
                        price=price,
                        product=product.upper(),
                    )
                    for item in res.get("results", []):
                        item["placed_qty"] = q_int
                        results.append(item)
                except Exception as exc:
                    self.log_message(f"[#ff0000]Primary Order Execution Failed for {name}:[/#] {exc}")

        elif isinstance(parent_cmd, ExitCommand):
            raw_target = parent_cmd.target
            if raw_target.isdigit():
                symbol, api_keys = resolve_symbol_or_id(
                    ExecutionContext(self.client, self.active_positions, self.position_id_map, self.selected_symbol, self.selected_account_api_key or "ALL"),
                    raw_target
                )
            elif raw_target.lower() == "selected":
                symbol = self.selected_symbol
                if not symbol:
                    self.log_message("[#ff0000]Error:[/#] No position selected to exit.")
                    return
            else:
                symbol = raw_target.upper()

            price = parent_cmd.price
            price_desc = f" @{price:.2f}" if price is not None else ""
            self.log_message(f"Exiting open positions for {symbol} across target accounts{price_desc}...")
            try:
                response = await self._run_api_call(
                    self.client.exit_positions,
                    api_keys=api_keys,
                    tradingsymbol=symbol,
                    price=price,
                )
            except Exception as exc:
                self.log_message(f"[#ff0000]Exit Execution Failed:[/#] {exc}")
                return

            results = []
            for res_item in response.get("results", []):
                for placed_pos in res_item.get("orders_placed", []):
                    results.append({
                        "name": res_item.get("name"),
                        "api_key": res_item.get("api_key"),
                        "status": "success",
                        "order_id": placed_pos.get("order_id"),
                        "placed_qty": abs(placed_pos.get("quantity", 0)),
                        "tradingsymbol": placed_pos.get("tradingsymbol", symbol),
                    })

        else:
            self.log_message(f"[#ff0000]Unsupported primary command:[/#] {type(parent_cmd)}")
            return

        has_success = False
        today_str = datetime.now().strftime("%Y-%m-%d")

        # Determine child details
        child_action = "SELL"
        child_sym = symbol
        child_pr = None
        child_prod = "NRML"

        if isinstance(child_cmd, PlaceOrderCommand):
            child_action = child_cmd.action.upper()
            child_sym = child_cmd.symbol_or_id or symbol
            child_pr = child_cmd.price
            child_prod = child_cmd.product or "NRML"
        elif isinstance(child_cmd, ExitCommand):
            child_action = "EXIT"
            child_sym = child_cmd.target if child_cmd.target != "selected" else symbol
            child_pr = child_cmd.price

        for res in results:
            name = res.get("name", "Unknown")
            status = res.get("status", "error")
            msg = res.get("message", "")
            ord_id = res.get("order_id")
            api_k = res.get("api_key")
            u_id = self.api_key_to_user_id.get(api_k, "UNKNOWN") if api_k else "UNKNOWN"
            p_qty = res.get("placed_qty", 0)

            # Child quantity: if child has explicit qty, resolve it; otherwise match parent's placed_qty!
            if isinstance(child_cmd, PlaceOrderCommand) and child_cmd.quantity:
                c_qty_val, _, _ = self._resolve_qty(child_cmd.quantity, child_sym)
                child_q = str(c_qty_val)
            else:
                child_q = str(p_qty)

            if status == "success" and ord_id:
                has_success = True
                leg_ids = [str(x).strip() for x in str(ord_id).split(",") if str(x).strip()]
                self.log_message(f"[#00ff00]✓ {name}:[/#] Primary placed ({p_qty} qty). ID(s): {', '.join(leg_ids)}")

                # Persist conditional order to SQLite
                cond_id = self.recorder.save_conditional_order(
                    trade_date=today_str,
                    parent_order_ids=leg_ids,
                    parent_symbol=symbol,
                    parent_action=getattr(parent_cmd, "action", "EXIT").upper(),
                    parent_qty=p_qty,
                    parent_price=getattr(parent_cmd, "price", None),
                    child_command_raw=child_raw,
                    child_action=child_action,
                    child_symbol=child_sym,
                    child_qty=child_q,
                    child_price=child_pr,
                    child_product=child_prod,
                    user_id=u_id,
                    account_key=api_k,
                    account_name=name,
                )

                # Add to in-memory watch map
                cond_entry = {
                    "id": cond_id,
                    "trade_date": today_str,
                    "parent_order_ids": leg_ids,
                    "parent_symbol": symbol,
                    "parent_action": getattr(parent_cmd, "action", "EXIT").upper(),
                    "parent_qty": p_qty,
                    "child_command_raw": child_raw,
                    "child_action": child_action,
                    "child_symbol": child_sym,
                    "child_qty": child_q,
                    "child_price": child_pr,
                    "child_product": child_prod,
                    "status": "PENDING",
                    "account_key": api_k,
                    "account_name": name,
                }
                for leg_id in leg_ids:
                    self.conditional_orders_map[leg_id] = cond_entry

                self.log_message(
                    f"[#00e5ff]⏳ Conditional Order #{cond_id} Registered [@{name}]:[/#] "
                    f"Will automatically execute '{child_action} {child_q} {child_sym}' once primary Order {', '.join(leg_ids)} is COMPLETE."
                )
            else:
                self.log_message(f"[#ff0000]✗ {name}:[/#] Primary failed — {msg}")

        if has_success:
            if hasattr(self, "app") and self.app and self.app.loop:
                asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)

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
        if qty_arg is None or not str(qty_arg).strip():
            return None, "original qty", None
        cleaned = str(qty_arg).strip()
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
            has_success = False
            for res in results:
                name = res.get("name", "Unknown")
                status = res.get("status", "error")
                msg = res.get("message", "")
                legs = res.get("legs", 1)
                ord_id = res.get("order_id")
                if status == "success":
                    has_success = True
                    if legs > 1:
                        self.log_message(f"[#00ff00]✓ {name}:[/#] Split into {legs} legs. IDs: {ord_id}")
                    else:
                        self.log_message(f"[#00ff00]✓ {name}:[/#] Placed. ID: {ord_id}")
                else:
                    self.log_message(f"[#ff0000]✗ {name}:[/#] Failed — {msg}")

            if has_success:
                if hasattr(self, "app") and self.app and self.app.loop:
                    asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)

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
            has_success = False
            for res in results:
                name = res.get("name", "Unknown")
                status = res.get("status", "error")
                msg = res.get("message", "")
                placed = res.get("orders_placed", [])
                
                if status == "success":
                    has_success = True
                    if placed:
                        symbols_exited = ", ".join(f"{item.get('tradingsymbol')} ({item.get('quantity')})" for item in placed)
                        self.log_message(f"[#00ff00]✓ {name}:[/#] Exited {symbols_exited}")
                    else:
                        self.log_message(f"[#87afaf]~ {name}:[/#] {msg}")
                else:
                    self.log_message(f"[#ff0000]✗ {name}:[/#] Failed — {msg}")

            if has_success:
                if hasattr(self, "app") and self.app and self.app.loop:
                    asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)

        except KCLIClientError as exc:
            self.log_message(f"[#ff0000]Exit Execution Failed:[/#] {exc}")

    async def execute_modify(self, order_id: str, qty_str: str | None, price_str: str, api_key: str) -> None:
        """Modify open order in executor."""
        # 1. Validate quantity (optional if modifying price only)
        qty = None
        if qty_str is not None and str(qty_str).strip() and str(qty_str).strip().lower() != "none":
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

        qty_desc = f"quantity {qty} " if qty is not None else ""
        self.log_message(f"Modifying order {order_id} to {qty_desc}@ {price or 'MARKET'}...")

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
                # Also cancel any attached conditional orders
                if hasattr(self, "recorder") and self.recorder:
                    cancelled_cos = self.recorder.cancel_conditional_orders_by_parent(order_id)
                    if cancelled_cos:
                        to_del = [k for k, v in getattr(self, "conditional_orders_map", {}).items() if v.get("id") in cancelled_cos]
                        for k in to_del:
                            self.conditional_orders_map.pop(k, None)
                        self.log_message(f"[#ff8700]Cancelled linked conditional order(s): {', '.join(f'#{x}' for x in cancelled_cos)}[/#]")
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
        if not getattr(self, "recorder", None):
            return
        user_id = self.api_key_to_user_id.get(api_key) if api_key else None
        try:
            parsed_str = json.dumps(action, default=str) if isinstance(action, dict) else str(action)
        except Exception:
            parsed_str = str(action)
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

    def _extract_custom_dates_from_cmd(self, cmd_text: str) -> list[str]:
        """Extract and parse date tokens into ISO YYYY-MM-DD strings supporting DDMMYYYY, DD-MM-YYYY, DD/MM/YYYY, YYYY-MM-DD."""
        import re
        tokens = re.findall(r"\b\d{8}\b|\b\d{1,2}[-/\.]\d{1,2}[-/\.]\d{4}\b|\b\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}\b", cmd_text)
        parsed = []
        for t in tokens:
            s = t.strip()
            if len(s) == 8 and s.isdigit():
                day, month, year = int(s[:2]), int(s[2:4]), int(s[4:])
                try:
                    parsed.append(datetime(year, month, day).strftime("%Y-%m-%d"))
                    continue
                except ValueError:
                    pass
            formats = [
                "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
                "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
                "%d-%b-%Y", "%d %b %Y"
            ]
            for fmt in formats:
                try:
                    dt = datetime.strptime(s, fmt)
                    parsed.append(dt.strftime("%Y-%m-%d"))
                    break
                except ValueError:
                    pass
        return parsed

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
                        # In price-only mode each entry carries its own qty; fall back to top-level qty otherwise
                        effective_qty = order_info.get("qty") if p.get("price_only") else p["qty"]
                        asyncio.create_task(
                            self.execute_modify(
                                order_info["order_id"],
                                effective_qty,
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
                elif p["type"] == "conditional":
                    asyncio.create_task(
                        self.execute_conditional_order(
                            p["cmd"],
                            api_keys=p.get("api_keys", []),
                        )
                    )
                elif p["type"] == "scheduled":
                    self.register_scheduled_order(
                        cmd_obj=p["cmd"],
                        command_raw=p.get("command_raw", ""),
                        api_keys=p.get("api_keys", []),
                    )
                else:
                    if p.get("orders"):
                        for o in p["orders"]:
                            asyncio.create_task(
                                self.execute_order(
                                    symbol=p["symbol"],
                                    transaction_type=p["type"].upper(),
                                    qty_str=str(o["qty"]),
                                    price_str=p.get("price"),
                                    product=p.get("product", "NRML"),
                                    api_keys=[o["api_key"]],
                                )
                            )
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

        # Direct commands to switch info pane mode
        cmd_norm = cmd.strip().lower()
        if cmd_norm in ("pending", "p", "1", "/1", "/pending"):
            self.info_mode = "orders_pending"
            self._update_info_buffer()
            if hasattr(self, "app") and self.app:
                self.app.invalidate()
            self.log_message("Switched info pane to [bold]Pending Orders[/bold].")
            return
        elif cmd_norm in ("executed", "e", "ex", "2", "/2", "/executed"):
            self.info_mode = "orders_executed"
            self._update_info_buffer()
            if hasattr(self, "app") and self.app:
                self.app.invalidate()
            self.log_message("Switched info pane to [bold]Executed Orders[/bold].")
            return
        elif cmd_norm in ("oc", "optionchain", "chain", "3", "/3", "/oc"):
            if getattr(self, "oc_data", None):
                self._update_oc_subscriptions(self.oc_data.get("strikes", []))
            self.info_mode = "oc"
            self._update_info_buffer()
            if hasattr(self, "app") and self.app:
                self.app.invalidate()
            self.log_message("Switched info pane to [bold]Option Chain[/bold].")
            return
        elif cmd_norm in ("oc comp", "occomp", "ocdual", "oc dual", "oc side", "oc2", "/oc2", "/occomp", "comp", "dual"):
            if not getattr(self, "oc_dual_data", None):
                api_key = self.primary_api_key or (self.accounts[0]["api_key"] if self.accounts else "")
                und = getattr(self, "oc_data", {}).get("underlying", "NIFTY") if getattr(self, "oc_data", None) else "NIFTY"
                if api_key:
                    asyncio.create_task(self._fetch_and_display_dual_option_chain(api_key, und, 0, 1))
            else:
                strikes1 = self.oc_dual_data.get("oc1", {}).get("strikes", [])
                strikes2 = self.oc_dual_data.get("oc2", {}).get("strikes", [])
                if strikes1 or strikes2:
                    self._update_oc_subscriptions(strikes1 + strikes2)
            self.info_mode = "oc_dual"
            self._update_info_buffer()
            if hasattr(self, "app") and self.app:
                self.app.invalidate()
            self.log_message("Switched info pane to [bold]Option Chain Comparison (Side-by-Side)[/bold].")
            return
        elif cmd_norm in ("advisor", "adv", "ai", "4", "/4", "/advisor"):
            self.info_mode = "advisor"
            self._update_info_buffer()
            if hasattr(self, "app") and self.app:
                self.app.invalidate()
            self.log_message("Switched info pane to [bold]AI Advisor[/bold].")
            return
        elif cmd_norm in ("pnl", "pl", "5", "/5", "/pnl") or cmd_norm.startswith("pnl ") or cmd_norm.startswith("pl "):
            self.info_mode = "pnl"
            dates = self._extract_custom_dates_from_cmd(cmd)
            if dates:
                self.pnl_period = "custom"
                self.pnl_custom_start = dates[0]
                self.pnl_custom_end = dates[1] if len(dates) >= 2 else dates[0]
            elif "last_week" in cmd_norm or "lastweek" in cmd_norm or " lw" in cmd_norm or cmd_norm == "pnl lw" or cmd_norm == "pnl 2":
                self.pnl_period = "last_week"
            elif "week" in cmd_norm or " w" in cmd_norm or cmd_norm == "pnl w" or cmd_norm == "pnl 1":
                self.pnl_period = "week"
            elif "month" in cmd_norm or " m" in cmd_norm or cmd_norm == "pnl m" or cmd_norm == "pnl 3":
                self.pnl_period = "month"
            elif "year" in cmd_norm or " y" in cmd_norm or cmd_norm == "pnl y" or cmd_norm == "pnl 4":
                self.pnl_period = "year"
            elif "custom" in cmd_norm or " c" in cmd_norm or cmd_norm == "pnl c" or cmd_norm == "pnl 5":
                self.pnl_period = "custom"
            else:
                self.pnl_period = "week"
            self._update_info_buffer()
            if hasattr(self, "app") and self.app:
                self.app.invalidate()
            self.log_message(f"Switched info pane to [bold]P&L Analytics ({getattr(self, 'pnl_period', 'week').replace('_', ' ').title()})[/bold].")
            return
        elif self.info_mode == "pnl" and cmd_norm in ("w", "lw", "m", "y", "c", "week", "lastweek", "last_week", "last-week", "month", "year", "custom", "1", "2", "3", "4", "5"):
            period_map = {
                "w": "week", "week": "week", "1": "week",
                "lw": "last_week", "lastweek": "last_week", "last_week": "last_week", "last-week": "last_week", "2": "last_week",
                "m": "month", "month": "month", "3": "month",
                "y": "year", "year": "year", "4": "year",
                "c": "custom", "custom": "custom", "5": "custom",
            }
            self.pnl_period = period_map[cmd_norm]
            self._update_info_buffer()
            if hasattr(self, "app") and self.app:
                self.app.invalidate()
            self.log_message(f"P&L period switched to [bold]{self.pnl_period.replace('_', ' ').title()}[/bold].")
            return
        elif self.info_mode == "pnl":
            dates = self._extract_custom_dates_from_cmd(cmd)
            if dates:
                self.pnl_period = "custom"
                self.pnl_custom_start = dates[0]
                self.pnl_custom_end = dates[1] if len(dates) >= 2 else dates[0]
                self._update_info_buffer()
                if hasattr(self, "app") and self.app:
                    self.app.invalidate()
                self.log_message(f"P&L custom date range set to [bold]{self.pnl_custom_start} -> {self.pnl_custom_end}[/bold].")
                return
        elif cmd_norm in ("md", "depth", "marketdepth", "6", "/6", "/md") or cmd_norm.startswith("md ") or cmd_norm.startswith("depth ") or cmd_norm.startswith("marketdepth "):
            parts = cmd.strip().split()
            if len(parts) >= 2:
                self.md_symbol = parts[1].strip().upper()
            self.info_mode = "md"
            self._update_info_buffer()
            if hasattr(self, "app") and self.app:
                self.app.invalidate()
            self.log_message(f"Switched info pane to [bold]Market Depth ({getattr(self, 'md_symbol', 'None')})[/bold].")
            return
        elif self.info_mode == "md" and not cmd_norm.startswith("/") and " " not in cmd.strip() and len(cmd.strip()) >= 2 and not cmd_norm.isdigit() and cmd_norm not in ("q", "quit", "exit", "help", "h"):
            self.md_symbol = cmd.strip().upper()
            self._update_info_buffer()
            if hasattr(self, "app") and self.app:
                self.app.invalidate()
            self.log_message(f"Market Depth symbol set to [bold]{self.md_symbol}[/bold].")
            return
        elif cmd_norm in ("holdings", "holding", "portfolio", "port", "hld", "7", "/7", "/holdings", "/portfolio", "/port") or cmd_norm.startswith(("holdings ", "holding ", "portfolio ", "port ", "hld ")):
            self.info_mode = "holdings"
            parts = cmd.strip().split()
            if len(parts) >= 2:
                arg1 = parts[1].lower()
                if arg1 in ("deep", "all", "detail", "details"):
                    self.holdings_view_mode = "deep"
                    self.holdings_target_account = parts[2] if len(parts) >= 3 else None
                elif arg1 in ("summary", "sum", "1"):
                    self.holdings_view_mode = "summary"
                    self.holdings_target_account = None
                elif arg1 in ("2",):
                    self.holdings_view_mode = "deep"
                    self.holdings_target_account = None
                else:
                    self.holdings_view_mode = "deep"
                    self.holdings_target_account = parts[1]
            self._update_info_buffer()
            if hasattr(self, "app") and self.app:
                self.app.invalidate()
            view_desc = f"Deep View ({self.holdings_target_account})" if self.holdings_target_account else ("Deep View" if self.holdings_view_mode == "deep" else "Summary")
            self.log_message(f"Switched info pane to [bold]Holdings & Portfolio ({view_desc})[/bold].")
            return
        elif self.info_mode == "holdings" and cmd_norm in ("1", "sum", "summary", "2", "deep", "all"):
            if cmd_norm in ("1", "sum", "summary"):
                self.holdings_view_mode = "summary"
                self.holdings_target_account = None
                self.log_message("Holdings view switched to [bold]Summary[/bold].")
            else:
                self.holdings_view_mode = "deep"
                self.holdings_target_account = None
                self.log_message("Holdings view switched to [bold]Deep View[/bold].")
            self._update_info_buffer()
            if hasattr(self, "app") and self.app:
                self.app.invalidate()
            return
        elif cmd_norm in ("help", "h", "8", "/8", "/help"):
            self.info_mode = "help"
            self._update_info_buffer()
            if hasattr(self, "app") and self.app:
                self.app.invalidate()
            self.log_message("Switched info pane to [bold]Help & Shortcuts[/bold].")
            return

        # Attempt parsing via the unified parser
        try:
            parsed_cmds = parse_command_line(cmd)
            # If parsed commands match trading/API targets, handle them
            if parsed_cmds and any(isinstance(pc, (AccountSelectCommand, OrderSelectCommand, PositionSelectCommand, PlaceOrderCommand, ExitCommand, CancelOrderCommand, ModifyOrderCommand, StatusCommand, PositionsCommand, OrdersCommand, MarketDepthCommand, HoldingsCommand, ConditionalOrderCommand, CancelConditionalOrderCommand, ConditionalOrdersViewCommand)) for pc in parsed_cmds):
                for parsed in parsed_cmds:
                    if isinstance(parsed, HoldingsCommand):
                        self.info_mode = "holdings"
                        self.holdings_view_mode = "deep" if parsed.deep else "summary"
                        self.holdings_target_account = parsed.account
                        self._update_info_buffer()
                        if hasattr(self, "app") and self.app:
                            self.app.invalidate()
                        view_desc = f"Deep View ({self.holdings_target_account})" if self.holdings_target_account else ("Deep View" if self.holdings_view_mode == "deep" else "Summary")
                        self.log_message(f"Switched info pane to [bold]Holdings & Portfolio ({view_desc})[/bold].")
                        continue
                    elif isinstance(parsed, OrderSelectCommand):
                        raw_ord = parsed.target
                        if raw_ord.lower() in ("none", "clear", "null", "empty"):
                            self._log_command(cmd, {"action": "clear_order_selection"}, "success")
                            self.selected_order = None
                            self.update_prompt_label()
                            self.log_message("Order selection cleared.")
                            continue

                        acct_and_order, err = self._find_pending_order(raw_ord)
                        if err:
                            self._log_command(cmd, {"action": "select_order", "query": raw_ord}, "error", err)
                            self.log_message(f"[#ff0000]Error:[/#] {err}")
                            continue

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
                        self.selected_symbol = None
                        self.update_prompt_label()
                        self._log_command(cmd, {"action": "select_order", "order": self.selected_order}, "success", api_key=self.selected_order["api_key"])
                        self.log_message(f"Selected pending order [bold]{self.selected_order['order_id']}[/bold] ({self.selected_order['tradingsymbol']} | {self.selected_order['transaction_type']} | {self.selected_order['quantity']} @ {self.selected_order['price']:.2f}) on @{self.selected_order['account_name']}.")
                        continue

                    elif isinstance(parsed, PositionSelectCommand):
                        raw_id = parsed.target
                        if raw_id.lower() in ("none", "clear", "null", "empty"):
                            self._log_command(cmd, {"action": "clear_position_selection"}, "success")
                            self.selected_symbol = None
                            self.selected_account_name = None
                            self.selected_account_api_key = None
                            self.selected_order = None
                            self.update_prompt_label()
                            self.log_message("Selection cleared.")
                            continue

                        symbol, api_key, err = self.resolve_symbol(raw_id)
                        if err:
                            self._log_command(cmd, {"action": "select_position", "query": raw_id}, "error", err)
                            self.log_message(f"[#ff0000]Error:[/#] {err}")
                            continue

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
                        continue

                    elif isinstance(parsed, MarketDepthCommand):
                        if parsed.symbol:
                            self.md_symbol = parsed.symbol.upper()
                        self.info_mode = "md"
                        self._update_info_buffer()
                        if hasattr(self, "app") and self.app:
                            self.app.invalidate()
                        self.log_message(f"Switched info pane to [bold]Market Depth ({getattr(self, 'md_symbol', 'None')})[/bold].")
                        continue
                    elif isinstance(parsed, AccountSelectCommand):
                        raw_acc = parsed.account_name
                        if raw_acc.lower() in ("none", "clear", "null", "empty", "all"):
                            self.selected_account_name = None
                            self.selected_account_api_key = None
                            self.selected_order = None
                            self.update_prompt_label()
                            self.log_message("Account selection cleared (orders will target all accounts).")
                            continue
                        acct, err = self.resolve_account(raw_acc)
                        if err:
                            self.log_message(f"[#ff0000]Error:[/#] {err}")
                            continue
                        self.selected_account_name = acct.get("name")
                        self.selected_account_api_key = acct.get("api_key")
                        self.selected_order = None
                        self.update_prompt_label()
                        self.log_message(f"Selected account: [bold]{self.selected_account_name}[/bold]. Orders will target this account only.")
                        continue
                        
                    elif isinstance(parsed, PlaceOrderCommand):
                        symbol = parsed.symbol_or_id
                        api_keys = [self.selected_account_api_key] if self.selected_account_api_key else []
                        
                        if symbol:
                            if symbol.isdigit():
                                symbol, api_keys = resolve_symbol_or_id(ExecutionContext(self.client, self.active_positions, self.position_id_map, self.selected_symbol, self.selected_account_api_key or "ALL"), symbol)
                            else:
                                symbol = symbol.upper()
                        else:
                            if not self.selected_symbol:
                                self.log_message("[#ff0000]Error:[/#] No symbol specified and no active selection.")
                                continue
                            symbol = self.selected_symbol
                            if not api_keys and self.selected_account_api_key:
                                api_keys = [self.selected_account_api_key]
                                
                        if not parsed.quantity:
                            matching_positions = [
                                p for p in self.active_positions
                                if p.get("tradingsymbol") == symbol and (not api_keys or p.get("api_key") in api_keys) and abs(p.get("quantity", 0)) > 0
                            ]
                            if not matching_positions:
                                self.log_message(f"[#ff0000]Error:[/#] Omitted quantity for {symbol}, but no open position found.")
                                continue

                            if not api_keys and len(matching_positions) > 1:
                                total_qty = sum(abs(p.get("quantity", 0)) for p in matching_positions)
                                acct_details = ", ".join(f"@{self._get_account_name(p['api_key'])}: {abs(p.get('quantity', 0))}" for p in matching_positions)
                                qty_val = None
                                qty_display = f"position quantities ({acct_details} | Total: {total_qty})"
                                per_acct_orders = [{"api_key": p["api_key"], "qty": abs(p.get("quantity", 0))} for p in matching_positions]
                            else:
                                first_p = matching_positions[0]
                                qty_val = str(abs(first_p.get("quantity", 0)))
                                qty_display = qty_val
                                per_acct_orders = None
                        else:
                            qty_val, qty_display, qty_err = self._resolve_qty(parsed.quantity, symbol)
                            if qty_err:
                                self.log_message(f"[#ff0000]Error:[/#] {qty_err}")
                                continue
                            per_acct_orders = None

                        price_desc = f"@{parsed.price:.2f}" if parsed.price else "MARKET"

                        if parsed.trigger_time:
                            if getattr(self, "_skip_confirmation", False):
                                self.register_scheduled_order(cmd_obj=parsed, command_raw=cmd, api_keys=api_keys)
                                continue
                            self.pending_order = {
                                "type": "scheduled",
                                "cmd": parsed,
                                "command_raw": cmd,
                                "api_keys": api_keys,
                            }
                            self._log_command(
                                cmd_text=cmd,
                                action=self.pending_order,
                                status="pending_confirmation",
                                api_key=api_keys[0] if api_keys else None
                            )
                            self.prompt_control.text = f" Confirm SCHEDULED {parsed.action} {qty_display} {symbol} ({parsed.product}) {price_desc} at {parsed.trigger_time.strftime('%Y-%m-%d %H:%M:%S')}? (y/n)> "
                            self.log_message(f"[#ff8700]Pending Confirmation (Scheduled Order):[/#] {parsed.action} {qty_display} {symbol} {price_desc} ({parsed.product}) at {parsed.trigger_time.strftime('%Y-%m-%d %H:%M:%S')}.\nPress [bold]y[/bold] to confirm.")
                            continue

                        if getattr(self, "_skip_confirmation", False):
                            if per_acct_orders:
                                for o in per_acct_orders:
                                    asyncio.create_task(
                                        self.execute_order(
                                            symbol,
                                            parsed.action.upper(),
                                            str(o["qty"]),
                                            str(parsed.price) if parsed.price else None,
                                            parsed.product,
                                            api_keys=[o["api_key"]]
                                        )
                                    )
                            else:
                                asyncio.create_task(
                                    self.execute_order(
                                        symbol,
                                        parsed.action.upper(),
                                        qty_val,
                                        str(parsed.price) if parsed.price else None,
                                        parsed.product,
                                        api_keys=api_keys
                                    )
                                )
                            continue

                        self.pending_order = {
                            "type": parsed.action.lower(),
                            "symbol": symbol,
                            "qty": qty_val,
                            "orders": per_acct_orders,
                            "price": str(parsed.price) if parsed.price else None,
                            "product": parsed.product,
                            "api_keys": api_keys
                        }
                        
                        self._log_command(
                            cmd_text=cmd,
                            action=self.pending_order,
                            status="pending_confirmation",
                            api_key=api_keys[0] if api_keys else None
                        )
                        
                        margin_summary = self._get_order_margin_summary(
                            api_keys=api_keys,
                            symbol=symbol,
                            transaction_type=parsed.action.upper(),
                            qty=int(qty_val) if str(qty_val).isdigit() else 1,
                            price=parsed.price,
                            product=parsed.product,
                        )
                        margin_inline = self._get_order_margin_summary_inline(
                            api_keys=api_keys,
                            symbol=symbol,
                            transaction_type=parsed.action.upper(),
                            qty=int(qty_val) if str(qty_val).isdigit() else 1,
                            price=parsed.price,
                            product=parsed.product,
                        )
                        self.prompt_control.text = f" Confirm {parsed.action} {qty_display} {symbol} ({parsed.product}) {price_desc}{margin_inline}? (y/n)> "
                        self.log_message(f"[#ff8700]Pending Confirmation:[/#] {parsed.action} {qty_display} {symbol} {price_desc} ({parsed.product}).{margin_summary}\nPress [bold]y[/bold] to confirm.")
                        continue

                    elif isinstance(parsed, ExitCommand):
                        target = parsed.target
                        symbol = None
                        api_keys = []
                        
                        if target == "selected":
                            if not self.selected_symbol:
                                self.log_message("[#ff0000]Error:[/#] No active selection to exit.")
                                continue
                            symbol = self.selected_symbol
                            api_keys = [self.selected_account_api_key] if self.selected_account_api_key else []
                            for p in self.active_positions:
                                if p.get("tradingsymbol") == symbol:
                                    api_keys = [p["api_key"]]
                                    break
                        else:
                            symbol, api_keys = resolve_symbol_or_id(ExecutionContext(self.client, self.active_positions, self.position_id_map, self.selected_symbol, self.selected_account_api_key or "ALL"), target)

                        price_desc = f"@{parsed.price:.2f}" if parsed.price else "MARKET"
                        target_display = symbol if symbol else "ALL"
                        
                        if parsed.trigger_time:
                            if getattr(self, "_skip_confirmation", False):
                                self.register_scheduled_order(cmd_obj=parsed, command_raw=cmd, api_keys=api_keys)
                                continue
                            self.pending_order = {
                                "type": "scheduled",
                                "cmd": parsed,
                                "command_raw": cmd,
                                "api_keys": api_keys,
                            }
                            self._log_command(
                                cmd_text=cmd,
                                action=self.pending_order,
                                status="pending_confirmation",
                                api_key=api_keys[0] if api_keys else None
                            )
                            self.prompt_control.text = f" Confirm SCHEDULED EXIT {target_display} {price_desc} at {parsed.trigger_time.strftime('%Y-%m-%d %H:%M:%S')}? (y/n)> "
                            self.log_message(f"[#ff8700]Pending Confirmation (Scheduled Exit):[/#] EXIT {target_display} positions {price_desc} at {parsed.trigger_time.strftime('%Y-%m-%d %H:%M:%S')}.\nPress [bold]y[/bold] to confirm.")
                            continue

                        if getattr(self, "_skip_confirmation", False):
                            asyncio.create_task(self.execute_exit(symbol if symbol else "all", api_keys, parsed.price))
                            continue

                        self.pending_order = {
                            "type": "exit",
                            "symbol": symbol if symbol else "all",
                            "api_keys": api_keys,
                            "price": parsed.price
                        }
                        
                        self._log_command(
                            cmd_text=cmd,
                            action=self.pending_order,
                            status="pending_confirmation",
                            api_key=api_keys[0] if api_keys else None
                        )
                        
                        self.prompt_control.text = f" Confirm EXIT of {target_display} positions {price_desc}? (y/n)> "
                        self.log_message(f"[#ff8700]Pending Confirmation:[/#] EXIT {target_display} positions {price_desc}. Press [bold]y[/bold] to confirm.")
                        continue

                    elif isinstance(parsed, CancelOrderCommand):
                        target = parsed.target
                        if target == "selected":
                            if self.selected_order:
                                target = self.selected_order.get("order_id")
                            else:
                                self.log_message("[#ff0000]Error:[/#] No pending order currently selected. Usage: cancel <id_suffix|symbol|all>")
                                continue
                        
                        # Find all matching pending orders
                        matches = self._find_all_matching_pending_orders(target)
                        if not matches:
                            self.log_message(f"[#ff0000]Error:[/#] No pending order matches ID/suffix/symbol '{target}'.")
                            continue

                        orders_to_cancel = [
                            {"order_id": o.get("order_id"), "api_key": a.get("api_key")}
                            for a, o in matches
                        ]

                        if len(orders_to_cancel) == 1:
                            ord_id = orders_to_cancel[0]["order_id"]
                            api_k = orders_to_cancel[0]["api_key"]
                            if getattr(self, "_skip_confirmation", False):
                                asyncio.create_task(self.execute_cancel(ord_id, api_k))
                                continue
                            self.pending_order = {
                                "type": "cancel",
                                "order_id": ord_id,
                                "api_key": api_k
                            }
                            self.prompt_control.text = f" Confirm CANCEL order {ord_id[-6:]}? (y/n)> "
                            self.log_message(f"[#ff8700]Pending Confirmation:[/#] CANCEL order {ord_id}. Press [bold]y[/bold] to confirm.")
                        else:
                            if getattr(self, "_skip_confirmation", False):
                                for ord_info in orders_to_cancel:
                                    asyncio.create_task(self.execute_cancel(ord_info["order_id"], ord_info["api_key"]))
                                continue
                            self.pending_order = {
                                "type": "cancel_multi",
                                "orders": orders_to_cancel
                            }
                            self.prompt_control.text = f" Confirm CANCEL of {len(orders_to_cancel)} orders? (y/n)> "
                            self.log_message(f"[#ff8700]Pending Confirmation:[/#] CANCEL {len(orders_to_cancel)} orders. Press [bold]y[/bold] to confirm.")
                        continue

                    elif isinstance(parsed, ModifyOrderCommand):
                        target_id = parsed.order_id
                        if target_id == "selected":
                            if self.selected_order:
                                target_id = self.selected_order.get("order_id")
                            else:
                                self.log_message("[#ff0000]Error:[/#] No pending order currently selected. Usage: order <id_or_sym> <price>")
                                continue

                        matches = self._find_all_matching_pending_orders(target_id)
                        if not matches:
                            self.log_message(f"[#ff0000]Error:[/#] No pending order matches ID/suffix/symbol '{target_id}'.")
                            continue
                        
                        first_acct, first_order = matches[0]
                        first_sym = first_order.get("tradingsymbol")
                        if parsed.quantity:
                            qty_val, qty_display, qty_err = self._resolve_qty(parsed.quantity, first_sym)
                            if qty_err:
                                self.log_message(f"[#ff0000]Error:[/#] {qty_err}")
                                continue
                        else:
                            qty_val = None
                            qty_display = ""

                        price_desc = f"@{parsed.price:.2f}" if parsed.price > 0 else "MARKET"
                        if len(matches) == 1:
                            acct, order = matches[0]
                            order_qty = qty_val if qty_val is not None else str(order.get("quantity") or order.get("pending_quantity") or "")
                            qty_disp_str = f"{qty_display} " if qty_display else ""
                            if getattr(self, "_skip_confirmation", False):
                                asyncio.create_task(self.execute_modify(order.get("order_id"), order_qty, str(parsed.price), acct.get("api_key")))
                                continue
                            self.pending_order = {
                                "type": "modify",
                                "order_id": order.get("order_id"),
                                "qty": order_qty,
                                "price": str(parsed.price),
                                "api_key": acct.get("api_key"),
                                "symbol": order.get("tradingsymbol")
                            }
                            self.prompt_control.text = f" Confirm MODIFY order {order.get('order_id')[-6:]} to {qty_disp_str}{price_desc}? (y/n)> "
                            self.log_message(f"[#ff8700]Pending Confirmation:[/#] MODIFY order {order.get('order_id')} to {qty_disp_str}{price_desc}. Press [bold]y[/bold] to confirm.")
                        else:
                            qty_disp_str = f"{qty_display} " if qty_display else ""
                            if getattr(self, "_skip_confirmation", False):
                                for a, o in matches:
                                    o_qty = qty_val if qty_val is not None else str(o.get("quantity") or o.get("pending_quantity") or "")
                                    asyncio.create_task(self.execute_modify(o.get("order_id"), o_qty, str(parsed.price), a.get("api_key")))
                                continue
                            self.pending_order = {
                                "type": "modify_multi",
                                "price_only": (qty_val is None),
                                "orders": [
                                    {
                                        "order_id": o.get("order_id"),
                                        "qty": qty_val if qty_val is not None else str(o.get("quantity") or o.get("pending_quantity") or ""),
                                        "api_key": a.get("api_key"),
                                        "account_name": a.get("name"),
                                        "symbol": o.get("tradingsymbol")
                                    }
                                    for a, o in matches
                                ],
                                "qty": qty_val,
                                "price": str(parsed.price)
                            }
                            self.prompt_control.text = f" Confirm MODIFY {len(matches)} orders to {qty_disp_str}{price_desc}? (y/n)> "
                            self.log_message(f"[#ff8700]Pending Confirmation:[/#] MODIFY {len(matches)} orders to {qty_disp_str}{price_desc}. Press [bold]y[/bold] to confirm.")
                        continue
                        
                    elif isinstance(parsed, StatusCommand):
                        self.log_message("[#00afaf]🔌 Account Health & Connection Status:[/#]")
                        w_acct = 10
                        w_brk = 8
                        w_ws = 11
                        w_rest = 12
                        w_diag = 28

                        top_b = f"┌─{'─'*w_acct}─┬─{'─'*w_brk}─┬─{'─'*w_ws}─┬─{'─'*w_rest}─┬─{'─'*w_diag}─┐"
                        head  = f"│ {'Account':<{w_acct}} │ {'Broker':<{w_brk}} │ {'WS Stream':<{w_ws}} │ {'REST Status':<{w_rest}} │ {'Diagnostics':<{w_diag}} │"
                        mid_b = f"├─{'─'*w_acct}─┼─{'─'*w_brk}─┼─{'─'*w_ws}─┼─{'─'*w_rest}─┼─{'─'*w_diag}─┤"
                        bot_b = f"└─{'─'*w_acct}─┴─{'─'*w_brk}─┴─{'─'*w_ws}─┴─{'─'*w_rest}─┴─{'─'*w_diag}─┘"

                        self.log_message(top_b)
                        self.log_message(head)
                        self.log_message(mid_b)

                        for acct in self.accounts:
                            key = acct.get("api_key")
                            name = acct.get("name", key)
                            broker = acct.get("broker", "zerodha").title()

                            ws_ok = self.websocket_connected.get(key, False)
                            rest_failed = self.account_rest_failed.get(key, False)

                            ws_str = "[#00ff00]🟢 Stream[/#]" if ws_ok else "[#ff0000]🔴 Closed[/#]"
                            rest_str = "[#ff0000]🔴 Failed[/#]" if rest_failed else "[#00ff00]🟢 Healthy[/#]"

                            access_token = self.client.get_access_token(key)
                            if not access_token and broker.lower() == "zerodha":
                                diag_str = "[#ff8700]No token (re-login)[/#]"
                            elif not ws_ok and rest_failed:
                                diag_str = "[#ff0000]Disconnected / Auth fail[/#]"
                            elif not ws_ok:
                                diag_str = "[#ff8700]WS reconnecting...[/#]"
                            else:
                                diag_str = "[#00ff00]Session active[/#]"

                            self.log_message(f"│ {name:<{w_acct}} │ {broker:<{w_brk}} │ {ws_str} │ {rest_str} │ {diag_str} │")

                        self.log_message(bot_b)
                        continue
                        
                    elif isinstance(parsed, PositionsCommand):
                        res = self.client.get_positions([self.selected_account_api_key] if self.selected_account_api_key else None)
                        for acct in res.get("accounts", []):
                            self.log_message(f"[#00afaf]📊 Account: {acct.get('name')}[/#] (P&L: {acct.get('total_pnl', 0.0):.2f})")
                            positions = [p for p in acct.get("positions", []) if p.get("quantity", 0) != 0]
                            if not positions:
                                self.log_message("  No open positions.")
                            else:
                                for p in positions:
                                    sym = p.get("tradingsymbol")
                                    pos_id = p.get("position_id", "-")
                                    qty = p.get("quantity", 0)
                                    avg = p.get("average_price", 0.0)
                                    pnl = p.get("pnl", 0.0)
                                    pnl_col = "[#00ff00]" if pnl >= 0 else "[#ff0000]"
                                    self.log_message(f"  ID: {pos_id} | {sym} | Qty: {qty} | Avg: ₹{avg:.2f} | P&L: {pnl_col}₹{pnl:.2f}[/#]")
                        continue
                        
                    elif isinstance(parsed, OrdersCommand):
                        res = self.client.get_orders([self.selected_account_api_key] if self.selected_account_api_key else None)
                        self.log_message("[#00afaf]📋 Pending Orders:[/#]")
                        found_any = False
                        for acct in res.get("accounts", []):
                            orders = [o for o in acct.get("orders", []) if o.get("status") in ("OPEN", "TRIGGER PENDING")]
                            if orders:
                                found_any = True
                                self.log_message(f"  Account: {acct.get('name')}")
                                for o in orders:
                                    self.log_message(
                                        f"    ID: {o.get('order_id')} | {o.get('transaction_type')} {o.get('tradingsymbol')} | "
                                        f"Qty: {o.get('pending_quantity')}/{o.get('quantity')} | Price: {o.get('price')} | Status: {o.get('status')}"
                                    )
                        if not found_any:
                            self.log_message("  No pending orders.")
                        continue

                    elif isinstance(parsed, ConditionalOrderCommand):
                        parent_cmd = parsed.parent_cmd
                        child_cmd = parsed.child_cmd
                        api_keys = [self.selected_account_api_key] if self.selected_account_api_key else []

                        # Validate parent order
                        if isinstance(parent_cmd, PlaceOrderCommand):
                            sym = parent_cmd.symbol_or_id
                            if sym:
                                if sym.isdigit():
                                    sym, api_keys = resolve_symbol_or_id(ExecutionContext(self.client, self.active_positions, self.position_id_map, self.selected_symbol, self.selected_account_api_key or "ALL"), sym)
                                else:
                                    sym = sym.upper()
                            else:
                                if not self.selected_symbol:
                                    self.log_message("[#ff0000]Error:[/#] No symbol specified and no active selection.")
                                    continue
                                sym = self.selected_symbol
                                if not api_keys and self.selected_account_api_key:
                                    api_keys = [self.selected_account_api_key]

                            if not parent_cmd.quantity:
                                matched_pos = None
                                for pos_item in self.active_positions:
                                    if pos_item.get("tradingsymbol") == sym and (not api_keys or pos_item.get("api_key") in api_keys):
                                        matched_pos = pos_item
                                        break
                                if not matched_pos:
                                    self.log_message("[#ff0000]Error:[/#] Omitted quantity for primary order, but no matching position found.")
                                    continue
                                p_qty_val = abs(matched_pos.get("quantity", 0))
                                p_qty_disp = str(p_qty_val)
                            else:
                                p_qty_val, p_qty_disp, p_qty_err = self._resolve_qty(parent_cmd.quantity, sym)
                                if p_qty_err:
                                    self.log_message(f"[#ff0000]Error in primary order:[/#] {p_qty_err}")
                                    continue

                            parent_price_desc = f"@{parent_cmd.price:.2f}" if parent_cmd.price else "MARKET"
                            parent_desc = f"{parent_cmd.action} {p_qty_disp} {sym} ({parent_cmd.product}) {parent_price_desc}"
                        else:
                            parent_desc = f"EXIT {parent_cmd.target}"

                        child_desc = parsed.child_raw_text

                        # Check if scheduled conditional chain
                        if getattr(parent_cmd, "trigger_time", None):
                            trig_str = parent_cmd.trigger_time.strftime('%Y-%m-%d %H:%M:%S')
                            if getattr(self, "_skip_confirmation", False):
                                self.register_scheduled_order(cmd_obj=parsed, command_raw=cmd, api_keys=api_keys)
                                continue

                            self.pending_order = {
                                "type": "scheduled",
                                "cmd": parsed,
                                "command_raw": cmd,
                                "api_keys": api_keys,
                            }
                            self._log_command(
                                cmd_text=cmd,
                                action=self.pending_order,
                                status="pending_confirmation",
                                api_key=api_keys[0] if api_keys else None,
                            )
                            self.prompt_control.text = f" Confirm SCHEDULED CONDITIONAL: 1) {parent_desc} at {trig_str} THEN 2) {child_desc}? (y/n)> "
                            self.log_message(
                                f"[#ff8700]Pending Confirmation (Scheduled Conditional Chain):[/#]\n"
                                f"  1) {parent_desc} at {trig_str}\n"
                                f"  THEN once filled:\n"
                                f"  2) {child_desc}\n"
                                f"Press [bold]y[/bold] to confirm."
                            )
                            continue

                        if getattr(self, "_skip_confirmation", False):
                            asyncio.create_task(
                                self.execute_conditional_order(parsed, api_keys=api_keys)
                            )
                            continue

                        self.pending_order = {
                            "type": "conditional",
                            "cmd": parsed,
                            "api_keys": api_keys,
                        }
                        self._log_command(
                            cmd_text=cmd,
                            action=self.pending_order,
                            status="pending_confirmation",
                            api_key=api_keys[0] if api_keys else None,
                        )
                        self.prompt_control.text = f" Confirm CONDITIONAL: 1) {parent_desc} THEN 2) {child_desc}? (y/n)> "
                        self.log_message(
                            f"[#ff8700]Pending Confirmation (Conditional Chain):[/#]\n"
                            f"  1) {parent_desc}\n"
                            f"  THEN once filled:\n"
                            f"  2) {child_desc}\n"
                            f"Press [bold]y[/bold] to confirm."
                        )
                        continue

                    elif isinstance(parsed, CancelConditionalOrderCommand):
                        target = parsed.target
                        if target.lower() == "all":
                            today_str = datetime.now().strftime("%Y-%m-%d")
                            with self.recorder.get_connection() as conn:
                                conn.execute("UPDATE conditional_orders SET status = 'CANCELLED', failure_reason = 'Cancelled by user' WHERE trade_date = ? AND status = 'PENDING';", (today_str,))
                                conn.commit()
                            self.conditional_orders_map.clear()
                            self.log_message("[#00ff00]✓ Cancelled all active conditional orders for today.[/#]")
                        else:
                            try:
                                cid = int(target)
                                ok = self.recorder.cancel_conditional_order(cid)
                                if ok:
                                    to_remove = [k for k, v in self.conditional_orders_map.items() if v.get("id") == cid]
                                    for k in to_remove:
                                        self.conditional_orders_map.pop(k, None)
                                    self.log_message(f"[#00ff00]✓ Cancelled conditional order #{cid}.[/#]")
                                else:
                                    self.log_message(f"[#ff8700]No pending conditional order found with ID #{cid}.[/#]")
                            except ValueError:
                                self.log_message(f"[#ff0000]Error:[/#] Invalid conditional order ID '{target}'. Usage: cancel co <id|all>")
                        if hasattr(self, "app") and self.app and self.app.loop:
                            asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)
                        continue

                    elif isinstance(parsed, ConditionalOrdersViewCommand):
                        today_str = datetime.now().strftime("%Y-%m-%d")
                        all_co = self.recorder.get_all_conditional_orders_today(today_str)
                        if not all_co:
                            self.log_message("[#58a6ff]Conditional Orders:[/#] No conditional orders recorded for today.")
                        else:
                            self.log_message(f"[#58a6ff]Conditional Orders Today ({len(all_co)}):[/#]")
                            for co_item in all_co:
                                cid = co_item.get("id")
                                p_ids = ", ".join(str(x) for x in co_item.get("parent_order_ids", []))
                                p_sym = co_item.get("parent_symbol", "")
                                p_act = co_item.get("parent_action", "")
                                c_raw = co_item.get("child_command_raw", "")
                                st = co_item.get("status", "")
                                self.log_message(f"  • [bold]#{cid}[/bold] | Parent: {p_act} {p_sym} (IDs: {p_ids}) -> Child: '{c_raw}' | Status: [bold]{st}[/bold]")
                        continue

                    elif isinstance(parsed, CancelScheduledOrderCommand):
                        target = parsed.target
                        if target.lower() == "all":
                            cnt = self.recorder.cancel_all_scheduled_orders()
                            self.scheduled_orders_map.clear()
                            self.log_message(f"[#00ff00]✓ Cancelled all ({cnt}) active scheduled order(s).[/#]")
                        else:
                            try:
                                sid = int(target)
                                ok = self.recorder.cancel_scheduled_order(sid)
                                if ok:
                                    self.scheduled_orders_map.pop(sid, None)
                                    self.log_message(f"[#00ff00]✓ Cancelled scheduled order #{sid}.[/#]")
                                else:
                                    self.log_message(f"[#ff8700]No pending scheduled order found with ID #{sid}.[/#]")
                            except ValueError:
                                self.log_message(f"[#ff0000]Error:[/#] Invalid scheduled order ID '{target}'. Usage: cancel sched <id|all>")
                        if hasattr(self, "app") and self.app and self.app.loop:
                            asyncio.run_coroutine_threadsafe(self._trigger_immediate_refresh(), self.app.loop)
                        continue

                    elif isinstance(parsed, ScheduledOrdersViewCommand):
                        today_str = datetime.now().strftime("%Y-%m-%d")
                        all_sched = self.recorder.get_all_scheduled_orders_today(today_str)
                        if not all_sched:
                            self.log_message("[#58a6ff]Scheduled Orders:[/#] No scheduled orders recorded for today.")
                        else:
                            self.log_message(f"[#58a6ff]Scheduled Orders Today ({len(all_sched)}):[/#]")
                            for so in all_sched:
                                sid = so.get("id")
                                t_str = so.get("trigger_time", "")
                                acct = so.get("account_name", "ALL")
                                act = so.get("action", "")
                                sym = so.get("symbol_or_id", "")
                                qty = so.get("quantity") or ""
                                pr = so.get("price")
                                pr_str = f"@{pr:.2f}" if pr else "@MKT"
                                is_c = bool(so.get("is_conditional"))
                                c_child = so.get("child_command_raw") or ""
                                st = so.get("status", "")
                                cond_info = f" -> THEN '{c_child}'" if is_c else ""
                                self.log_message(f"  • [bold]#{sid}[/bold] | [{acct}] {act} {qty} {sym} {pr_str} at {t_str}{cond_info} | Status: [bold]{st}[/bold]")
                        continue
                return
        except Exception:
            pass

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

        # Modify Order Command:
        #   order <id_suffix|full_id|symbol> <price>           → price-only (each order keeps its own qty)
        #   order <id_suffix|full_id|symbol> <quantity|lotsL> <price>  → full modify (qty + price)
        if primary_cmd == "order":
            if len(parts) < 3:
                self.log_message("[#ff0000]Usage:[/#] order <id_or_sym> <price>  OR  order <id_or_sym> <qty> <price>")
                return

            price_only = len(parts) == 3  # True → "order <sym> <price>", False → "order <sym> <qty> <price>"

            raw_id_or_sym = parts[1]

            if price_only:
                price_str = parts[2]
                qty_str = None
            else:
                qty_str = parts[2]
                price_str = parts[3]

            # Find all matching pending orders
            matches = self._find_all_matching_pending_orders(raw_id_or_sym)
            if not matches:
                self.log_message(f"[#ff0000]Error:[/#] No pending order matches ID/suffix/symbol '{raw_id_or_sym}'.")
                return

            first_acct, first_order = matches[0]
            first_sym = first_order.get("tradingsymbol")

            # Resolve quantity only in full-modify mode
            if not price_only:
                raw_qty_str, qty_display, qty_err = self._resolve_qty(qty_str, first_sym)
                if qty_err:
                    self.log_message(f"[#ff0000]Error:[/#] {qty_err}")
                    return
            else:
                raw_qty_str = None
                qty_display = "existing qty"

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
                # In price-only mode use the order's own qty
                effective_qty = raw_qty_str if not price_only else str(order.get("quantity", 1))
                qty_disp = qty_display if not price_only else str(order.get("quantity", 1))
                self.pending_order = {
                    "type": "modify",
                    "order_id": order_id,
                    "qty": effective_qty,
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
                self.prompt_control.text = f" Confirm MODIFY order {order_id[-6:]} to {qty_disp} {symbol} {price_desc}? (y/n)> "
                self.log_message(f"[#ff8700]Pending Confirmation:[/#] MODIFY order {order_id} ({symbol}) to {qty_disp} {price_desc}. Press [bold]y[/bold] to confirm, any other key to cancel.")
            else:
                # Multi-order modify — in price-only mode store each order's own qty per entry
                orders_payload = []
                for a, o in matches:
                    entry = {
                        "order_id": o.get("order_id"),
                        "api_key": a.get("api_key"),
                        "account_name": a.get("name"),
                        "symbol": o.get("tradingsymbol"),
                    }
                    if price_only:
                        entry["qty"] = str(o.get("quantity", 1))  # preserve each order's own qty
                    orders_payload.append(entry)

                self.pending_order = {
                    "type": "modify_multi",
                    "orders": orders_payload,
                    "qty": raw_qty_str,   # None in price-only mode; per-order qty is in each entry
                    "price": price_str,
                    "price_only": price_only,
                }
                self._log_command(
                    cmd_text=cmd,
                    action=self.pending_order,
                    status="pending_confirmation",
                    api_key=None,
                )
                accts_list = ", ".join(
                    f"@{o['account_name']}({o.get('qty', raw_qty_str)})" for o in orders_payload
                )
                self.prompt_control.text = f" Confirm MODIFY {len(matches)} orders ({first_sym}) {price_desc} on {accts_list}? (y/n)> "
                per_order_lines = "\n".join(
                    f"  @{o['account_name']}: order ...{o['order_id'][-6:]} qty={o.get('qty', raw_qty_str)}"
                    for o in orders_payload
                )
                self.log_message(
                    f"[#ff8700]Pending Confirmation:[/#] MODIFY {len(matches)} pending orders for {first_sym} "
                    f"\u2192 {price_desc}:\n{per_order_lines}\n"
                    f"Press [bold]y[/bold] to confirm, any other key to cancel."
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
            margin_summary = self._get_order_margin_summary(
                api_keys=target_keys,
                symbol=symbol,
                transaction_type=primary_cmd.upper(),
                qty=int(raw_qty_str) if str(raw_qty_str).isdigit() else 1,
                price=float(price_str) if price_str and price_str.replace('.', '', 1).isdigit() else None,
                product=product,
            )
            margin_inline = self._get_order_margin_summary_inline(
                api_keys=target_keys,
                symbol=symbol,
                transaction_type=primary_cmd.upper(),
                qty=int(raw_qty_str) if str(raw_qty_str).isdigit() else 1,
                price=float(price_str) if price_str and price_str.replace('.', '', 1).isdigit() else None,
                product=product,
            )
            self.prompt_control.text = f" Confirm {primary_cmd.upper()} {qty_display} {symbol} ({product}) {price_desc} on {accts_desc}{margin_inline}? (y/n)> "
            self.log_message(f"[#ff8700]Pending Confirmation:[/#] {primary_cmd.upper()} {qty_display} {symbol} ({product}) {price_desc} on {accts_desc}.{margin_summary}\nPress [bold]y[/bold] to confirm, any other key to cancel.")
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
        #   oc <UNDERLYING>                  → current week (centered on ATM)
        #   oc <UNDERLYING> monthly          → current month's monthly expiry
        #   oc <UNDERLYING> next month       → next month's monthly expiry
        #   oc <UNDERLYING> far month        → far month's monthly expiry
        #   oc <UNDERLYING> week <N>         → N-th week (0=current, 1=next, 2=far, ...)
        #   oc <UNDERLYING> <YYYY-MM-DD>     → specific expiry date
        #   oc exp / oc dropdown             → toggle interactive expiry dropdown
        if primary_cmd in ("oc", "optionchain", "chain"):
            if len(parts) >= 2 and parts[1].lower() in ("comp", "compare", "dual", "side", "2"):
                import re as _re
                underlying = "NIFTY"
                exp1_idx = 0
                exp2_idx = 1
                exp1_date = None
                exp2_date = None

                args = parts[2:]
                if args:
                    if not args[0].isdigit() and not _re.match(r"^\d{4}-\d{2}-\d{2}$", args[0]):
                        underlying = args[0].upper()
                        args = args[1:]

                if len(args) >= 1:
                    if args[0].isdigit():
                        exp1_idx = int(args[0])
                    elif _re.match(r"^\d{4}-\d{2}-\d{2}$", args[0]):
                        exp1_date = args[0]
                if len(args) >= 2:
                    if args[1].isdigit():
                        exp2_idx = int(args[1])
                    elif _re.match(r"^\d{4}-\d{2}-\d{2}$", args[1]):
                        exp2_date = args[1]

                if not self.accounts:
                    self.log_message("[#ff0000]Error:[/#] No accounts configured.")
                    return
                api_key = self.primary_api_key or self.accounts[0]["api_key"]
                self.log_message(f"Fetching dual option chain for [bold]{underlying}[/bold]...")
                asyncio.create_task(self._fetch_and_display_dual_option_chain(
                    api_key, underlying, exp1_idx, exp2_idx, exp1_date, exp2_date
                ))
                return

            if len(parts) >= 2 and parts[1].lower() in ("exp", "dropdown", "menu"):
                self.oc_dropdown_open = not getattr(self, "oc_dropdown_open", False)
                self._update_info_buffer(reset_scroll=False)
                if hasattr(self, "app") and self.app:
                    self.app.invalidate()
                return

            if len(parts) < 2:
                self.log_message("[#ff0000]Usage:[/#] oc <UNDERLYING> [monthly | next month | far month | week <N> | <YYYY-MM-DD>]")
                self.log_message("  e.g. [bold]oc NIFTY[/bold]              — current week")
                self.log_message("  e.g. [bold]oc NIFTY monthly[/bold]      — current month's monthly expiry")
                self.log_message("  e.g. [bold]oc NIFTY next month[/bold]   — next month's monthly expiry")
                self.log_message("  e.g. [bold]oc NIFTY far month[/bold]    — far month's monthly expiry")
                self.log_message("  e.g. [bold]oc NIFTY week 1[/bold]       — next week expiry")
                self.log_message("  e.g. [bold]oc NIFTY far week[/bold]     — far week expiry")
                self.log_message("  e.g. [bold]oc BANKNIFTY 2024-06-27[/bold] — specific date")
                return

            underlying = parts[1].upper()
            expiry_week = 0
            expiry_date = None

            show_all = "all" in [p.lower() for p in parts]
            self.oc_show_all = show_all

            non_all_parts = [p for p in parts if p.lower() != "all"]
            if len(non_all_parts) >= 3:
                second_arg = non_all_parts[2].lower()
                # 'monthly' / 'month' / 'm'
                if second_arg in ("monthly", "month", "m"):
                    expiry_date = "monthly"
                    self.oc_context_mode = "monthly"
                elif second_arg == "next" and len(non_all_parts) >= 4 and non_all_parts[3].lower() in ("month", "monthly"):
                    expiry_date = "next monthly"
                    self.oc_context_mode = "monthly"
                elif second_arg == "far" and len(non_all_parts) >= 4 and non_all_parts[3].lower() in ("month", "monthly"):
                    expiry_date = "far monthly"
                    self.oc_context_mode = "monthly"
                elif second_arg == "next" and len(non_all_parts) >= 4 and non_all_parts[3].lower() == "week":
                    expiry_week = 1
                    self.oc_context_mode = "weekly"
                elif second_arg == "far" and len(non_all_parts) >= 4 and non_all_parts[3].lower() == "week":
                    expiry_week = 2
                    self.oc_context_mode = "weekly"
                # 'week N' suffix
                elif second_arg == "week" and len(non_all_parts) >= 4:
                    try:
                        expiry_week = int(non_all_parts[3])
                        self.oc_context_mode = "weekly"
                    except ValueError:
                        self.log_message(f"[#ff0000]Error:[/#] Invalid week number '{non_all_parts[3]}'.")
                        return
                else:
                    # Try to parse as a date
                    import re as _re
                    if _re.match(r"\d{4}-\d{2}-\d{2}", non_all_parts[2]):
                        expiry_date = non_all_parts[2]
                    else:
                        self.log_message(f"[#ff0000]Error:[/#] Unknown option '{non_all_parts[2]}'. Use 'monthly', 'next month', 'week <N>', or 'YYYY-MM-DD'.")
                        return
            else:
                self.oc_context_mode = "weekly"

            # Use the primary streaming account so the option chain can stream;
            # fall back to the first configured account for the REST fetch.
            if not self.accounts:
                self.log_message("[#ff0000]Error:[/#] No accounts configured.")
                return
            api_key = self.primary_api_key or self.accounts[0]["api_key"]

            self.log_message(f"Fetching option chain for [bold]{underlying}[/bold] (expiry={expiry_date or f'week {expiry_week}'})...")
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
            "spot_price": response.get("spot_price", 0.0),
            "summary": response.get("summary", {}),
            "strikes": strikes,
            "available_expiries": expiries,
        }

        # Subscribe the option tokens on the primary ticker so the LTPs stream.
        self._update_oc_subscriptions(strikes)

        self._last_oc_text = self._render_oc_text()
        self.info_mode = "oc"
        self._update_info_buffer(reset_scroll=False)

        # Center initial scroll position around ATM strike
        atm_strike = response.get("summary", {}).get("atm_strike")
        if atm_strike and hasattr(self, "_info_control"):
            for idx, s in enumerate(strikes):
                if float(s.get("strike", 0)) == atm_strike:
                    target_scroll = max(0, (10 + idx) - 12)
                    self._info_control.vertical_scroll_position = target_scroll
                    if hasattr(self, "info_window"):
                        self.info_window.vertical_scroll = target_scroll
                    break

        stream_note = " (streaming)" if self.primary_api_key else ""
        self.log_message(
            f"Option chain loaded for {underlying} expiry {expiry}{stream_note}. "
            f"Press F3 to view in right pane."
        )

    def _render_oc_text(self) -> str:
        """Render the current ``self.oc_data`` into the rich option-chain matrix."""
        data = self.oc_data
        if not data:
            return self._last_oc_text

        underlying = data.get("underlying", "")
        expiry = data.get("expiry", "")
        spot_price = float(data.get("spot_price") or 0.0)
        summary = data.get("summary", {})
        strikes = data.get("strikes", [])
        expiries = data.get("available_expiries", [])

        # If spot_price is 0, check if we have index LTP streaming
        if spot_price <= 0 and hasattr(self, "index_ltp_map"):
            for sym, ltp in self.index_ltp_map.items():
                if underlying in sym:
                    spot_price = float(ltp or 0.0)
                    break

        atm_strike = summary.get("atm_strike")
        atm_straddle = summary.get("atm_straddle")
        pcr = summary.get("pcr", 0.0)
        max_pain = summary.get("max_pain", 0.0)

        # Days to expiry formatting
        from datetime import datetime as _dt, date as _d
        dte_str = ""
        if expiry:
            try:
                exp_d = _dt.strptime(expiry[:10], "%Y-%m-%d").date()
                dte = (exp_d - _d.today()).days
                dte_str = f"({max(0, dte)} DTE)"
            except Exception:
                pass

        def _fmt_tab_dt(exp_str: str) -> str:
            try:
                d = _dt.strptime(exp_str[:10], "%Y-%m-%d").date()
                return d.strftime("%d-%b")
            except Exception:
                return exp_str

        def _fmt_spark(val, max_val, is_call=True) -> str:
            if not val or not max_val or val <= 0:
                return "  "
            ratio = min(1.0, max(0.0, float(val) / float(max_val)))
            num_blocks = int(round(ratio * 2))
            if num_blocks == 0 and ratio > 0.05:
                num_blocks = 1
            blocks = "▇" * num_blocks
            return f"{blocks:<2}" if is_call else f"{blocks:>2}"

        def _fmt_oi(val) -> str:
            if val is None or val == 0:
                return "     -"
            val_int = int(val)
            if val_int >= 10_000_000:
                return f"{val_int / 10_000_000:>5.1f}Cr"
            if val_int >= 100_000:
                return f"{val_int / 100_000:>5.1f}L"
            if val_int >= 1_000:
                return f"{val_int / 1_000:>5.1f}k"
            return f"{val_int:>6}"

        def _fmt_iv(val) -> str:
            if val is None or val <= 0:
                return "     -"
            return f"{val:>5.1f}%"

        def _fmt_delta(val) -> str:
            if val is None:
                return "     -"
            return f"{val:>6.2f}"

        def _fmt_ltp(val) -> str:
            if val is None:
                return "       -"
            return f"{val:>8.2f}"

        # Classify weekly vs monthly expiries
        weekly_expiries = [e for e in expiries if not e.get("is_monthly")]
        monthly_expiries = [e for e in expiries if e.get("is_monthly")]
        is_monthly_context = any(e.get("expiry") == expiry and e.get("is_monthly") for e in expiries) or getattr(self, "oc_context_mode", "weekly") == "monthly"

        # 1. Top Expiry Tabs (Exactly 3 tabs + 1 dropdown tab)
        top_tabs = []
        if is_monthly_context and monthly_expiries:
            for i, m in enumerate(monthly_expiries[:3]):
                lbl = "Current" if i == 0 else "Next" if i == 1 else "Far"
                dt_disp = _fmt_tab_dt(m.get("expiry", ""))
                is_active = (m.get("expiry") == expiry)
                prefix = "● " if is_active else ""
                top_tabs.append(f"[ {prefix}{lbl}: {dt_disp} ]")
        else:
            for i, w in enumerate(weekly_expiries[:3]):
                lbl = "Current" if i == 0 else "Next" if i == 1 else "Far"
                dt_disp = _fmt_tab_dt(w.get("expiry", ""))
                is_active = (w.get("expiry") == expiry)
                prefix = "● " if is_active else ""
                top_tabs.append(f"[ {prefix}{lbl}: {dt_disp} ]")

        top_tabs.append(f"[ ▾ Expiries ({len(expiries)}) ]")
        tab_line = " " + "  ".join(top_tabs)

        lines = [
            tab_line,
        ]

        # 2. Dropdown (if open)
        if getattr(self, "oc_dropdown_open", False):
            lines.append("┌── Select Expiry (Click to switch) ────────────────────────────────────────┐")
            for i, e in enumerate(expiries):
                prefix = "►" if e.get("expiry") == expiry else " "
                lbl = e.get("week_label", "")
                dt = e.get("expiry", "")
                lines.append(f"│  {prefix} {dt:<12} • {lbl:<52}│")
            lines.append("│  [ ▲ Close Dropdown ]                                                     │")
            lines.append("└───────────────────────────────────────────────────────────────────────────┘")

        lines.append("────────────────────────────────────────────────────────────────────────────")

        # 3. Symmetrical Tidied Info Container with Visual Gauges (exactly 76 chars wide)
        atm_str_label = f"₹{atm_straddle:.2f} ({int(atm_strike or 0)} ATM)" if atm_straddle else "-"
        exp_full = f"Expiry: {expiry} {dte_str}"

        lower_range = int(round(atm_strike - atm_straddle)) if (atm_strike and atm_straddle) else None
        upper_range = int(round(atm_strike + atm_straddle)) if (atm_strike and atm_straddle) else None

        filled = min(8, max(0, int(round((float(pcr or 0.0) / 2.0) * 8))))
        pcr_bar = "█" * filled + "░" * (8 - filled)

        s1 = f" {underlying} Spot: {spot_price:,.2f}"[:26]
        s2 = f"{exp_full}"[:30]
        s3 = f"Max Pain: {int(max_pain or 0)} "[:16]

        p1 = f" PCR: {pcr:.2f} [{pcr_bar}]"[:25]
        p2 = (f"ATM Str: ₹{atm_straddle:.1f} (Range: {lower_range}-{upper_range})" if (lower_range and upper_range) else f"ATM Straddle: {atm_str_label}")[:47]

        lines.append("┌──────────────────────────────────────────────────────────────────────────┐")
        lines.append(f"│ {s1:<26}{s2:^30}{s3:>16} │")
        lines.append(f"│ {p1:<25}{p2:<47} │")
        lines.append("└──────────────────────────────────────────────────────────────────────────┘")

        # 4. Table Header & Matrix Divider (76 chars)
        lines += [
            "   OI        IV   Delta    LTP  │  STRIKE  │  LTP    Delta    IV        OI  ",
            "────────────────────────────────┼──────────┼────────────────────────────────",
        ]

        max_oi = max(
            max((s.get("ce_oi") or 0) for s in strikes) if strikes else 1,
            max((s.get("pe_oi") or 0) for s in strikes) if strikes else 1,
            1,
        )
        max_ce_strike = max(strikes, key=lambda s: s.get("ce_oi") or 0)["strike"] if strikes else None
        max_pe_strike = max(strikes, key=lambda s: s.get("pe_oi") or 0)["strike"] if strikes else None

        for s in strikes:
            strike = float(s.get("strike", 0.0))
            is_atm = (strike == atm_strike)

            ce_oi_str = _fmt_oi(s.get("ce_oi"))
            ce_bar = _fmt_spark(s.get("ce_oi"), max_oi, is_call=True)
            ce_iv_str = _fmt_iv(s.get("ce_iv"))
            ce_delta_str = _fmt_delta(s.get("ce_delta"))
            ce_ltp_str = _fmt_ltp(s.get("ce_ltp"))

            pe_ltp_str = _fmt_ltp(s.get("pe_ltp"))
            pe_delta_str = _fmt_delta(s.get("pe_delta"))
            pe_iv_str = _fmt_iv(s.get("pe_iv"))
            pe_bar = _fmt_spark(s.get("pe_oi"), max_oi, is_call=False)
            pe_oi_str = _fmt_oi(s.get("pe_oi"))

            if is_atm:
                strike_disp = f"[{int(strike):^6}]"
            elif strike == max_ce_strike:
                strike_disp = f" {int(strike)} R"
            elif strike == max_pe_strike:
                strike_disp = f" {int(strike)} S"
            elif max_pain and int(strike) == int(max_pain):
                strike_disp = f" {int(strike)} *"
            else:
                strike_disp = f" {int(strike):^6} "

            call_side = f"{ce_oi_str} {ce_bar} {ce_iv_str} {ce_delta_str} {ce_ltp_str} "
            put_side = f" {pe_ltp_str} {pe_delta_str} {pe_iv_str} {pe_bar} {pe_oi_str}"
            lines.append(f"{call_side}│{strike_disp}│{put_side}")

        lines += [
            "────────────────────────────────┴──────────┴────────────────────────────────",
            " Tip: Click any strike to stage order | Type 'oc <SYM> monthly' or 'oc <SYM> week <N>'",
        ]
        return "\n".join(lines)

    def _build_oc_formatted_fragments(self) -> list:
        """Build interactive formatted text fragments with mouse click handlers for the Option Chain."""
        from prompt_toolkit.mouse_events import MouseEventType
        from datetime import datetime as _dt, date as _d

        data = self.oc_data
        if not data:
            return [("", self._last_oc_text or "No option chain data loaded.")]

        underlying = data.get("underlying", "")
        expiry = data.get("expiry", "")
        spot_price = float(data.get("spot_price") or 0.0)
        summary = data.get("summary", {})
        strikes = data.get("strikes", [])
        expiries = data.get("available_expiries", [])

        if spot_price <= 0 and hasattr(self, "index_ltp_map"):
            for sym, ltp in self.index_ltp_map.items():
                if underlying in sym:
                    spot_price = float(ltp or 0.0)
                    break

        atm_strike = summary.get("atm_strike")
        atm_straddle = summary.get("atm_straddle")
        pcr = summary.get("pcr", 0.0)
        max_pain = summary.get("max_pain", 0.0)

        # Days to expiry formatting
        dte_str = ""
        if expiry:
            try:
                exp_d = _dt.strptime(expiry[:10], "%Y-%m-%d").date()
                dte = (exp_d - _d.today()).days
                dte_str = f"({max(0, dte)} DTE)"
            except Exception:
                pass

        def _fmt_tab_dt(exp_str: str) -> str:
            try:
                d = _dt.strptime(exp_str[:10], "%Y-%m-%d").date()
                return d.strftime("%d-%b")
            except Exception:
                return exp_str

        def _fmt_spark(val, max_val, is_call=True) -> str:
            if not val or not max_val or val <= 0:
                return "  "
            ratio = min(1.0, max(0.0, float(val) / float(max_val)))
            num_blocks = int(round(ratio * 2))
            if num_blocks == 0 and ratio > 0.05:
                num_blocks = 1
            blocks = "▇" * num_blocks
            return f"{blocks:<2}" if is_call else f"{blocks:>2}"

        def _fmt_oi(val) -> str:
            if val is None or val == 0:
                return "     -"
            val_int = int(val)
            if val_int >= 10_000_000:
                return f"{val_int / 10_000_000:>5.1f}Cr"
            if val_int >= 100_000:
                return f"{val_int / 100_000:>5.1f}L"
            if val_int >= 1_000:
                return f"{val_int / 1_000:>5.1f}k"
            return f"{val_int:>6}"

        def _fmt_iv(val) -> str:
            if val is None or val <= 0:
                return "     -"
            return f"{val:>5.1f}%"

        def _fmt_delta(val) -> str:
            if val is None:
                return "     -"
            return f"{val:>6.2f}"

        def _fmt_ltp(val) -> str:
            if val is None:
                return "       -"
            return f"{val:>8.2f}"

        def _make_exp_click(w_idx, exp_val):
            def _handler(mouse_event):
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    self.oc_dropdown_open = False
                    und = self.oc_data.get("underlying", "NIFTY")
                    ak = self.primary_api_key or (self.accounts[0]["api_key"] if self.accounts else "")
                    if ak:
                        self.log_message(f"Loading Option Chain for [bold]{und}[/bold] expiry [bold]{exp_val}[/bold]...")
                        import asyncio
                        asyncio.create_task(self._fetch_and_display_option_chain(ak, und, expiry_week=w_idx, expiry_date=exp_val))
            return _handler

        def _make_dropdown_toggle():
            def _handler(mouse_event):
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    self.oc_dropdown_open = not getattr(self, "oc_dropdown_open", False)
                    self._update_info_buffer(reset_scroll=False)
                    if hasattr(self, "app") and self.app:
                        self.app.invalidate()
            return _handler

        # 1. Top Expiry Navigation Tabs (3 tabs + 1 dropdown tab)
        weekly_expiries = [(i, e) for i, e in enumerate(expiries) if not e.get("is_monthly")]
        monthly_expiries = [(i, e) for i, e in enumerate(expiries) if e.get("is_monthly")]
        is_monthly_context = any(e.get("expiry") == expiry and e.get("is_monthly") for e in expiries) or getattr(self, "oc_context_mode", "weekly") == "monthly"

        frags = [("", " ")]
        if is_monthly_context and monthly_expiries:
            for count, (idx, m) in enumerate(monthly_expiries[:3]):
                lbl = "Current" if count == 0 else "Next" if count == 1 else "Far"
                m_dt = m.get("expiry", "")
                dt_disp = _fmt_tab_dt(m_dt)
                is_cur = (m_dt == expiry)
                style = "class:oc.expiry.active" if is_cur else "class:oc.expiry.inactive"
                prefix = "● " if is_cur else ""
                frags.append((style, f"[ {prefix}{lbl}: {dt_disp} ]", _make_exp_click(idx, m_dt)))
                frags.append(("", "  "))
        else:
            for count, (idx, w) in enumerate(weekly_expiries[:3]):
                lbl = "Current" if count == 0 else "Next" if count == 1 else "Far"
                w_dt = w.get("expiry", "")
                dt_disp = _fmt_tab_dt(w_dt)
                is_cur = (w_dt == expiry)
                style = "class:oc.expiry.active" if is_cur else "class:oc.expiry.inactive"
                prefix = "● " if is_cur else ""
                frags.append((style, f"[ {prefix}{lbl}: {dt_disp} ]", _make_exp_click(idx, w_dt)))
                frags.append(("", "  "))

        dd_style = "class:oc.expiry.active" if getattr(self, "oc_dropdown_open", False) else "class:oc.dropdown.btn"
        frags.append((dd_style, f"[ ▾ Expiries ({len(expiries)}) ]", _make_dropdown_toggle()))
        frags.append(("", "\n"))

        # 2. Interactive Dropdown Menu (if open)
        if getattr(self, "oc_dropdown_open", False):
            frags.append(("class:oc.border", "┌── Select Expiry (Click to switch) ────────────────────────────────────────┐\n"))
            for i, e in enumerate(expiries):
                dt = e.get("expiry", "")
                lbl = e.get("week_label", "")
                is_cur = (dt == expiry)
                prefix = "► " if is_cur else "  "
                item_style = "class:oc.dropdown.active" if is_cur else "class:oc.dropdown.item"
                frags.append(("class:oc.border", "│ "))
                frags.append((item_style, f"{prefix}{dt:<12} • {lbl:<52}", _make_exp_click(i, dt)))
                frags.append(("class:oc.border", " │\n"))
            frags.append(("class:oc.border", "│ "))
            frags.append(("class:oc.dropdown.close", "[ ▲ Close Dropdown ]                                                     ", _make_dropdown_toggle()))
            frags.append(("class:oc.border", " │\n"))
            frags.append(("class:oc.border", "└───────────────────────────────────────────────────────────────────────────┘\n"))

        # 3. Distinction Line
        frags.append(("class:oc.border", "────────────────────────────────────────────────────────────────────────────\n"))

        # 4. Tidied Symmetrical Metrics Container with Visual Gauges (exact 76 chars wide)
        atm_str_label = f"₹{atm_straddle:.2f} ({int(atm_strike or 0)} ATM)" if atm_straddle else "-"
        exp_full = f"Expiry: {expiry} {dte_str}"

        lower_range = int(round(atm_strike - atm_straddle)) if (atm_strike and atm_straddle) else None
        upper_range = int(round(atm_strike + atm_straddle)) if (atm_strike and atm_straddle) else None

        filled = min(8, max(0, int(round((float(pcr or 0.0) / 2.0) * 8))))
        pcr_bar = "█" * filled + "░" * (8 - filled)

        s1 = f" {underlying} Spot: {spot_price:,.2f}"[:26]
        s2 = f"{exp_full}"[:30]
        s3 = f"Max Pain: {int(max_pain or 0)} "[:16]

        p1 = f" PCR: {pcr:.2f} [{pcr_bar}]"[:25]
        p2 = (f"ATM Str: ₹{atm_straddle:.1f} (Range: {lower_range}-{upper_range})" if (lower_range and upper_range) else f"ATM Straddle: {atm_str_label}")[:47]

        frags += [
            ("class:oc.border", "┌──────────────────────────────────────────────────────────────────────────┐\n"),
            ("class:oc.border", "│ "),
            ("class:oc.header.spot", f"{s1:<26}"),
            ("class:oc.header.expiry", f"{s2:^30}"),
            ("class:oc.header.pain", f"{s3:>16}"),
            ("class:oc.border", " │\n"),
            ("class:oc.border", "│ "),
            ("class:oc.header.pcr", f"{p1:<25}"),
            ("class:oc.header.straddle", f"{p2:<47}"),
            ("class:oc.border", " │\n"),
            ("class:oc.border", "└──────────────────────────────────────────────────────────────────────────┘\n"),
        ]

        # 5. Table Headers
        frags += [
            ("class:oc.table.cols",   "   OI        IV   Delta    LTP  │  STRIKE  │  LTP    Delta    IV        OI  \n"),
            ("class:oc.border",       "────────────────────────────────┼──────────┼────────────────────────────────\n"),
        ]

        def _make_ce_click(sym, ltp):
            def _handler(mouse_event):
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    if sym and sym != "-":
                        self.log_message(f"Selected [bold green]Call (CE): {sym}[/bold green] (LTP: {ltp or 'N/A'})")
                        if hasattr(self, "input_field") and self.input_field:
                            buf = self.input_field.buffer
                            price_str = f" {float(ltp):.2f}" if ltp and float(ltp) > 0 else ""
                            buf.text = f"sell {sym} 27L{price_str}"
                            buf.cursor_position = len(buf.text)
                            if hasattr(self, "app") and self.app:
                                self.app.layout.focus(self.input_field)
                        self._update_display_and_invalidate()
            return _handler

        def _make_pe_click(sym, ltp):
            def _handler(mouse_event):
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    if sym and sym != "-":
                        self.log_message(f"Selected [bold red]Put (PE): {sym}[/bold red] (LTP: {ltp or 'N/A'})")
                        if hasattr(self, "input_field") and self.input_field:
                            buf = self.input_field.buffer
                            price_str = f" {float(ltp):.2f}" if ltp and float(ltp) > 0 else ""
                            buf.text = f"sell {sym} 27L{price_str}"
                            buf.cursor_position = len(buf.text)
                            if hasattr(self, "app") and self.app:
                                self.app.layout.focus(self.input_field)
                        self._update_display_and_invalidate()
            return _handler

        # 6. Render ALL strikes with OI Depth Spark-Bars, Wall Annotations, and ITM/OTM Shading
        max_oi = max(
            max((s.get("ce_oi") or 0) for s in strikes) if strikes else 1,
            max((s.get("pe_oi") or 0) for s in strikes) if strikes else 1,
            1,
        )
        max_ce_strike = max(strikes, key=lambda s: s.get("ce_oi") or 0)["strike"] if strikes else None
        max_pe_strike = max(strikes, key=lambda s: s.get("pe_oi") or 0)["strike"] if strikes else None

        for s in strikes:
            strike = float(s.get("strike", 0.0))
            is_atm = (strike == atm_strike)

            ce_sym = s.get("ce_symbol") or "-"
            pe_sym = s.get("pe_symbol") or "-"
            ce_ltp = s.get("ce_ltp")
            pe_ltp = s.get("pe_ltp")

            ce_oi_str = _fmt_oi(s.get("ce_oi"))
            ce_bar = _fmt_spark(s.get("ce_oi"), max_oi, is_call=True)
            ce_iv_str = _fmt_iv(s.get("ce_iv"))
            ce_delta_str = _fmt_delta(s.get("ce_delta"))
            ce_ltp_str = _fmt_ltp(ce_ltp)

            pe_ltp_str = _fmt_ltp(pe_ltp)
            pe_delta_str = _fmt_delta(s.get("pe_delta"))
            pe_iv_str = _fmt_iv(s.get("pe_iv"))
            pe_bar = _fmt_spark(s.get("pe_oi"), max_oi, is_call=False)
            pe_oi_str = _fmt_oi(s.get("pe_oi"))

            ce_click = _make_ce_click(ce_sym, ce_ltp)
            pe_click = _make_pe_click(pe_sym, pe_ltp)

            # ITM vs OTM Shading
            is_ce_itm = (spot_price > 0 and strike < spot_price)
            is_pe_itm = (spot_price > 0 and strike > spot_price)

            ce_style = "class:oc.ce.itm" if is_ce_itm else "class:oc.ce"
            pe_style = "class:oc.pe.itm" if is_pe_itm else "class:oc.pe"

            # Call Side (33 chars)
            frags.append((ce_style, f"{ce_oi_str} ", ce_click))
            frags.append(("class:oc.bar.call", f"{ce_bar} ", ce_click))
            frags.append((ce_style, f"{ce_iv_str} {ce_delta_str} {ce_ltp_str} ", ce_click))

            frags.append(("class:oc.border", "│"))

            # Strike Column (8 chars)
            if is_atm:
                frags.append(("class:oc.atm", f"[{int(strike):^6}]"))
            elif strike == max_ce_strike:
                frags.append(("class:oc.wall.call", f" {int(strike)} R"))
            elif strike == max_pe_strike:
                frags.append(("class:oc.wall.put", f" {int(strike)} S"))
            elif max_pain and int(strike) == int(max_pain):
                frags.append(("class:oc.maxpain", f" {int(strike)} *"))
            else:
                frags.append(("class:oc.strike", f" {int(strike):^6} "))

            frags.append(("class:oc.border", "│"))

            # Put Side (33 chars)
            frags.append((pe_style, f" {pe_ltp_str} {pe_delta_str} {pe_iv_str} ", pe_click))
            frags.append(("class:oc.bar.put", f"{pe_bar} ", pe_click))
            frags.append((pe_style, f"{pe_oi_str}", pe_click))
            frags.append(("", "\n"))

        frags.append(("class:oc.border", "────────────────────────────────┴──────────┴────────────────────────────────\n"))
        frags.append(("class:oc.tip", " Tip: Click any strike to stage order | Type 'oc <SYM> monthly' or 'oc <SYM> week <N>'\n"))
        return frags

    def _update_oc_subscriptions(self, strikes: list[dict]) -> None:
        """Subscribe the option-chain instrument tokens on the primary ticker
        (unsubscribing any tokens from a previously displayed chain).

        Also rebuilds ``self.oc_token_map`` so incoming ticks can be routed to
        the correct strike/side for live LTP updates.
        """
        new_tokens: set[int] = set()
        token_map: dict[int, list[tuple[dict, str]]] = defaultdict(list)
        for s in strikes:
            ce_tok = s.get("ce_token")
            pe_tok = s.get("pe_token")
            if ce_tok:
                c_tok_int = int(ce_tok)
                new_tokens.add(c_tok_int)
                token_map[c_tok_int].append((s, "ce"))
            if pe_tok:
                p_tok_int = int(pe_tok)
                new_tokens.add(p_tok_int)
                token_map[p_tok_int].append((s, "pe"))

        self.oc_token_map = dict(token_map)

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
        if hasattr(self, "oc_data") and self.oc_data:
            from cli.greeks import enrich_option_chain_greeks
            spot = float(self.oc_data.get("spot_price") or 0.0)
            exp = self.oc_data.get("expiry")
            strikes = self.oc_data.get("strikes", [])
            if spot > 0 and exp and strikes:
                self.oc_data["summary"] = enrich_option_chain_greeks(strikes, spot, exp)
        if hasattr(self, "oc_dual_data") and self.oc_dual_data:
            from cli.greeks import enrich_option_chain_greeks
            for oc_k in ("oc1", "oc2"):
                chain = self.oc_dual_data.get(oc_k)
                if chain:
                    spot = float(chain.get("spot_price") or 0.0)
                    exp = chain.get("expiry")
                    strikes = chain.get("strikes", [])
                    if spot > 0 and exp and strikes:
                        chain["summary"] = enrich_option_chain_greeks(strikes, spot, exp)

        self._last_oc_text = self._render_oc_text()
        self._last_oc_dual_text = self._render_oc_dual_text()
        if self.info_mode in ("oc", "oc_dual"):
            self._update_info_buffer(reset_scroll=False)
        if hasattr(self, "app") and self.app:
            self.app.invalidate()

    async def _fetch_and_display_dual_option_chain(
        self,
        api_key: str,
        underlying: str = "NIFTY",
        exp1_idx: int = 0,
        exp2_idx: int = 1,
        exp1_date: str | None = None,
        exp2_date: str | None = None,
    ) -> None:
        """Fetch and display two option chains side-by-side in the info pane."""
        try:
            task1 = self._run_api_call(
                self.client.get_option_chain,
                api_key=api_key,
                underlying=underlying,
                expiry_week=exp1_idx,
                expiry_date=exp1_date,
            )
            task2 = self._run_api_call(
                self.client.get_option_chain,
                api_key=api_key,
                underlying=underlying,
                expiry_week=exp2_idx,
                expiry_date=exp2_date,
            )
            res1, res2 = await asyncio.gather(task1, task2)
        except Exception as exc:
            self.log_message(f"Dual option chain error: {exc}")
            return

        if res1.get("status") != "success":
            self.log_message(f"Option chain 1 error: {res1.get('message', 'Unknown error')}")
            return
        if res2.get("status") != "success":
            self.log_message(f"Option chain 2 error: {res2.get('message', 'Unknown error')}")
            return

        strikes1 = res1.get("strikes", [])
        strikes2 = res2.get("strikes", [])
        expiries = res1.get("available_expiries", []) or res2.get("available_expiries", [])

        self.oc_dual_data = {
            "underlying": underlying,
            "oc1": res1,
            "oc2": res2,
            "available_expiries": expiries,
            "exp1_idx": exp1_idx,
            "exp2_idx": exp2_idx,
        }

        # Subscribe tokens for both chains so LTPs stream live
        all_strikes = strikes1 + strikes2
        self._update_oc_subscriptions(all_strikes)

        self._last_oc_dual_text = self._render_oc_dual_text()
        self.info_mode = "oc_dual"
        self._update_info_buffer(reset_scroll=False)

        # Center scroll position around ATM strike
        atm1 = res1.get("summary", {}).get("atm_strike")
        if atm1 and hasattr(self, "_info_control"):
            s1_map = {float(s.get("strike", 0)): idx for idx, s in enumerate(strikes1)}
            if atm1 in s1_map:
                target_scroll = max(0, (10 + s1_map[atm1]) - 12)
                self._info_control.vertical_scroll_position = target_scroll
                if hasattr(self, "info_window"):
                    self.info_window.vertical_scroll = target_scroll

        stream_note = " (streaming)" if self.primary_api_key else ""
        self.log_message(
            f"Dual option chain loaded for {underlying}: "
            f"{res1.get('expiry')} vs {res2.get('expiry')}{stream_note}."
        )

    def _render_oc_dual_text(self) -> str:
        """Render the current ``self.oc_dual_data`` into plain text."""
        data = self.oc_dual_data
        if not data:
            return self._last_oc_dual_text

        underlying = data.get("underlying", "NIFTY")
        oc1 = data.get("oc1", {})
        oc2 = data.get("oc2", {})
        exp1 = oc1.get("expiry", "")
        exp2 = oc2.get("expiry", "")
        spot1 = float(oc1.get("spot_price") or 0.0)
        spot2 = float(oc2.get("spot_price") or spot1)

        sum1 = oc1.get("summary", {})
        sum2 = oc2.get("summary", {})
        strikes1 = oc1.get("strikes", [])
        strikes2 = oc2.get("strikes", [])
        expiries = data.get("available_expiries", [])

        def _fmt_dt(exp_str):
            try:
                from datetime import datetime as _dt
                d = _dt.strptime(exp_str[:10], "%Y-%m-%d").date()
                return d.strftime("%d-%b")
            except Exception:
                return exp_str

        exp1_dt = _fmt_dt(exp1)
        exp2_dt = _fmt_dt(exp2)

        lines = []
        top_bar = (" [ ▾ Expiry 1: " + exp1_dt + " ]").ljust(60) + (" [ ▾ Expiry 2: " + exp2_dt + " ]").rjust(60)
        lines.append(top_bar)
        lines.append("─" * 120)

        c1_str = f"₹{sum1.get('atm_straddle', 0.0):.1f}" if sum1.get("atm_straddle") else "-"
        c2_str = f"₹{sum2.get('atm_straddle', 0.0):.1f}" if sum2.get("atm_straddle") else "-"
        m1_pain = int(sum1.get("max_pain", 0))
        m2_pain = int(sum2.get("max_pain", 0))

        left_title = f" Expiry 1: {exp1_dt} "
        right_title = f" Expiry 2: {exp2_dt} "
        top_left = "┌──" + left_title + ("─" * max(0, 56 - len(left_title)))
        top_right = "──" + right_title + ("─" * max(0, 57 - len(right_title))) + "┐"
        lines.append(top_left + "┬" + top_right)

        c1_l1 = f"Spot: {spot1:,.2f}  Str: {c1_str:<7}"[:56].ljust(56)
        c2_l1 = f"Spot: {spot2:,.2f}  Str: {c2_str:<7}"[:57].ljust(57)
        lines.append(f"│ {c1_l1} │ {c2_l1} │")

        c1_l2 = f"PCR: {sum1.get('pcr', 0.0):.2f}  Max Pain: {m1_pain:<5}"[:56].ljust(56)
        c2_l2 = f"PCR: {sum2.get('pcr', 0.0):.2f}  Max Pain: {m2_pain:<5}"[:57].ljust(57)
        lines.append(f"│ {c1_l2} │ {c2_l2} │")
        lines.append("└" + ("─" * 58) + "┴" + ("─" * 59) + "┘")

        # Table Header (Enclosed Container)
        lines.append("┌───────────────┬─────────────┬───────────┬───────────┬──────────┬───────────────┬─────────────┬───────────┬───────────┐")
        lines.append("│   LTP1   LTP2 │  IV1    IV2 │  Δ1   Δ2  │  θ1   θ2  │  STRIKE  │   LTP1   LTP2 │  IV1    IV2 │  Δ1   Δ2  │  θ1   θ2  │")
        lines.append("├───────────────┼─────────────┼───────────┼───────────┼──────────┼───────────────┼─────────────┼───────────┼───────────┤")

        s1_map = {float(s.get("strike", 0)): s for s in strikes1}
        s2_map = {float(s.get("strike", 0)): s for s in strikes2}
        all_strikes = sorted(set(list(s1_map.keys()) + list(s2_map.keys())))

        atm1 = sum1.get("atm_strike")
        atm2 = sum2.get("atm_strike")

        def _fmt_iv(val):
            if val is None or val <= 0:
                return "     -"
            return f"{float(val):>5.1f}%"[:6]

        def _fmt_ltp(val):
            if val is None:
                return "      -"
            return f"{float(val):>7.2f}"[:7]

        def _fmt_delta(val):
            if val is None:
                return "    -"
            return f"{float(val):>5.2f}"[:5]

        def _fmt_theta(val):
            if val is None:
                return "    -"
            return f"{float(val):>5.1f}"[:5]

        for k in all_strikes:
            s1 = s1_map.get(k, {})
            s2 = s2_map.get(k, {})

            ce1_ltp_str = _fmt_ltp(s1.get("ce_ltp"))
            ce2_ltp_str = _fmt_ltp(s2.get("ce_ltp"))
            ce1_iv_str  = _fmt_iv(s1.get("ce_iv"))
            ce2_iv_str  = _fmt_iv(s2.get("ce_iv"))
            ce1_dlt_str = _fmt_delta(s1.get("ce_delta"))
            ce2_dlt_str = _fmt_delta(s2.get("ce_delta"))
            ce1_tht_str = _fmt_theta(s1.get("ce_theta"))
            ce2_tht_str = _fmt_theta(s2.get("ce_theta"))

            pe1_ltp_str = _fmt_ltp(s1.get("pe_ltp"))
            pe2_ltp_str = _fmt_ltp(s2.get("pe_ltp"))
            pe1_iv_str  = _fmt_iv(s1.get("pe_iv"))
            pe2_iv_str  = _fmt_iv(s2.get("pe_iv"))
            pe1_dlt_str = _fmt_delta(s1.get("pe_delta"))
            pe2_dlt_str = _fmt_delta(s2.get("pe_delta"))
            pe1_tht_str = _fmt_theta(s1.get("pe_theta"))
            pe2_tht_str = _fmt_theta(s2.get("pe_theta"))

            is_atm = (k == atm1 or k == atm2)
            s_str = str(int(k))
            disp_stk = f"[{s_str:^8}]" if is_atm else f" {s_str:^8} "

            c_ltp = f"{ce1_ltp_str} {ce2_ltp_str}"
            c_iv  = f"{ce1_iv_str} {ce2_iv_str}"
            c_dlt = f"{ce1_dlt_str} {ce2_dlt_str}"
            c_tht = f"{ce1_tht_str} {ce2_tht_str}"

            p_ltp = f"{pe1_ltp_str} {pe2_ltp_str}"
            p_iv  = f"{pe1_iv_str} {pe2_iv_str}"
            p_dlt = f"{pe1_dlt_str} {pe2_dlt_str}"
            p_tht = f"{pe1_tht_str} {pe2_tht_str}"

            lines.append(f"│{c_ltp}│{c_iv}│{c_dlt}│{c_tht}│{disp_stk}│{p_ltp}│{p_iv}│{p_dlt}│{p_tht}│")

        lines.append("└───────────────┴─────────────┴───────────┴───────────┴──────────┴───────────────┴─────────────┴───────────┴───────────┘")
        lines.append(" Tip: Click any strike to stage order | Click dropdowns to switch expiries")
        return "\n".join(lines)

    def _build_oc_dual_formatted_fragments(self) -> list[tuple]:
        """Build FormattedTextControl fragment list for Side-by-Side Dual Option Chain with click routing."""
        data = self.oc_dual_data
        if not data:
            return to_formatted_text(ANSI(self._last_oc_dual_text))

        underlying = data.get("underlying", "NIFTY")
        oc1 = data.get("oc1", {})
        oc2 = data.get("oc2", {})
        exp1 = oc1.get("expiry", "")
        exp2 = oc2.get("expiry", "")
        spot1 = float(oc1.get("spot_price") or 0.0)
        spot2 = float(oc2.get("spot_price") or spot1)

        sum1 = oc1.get("summary", {})
        sum2 = oc2.get("summary", {})
        strikes1 = oc1.get("strikes", [])
        strikes2 = oc2.get("strikes", [])
        expiries = data.get("available_expiries", [])

        def _fmt_tab_dt(exp_str: str) -> str:
            try:
                from datetime import datetime as _dt
                d = _dt.strptime(exp_str[:10], "%Y-%m-%d").date()
                return d.strftime("%d-%b")
            except Exception:
                return exp_str

        exp1_dt = _fmt_tab_dt(exp1)
        exp2_dt = _fmt_tab_dt(exp2)

        def _make_exp1_click(idx: int, exp_val: str):
            def _handler(mouse_event):
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    self.oc_dual_dropdown_1 = False
                    und = self.oc_dual_data.get("underlying", "NIFTY")
                    ak = self.primary_api_key or (self.accounts[0]["api_key"] if self.accounts else "")
                    if ak:
                        e2_idx = self.oc_dual_data.get("exp2_idx", 1)
                        asyncio.create_task(self._fetch_and_display_dual_option_chain(
                            ak, und, exp1_idx=idx, exp2_idx=e2_idx, exp1_date=exp_val
                        ))
            return _handler

        def _make_exp2_click(idx: int, exp_val: str):
            def _handler(mouse_event):
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    self.oc_dual_dropdown_2 = False
                    und = self.oc_dual_data.get("underlying", "NIFTY")
                    ak = self.primary_api_key or (self.accounts[0]["api_key"] if self.accounts else "")
                    if ak:
                        e1_idx = self.oc_dual_data.get("exp1_idx", 0)
                        asyncio.create_task(self._fetch_and_display_dual_option_chain(
                            ak, und, exp1_idx=e1_idx, exp2_idx=idx, exp2_date=exp_val
                        ))
            return _handler

        def _make_dropdown_1_toggle():
            def _handler(mouse_event):
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    self.oc_dual_dropdown_1 = not getattr(self, "oc_dual_dropdown_1", False)
                    self.oc_dual_dropdown_2 = False
                    self._update_info_buffer(reset_scroll=False)
                    if hasattr(self, "app") and self.app:
                        self.app.invalidate()
            return _handler

        def _make_dropdown_2_toggle():
            def _handler(mouse_event):
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    self.oc_dual_dropdown_2 = not getattr(self, "oc_dual_dropdown_2", False)
                    self.oc_dual_dropdown_1 = False
                    self._update_info_buffer(reset_scroll=False)
                    if hasattr(self, "app") and self.app:
                        self.app.invalidate()
            return _handler

        def _make_strike_click(sym: str, ltp_val: float | None, is_call: bool = True, exp_num: int = 1):
            def _handler(mouse_event):
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    if sym and sym != "-":
                        tag = "Call (CE)" if is_call else "Put (PE)"
                        color = "green" if is_call else "red"
                        self.log_message(f"Selected [bold {color}]Expiry {exp_num} {tag}: {sym}[/bold {color}] (LTP: {ltp_val or 'N/A'})")
                        buf = None
                        if hasattr(self, "input_field") and self.input_field:
                            buf = self.input_field.buffer
                        elif hasattr(self, "cmd_buffer") and self.cmd_buffer:
                            buf = self.cmd_buffer

                        if buf is not None:
                            price_str = f" {float(ltp_val):.2f}" if (ltp_val is not None and float(ltp_val) > 0) else ""
                            buf.text = f"sell {sym} 27L{price_str}"
                            buf.cursor_position = len(buf.text)
                            if hasattr(self, "app") and self.app and hasattr(self, "input_field") and self.input_field:
                                self.app.layout.focus(self.input_field)
                        self._update_display_and_invalidate()
            return _handler

        frags = [("", " ")]

        # 1. Top Expiry Selectors (2 Dropdown buttons)
        dd1_style = "class:oc.expiry.active" if getattr(self, "oc_dual_dropdown_1", False) else "class:oc.set1.btn"
        dd2_style = "class:oc.expiry.active" if getattr(self, "oc_dual_dropdown_2", False) else "class:oc.set2.btn"

        frags.append((dd1_style, f"[ ▾ Expiry 1: {exp1_dt} ]", _make_dropdown_1_toggle()))
        frags.append(("", " " * 60))
        frags.append((dd2_style, f"[ ▾ Expiry 2: {exp2_dt} ]", _make_dropdown_2_toggle()))
        frags.append(("", "\n"))

        # 2. Interactive Dropdown Menus
        if getattr(self, "oc_dual_dropdown_1", False):
            frags.append(("class:oc.border", "┌── Select Expiry 1 (Click to switch) ─────────────────────────────────────────────────────────────────────────┐\n"))
            for i, e in enumerate(expiries):
                dt = e.get("expiry", "")
                lbl = e.get("week_label", "")
                is_cur = (dt == exp1)
                prefix = "► " if is_cur else "  "
                item_style = "class:oc.dropdown.active" if is_cur else "class:oc.dropdown.item"
                frags.append(("class:oc.border", "│ "))
                frags.append((item_style, f"{prefix}{dt:<12} • {lbl:<96}", _make_exp1_click(i, dt)))
                frags.append(("class:oc.border", " │\n"))
            frags.append(("class:oc.border", "│ "))
            frags.append(("class:oc.dropdown.close", "[ ▲ Close Dropdown ]                                                                                        ", _make_dropdown_1_toggle()))
            frags.append(("class:oc.border", " │\n"))
            frags.append(("class:oc.border", "└────────────────────────────────────────────────────────────────────────────────────────────────────────────┘\n"))

        if getattr(self, "oc_dual_dropdown_2", False):
            frags.append(("class:oc.border", "┌── Select Expiry 2 (Click to switch) ─────────────────────────────────────────────────────────────────────────┐\n"))
            for i, e in enumerate(expiries):
                dt = e.get("expiry", "")
                lbl = e.get("week_label", "")
                is_cur = (dt == exp2)
                prefix = "► " if is_cur else "  "
                item_style = "class:oc.dropdown.active" if is_cur else "class:oc.dropdown.item"
                frags.append(("class:oc.border", "│ "))
                frags.append((item_style, f"{prefix}{dt:<12} • {lbl:<96}", _make_exp2_click(i, dt)))
                frags.append(("class:oc.border", " │\n"))
            frags.append(("class:oc.border", "│ "))
            frags.append(("class:oc.dropdown.close", "[ ▲ Close Dropdown ]                                                                                        ", _make_dropdown_2_toggle()))
            frags.append(("class:oc.border", " │\n"))
            frags.append(("class:oc.border", "└────────────────────────────────────────────────────────────────────────────────────────────────────────────┘\n"))

        # 3. Distinction Line
        frags.append(("class:oc.border", "────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────\n"))

        # 4. Symmetrical Dual Metrics Card (exact 120 chars wide)
        c1_str = f"₹{sum1.get('atm_straddle', 0.0):.1f}" if sum1.get("atm_straddle") else "-"
        c2_str = f"₹{sum2.get('atm_straddle', 0.0):.1f}" if sum2.get("atm_straddle") else "-"
        m1_pain = int(sum1.get("max_pain", 0))
        m2_pain = int(sum2.get("max_pain", 0))

        left_title = f" Expiry 1: {exp1_dt} "
        right_title = f" Expiry 2: {exp2_dt} "
        top_left = "┌──" + left_title + ("─" * max(0, 56 - len(left_title)))
        top_right = "──" + right_title + ("─" * max(0, 57 - len(right_title))) + "┐"
        frags.append(("class:oc.border", top_left + "┬" + top_right + "\n"))

        c1_l1 = f"Spot: {spot1:,.2f}  Str: {c1_str:<7}"[:56].ljust(56)
        c2_l1 = f"Spot: {spot2:,.2f}  Str: {c2_str:<7}"[:57].ljust(57)
        frags.append(("class:oc.border", "│ "))
        frags.append(("class:oc.set1.hdr", c1_l1))
        frags.append(("class:oc.border", " │ "))
        frags.append(("class:oc.set2.hdr", c2_l1))
        frags.append(("class:oc.border", " │\n"))

        c1_l2 = f"PCR: {sum1.get('pcr', 0.0):.2f}  Max Pain: {m1_pain:<5}"[:56].ljust(56)
        c2_l2 = f"PCR: {sum2.get('pcr', 0.0):.2f}  Max Pain: {m2_pain:<5}"[:57].ljust(57)
        frags.append(("class:oc.border", "│ "))
        frags.append(("class:oc.set1.hdr", c1_l2))
        frags.append(("class:oc.border", " │ "))
        frags.append(("class:oc.set2.hdr", c2_l2))
        frags.append(("class:oc.border", " │\n"))
        frags.append(("class:oc.border", "└" + ("─" * 58) + "┴" + ("─" * 59) + "┘\n"))

        # 5. Table Header (Enclosed Container)
        frags.append(("class:oc.border", "┌───────────────┬─────────────┬───────────┬───────────┬──────────┬───────────────┬─────────────┬───────────┬───────────┐\n"))
        frags.append(("class:oc.border", "│"))
        frags.append(("class:oc.set1.hdr", "   LTP1"))
        frags.append(("class:oc.set2.hdr", "   LTP2 "))
        frags.append(("class:oc.border", "│"))
        frags.append(("class:oc.set1.hdr", "  IV1 "))
        frags.append(("class:oc.set2.hdr", "   IV2 "))
        frags.append(("class:oc.border", "│"))
        frags.append(("class:oc.set1.hdr", "  Δ1  "))
        frags.append(("class:oc.set2.hdr", " Δ2  "))
        frags.append(("class:oc.border", "│"))
        frags.append(("class:oc.set1.hdr", "  θ1  "))
        frags.append(("class:oc.set2.hdr", " θ2  "))
        frags.append(("class:oc.border", "│"))
        frags.append(("class:oc.strike",   "  STRIKE  "))
        frags.append(("class:oc.border", "│"))
        frags.append(("class:oc.set1.hdr", "   LTP1"))
        frags.append(("class:oc.set2.hdr", "   LTP2 "))
        frags.append(("class:oc.border", "│"))
        frags.append(("class:oc.set1.hdr", "  IV1 "))
        frags.append(("class:oc.set2.hdr", "   IV2 "))
        frags.append(("class:oc.border", "│"))
        frags.append(("class:oc.set1.hdr", "  Δ1  "))
        frags.append(("class:oc.set2.hdr", " Δ2  "))
        frags.append(("class:oc.border", "│"))
        frags.append(("class:oc.set1.hdr", "  θ1  "))
        frags.append(("class:oc.set2.hdr", " θ2  "))
        frags.append(("class:oc.border", "│\n"))
        frags.append(("class:oc.border", "├───────────────┼─────────────┼───────────┼───────────┼──────────┼───────────────┼─────────────┼───────────┼───────────┤\n"))

        # 6. Strike Rows
        s1_map = {float(s.get("strike", 0)): s for s in strikes1}
        s2_map = {float(s.get("strike", 0)): s for s in strikes2}
        all_strikes = sorted(set(list(s1_map.keys()) + list(s2_map.keys())))

        atm1 = sum1.get("atm_strike")
        atm2 = sum2.get("atm_strike")

        def _fmt_iv(val):
            if val is None or val <= 0:
                return "     -"
            return f"{float(val):>5.1f}%"[:6]

        def _fmt_ltp(val):
            if val is None:
                return "      -"
            return f"{float(val):>7.2f}"[:7]

        def _fmt_delta(val):
            if val is None:
                return "    -"
            return f"{float(val):>5.2f}"[:5]

        def _fmt_theta(val):
            if val is None:
                return "    -"
            return f"{float(val):>5.1f}"[:5]

        for k in all_strikes:
            s1 = s1_map.get(k, {})
            s2 = s2_map.get(k, {})

            ce1_sym = s1.get("ce_symbol") or "-"
            ce2_sym = s2.get("ce_symbol") or "-"
            pe1_sym = s1.get("pe_symbol") or "-"
            pe2_sym = s2.get("pe_symbol") or "-"

            ce1_ltp = s1.get("ce_ltp")
            ce2_ltp = s2.get("ce_ltp")
            pe1_ltp = s1.get("pe_ltp")
            pe2_ltp = s2.get("pe_ltp")

            ce1_ltp_str = _fmt_ltp(ce1_ltp)
            ce2_ltp_str = _fmt_ltp(ce2_ltp)
            ce1_iv_str  = _fmt_iv(s1.get("ce_iv"))
            ce2_iv_str  = _fmt_iv(s2.get("ce_iv"))
            ce1_dlt_str = _fmt_delta(s1.get("ce_delta"))
            ce2_dlt_str = _fmt_delta(s2.get("ce_delta"))
            ce1_tht_str = _fmt_theta(s1.get("ce_theta"))
            ce2_tht_str = _fmt_theta(s2.get("ce_theta"))

            pe1_ltp_str = _fmt_ltp(pe1_ltp)
            pe2_ltp_str = _fmt_ltp(pe2_ltp)
            pe1_iv_str  = _fmt_iv(s1.get("pe_iv"))
            pe2_iv_str  = _fmt_iv(s2.get("pe_iv"))
            pe1_dlt_str = _fmt_delta(s1.get("pe_delta"))
            pe2_dlt_str = _fmt_delta(s2.get("pe_delta"))
            pe1_tht_str = _fmt_theta(s1.get("pe_theta"))
            pe2_tht_str = _fmt_theta(s2.get("pe_theta"))

            ce1_click = _make_strike_click(ce1_sym, ce1_ltp, is_call=True, exp_num=1)
            ce2_click = _make_strike_click(ce2_sym, ce2_ltp, is_call=True, exp_num=2)
            pe1_click = _make_strike_click(pe1_sym, pe1_ltp, is_call=False, exp_num=1)
            pe2_click = _make_strike_click(pe2_sym, pe2_ltp, is_call=False, exp_num=2)

            is_atm = (k == atm1 or k == atm2)
            stk_style = "class:oc.atm" if is_atm else "class:oc.strike"
            s_str = str(int(k))
            disp_stk = f"[{s_str:^8}]" if is_atm else f" {s_str:^8} "

            frags.append(("class:oc.border", "│"))
            # Call LTP1 & LTP2
            frags.append(("class:oc.set1.ce", ce1_ltp_str, ce1_click))
            frags.append(("", " "))
            frags.append(("class:oc.set2.ce", ce2_ltp_str, ce2_click))
            frags.append(("class:oc.border", "│"))
            # Call IV1 & IV2
            frags.append(("class:oc.set1.ce", ce1_iv_str, ce1_click))
            frags.append(("", " "))
            frags.append(("class:oc.set2.ce", ce2_iv_str, ce2_click))
            frags.append(("class:oc.border", "│"))
            # Call Delta 1 & 2
            frags.append(("class:oc.set1.ce", ce1_dlt_str, ce1_click))
            frags.append(("", " "))
            frags.append(("class:oc.set2.ce", ce2_dlt_str, ce2_click))
            frags.append(("class:oc.border", "│"))
            # Call Theta 1 & 2
            frags.append(("class:oc.set1.ce", ce1_tht_str, ce1_click))
            frags.append(("", " "))
            frags.append(("class:oc.set2.ce", ce2_tht_str, ce2_click))
            frags.append(("class:oc.border", "│"))
            # Strike
            frags.append((stk_style, disp_stk))
            frags.append(("class:oc.border", "│"))
            # Put LTP1 & LTP2
            frags.append(("class:oc.set1.pe", pe1_ltp_str, pe1_click))
            frags.append(("", " "))
            frags.append(("class:oc.set2.pe", pe2_ltp_str, pe2_click))
            frags.append(("class:oc.border", "│"))
            # Put IV1 & IV2
            frags.append(("class:oc.set1.pe", pe1_iv_str, pe1_click))
            frags.append(("", " "))
            frags.append(("class:oc.set2.pe", pe2_iv_str, pe2_click))
            frags.append(("class:oc.border", "│"))
            # Put Delta 1 & 2
            frags.append(("class:oc.set1.pe", pe1_dlt_str, pe1_click))
            frags.append(("", " "))
            frags.append(("class:oc.set2.pe", pe2_dlt_str, pe2_click))
            frags.append(("class:oc.border", "│"))
            # Put Theta 1 & 2
            frags.append(("class:oc.set1.pe", pe1_tht_str, pe1_click))
            frags.append(("", " "))
            frags.append(("class:oc.set2.pe", pe2_tht_str, pe2_click))
            frags.append(("class:oc.border", "│\n"))

        frags.append(("class:oc.border", "└───────────────┴─────────────┴───────────┴───────────┴──────────┴───────────────┴─────────────┴───────────┴───────────┘\n"))
        frags.append(("class:oc.tip", " Tip: Click any strike to stage order | Click dropdowns to switch expiries\n"))
        return frags

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


    def get_symbol_ltp(self, symbol: str) -> float | None:
        """Look up current LTP for a symbol from WebSocket caches, positions, or option chains."""
        if not symbol:
            return None
        sym_clean = symbol.strip().upper()
        if hasattr(self, "ws_symbol_ltp_cache") and sym_clean in self.ws_symbol_ltp_cache:
            return float(self.ws_symbol_ltp_cache[sym_clean])
        if hasattr(self, "ws_symbol_to_token") and sym_clean in self.ws_symbol_to_token:
            tok = self.ws_symbol_to_token[sym_clean]
            if hasattr(self, "ws_ltp_cache") and tok in self.ws_ltp_cache:
                return float(self.ws_ltp_cache[tok])
        for p in getattr(self, "active_positions", []):
            if p.get("tradingsymbol", "").upper() == sym_clean and p.get("last_price"):
                return float(p["last_price"])
        resp = getattr(self, "last_positions_response", None)
        if resp:
            for acct in resp.get("accounts", []):
                for p in acct.get("positions", []):
                    if p.get("tradingsymbol", "").upper() == sym_clean and p.get("last_price"):
                        return float(p["last_price"])
        if hasattr(self, "oc_data") and self.oc_data:
            for s in self.oc_data.get("strikes", []):
                if s.get("ce_symbol") == sym_clean and s.get("ce_ltp"):
                    return float(s["ce_ltp"])
                if s.get("pe_symbol") == sym_clean and s.get("pe_ltp"):
                    return float(s["pe_ltp"])
        if hasattr(self, "oc_dual_data") and self.oc_dual_data:
            for oc_k in ("oc1", "oc2"):
                chain = self.oc_dual_data.get(oc_k)
                if chain:
                    for s in chain.get("strikes", []):
                        if s.get("ce_symbol") == sym_clean and s.get("ce_ltp"):
                            return float(s["ce_ltp"])
                        if s.get("pe_symbol") == sym_clean and s.get("pe_ltp"):
                            return float(s["pe_ltp"])
        return None

    def _render_orders_pane(self, response: dict, mode: str) -> str:
        """Render pending or executed orders from a /api/orders response into tabular formatted text with status colors."""
        PENDING_STATUSES = {"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "PUT ORDER REQ RECEIVED"}
        EXECUTED_STATUSES = {"COMPLETE", "REJECTED", "CANCELLED"}
        lines = []
        any_orders = False

        for acct in response.get("accounts", []):
            name = acct.get("name", "?")
            acct_orders = acct.get("orders", [])

            if mode == "orders_pending":
                # Main Orders pane shows ALL open/pending orders + latest 4 executed/terminal orders
                pending_orders = [o for o in acct_orders if o.get("status", "").upper() in PENDING_STATUSES]
                executed_orders = [o for o in acct_orders if o.get("status", "").upper() in EXECUTED_STATUSES]
                recent_executed = executed_orders[-4:]
                orders = pending_orders + recent_executed
            else:
                orders = [o for o in acct_orders if o.get("status", "").upper() in EXECUTED_STATUSES]

            lines.append(f"Account: {name}")

            if acct.get("status", "").startswith("error"):
                err_detail = acct["status"].split("error:")[-1].strip() if "error:" in acct["status"] else acct["status"]
                lines.append(f"  Error: {self._clean_error(err_detail)}")
            elif not orders:
                lines.append("  (no orders)")
            else:
                any_orders = True
                w_oid = 8
                w_tx = 4
                w_sym = max(18, max((len(str(o.get("tradingsymbol", ""))) for o in orders), default=18))
                w_qty = max(9, max((len(f"{o.get('filled_quantity', 0)}/{o.get('quantity', 0)}") for o in orders), default=9))
                w_price = 14
                w_ltp = 9
                w_prod = 5
                w_status = max(10, max((len(str(o.get("status", ""))) for o in orders), default=10))

                top_border = f"┌─{'─'*w_oid}─┬─{'─'*w_tx}─┬─{'─'*w_sym}─┬─{'─'*w_qty}─┬─{'─'*w_price}─┬─{'─'*w_ltp}─┬─{'─'*w_prod}─┬─{'─'*w_status}─┐"
                header_row = f"│ {'Order ID':<{w_oid}} │ {'Tx':<{w_tx}} │ {'Symbol':<{w_sym}} │ {'Qty':<{w_qty}} │ {'Placed Price':<{w_price}} │ {'Live LTP':<{w_ltp}} │ {'Prod':<{w_prod}} │ {'Status':<{w_status}} │"
                mid_border = f"├─{'─'*w_oid}─┼─{'─'*w_tx}─┼─{'─'*w_sym}─┼─{'─'*w_qty}─┼─{'─'*w_price}─┼─{'─'*w_ltp}─┼─{'─'*w_prod}─┼─{'─'*w_status}─┤"
                bot_border = f"└─{'─'*w_oid}─┴─{'─'*w_tx}─┴─{'─'*w_sym}─┴─{'─'*w_qty}─┴─{'─'*w_price}─┴─{'─'*w_ltp}─┴─{'─'*w_prod}─┴─{'─'*w_status}─┘"

                lines.append(top_border)
                lines.append(header_row)
                lines.append(mid_border)

                for o in orders:
                    sym = o.get("tradingsymbol", "?")
                    tx = o.get("transaction_type", "?")
                    qty = o.get("quantity", 0)
                    filled = o.get("filled_quantity", 0)
                    otype = o.get("order_type", "?")
                    product = o.get("product", "?")
                    status = str(o.get("status", "?"))
                    status_upper = status.upper()
                    price = o.get("price", 0.0)
                    avg = o.get("average_price", 0.0)
                    oid = str(o.get("order_id", "?"))[-6:]

                    if status_upper == "COMPLETE":
                        price_desc = f"{otype} avg={avg:.2f}" if avg else f"{otype} MKT"
                    else:
                        price_desc = f"{otype} @ {price:.2f}" if price else f"{otype} MKT"

                    ltp = self.get_symbol_ltp(sym)
                    ltp_desc = f"₹{ltp:.2f}" if ltp is not None else "-"

                    qty_desc = f"{filled}/{qty}"
                    status_desc = status

                    # Status Color Coding:
                    # Open/Pending: Orange (\033[38;5;208m)
                    # Complete: Green (\033[38;5;48m)
                    # Rejected/Failed/Cancelled: Red (\033[38;5;196m)
                    if status_upper in PENDING_STATUSES:
                        color_start = "\033[38;5;208m"
                    elif status_upper == "COMPLETE":
                        color_start = "\033[38;5;48m"
                    else:  # REJECTED, CANCELLED, FAILED, etc.
                        color_start = "\033[38;5;196m"
                    color_end = "\033[0m"

                    lines.append(
                        f"│ {color_start}{oid:<{w_oid}}{color_end} │ "
                        f"{color_start}{tx:<{w_tx}}{color_end} │ "
                        f"{color_start}{sym:<{w_sym}}{color_end} │ "
                        f"{color_start}{qty_desc:<{w_qty}}{color_end} │ "
                        f"{color_start}{price_desc:<{w_price}}{color_end} │ "
                        f"{color_start}{ltp_desc:<{w_ltp}}{color_end} │ "
                        f"{color_start}{product:<{w_prod}}{color_end} │ "
                        f"{color_start}{status_desc:<{w_status}}{color_end} │"
                    )
                lines.append(bot_border)
            lines.append("")

        if not any_orders and mode == "orders_pending":
            lines.append("No open or recent orders across all accounts.")

        # Scheduled orders section (in Pending Orders view)
        if mode == "orders_pending" and hasattr(self, "recorder") and self.recorder:
            try:
                today_str = datetime.now().strftime("%Y-%m-%d")
                sched_list = self.recorder.get_all_scheduled_orders_today(today_str)
                if sched_list:
                    lines.append("")
                    lines.append("⏰ Scheduled Orders (Today):")
                    w_sid = 4
                    w_trig = 19
                    w_cd = 14
                    w_acct = 8
                    w_order = 28
                    w_st = 11

                    s_top = f"┌─{'─'*w_sid}─┬─{'─'*w_trig}─┬─{'─'*w_cd}─┬─{'─'*w_acct}─┬─{'─'*w_order}─┬─{'─'*w_st}─┐"
                    s_hdr = f"│ {'ID':<{w_sid}} │ {'Target Time':<{w_trig}} │ {'Countdown':<{w_cd}} │ {'Account':<{w_acct}} │ {'Order Details':<{w_order}} │ {'Status':<{w_st}} │"
                    s_mid = f"├─{'─'*w_sid}─┼─{'─'*w_trig}─┼─{'─'*w_cd}─┼─{'─'*w_acct}─┼─{'─'*w_order}─┼─{'─'*w_st}─┤"
                    s_bot = f"└─{'─'*w_sid}─┴─{'─'*w_trig}─┴─{'─'*w_cd}─┴─{'─'*w_acct}─┴─{'─'*w_order}─┴─{'─'*w_st}─┘"

                    lines.append(s_top)
                    lines.append(s_hdr)
                    lines.append(s_mid)

                    now = datetime.now()
                    for so in sched_list:
                        sid_str = str(so.get("id", ""))
                        trig_str = str(so.get("trigger_time", ""))[:w_trig]
                        acct_str = str(so.get("account_name", "ALL"))[:w_acct]
                        act = so.get("action", "")
                        sym = so.get("symbol_or_id", "")
                        qty = so.get("quantity") or ""
                        pr = so.get("price")
                        pr_str = f"@{pr:.2f}" if pr else "@MKT"
                        is_c = bool(so.get("is_conditional"))
                        c_child = so.get("child_command_raw") or ""

                        ord_detail = f"{act} {qty} {sym} {pr_str}".strip()
                        if is_c:
                            ord_detail = f"{ord_detail} -> {c_child}"
                        ord_detail = ord_detail[:w_order]

                        st_str = str(so.get("status", ""))

                        # Compute countdown if pending
                        if st_str == "PENDING":
                            try:
                                if "T" in trig_str:
                                    t_dt = datetime.fromisoformat(trig_str)
                                else:
                                    t_dt = datetime.strptime(trig_str, "%Y-%m-%d %H:%M:%S")
                                diff = (t_dt - now).total_seconds()
                                if diff > 0:
                                    hours, rem = divmod(int(diff), 3600)
                                    mins, secs = divmod(rem, 60)
                                    cd_str = f"T - {hours:02d}:{mins:02d}:{secs:02d}"
                                else:
                                    cd_str = "Triggering..."
                            except Exception:
                                cd_str = "-"
                            s_col = "\033[38;5;51m"   # Cyan
                        elif st_str in ("TRIGGERED", "COMPLETE"):
                            cd_str = "Triggered"
                            s_col = "\033[38;5;48m"   # Green
                        else:
                            cd_str = "-"
                            s_col = "\033[38;5;196m"  # Red

                        s_end = "\033[0m"
                        lines.append(
                            f"│ {s_col}{sid_str:<{w_sid}}{s_end} │ "
                            f"{s_col}{trig_str:<{w_trig}}{s_end} │ "
                            f"{s_col}{cd_str:<{w_cd}}{s_end} │ "
                            f"{s_col}{acct_str:<{w_acct}}{s_end} │ "
                            f"{s_col}{ord_detail:<{w_order}}{s_end} │ "
                            f"{s_col}{st_str:<{w_st}}{s_end} │"
                        )
                    lines.append(s_bot)
            except Exception as exc:
                logger.warning("Error rendering scheduled orders in orders pane: %s", exc)

        # Conditional orders section (in Pending Orders view)
        if mode == "orders_pending" and hasattr(self, "recorder") and self.recorder:
            try:
                today_str = datetime.now().strftime("%Y-%m-%d")
                cond_list = self.recorder.get_all_conditional_orders_today(today_str)
                if cond_list:
                    lines.append("")
                    lines.append("⏳ Conditional / Chained Orders (Today):")
                    w_cid = 4
                    w_poid = 10
                    w_sym = 18
                    w_cond = 24
                    w_st = 11

                    c_top = f"┌─{'─'*w_cid}─┬─{'─'*w_poid}─┬─{'─'*w_sym}─┬─{'─'*w_cond}─┬─{'─'*w_st}─┐"
                    c_hdr = f"│ {'ID':<{w_cid}} │ {'Parent ID':<{w_poid}} │ {'Symbol':<{w_sym}} │ {'Child Order (On Fill)':<{w_cond}} │ {'Status':<{w_st}} │"
                    c_mid = f"├─{'─'*w_cid}─┼─{'─'*w_poid}─┼─{'─'*w_sym}─┼─{'─'*w_cond}─┼─{'─'*w_st}─┤"
                    c_bot = f"└─{'─'*w_cid}─┴─{'─'*w_poid}─┴─{'─'*w_sym}─┴─{'─'*w_cond}─┴─{'─'*w_st}─┘"

                    lines.append(c_top)
                    lines.append(c_hdr)
                    lines.append(c_mid)

                    for co in cond_list:
                        cid_str = str(co.get("id", ""))
                        p_ids = co.get("parent_order_ids", [])
                        p_oid_str = str(p_ids[0])[-8:] if p_ids else "-"
                        sym_str = str(co.get("parent_symbol", ""))[:w_sym]
                        child_cmd_str = str(co.get("child_command_raw", ""))[:w_cond]
                        st_str = str(co.get("status", ""))

                        if st_str == "PENDING":
                            c_col = "\033[38;5;208m"  # Orange
                        elif st_str in ("TRIGGERED", "COMPLETE"):
                            c_col = "\033[38;5;48m"   # Green
                        else:
                            c_col = "\033[38;5;196m"  # Red
                        c_end = "\033[0m"

                        lines.append(
                            f"│ {c_col}{cid_str:<{w_cid}}{c_end} │ "
                            f"{c_col}{p_oid_str:<{w_poid}}{c_end} │ "
                            f"{c_col}{sym_str:<{w_sym}}{c_end} │ "
                            f"{c_col}{child_cmd_str:<{w_cond}}{c_end} │ "
                            f"{c_col}{st_str:<{w_st}}{c_end} │"
                        )
                    lines.append(c_bot)
            except Exception as exc:
                logger.warning("Error rendering conditional orders in orders pane: %s", exc)

        return "\n".join(lines)

    def _build_help_text(self) -> str:
        """Return formatted keyboard shortcuts & command cheat sheet in clean tabular layout."""
        return """┌─────────────────────────────────────────────────────────────────────────────┐
│                       KiteCLI COMMAND & HOTKEY CHEAT SHEET                  │
└─────────────────────────────────────────────────────────────────────────────┘

 1. TAB SWITCHING (RIGHT PANE)
┌───────────────────────┬───────────────────────┬─────────────────────────────┐
│ Prompt Command        │ Keyboard Hotkey       │ Target Tab                  │
├───────────────────────┼───────────────────────┼─────────────────────────────┤
│ p  / pending  / 1     │ Ctrl + P  / F1        │ Pending Orders Pane         │
│ e  / executed / 2     │ Ctrl + 2  / F2        │ Executed Orders Pane        │
│ oc / chain    / 3     │ Ctrl + O  / F3        │ Option Chain Pane           │
│ adv / advisor / 4     │ Ctrl + G  / F4        │ AI Expiry Advisor Pane      │
│ pnl / pl      / 5     │ F5 / Esc+5            │ P&L Analytics (Week/Mo/Yr)  │
│ md / depth    / 6     │ F6 / Esc+6            │ Market Depth (5-level Book) │
│ port/ holdings / 7    │ Ctrl + 7  / F7        │ Holdings & Portfolio Pane   │
│ h  / help     / 8     │ Ctrl + 8  / F8        │ Help & Cheat Sheet Pane     │
└───────────────────────┴───────────────────────┴─────────────────────────────┘
  * Quick Navigation: Click directly on any tab title in the header bar.
  * Holdings Commands: Type 'holdings' (Summary), 'holdings deep' (Constituents), or 'holdings <acct>' (Drill-down).
  * Market Depth Command: Type 'md <symbol>' (e.g. 'md NIFTY2682025000CE' or 'md INFY').
  * P&L Period Options: Type 'w' (Week), 'lw' (Last Week), 'm' (Month), 'y' (Year), 'c' (Custom) when P&L tab is active.

 2. ORDER PLACEMENT & TRADING
┌──────────────────────────────────┬──────────────────────────────────────────┐
│ Command Syntax                   │ Description / Example                    │
├──────────────────────────────────┼──────────────────────────────────────────┤
│ b <symbol> <qty> [price]         │ Buy limit or market                      │
│                                  │ e.g. b NIFTY26JUL22500PE 100 1.40        │
│ s <symbol> <qty> [price]         │ Sell limit or market                     │
│                                  │ e.g. s BANKNIFTY26JUL50000CE 15 2.50     │
│ b <pos_id> <qty> [price]         │ Buy additional qty for position ID       │
│                                  │ e.g. b 1 100                             │
│ s <pos_id> [price]               │ Sell / Exit position by table ID         │
│                                  │ e.g. s 2                                 │
└──────────────────────────────────┴──────────────────────────────────────────┘

 3. ORDERS, POSITION EXITS, MODIFY & CANCEL
┌──────────────────────────────────┬──────────────────────────────────────────┐
│ Command Syntax                   │ Description / Example                    │
├──────────────────────────────────┼──────────────────────────────────────────┤
│ orders                           │ List / view open pending orders          │
│                                  │ e.g. orders  (or ord)                    │
│ exit <symbol|id> [price]         │ Exit position by symbol or index ID      │
│                                  │ e.g. exit 1  /  exit NIFTY26JUL22200PE    │
│ exit all                         │ Square off ALL active positions (Market) │
│                                  │ e.g. exit all                            │
│ order <id|sym> <price>           │ Modify open order price (or modify / m)  │
│                                  │ e.g. order 12345 1.50  /  order 1 2.10    │
│ order <id|sym> <qty> <price>     │ Modify open order quantity & price       │
│                                  │ e.g. order 12345 50 1.50                 │
│ cancel <order_id|symbol|all>     │ Cancel order(s) by ID, symbol or all     │
│                                  │ e.g. cancel 12345  /  cancel all (or c)  │
└──────────────────────────────────┴──────────────────────────────────────────┘

 4. SCHEDULED ORDERS & CONDITIONAL CHAINS (at <time> [date] / THEN / ->)
┌──────────────────────────────────┬──────────────────────────────────────────┐
│ Command Syntax                   │ Description / Example                    │
├──────────────────────────────────┼──────────────────────────────────────────┤
│ <cmd> at <time> [date]           │ Place order at exact scheduled time/date │
│                                  │ e.g. b NIFTY... 50 120 at 09:20          │
│                                  │ e.g. exit all at 15:25                   │
│                                  │ e.g. b INFY 10 1450 on 2026-09-02 at 09:20│
│ <cmd1> at <time> THEN <cmd2>     │ Timed primary; upon fill, place child leg│
│                                  │ e.g. b NIFTY... 50 120 at 09:20 then s 150│
│ sched / scheduled                │ View active and today's scheduled orders │
│ cancel sched <id|all>            │ Cancel pending scheduled order(s)        │
│ cond / co                        │ View active and today's conditional orders│
│ cancel co <id|all>               │ Cancel pending conditional order(s)      │
└──────────────────────────────────┴──────────────────────────────────────────┘

 5. ACCOUNT & SELECTION CONTROL
┌──────────────────────────────────┬──────────────────────────────────────────┐
│ Command Syntax / Action          │ Description / Example                    │
├──────────────────────────────────┼──────────────────────────────────────────┤
│ account <name|all>               │ Switch / filter by account name or all   │
│                                  │ e.g. account Account1 (or acct / a)      │
│ Click Row                        │ Select / target specific position/account│
│ Esc                              │ Clear active selection / reset filter    │
└──────────────────────────────────┴──────────────────────────────────────────┘

 6. GLOBAL NAVIGATION & CONTROL
┌───────────────────────┬─────────────────────────────────────────────────────┐
│ Key / Action          │ Description / Function                              │
├───────────────────────┼─────────────────────────────────────────────────────┤
│ Tab                   │ Cycle focus: Input box ➔ Positions ➔ Right Pane      │
│ Ctrl + R              │ Interactive command history search (press repeatedly)│
│ Ctrl + Left / Right   │ Adjust left / right split pane width                │
│ Ctrl + C              │ Cancel current command line / clear selection       │
│ Ctrl + D              │ Quit KiteCLI                                        │
└───────────────────────┴─────────────────────────────────────────────────────┘
"""

    def _get_pnl_date_range(self, period: str) -> tuple[datetime, datetime, str]:
        """Compute the start date, end date, and human-readable label for a P&L period.
        
        Periods:
        - "week": From most recent Wednesday 00:00:00 to now.
        - "last_week": From previous week's Wednesday 00:00:00 to most recent Tuesday 23:59:59.
        - "month": From day after last month's last Tuesday (Wednesday) 00:00:00 to now.
        - "year": From current financial year April 1st 00:00:00 to now.
        - "custom": Custom start and end date range.
        """
        now = datetime.now()
        
        # NIFTY Options F&O Week: Wednesday 00:00:00 to Tuesday (most recent Wednesday)
        d = now.weekday()
        days_ago = d - 2 if d >= 2 else d + 5
        this_wed = (now - timedelta(days=days_ago)).replace(hour=0, minute=0, second=0, microsecond=0)

        if period == "week":
            start_dt = this_wed
            label = f"Week (Wed {start_dt.strftime('%b %d')} -> Now)"
            return start_dt, now, label
            
        elif period == "last_week":
            last_wed = this_wed - timedelta(days=7)
            last_tue = (this_wed - timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=999999)
            label = f"Last Week (Wed {last_wed.strftime('%b %d')} -> Tue {last_tue.strftime('%b %d')})"
            return last_wed, last_tue, label

        elif period == "month":
            first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            last_day_prev_month = first_of_this_month - timedelta(days=1)
            offset = (last_day_prev_month.weekday() - 1) % 7
            last_tue_prev_month = last_day_prev_month - timedelta(days=offset)
            start_dt = (last_tue_prev_month + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            label = f"Month (Wed {start_dt.strftime('%b %d')} -> Now)"
            return start_dt, now, label

        elif period == "custom":
            c_start = getattr(self, "pnl_custom_start", None)
            c_end = getattr(self, "pnl_custom_end", None)
            if not c_start:
                c_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
            if not c_end:
                c_end = now.strftime("%Y-%m-%d")

            try:
                start_dt = datetime.strptime(c_start, "%Y-%m-%d").replace(hour=0, minute=0, second=0, microsecond=0)
            except Exception:
                start_dt = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)

            try:
                end_dt = datetime.strptime(c_end, "%Y-%m-%d").replace(hour=23, minute=59, second=59, microsecond=999999)
            except Exception:
                end_dt = now

            label = f"Custom Range ({start_dt.strftime('%b %d, %Y')} -> {end_dt.strftime('%b %d, %Y')})"
            return start_dt, end_dt, label

        else:  # "year" (Financial Year starting April 1st)
            fy_year = now.year if now.month >= 4 else now.year - 1
            start_dt = datetime(fy_year, 4, 1, 0, 0, 0)
            label = f"FY Year (Apr 01, {fy_year} -> Now)"
            return start_dt, now, label

    def _render_pnl_pane(self) -> str:
        """Render the P&L Analytics tab with Grand Total and Account-wise breakdown."""
        period = getattr(self, "pnl_period", "week")
        start_dt, end_dt, period_label = self._get_pnl_date_range(period)
        now_dt = datetime.now()
        is_past_period = end_dt < now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat()

        # Build period selector buttons header line
        def _fmt_btn(p_key, p_name):
            if period == p_key:
                return f"\033[30;46;1m [{p_name}] \033[0m"  # Cyan background for active period
            return f"\033[36m [{p_name}] \033[0m"

        btn_week = _fmt_btn("week", "1: Week")
        btn_last_week = _fmt_btn("last_week", "2: Last Week")
        btn_month = _fmt_btn("month", "3: Month")
        btn_year = _fmt_btn("year", "4: Year")
        btn_custom = _fmt_btn("custom", "5: Custom Range")

        header_selector = f"Period:  {btn_week}  │  {btn_last_week}  │  {btn_month}  │  {btn_year}  │  {btn_custom}"
        if period == "custom":
            header_selector += "  │  \033[90m(Type 'pnl YYYY-MM-DD YYYY-MM-DD' to set range)\033[0m"

        # Collect completed orders strictly from SQLite database (~/.kcli/data.db)
        executed_orders_map = {}

        try:
            import sqlite3
            db_path = getattr(self, "recorder", None).db_path if getattr(self, "recorder", None) else None
            if not db_path:
                from pathlib import Path
                db_path = Path.home() / ".kcli" / "data.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT timestamp, account_name, user_id, tradingsymbol, transaction_type, quantity, price, order_id, status, COALESCE(is_settled_externally, 0), COALESCE(total_charges, 0.0)
                    FROM executed_order_context
                    WHERE (UPPER(status) = 'COMPLETE' OR (quantity > 0 AND UPPER(status) NOT IN ('REJECTED', 'OPEN')))
                    ORDER BY id ASC;
                    """
                )
                for row in cursor.fetchall():
                    raw_oid = row[7]
                    oid = str(raw_oid) if raw_oid and str(raw_oid).strip() != "" else f"DB_ROW_{row[0]}_{row[3]}"
                    tt = str(row[4]).upper()
                    if tt == "B": tt = "BUY"
                    if tt == "S": tt = "SELL"
                    executed_orders_map[oid] = {
                        "timestamp": row[0],
                        "account_name": row[1] or row[2] or "Default",
                        "user_id": row[2] or "Default",
                        "tradingsymbol": row[3],
                        "transaction_type": tt,
                        "quantity": int(row[5]),
                        "price": float(row[6]),
                        "order_id": oid,
                        "status": str(row[8]),
                        "is_settled_externally": int(row[9] or 0),
                        "total_charges": float(row[10] or 0.0),
                    }
                conn.close()
        except Exception as exc:
            logger.warning("Failed to query executed_order_context for P&L: %s", exc)

        executed_orders = list(executed_orders_map.values())

        # Active positions
        open_positions = getattr(self, "active_positions", []) if not is_past_period else []

        # Group executed orders & active positions by account
        acct_orders_map = defaultdict(list)
        for o in executed_orders:
            acct_orders_map[o["account_name"]].append(o)

        acct_pos_map = defaultdict(list)
        for p in open_positions:
            acct_name = p.get("account_name") or p.get("user_id") or "Default"
            acct_pos_map[acct_name].append(p)

        all_acct_names = sorted(list(set(list(acct_orders_map.keys()) + list(acct_pos_map.keys()))))
        if not all_acct_names:
            all_acct_names = ["Default"]

        from datetime import time
        from cli.greeks import parse_nfo_symbol

        grand_realised = 0.0
        grand_unrealised = 0.0
        grand_closed_cnt = 0
        grand_win_cnt = 0

        acct_summaries = {}

        def _to_iso_str(ts_val):
            if not ts_val:
                return ""
            if hasattr(ts_val, "isoformat"):
                return ts_val.isoformat()
            s = str(ts_val).strip()
            if not s:
                return ""
            formats = [
                "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S",
                "%d-%b-%Y %H:%M:%S",
                "%d-%b-%Y %H:%M",
                "%Y-%m-%d",
            ]
            for fmt in formats:
                try:
                    dt = datetime.strptime(s, fmt)
                    return dt.isoformat()
                except ValueError:
                    pass
            return s

        for acct in all_acct_names:
            orders = acct_orders_map[acct]
            positions = acct_pos_map[acct]

            trades_by_symbol = defaultdict(list)
            for o in orders:
                trades_by_symbol[o["tradingsymbol"]].append(o)

            from cli.charges import calculate_option_charges

            realised_pnl = 0.0
            closed_cnt = 0
            win_cnt = 0
            trade_rows = []

            # Calculate total charges strictly for trades EXECUTED within the selected period
            total_acct_charges = sum(
                float(o.get("total_charges") or 0.0)
                for o in orders
                if int(o.get("is_settled_externally", 0)) == 0
                and start_iso <= _to_iso_str(o.get("timestamp")) <= end_iso
            )

            for symbol, s_orders in trades_by_symbol.items():
                # FIFO trade matching for completed legs across all available history
                buys = [{"qty": int(o["quantity"]), "orig_qty": int(o["quantity"]), "price": float(o["price"]), "ts": _to_iso_str(o.get("timestamp")), "chg": float(o.get("total_charges") or 0.0)} for o in s_orders if str(o.get("transaction_type", "")).upper() in ("B", "BUY") and int(o.get("is_settled_externally", 0)) == 0]
                sells = [{"qty": int(o["quantity"]), "orig_qty": int(o["quantity"]), "price": float(o["price"]), "ts": _to_iso_str(o.get("timestamp")), "chg": float(o.get("total_charges") or 0.0)} for o in s_orders if str(o.get("transaction_type", "")).upper() in ("S", "SELL") and int(o.get("is_settled_externally", 0)) == 0]

                m_pnl = 0.0
                matched_qty = 0
                b_val_matched = 0.0
                s_val_matched = 0.0
                m_chg = 0.0
                b_idx, s_idx = 0, 0

                while b_idx < len(buys) and s_idx < len(sells):
                    b = buys[b_idx]
                    s = sells[s_idx]
                    match_q = min(b["qty"], s["qty"])
                    if match_q > 0:
                        close_ts = max(b["ts"], s["ts"])
                        b_ratio = match_q / b["orig_qty"] if b["orig_qty"] > 0 else 1.0
                        s_ratio = match_q / s["orig_qty"] if s["orig_qty"] > 0 else 1.0

                        b_chg = b_ratio * b["chg"] if start_iso <= b["ts"] <= end_iso else 0.0
                        s_chg = s_ratio * s["chg"] if start_iso <= s["ts"] <= end_iso else 0.0
                        leg_chg = b_chg + s_chg

                        if start_iso <= close_ts <= end_iso:
                            m_pnl += (s["price"] - b["price"]) * match_q
                            matched_qty += match_q
                            b_val_matched += match_q * b["price"]
                            s_val_matched += match_q * s["price"]
                            m_chg += leg_chg

                        b["qty"] -= match_q
                        s["qty"] -= match_q
                    if b["qty"] == 0:
                        b_idx += 1
                    if s["qty"] == 0:
                        s_idx += 1

                parsed = parse_nfo_symbol(symbol)
                exp_date = parsed.get("expiry_date") if parsed else None

                is_expired = False
                if exp_date:
                    if now_dt.date() > exp_date:
                        is_expired = True
                    elif now_dt.date() == exp_date and now_dt.time() >= time(15, 30):
                        is_expired = True

                rem_buy_qty = sum(b["qty"] for b in buys[b_idx:])
                rem_sell_qty = sum(s["qty"] for s in sells[s_idx:])
                net_qty = rem_buy_qty - rem_sell_qty

                if matched_qty > 0:
                    realised_pnl += m_pnl
                    closed_cnt += 1
                    if m_pnl >= 0:
                        win_cnt += 1
                    avg_b = b_val_matched / matched_qty if matched_qty > 0 else 0.0
                    avg_s = s_val_matched / matched_qty if matched_qty > 0 else 0.0
                    c_val = round(m_chg, 2)
                    trade_rows.append({"symbol": symbol, "qty": matched_qty, "buy_price": avg_b, "sell_price": avg_s, "pnl": m_pnl, "charges": c_val, "net_pnl": m_pnl - c_val})

                if is_expired and net_qty != 0:
                    if net_qty < 0:  # Short option expired worthless
                        exp_qty = abs(net_qty)
                        rem_s_val = sum(s["qty"] * s["price"] for s in sells[s_idx:])
                        exp_pnl = rem_s_val if rem_sell_qty > 0 else 0.0
                        realised_pnl += exp_pnl
                        closed_cnt += 1
                        win_cnt += 1
                        avg_s = rem_s_val / exp_qty if exp_qty > 0 else 0.0
                        chg_info = calculate_option_charges(0.0, avg_s, exp_qty, 0, 1)
                        c_val = chg_info["total_charges"]
                        total_acct_charges += c_val
                        trade_rows.append({"symbol": symbol, "qty": exp_qty, "buy_price": 0.0, "sell_price": avg_s, "pnl": exp_pnl, "charges": c_val, "net_pnl": exp_pnl - c_val})
                    elif net_qty > 0:  # Long option expired worthless
                        exp_qty = net_qty
                        rem_b_val = sum(b["qty"] * b["price"] for b in buys[b_idx:])
                        exp_pnl = -rem_b_val if rem_buy_qty > 0 else 0.0
                        realised_pnl += exp_pnl
                        closed_cnt += 1
                        avg_b = rem_b_val / exp_qty if exp_qty > 0 else 0.0
                        chg_info = calculate_option_charges(avg_b, 0.0, exp_qty, 1, 0)
                        c_val = chg_info["total_charges"]
                        total_acct_charges += c_val
                        trade_rows.append({"symbol": symbol, "qty": exp_qty, "buy_price": avg_b, "sell_price": 0.0, "pnl": exp_pnl, "charges": c_val, "net_pnl": exp_pnl - c_val})

            # Fallback for closed positions today (q == 0) in positions if not already in trade_rows
            for p in positions:
                q = p.get("quantity", 0)
                sym = p.get("tradingsymbol", "")
                if q == 0 and sym and not any(r["symbol"] == sym for r in trade_rows):
                    p_pnl = float(p.get("realised") or p.get("pnl") or 0.0)
                    if p_pnl != 0.0:
                        realised_pnl += p_pnl
                        closed_cnt += 1
                        if p_pnl >= 0:
                            win_cnt += 1
                        bp = float(p.get("buy_price") or p.get("average_price") or 0.0)
                        sp = float(p.get("sell_price") or p.get("average_price") or 0.0)
                        matched_q = abs(p.get("buy_quantity") or p.get("sell_quantity") or 0)
                        chg_info = calculate_option_charges(bp, sp, matched_q, 1, 1)
                        c_val = chg_info["total_charges"]
                        total_acct_charges += c_val
                        trade_rows.append({"symbol": sym, "qty": matched_q, "buy_price": bp, "sell_price": sp, "pnl": p_pnl, "charges": c_val, "net_pnl": p_pnl - c_val})

            unrealised_pnl = sum(float(p.get("pnl", p.get("unrealised", 0.0))) for p in positions if p.get("quantity", 0) != 0)
            net_pnl = (realised_pnl - total_acct_charges) + unrealised_pnl

            grand_realised += realised_pnl
            grand_unrealised += unrealised_pnl
            grand_closed_cnt += closed_cnt
            grand_win_cnt += win_cnt
            if not hasattr(self, "_grand_charges"): self._grand_charges = 0.0
            self._grand_charges += total_acct_charges

            acct_summaries[acct] = {
                "realised": realised_pnl,
                "charges": total_acct_charges,
                "net_realised": realised_pnl - total_acct_charges,
                "unrealised": unrealised_pnl,
                "net": net_pnl,
                "closed_cnt": closed_cnt,
                "win_cnt": win_cnt,
                "trade_rows": trade_rows,
            }

        def _fmt_money(val: float) -> str:
            color = "\033[38;5;48m" if val >= 0 else "\033[38;5;196m"
            if val >= 0:
                return f"{color}+₹{val:,.2f}\033[0m"
            else:
                return f"{color}-₹{abs(val):,.2f}\033[0m"

        def _fmt_charges(val: float) -> str:
            return f"\033[38;5;208m-₹{val:,.2f}\033[0m"

        lines = [header_selector, ""]

        # Account Summary Table
        w_acct = 12
        w_real = 16
        w_chg = 14
        w_net_r = 16
        w_unreal = 16
        w_net = 16

        top_b = f"┌─{'─'*w_acct}─┬─{'─'*w_real}─┬─{'─'*w_chg}─┬─{'─'*w_net_r}─┬─{'─'*w_unreal}─┬─{'─'*w_net}─┐"
        head_r = f"│ {'Account':<{w_acct}} │ {'Realised P&L':<{w_real}} │ {'Charges':<{w_chg}} │ {'Net Realised':<{w_net_r}} │ {'Unrealised P&L':<{w_unreal}} │ {'Net P&L':<{w_net}} │"
        mid_b = f"├─{'─'*w_acct}─┼─{'─'*w_real}─┼─{'─'*w_chg}─┼─{'─'*w_net_r}─┼─{'─'*w_unreal}─┼─{'─'*w_net}─┤"
        bot_b = f"└─{'─'*w_acct}─┴─{'─'*w_real}─┴─{'─'*w_chg}─┴─{'─'*w_net_r}─┴─{'─'*w_unreal}─┴─{'─'*w_net}─┘"

        lines.append(top_b)
        lines.append(head_r)
        lines.append(mid_b)

        all_trade_rows = []

        grand_total_charges = 0.0

        for acct in all_acct_names:
            d = acct_summaries[acct]
            grand_total_charges += d["charges"]
            acct_s = _pad_ansi(acct, w_acct)
            r_s = _pad_ansi(_fmt_money(d["realised"]), w_real, ">")
            chg_s = _pad_ansi(_fmt_charges(d["charges"]), w_chg, ">")
            nr_s = _pad_ansi(_fmt_money(d["net_realised"]), w_net_r, ">")
            u_s = _pad_ansi(_fmt_money(d["unrealised"]), w_unreal, ">")
            n_s = _pad_ansi(_fmt_money(d["net"]), w_net, ">")
            lines.append(f"│ {acct_s} │ {r_s} │ {chg_s} │ {nr_s} │ {u_s} │ {n_s} │")

            for r in d["trade_rows"]:
                all_trade_rows.append({
                    "account": acct,
                    "symbol": r["symbol"],
                    "qty": r["qty"],
                    "buy_price": r.get("buy_price", 0.0),
                    "sell_price": r.get("sell_price", 0.0),
                    "pnl": r["pnl"],
                    "charges": r.get("charges", 0.0),
                    "net_pnl": r.get("net_pnl", r["pnl"]),
                })

        lines.append(mid_b)

        # Grand Total Row
        grand_net_realised = grand_realised - grand_total_charges
        grand_net = grand_net_realised + grand_unrealised

        gt_acct_s = _pad_ansi("\033[1mGRAND TOTAL\033[0m", w_acct)
        gt_r_s = _pad_ansi(_fmt_money(grand_realised), w_real, ">")
        gt_chg_s = _pad_ansi(_fmt_charges(grand_total_charges), w_chg, ">")
        gt_nr_s = _pad_ansi(_fmt_money(grand_net_realised), w_net_r, ">")
        gt_u_s = _pad_ansi(_fmt_money(grand_unrealised), w_unreal, ">")
        gt_n_s = _pad_ansi(_fmt_money(grand_net), w_net, ">")

        lines.append(f"│ {gt_acct_s} │ {gt_r_s} │ {gt_chg_s} │ {gt_nr_s} │ {gt_u_s} │ {gt_n_s} │")
        lines.append(bot_b)
        lines.append("")

        # Single Trade Breakdown Table
        if all_trade_rows:
            lines.append("Trade Breakdown:")
            w_acct_tb = max(10, max(len(r["account"]) for r in all_trade_rows))
            w_sym_tb = max(18, max(len(r["symbol"]) for r in all_trade_rows))
            w_qty_tb = 8
            w_bp_tb = 11
            w_sp_tb = 11
            w_pnl_tb = 14
            w_chg_tb = 12
            w_net_tb = 14

            tb_top = f"┌─{'─'*w_acct_tb}─┬─{'─'*w_sym_tb}─┬─{'─'*w_qty_tb}─┬─{'─'*w_bp_tb}─┬─{'─'*w_sp_tb}─┬─{'─'*w_pnl_tb}─┬─{'─'*w_chg_tb}─┬─{'─'*w_net_tb}─┐"
            tb_head = f"│ {'Account':<{w_acct_tb}} │ {'Symbol':<{w_sym_tb}} │ {'Qty':<{w_qty_tb}} │ {'Buy Price':<{w_bp_tb}} │ {'Sell Price':<{w_sp_tb}} │ {'Realised P&L':<{w_pnl_tb}} │ {'Charges':<{w_chg_tb}} │ {'Net P&L':<{w_net_tb}} │"
            tb_mid = f"├─{'─'*w_acct_tb}─┼─{'─'*w_sym_tb}─┼─{'─'*w_qty_tb}─┼─{'─'*w_bp_tb}─┼─{'─'*w_sp_tb}─┼─{'─'*w_pnl_tb}─┼─{'─'*w_chg_tb}─┼─{'─'*w_net_tb}─┤"
            tb_bot = f"└─{'─'*w_acct_tb}─┴─{'─'*w_sym_tb}─┴─{'─'*w_qty_tb}─┴─{'─'*w_bp_tb}─┴─{'─'*w_sp_tb}─┴─{'─'*w_pnl_tb}─┴─{'─'*w_chg_tb}─┴─{'─'*w_net_tb}─┘"

            lines.append(tb_top)
            lines.append(tb_head)
            lines.append(tb_mid)
            for r in all_trade_rows:
                acct_s = f"{r['account']:<{w_acct_tb}}"
                sym_s = f"{r['symbol']:<{w_sym_tb}}"
                qty_s = f"{r['qty']:<{w_qty_tb}}"
                bp_s = _pad_ansi(f"₹{r['buy_price']:.2f}", w_bp_tb, ">")
                sp_s = _pad_ansi(f"₹{r['sell_price']:.2f}", w_sp_tb, ">")
                pnl_s = _pad_ansi(_fmt_money(r["pnl"]), w_pnl_tb, ">")
                chg_s = _pad_ansi(_fmt_charges(r["charges"]), w_chg_tb, ">")
                net_s = _pad_ansi(_fmt_money(r["net_pnl"]), w_net_tb, ">")
                lines.append(f"│ {acct_s} │ {sym_s} │ {qty_s} │ {bp_s} │ {sp_s} │ {pnl_s} │ {chg_s} │ {net_s} │")
            lines.append(tb_bot)
            lines.append("")

        return "\n".join(lines)

    def _render_market_depth_pane(self) -> str:
        """Render 5-depth order book, OHLC, volume, and statistics for self.md_symbol."""
        sym = getattr(self, "md_symbol", "").strip().upper()
        if not sym:
            return """┌─────────────────────────────────────────────────────────────────────────────┐
│                            MARKET DEPTH (5-LEVEL)                           │
└─────────────────────────────────────────────────────────────────────────────┘

 No ticker symbol selected.

 To view market depth for any ticker, type:
   md <symbol>        (e.g. 'md NIFTY2682025000CE' or 'md INFY' or 'md RELIANCE')
   depth <symbol>

 Or enter any symbol directly in the prompt below while MD tab is active.
"""

        res = self.client.get_market_depth(sym)
        if res.get("status") != "success":
            err_msg = res.get("message", "Unknown error")
            return f"""┌─────────────────────────────────────────────────────────────────────────────┐
│                     MARKET DEPTH: {sym:<41} │
└─────────────────────────────────────────────────────────────────────────────┘

 ❌ Failed to fetch market depth: {err_msg}

 Enter a valid symbol with:
   md <symbol> (e.g. 'md NIFTY2682025000CE' or 'md INFY')
"""

        ltp = float(res.get("last_price", 0.0))
        chg = float(res.get("change", 0.0))
        chg_pct = float(res.get("change_pct", 0.0))
        vol = int(res.get("volume", 0))
        oi = int(res.get("oi", 0))
        tot_b = int(res.get("total_buy_quantity", 0))
        tot_s = int(res.get("total_sell_quantity", 0))
        ohlc = res.get("ohlc", {})
        open_pr = float(ohlc.get("open", 0.0))
        high_pr = float(ohlc.get("high", 0.0))
        low_pr = float(ohlc.get("low", 0.0))
        close_pr = float(ohlc.get("close", 0.0))
        lc = float(res.get("lower_circuit_limit", 0.0))
        uc = float(res.get("upper_circuit_limit", 0.0))

        c_green = "\033[38;5;48m"
        c_red = "\033[38;5;196m"
        c_cyan = "\033[38;5;51m"
        c_bold = "\033[1m"
        c_reset = "\033[0m"

        ltp_color = c_green if chg >= 0 else c_red
        chg_str = f"{chg:+.2f} ({chg_pct:+.2f}%)"

        lines = []
        lines.append(f"┌─ {c_bold}{sym}{c_reset} {'─' * max(2, 60 - len(sym))}┐")
        lines.append(
            f"│ LTP: {ltp_color}{c_bold}₹{ltp:<9.2f}{c_reset} {ltp_color}{chg_str:<16}{c_reset} "
            f"Vol: {c_cyan}{vol:<10,}{c_reset} OI: {c_cyan}{oi:<9,}{c_reset} │"
        )
        lines.append("├───────────────────────────────┬───────────────────────────────┤")
        lines.append(f"│ {c_green}{c_bold}{'BUY (BIDS)':^29}{c_reset} │ {c_red}{c_bold}{'SELL (ASKS)':^29}{c_reset} │")
        lines.append("├───────┬──────────┬────────────┼────────────┬──────────┬───────┤")
        lines.append("│ Orders│ Quantity │  Bid Price │  Ask Price │ Quantity │ Orders│")
        lines.append("├───────┼──────────┼────────────┼────────────┼──────────┼───────┤")

        bids = res.get("buy_depth", [])
        asks = res.get("sell_depth", [])

        for i in range(5):
            b = bids[i] if i < len(bids) else {}
            a = asks[i] if i < len(asks) else {}

            b_ord = f"{b.get('orders', 0):>5}" if b.get('orders') else "    -"
            b_qty = f"{b.get('quantity', 0):>8,}" if b.get('quantity') else "       -"
            b_pr = f"₹{b.get('price', 0.0):>9.2f}" if b.get('price') else "         -"

            a_pr = f"₹{a.get('price', 0.0):>9.2f}" if a.get('price') else "         -"
            a_qty = f"{a.get('quantity', 0):>8,}" if a.get('quantity') else "       -"
            a_ord = f"{a.get('orders', 0):>5}" if a.get('orders') else "    -"

            lines.append(
                f"│ {b_ord} │ {b_qty} │ {c_green}{b_pr}{c_reset} │ "
                f"{c_red}{a_pr}{c_reset} │ {a_qty} │ {a_ord} │"
            )

        lines.append("├───────┴──────────┴────────────┼────────────┴──────────┴───────┤")
        lines.append(
            f"│ Total Buy: {c_green}{c_bold}{tot_b:>17,}{c_reset} │ "
            f"Total Sell: {c_red}{c_bold}{tot_s:>16,}{c_reset} │"
        )

        tot_both = tot_b + tot_s
        if tot_both > 0:
            b_pct = int(round((tot_b / tot_both) * 100))
            s_pct = 100 - b_pct
            b_bars = int(round((b_pct / 100) * 28))
            s_bars = 28 - b_bars
            bar_visual = f"{c_green}{'█' * b_bars}{c_reset}{c_red}{'█' * s_bars}{c_reset}"
            lines.append(f"│ Buy {b_pct:>2}% [{bar_visual}] {s_pct:>2}% Sell │")

        lines.append("├───────────────────────────────┴───────────────────────────────┤")
        lines.append("│ STATISTICS                                                    │")
        lines.append(
            f"│ Open: ₹{open_pr:<8.2f} High: ₹{high_pr:<8.2f} Low: ₹{low_pr:<8.2f} Close: ₹{close_pr:<8.2f}│"
        )
        if lc > 0 or uc > 0:
            lines.append(
                f"│ Lower Circuit: ₹{lc:<10.2f} Upper Circuit: ₹{uc:<16.2f}│"
            )
        lines.append("└───────────────────────────────────────────────────────────────┘")

        return "\n".join(lines)

    def _render_holdings_pane(self) -> str:
        """Render Holdings / Portfolio pane text (Summary or Deep View)."""
        if getattr(self, "last_holdings_response", None) is None:
            try:
                self.last_holdings_response = self.client.get_holdings()
            except Exception as e:
                return f"\x1b[1;31mError fetching holdings:\x1b[0m {e}"

        holdings_resp = self.last_holdings_response or {}
        deep = (getattr(self, "holdings_view_mode", "summary") == "deep")
        target_acct = getattr(self, "holdings_target_account", None)

        width = 100
        if hasattr(self, "_info_control") and hasattr(self, "app") and self.app and self.app.output:
            width = max(80, int(self.app.output.get_size().columns * (100 - self.left_width_pct) / 100) - 4)

        # Header sub-navigation bar
        def _fmt_btn(is_active, label):
            if is_active:
                return f"\033[30;46;1m [{label}] \033[0m"
            return f"\033[36m [{label}] \033[0m"

        btn_sum = _fmt_btn(not deep and not target_acct, "1: Summary")
        btn_deep = _fmt_btn(deep and not target_acct, "2: Deep View")
        
        target_info = f"  │  \033[36mAccount: {target_acct}\033[0m \033[90m(type 'holdings all' to reset)\033[0m" if target_acct else ""

        nav_line = f"View:  {btn_sum}  │  {btn_deep}{target_info}\n"

        body = render_holdings_to_string(
            holdings_resp,
            width=width,
            deep=deep,
            target_account=target_acct,
        )
        return nav_line + "\n" + body

    def _update_info_buffer(self, reset_scroll: bool = True) -> None:
        """Push current info_mode text into the info control.

        Args:
            reset_scroll: When True (default) the pane scrolls back to the top.
                          Pass False for live tick/data refreshes where the user
                          may have scrolled down — preserves their position.
        """
        if not hasattr(self, "_info_control"):
            return
        if self.info_mode == "orders_pending":
            resp = getattr(self, "last_orders_response", None)
            if resp:
                self._last_pending_text = self._render_orders_pane(resp, "orders_pending")
            text = self._last_pending_text
        elif self.info_mode == "orders_executed":
            resp = getattr(self, "last_orders_response", None)
            if resp:
                self._last_executed_text = self._render_orders_pane(resp, "orders_executed")
            text = self._last_executed_text
        elif self.info_mode == "advisor":
            try:
                self._last_advisor_text = self._render_advisor_text()
            except Exception as exc:
                self._last_advisor_text = f"=== Tuesday Expiry Advisor ===\n\nFailed to plan: {exc}"
            text = self._last_advisor_text
        elif self.info_mode == "pnl":
            try:
                self._last_pnl_text = self._render_pnl_pane()
            except Exception as exc:
                self._last_pnl_text = f"=== P&L Analytics ===\n\nFailed to calculate P&L: {exc}"
            text = self._last_pnl_text
        elif self.info_mode == "md":
            try:
                self._last_md_text = self._render_market_depth_pane()
            except Exception as exc:
                self._last_md_text = f"=== Market Depth ===\n\nFailed to calculate depth: {exc}"
            text = self._last_md_text
        elif self.info_mode == "holdings":
            try:
                self._last_holdings_text = self._render_holdings_pane()
            except Exception as exc:
                self._last_holdings_text = f"=== Holdings / Portfolio ===\n\nFailed to fetch holdings: {exc}"
            text = self._last_holdings_text
        elif self.info_mode == "oc_dual":
            text = self._last_oc_dual_text
        elif self.info_mode == "help":
            text = self._build_help_text()
        else:
            text = self._last_oc_text

        if reset_scroll:
            self._info_control.vertical_scroll_position = 0

        if self.info_mode == "oc":
            self._info_control.text = self._build_oc_formatted_fragments()
        elif self.info_mode == "oc_dual":
            self._info_control.text = self._build_oc_dual_formatted_fragments()
        else:
            self._info_control.text = to_formatted_text(ANSI(text))
        if hasattr(self, "app") and self.app:
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
        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        # Keep noisy third-party WebSocket loggers off the full-screen TUI.
        _silence_websocket_loggers()
        # ── UI Controls ─────────────────────────────────────────────
        self.left_header_control = FormattedTextControl(
            text=self._get_left_header_text,
            style="class:header",
        )
        self.right_header_control = FormattedTextControl(
            text=self._get_right_header_text,
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

        # Info Pane (right half) — ScrollableFormattedTextControl: scroll, ANSI formatting
        self._info_control = ScrollableFormattedTextControl(
            text=to_formatted_text(ANSI(self._last_pending_text)),
            focusable=True,
            show_cursor=False,
        )

        # Command input
        # Pass FileHistory to support persistent command line history
        from pathlib import Path
        history_dir = Path.home() / ".kcli"
        history_dir.mkdir(parents=True, exist_ok=True)
        history_file = history_dir / "history.txt"

        self.search_field = SearchToolbar(
            text_if_not_searching=[("class:not-searching", "Press Ctrl+R to search history.")],
            forward_search_prompt="Search: ",
        )

        self.input_field = TextArea(
            multiline=False,
            prompt="",
            style="class:input_text",
            accept_handler=self.handle_input,
            history=FileHistory(str(history_file)),
            focus_on_click=True,
            search_field=self.search_field,
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
            res = original_info_mouse_handler(mouse_event) if original_info_mouse_handler else None
            from prompt_toolkit.mouse_events import MouseEventType
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                if self.info_mode == "pnl":
                    v_scroll = getattr(self._info_control, "vertical_scroll_position", 0)
                    if mouse_event.position.y == 0 or mouse_event.position.y - v_scroll == 0:
                        x = mouse_event.position.x
                        if 8 <= x <= 22:
                            self.pnl_period = "week"
                            self._update_info_buffer()
                            if hasattr(self, "app") and self.app:
                                self.app.invalidate()
                            self.log_message("P&L period switched to [bold]Week[/bold].")
                            return res
                        elif 23 <= x <= 43:
                            self.pnl_period = "last_week"
                            self._update_info_buffer()
                            if hasattr(self, "app") and self.app:
                                self.app.invalidate()
                            self.log_message("P&L period switched to [bold]Last Week[/bold].")
                            return res
                        elif 44 <= x <= 60:
                            self.pnl_period = "month"
                            self._update_info_buffer()
                            if hasattr(self, "app") and self.app:
                                self.app.invalidate()
                            self.log_message("P&L period switched to [bold]Month[/bold].")
                            return res
                        elif 61 <= x <= 76:
                            self.pnl_period = "year"
                            self._update_info_buffer()
                            if hasattr(self, "app") and self.app:
                                self.app.invalidate()
                            self.log_message("P&L period switched to [bold]Year[/bold].")
                            return res
                        elif 77 <= x <= 110:
                            self.pnl_period = "custom"
                            self._update_info_buffer()
                            if hasattr(self, "app") and self.app:
                                self.app.invalidate()
                            self.log_message("P&L period switched to [bold]Custom Range[/bold].")
                            return res
                elif self.info_mode == "holdings":
                    v_scroll = getattr(self._info_control, "vertical_scroll_position", 0)
                    if mouse_event.position.y == 0 or mouse_event.position.y - v_scroll == 0:
                        x = mouse_event.position.x
                        if 6 <= x <= 22:
                            self.holdings_view_mode = "summary"
                            self.holdings_target_account = None
                            self._update_info_buffer()
                            if hasattr(self, "app") and self.app:
                                self.app.invalidate()
                            self.log_message("Holdings view switched to [bold]Summary[/bold].")
                            return res
                        elif 23 <= x <= 45:
                            self.holdings_view_mode = "deep"
                            self._update_info_buffer()
                            if hasattr(self, "app") and self.app:
                                self.app.invalidate()
                            self.log_message("Holdings view switched to [bold]Deep View[/bold].")
                            return res

                if hasattr(self._info_control, "buffer"):
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
            get_vertical_scroll=lambda win: getattr(self._info_control, "vertical_scroll_position", 0),
        )

        # Define tab bar control and window for the info pane
        self.tab_bar_control = FormattedTextControl(
            text=self._get_tab_bar_text,
            focusable=False,
        )
        self.tab_bar_window = Window(
            content=self.tab_bar_control,
            height=1,
            wrap_lines=False,
            style="class:tab_bar",
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
                    elif hasattr(control, "vertical_scroll_position"):
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

        @kb.add("c-r", filter=is_searching)
        def _reverse_search_next(event):
            do_incremental_search(SearchDirection.BACKWARD, count=event.arg)

        @kb.add("c-r", filter=~is_searching)
        def _start_reverse_search(event):
            start_search(self.input_field.control, SearchDirection.BACKWARD)

        @kb.add("c-d")
        def _quit(event):
            self.running = False
            event.app.exit()

        @kb.add("c-c")
        def _cancel_command(event):
            buf = self.input_field.buffer
            if buf.text:
                buf.text = ""
                buf.cursor_position = 0
                self.log_message("Command cancelled.")
            else:
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
            if hasattr(cur, "vertical_scroll_position"):
                cur.vertical_scroll_position = max(0, cur.vertical_scroll_position - 1)
            elif hasattr(cur, "buffer"):
                cur.buffer.cursor_up()

        @kb.add("down", filter=~has_focus(self.input_field))
        def _scroll_down_kb(event):
            cur = event.app.layout.current_control
            if hasattr(cur, "vertical_scroll_position"):
                cur.vertical_scroll_position += 1
            elif hasattr(cur, "buffer"):
                cur.buffer.cursor_down()

        @kb.add("pageup", filter=~has_focus(self.input_field))
        def _page_up_kb(event):
            cur = event.app.layout.current_control
            if hasattr(cur, "vertical_scroll_position"):
                cur.vertical_scroll_position = max(0, cur.vertical_scroll_position - 10)
            elif hasattr(cur, "buffer"):
                for _ in range(10):
                    cur.buffer.cursor_up()

        @kb.add("pagedown", filter=~has_focus(self.input_field))
        def _page_down_kb(event):
            cur = event.app.layout.current_control
            if hasattr(cur, "vertical_scroll_position"):
                cur.vertical_scroll_position += 10
            elif hasattr(cur, "buffer"):
                for _ in range(10):
                    cur.buffer.cursor_down()

        @kb.add("tab")
        def _tab(event):
            """Cycle focus: input -> positions -> info pane -> input."""
            cur = event.app.layout.current_control
            cycle = [
                self.input_field.control,
                self.positions_control,
                self._info_control,
            ]
            try:
                idx = cycle.index(cur)
                nxt = cycle[(idx + 1) % len(cycle)]
            except ValueError:
                nxt = self.input_field.control
            event.app.layout.focus(nxt)

        # Ctrl+1/2/3/4, Ctrl+P/E/O/G, Alt/Esc+1/2/3/4, F1-F4 — switch info pane content
        @kb.add("c-1")
        @kb.add("c-p")
        @kb.add("escape", "1")
        @kb.add("f1")
        def _c1(event):
            self.info_mode = "orders_pending"
            self._update_info_buffer()
            event.app.invalidate()

        @kb.add("c-2")
        @kb.add("escape", "2")
        @kb.add("f2")
        def _c2(event):
            self.info_mode = "orders_executed"
            self._update_info_buffer()
            event.app.invalidate()

        @kb.add("c-3")
        @kb.add("c-o")
        @kb.add("escape", "3")
        @kb.add("f3")
        def _c3(event):
            self.info_mode = "oc"
            self._update_info_buffer()
            event.app.invalidate()

        @kb.add("c-4")
        @kb.add("c-g")
        @kb.add("escape", "4")
        @kb.add("f4")
        def _c4(event):
            self.info_mode = "advisor"
            self._update_info_buffer()
            event.app.invalidate()

        @kb.add("c-5")
        @kb.add("escape", "5")
        @kb.add("f5")
        def _c5(event):
            self.info_mode = "pnl"
            self._update_info_buffer()
            event.app.invalidate()

        @kb.add("c-6")
        @kb.add("escape", "6")
        @kb.add("f6")
        def _c6(event):
            self.info_mode = "md"
            self._update_info_buffer()
            event.app.invalidate()

        @kb.add("c-7")
        @kb.add("escape", "7")
        @kb.add("f7")
        def _c7(event):
            self.info_mode = "holdings"
            self._update_info_buffer()
            event.app.invalidate()

        @kb.add("c-8")
        @kb.add("escape", "8")
        @kb.add("f8")
        def _c8(event):
            self.info_mode = "help"
            self._update_info_buffer()
            event.app.invalidate()

        # Ctrl+Left/Right — adjust left/right pane split
        @kb.add("c-left")
        def _narrow_left(event):
            self.left_width_pct = max(20, self.left_width_pct - 5)
            event.app.invalidate()

        @kb.add("c-right")
        def _widen_left(event):
            self.left_width_pct = min(80, self.left_width_pct + 5)
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

        # Divider mouse handlers
        def v_divider_mouse_handler(mouse_event):
            from prompt_toolkit.mouse_events import MouseEventType
            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                self.dragging_vertical = True
                self.vertical_divider.style = "class:divider.dragging"
                self.app.invalidate()

        self.vertical_divider.content.mouse_handler = v_divider_mouse_handler

        # Monkey-patch MouseHandlers.__init__ to wrap with DragInterceptDict
        original_init = MouseHandlers.__init__
        def new_init(lh_self, *args, **kwargs):
            original_init(lh_self, *args, **kwargs)
            lh_self.mouse_handlers = DragInterceptDict(self, lh_self.mouse_handlers)
        MouseHandlers.__init__ = new_init

        # ── Dynamic Layout ─────────────────────────────────────────
        # DynamicContainer rebuilds the body on each render using current
        # self.left_width_pct value.
        def _build_body():
            self._update_positions_display()
            lw = self.left_width_pct           # left weight (20-80)
            rw = 100 - lw                      # right weight

            return VSplit([
                # ── LEFT half: Positions (scrollable) ──
                Frame(
                    title="Positions",
                    body=self.positions_window,
                    style=self._get_frame_style(self.positions_window),
                    width=D(weight=lw),
                ),
                self.vertical_divider,
                # ── RIGHT half: Info Pane with clickable tabs (scrollable) ──
                Frame(
                    title="",
                    body=HSplit([
                        self.tab_bar_window,
                        Window(height=1, char="─", style="class:divider"),
                        Window(
                            content=self.market_indices_control,
                            height=1,
                            style="class:market_indices",
                            wrap_lines=False,
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
                VSplit([
                    Window(content=self.left_header_control, height=1, style="class:header", wrap_lines=False),
                    Window(content=self.right_header_control, height=1, style="class:header", wrap_lines=False, align=WindowAlign.RIGHT),
                ]),
                DynamicContainer(lambda: _build_body()),
                # ── Quick-action button bar ──
                Window(
                    content=self.quickaction_control,
                    height=1,
                    style="class:quickaction",
                    wrap_lines=False,
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
                self.search_field,
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
