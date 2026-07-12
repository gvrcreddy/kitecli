"""
Kotak Neo broker account manager.

Implements the BaseBrokerManager interface for Kotak Securities Kotak Neo
(using the `neo-api-client` Python package).

Key design decisions
--------------------
- **No WebSocket**: Kotak Neo does not expose a public WebSocket price stream
  compatible with our existing Zerodha subscription model.  Live LTPs for
  Kotak positions are updated by the primary Zerodha ticker in live_session.py
  via symbol-based matching (see _on_ticks).  ``supports_websocket()`` returns
  False so live_session skips the ticker setup for Kotak accounts.
- **TOTP auth**: Generates TOTP from ``totp_secret`` and uses neo-api-client's
  ``login()`` / ``session_2fa()`` flow.
- **Session persistence**: Access token persisted alongside Zerodha tokens in
  ``~/.kcli/sessions.json`` using the consumer_key as the key.
- **Normalised output**: ``get_positions()`` and ``get_orders()`` map Kotak
  response fields to the common KiteCLI schema so live_session needs no
  Kotak-specific branching (beyond the symbol-based LTP lookup).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pyotp
import threading as _threading

# Globally default all background threads to be daemon threads.
# This prevents the CLI process from hanging on Ctrl+C / exit while waiting for
# Kotak's background WebSocket loops and ping threads to terminate.
_orig_thread_init = _threading.Thread.__init__
def _patched_thread_init(self, *args, **kwargs):
    kwargs.setdefault("daemon", True)
    _orig_thread_init(self, *args, **kwargs)
_threading.Thread.__init__ = _patched_thread_init

from cli.base_manager import BaseBrokerManager

logger = logging.getLogger(__name__)

SESSIONS_FILE = Path.home() / ".kcli" / "sessions.json"


def _check_neo_error(resp: Any) -> None:
    """Helper to detect error dictionaries returned by the neo-api-client.

    Instead of raising standard HTTP exceptions, the neo-api-client library
    frequently catches exceptions internally and returns a dictionary
    containing 'Error' or 'Error Message' keys.
    Additionally, the Kotak Neo backend server returns dictionaries with
    'stat': 'Not_Ok' and 'errMsg' keys when a request fails.
    """
    if isinstance(resp, dict):
        # 1. SDK-level caught errors
        if "Error" in resp:
            err = resp["Error"]
            raise RuntimeError(str(err))
        if "Error Message" in resp:
            raise RuntimeError(str(resp["Error Message"]))

        # 2. Backend server-level errors
        stat = str(resp.get("stat", "")).upper()
        if stat in ("NOT_OK", "FAIL", "ERROR"):
            err_msg = resp.get("errMsg") or resp.get("desc") or resp.get("message") or "Unknown Kotak API error"
            st_code = resp.get("stCode") or resp.get("stcode")
            # If the error is simply 'No Data' (empty order book or positions), do not raise an exception
            if "no data" in err_msg.lower() or st_code in (5203, 11000):
                return
            code_desc = f" (Code: {st_code})" if st_code else ""
            raise RuntimeError(f"{err_msg}{code_desc}")



# ── session persistence (shared file with Zerodha tokens) ─────────────────────

def _load_sessions() -> dict[str, str]:
    if not SESSIONS_FILE.exists():
        return {}
    try:
        with open(SESSIONS_FILE, "r") as f:
            return json.load(f)
    except Exception as exc:
        logger.error("Failed to load sessions: %s", exc)
        return {}


def _save_session(key: str, token: str) -> None:
    sessions = _load_sessions()
    sessions[key] = token
    try:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SESSIONS_FILE, "w") as f:
            json.dump(sessions, f, indent=2)
    except Exception as exc:
        logger.error("Failed to save session for %s: %s", key, exc)


# ── KotakAccountManager ────────────────────────────────────────────────────────

class KotakAccountManager(BaseBrokerManager):
    """Manages one or more Kotak Neo accounts.

    Each account is keyed by its ``consumer_key`` (analogous to Zerodha's
    ``api_key``).
    """

    # ── BaseBrokerManager identity ─────────────────────────────────────────────

    @property
    def broker_name(self) -> str:
        return "kotak"

    def supports_websocket(self) -> bool:
        """Kotak Neo supports order feed and market feed WebSocket connections."""
        return True

    # ── internal state ─────────────────────────────────────────────────────────

    def __init__(self) -> None:
        # consumer_key → neo.api.client.NEOClient instance
        self._clients: dict[str, Any] = {}
        self._account_names: dict[str, str] = {}
        self._authenticated: dict[str, bool] = {}
        # consumer_key → stored credentials for re-auth
        self._credentials: dict[str, dict] = {}

    # ── account lifecycle ──────────────────────────────────────────────────────

    def init_account(self, account_key: str, **credentials) -> str:
        """ABC entry point — delegates to init_account_kotak."""
        return self.init_account_kotak(consumer_key=account_key, **credentials)

    def init_account_kotak(
        self,
        consumer_key: str,
        consumer_secret: str,
        mobile_number: str = "",
        password: str = "",
        mpin: str = "",
        ucc: str = "",
        name: str = "",
        totp_secret: str = "",
        proxy: str | None = None,
    ) -> str:
        """Register a Kotak Neo account and attempt token restore.

        Args:
            consumer_key: Kotak Neo API consumer key.
            consumer_secret: Kotak Neo API consumer secret.
            mobile_number: Registered mobile number (for OTP/TOTP login).
            password: Trading password.
            mpin: MPIN for the Kotak Neo account.
            ucc: UCC (Unique Client Code) for the Kotak account.
            name: Human-readable display name.
            totp_secret: TOTP shared secret (for automatic 2FA).
            proxy: Optional HTTP proxy URL (e.g. "http://user:pass@host:port").
                   When set, all SDK HTTP calls are routed through this proxy.

        Returns:
            Empty string (Kotak uses app-based / OTP auth, no browser URL).
        """
        try:
            from neo_api_client import NeoAPI
            try:
                from neo_api_client.NeoWebSocket import NeoWebSocket

                # Patch ping threads to handle closed connections gracefully
                def patched_start_hsi_ping_thread(self_ws):
                    import time as _time
                    import json as _json
                    while self_ws.hsiWebsocket and self_ws.is_hsi_open:
                        _time.sleep(30)
                        if not self_ws.is_hsi_open or not self_ws.hsiWebsocket:
                            break
                        try:
                            payload = {"type": "HB"}
                            self_ws.hsiWebsocket.send(_json.dumps(payload))
                        except Exception:
                            break

                def patched_start_hsm_ping_thread(self_ws):
                    import time as _time
                    import json as _json
                    while self_ws.hsWebsocket and self_ws.is_hsw_open:
                        _time.sleep(29)
                        if not self_ws.is_hsw_open or not self_ws.hsWebsocket:
                            break
                        try:
                            payload = {"type": "hb"}
                            self_ws.hsWebsocket.hs_send(_json.dumps(payload))
                        except Exception:
                            break

                NeoWebSocket.start_hsi_ping_thread = patched_start_hsi_ping_thread
                NeoWebSocket.start_hsm_ping_thread = patched_start_hsm_ping_thread
            except Exception:
                # Fallback if NeoWebSocket cannot be imported (e.g. during unit testing mocking)
                pass
        except ImportError as exc:
            raise ImportError(
                "Kotak Neo support requires the 'neo-api-client' package. "
                "Install it with: pip install neo-api-client"
            ) from exc

        client = NeoAPI(environment="prod", consumer_key=consumer_key)

        # Inject proxy into the SDK's REST client so every HTTP call
        # (login, order placement, positions, etc.) routes through the proxy.
        if proxy:
            import requests as _req
            import json as _json
            import re as _re
            from six.moves.urllib.parse import urlencode as _urlencode
            _proxies = {"http": proxy, "https": proxy}
            _orig_request = client.api_client.rest_client.request

            def _proxied_request(method, url, query_params=None, headers=None, body=None):
                method = method.upper()
                headers = headers or {}
                if 'Content-Type' not in headers:
                    headers['Content-Type'] = 'application/json'
                if method in ['POST', 'PUT', 'PATCH', 'DELETE']:
                    if query_params:
                        url += '?' + _urlencode(query_params)
                    if _re.search('json', headers['Content-Type'], _re.IGNORECASE):
                        request_body = _json.dumps(body) if body is not None else None
                        return _req.post(url=url, headers=headers, data=request_body, proxies=_proxies)
                    elif _re.search('x-www-form-urlencoded', headers['Content-Type'], _re.IGNORECASE):
                        request_body = {"jData": _json.dumps(body)} if body is not None else {}
                        return _req.post(url=url, headers=headers, data=request_body, proxies=_proxies)
                elif method == 'GET':
                    if query_params:
                        url += '?' + _urlencode(query_params)
                    return _req.get(url=url, headers=headers, proxies=_proxies)
                return _orig_request(method, url, query_params=query_params, headers=headers, body=body)

            client.api_client.rest_client.request = _proxied_request
            logger.info("Kotak account '%s': HTTP proxy enabled (%s…)", name, proxy[:20])

            # Also monkeypatch websocket-client (used by Kotak Neo WebSocket) to route over the proxy.
            try:
                import websocket as _ws_lib
                from urllib.parse import urlparse as _urlparse
                _parsed = _urlparse(proxy)
                _proxy_host = _parsed.hostname
                _proxy_port = _parsed.port
                _proxy_auth = (_parsed.username, _parsed.password) if _parsed.username and _parsed.password else None
                
                _orig_run_forever = _ws_lib.WebSocketApp.run_forever
                def _proxied_run_forever(self, *args, **kwargs):
                    kwargs.setdefault("http_proxy_host", _proxy_host)
                    kwargs.setdefault("http_proxy_port", _proxy_port)
                    kwargs.setdefault("proxy_type", "http")
                    if _proxy_auth:
                        kwargs.setdefault("http_proxy_auth", _proxy_auth)
                    return _orig_run_forever(self, *args, **kwargs)
                
                _ws_lib.WebSocketApp.run_forever = _proxied_run_forever
                logger.info("Kotak account '%s': WebSocket proxy monkeypatch applied", name)
            except Exception as e:
                logger.error("Failed to apply WebSocket proxy patch for Kotak: %s", e)
        self._clients[consumer_key] = client
        self._account_names[consumer_key] = name or consumer_key
        self._authenticated[consumer_key] = False
        self._credentials[consumer_key] = {
            "consumer_key": consumer_key,
            "consumer_secret": consumer_secret,
            "mobile_number": mobile_number,
            "password": password,
            "mpin": mpin,
            "ucc": ucc,
            "totp_secret": totp_secret,
        }

        # Attempt to restore a saved session token
        sessions = _load_sessions()
        saved_session = sessions.get(f"kotak:{consumer_key}")
        if saved_session and isinstance(saved_session, dict):
            logger.info(
                "Found saved Kotak session for '%s' (key=%s…). Verifying...",
                name, consumer_key[:8],
            )
            try:
                client.configuration.edit_token = saved_session.get("edit_token")
                client.configuration.edit_sid = saved_session.get("edit_sid")
                client.configuration.edit_rid = saved_session.get("edit_rid")
                client.configuration.serverId = saved_session.get("serverId")
                client.configuration.data_center = saved_session.get("data_center")
                client.configuration.base_url = saved_session.get("base_url")

                # Verify token liveness
                limits_resp = client.limits()
                _check_neo_error(limits_resp)
                self._authenticated[consumer_key] = True
                logger.info(
                    "Restored valid Kotak session for '%s' (key=%s…)", name, consumer_key[:8]
                )
            except Exception as exc:
                logger.info(
                    "Saved Kotak token for '%s' (key=%s…) expired: %s",
                    name, consumer_key[:8], exc,
                )
                self._authenticated[consumer_key] = False

        return ""  # no login URL for Kotak

    def auto_login(self, account_key: str, **_kwargs) -> bool:
        """Perform TOTP-based auto-login for the Kotak account."""
        creds = self._credentials.get(account_key, {})
        totp_secret = creds.get("totp_secret", "")
        mobile_number = creds.get("mobile_number", "")
        mpin = creds.get("mpin", "")
        ucc = creds.get("ucc", "")

        if not (mobile_number and ucc and mpin and totp_secret):
            logger.warning(
                "Kotak auto-login skipped for key=%s…: incomplete credentials",
                account_key[:8],
            )
            return False

        client = self._clients.get(account_key)
        if not client:
            return False

        try:
            totp_code = pyotp.TOTP(totp_secret).now()
            logger.info("Kotak login: sending login request for key=%s…", account_key[:8])

            # Step 1: Initiate login
            login_resp = client.totp_login(
                mobile_number=mobile_number,
                ucc=ucc,
                totp=totp_code,
            )
            logger.debug("Kotak login resp: %s", login_resp)
            if isinstance(login_resp, dict) and "Error" in login_resp:
                raise RuntimeError(f"TOTP login failed: {login_resp}")

            # Step 2: 2FA validation with MPIN
            session_resp = client.totp_validate(mpin=mpin)
            logger.debug("Kotak 2FA resp: %s", session_resp)
            if isinstance(session_resp, dict) and "Error" in session_resp:
                raise RuntimeError(f"MPIN validation failed: {session_resp}")

            # Persist token
            conf = client.configuration
            if getattr(conf, "edit_token", None):
                session_data = {
                    "edit_token": conf.edit_token,
                    "edit_sid": conf.edit_sid,
                    "edit_rid": conf.edit_rid,
                    "serverId": conf.serverId,
                    "data_center": conf.data_center,
                    "base_url": conf.base_url,
                }
                _save_session(f"kotak:{account_key}", session_data)

            self._authenticated[account_key] = True
            logger.info("Kotak auto-login successful for key=%s…", account_key[:8])
            return True

        except Exception as exc:
            logger.error(
                "Kotak auto-login failed for key=%s…: %s", account_key[:8], exc, exc_info=True
            )
            self._authenticated[account_key] = False
            return False

    def is_authenticated(self, account_key: str) -> bool:
        return self._authenticated.get(account_key, False)

    def get_account_info(self, account_key: str) -> dict[str, Any]:
        return {
            "name": self._account_names.get(account_key, account_key),
            "api_key": account_key,       # alias used throughout KiteCLI
            "account_key": account_key,
            "authenticated": self.is_authenticated(account_key),
            "broker": "kotak",
        }

    def get_access_token(self, account_key: str) -> str | None:
        client = self._clients.get(account_key)
        if client:
            return getattr(client.configuration, "access_token", None)
        return None

    def get_all_account_keys(self) -> list[str]:
        return list(self._clients.keys())

    # ── market data ────────────────────────────────────────────────────────────

    def get_positions(self, account_key: str) -> list[dict[str, Any]]:
        """Fetch Kotak Neo net positions and normalise to KiteCLI schema.

        Kotak positions do NOT carry an ``instrument_token`` field.  The
        ``instrument_token`` field is set to ``None`` in the output so
        live_session falls back to symbol-based LTP matching instead of
        token-based matching.
        """
        client = self._clients.get(account_key)
        if not client:
            raise ValueError(f"Kotak account not found: key={account_key[:8]}…")
        if not self.is_authenticated(account_key):
            raise RuntimeError(f"Kotak account not authenticated: key={account_key[:8]}…")

        try:
            resp = client.positions()
            _check_neo_error(resp)
        except Exception as exc:
            raise RuntimeError(f"Kotak get_positions failed: {exc}") from exc

        raw_positions = []
        if isinstance(resp, dict):
            # neo-api-client wraps positions under different keys depending on version
            raw_positions = (
                resp.get("data", [])
                or resp.get("positions", [])
                or resp.get("Net", [])
                or []
            )
        elif isinstance(resp, list):
            raw_positions = resp

        # Fetch holdings to obtain correct average cost for carried-forward positions
        holdings_map = {}
        try:
            holdings_resp = client.holdings()
            if isinstance(holdings_resp, dict):
                holdings_data = holdings_resp.get("data") or []
                for h in holdings_data:
                    tok = str(h.get("exchangeIdentifier") or "")
                    if tok:
                        holdings_map[tok] = {
                            "quantity": int(h.get("quantity") or 0),
                            "average_price": float(h.get("averagePrice") or 0.0),
                        }
        except Exception as e:
            logger.warning("Failed to fetch holdings for Kotak account: %s", e)

        result = []
        for pos in raw_positions:
            tok = str(pos.get("tok", ""))
            
            # If the position is in holdings or has carry-forward/day-filled fields, use the detailed breakdown
            has_breakdown = (
                tok in holdings_map
                or "cfBuyQty" in pos
                or "cfSellQty" in pos
                or "flBuyQty" in pos
                or "flSellQty" in pos
            )
            
            if has_breakdown:
                fl_buy_qty = int(pos.get("flBuyQty", 0) or 0)
                fl_sell_qty = int(pos.get("flSellQty", 0) or 0)
                buy_amt = float(pos.get("buyAmt", 0.0) or 0.0)
                sell_amt = float(pos.get("sellAmt", 0.0) or 0.0)

                # Determine carried forward values (from holdings if available, else from positions response)
                if tok in holdings_map:
                    h_qty = holdings_map[tok]["quantity"]
                    h_avg = holdings_map[tok]["average_price"]
                    cf_buy_qty = h_qty if h_qty > 0 else 0
                    cf_buy_amt = cf_buy_qty * h_avg
                    cf_sell_qty = abs(h_qty) if h_qty < 0 else 0
                    cf_sell_amt = cf_sell_qty * h_avg
                else:
                    cf_buy_qty = int(pos.get("cfBuyQty", 0) or 0)
                    cf_sell_qty = int(pos.get("cfSellQty", 0) or 0)
                    cf_buy_amt = float(pos.get("cfBuyAmt", 0.0) or 0.0)
                    cf_sell_amt = float(pos.get("cfSellAmt", 0.0) or 0.0)

                qty = (cf_buy_qty + fl_buy_qty) - (cf_sell_qty + fl_sell_qty)
                total_buy_amt = cf_buy_amt + buy_amt
                total_sell_amt = cf_sell_amt + sell_amt
                total_buy_qty = cf_buy_qty + fl_buy_qty
                total_sell_qty = cf_sell_qty + fl_sell_qty

                if qty > 0:
                    avg_price = total_buy_amt / total_buy_qty if total_buy_qty > 0 else 0.0
                elif qty < 0:
                    avg_price = total_sell_amt / total_sell_qty if total_sell_qty > 0 else 0.0
                else:
                    avg_price = total_buy_amt / total_buy_qty if total_buy_qty > 0 else (total_sell_amt / total_sell_qty if total_sell_qty > 0 else 0.0)

                realised = float(pos.get("realizedPL", pos.get("realised", 0.0) or 0.0))
                if realised == 0.0 and qty == 0:
                    realised = total_sell_amt - total_buy_amt
            else:
                qty_str = str(pos.get("netQty", pos.get("quantity", "0")))
                try:
                    qty = int(qty_str)
                except ValueError:
                    qty = 0

                avg_price_str = str(pos.get("avgPrice", pos.get("average_price", "0")))
                try:
                    avg_price = float(avg_price_str)
                except ValueError:
                    avg_price = 0.0
                realised = float(pos.get("realizedPL", pos.get("realised", 0.0) or 0.0))

            last_price_str = str(pos.get("ltp", pos.get("last_price", "0")))
            try:
                last_price = float(last_price_str)
            except ValueError:
                last_price = avg_price

            if last_price == 0.0:
                last_price = avg_price

            pnl = (last_price - avg_price) * qty if qty != 0 else realised
            pnl_pct = (
                (pnl / (avg_price * abs(qty))) * 100
                if avg_price > 0 and qty != 0
                else 0.0
            )

            result.append({
                "tradingsymbol": pos.get("trdSym", pos.get("tradingsymbol", "")),
                "quantity": qty,
                "average_price": avg_price,
                "last_price": last_price,
                "pnl": pnl,
                "realised": realised,
                "unrealised": (last_price - avg_price) * qty if qty != 0 else 0.0,
                "pnl_pct": pnl_pct,
                "product": pos.get("prod", pos.get("product", "NRML")),
                "exchange": pos.get("exch", pos.get("exchange", "NFO")),
                "instrument_token": None,
            })

        logger.info(
            "Fetched %d Kotak positions for key=%s…", len(result), account_key[:8]
        )
        return result

    def get_orders(self, account_key: str) -> list[dict[str, Any]]:
        """Fetch Kotak Neo order book and normalise to KiteCLI schema."""
        client = self._clients.get(account_key)
        if not client:
            raise ValueError(f"Kotak account not found: key={account_key[:8]}…")
        if not self.is_authenticated(account_key):
            raise RuntimeError(f"Kotak account not authenticated: key={account_key[:8]}…")

        try:
            resp = client.order_report()
            _check_neo_error(resp)
        except Exception as exc:
            raise RuntimeError(f"Kotak get_orders failed: {exc}") from exc

        raw_orders = []
        if isinstance(resp, dict):
            raw_orders = resp.get("data", resp.get("orders", []))
        elif isinstance(resp, list):
            raw_orders = resp

        result = []
        for ord in raw_orders:
            # Map Kotak status strings to standard KiteCLI strings
            raw_status = str(ord.get("ordSt", ord.get("status", ""))).upper()
            status_map = {
                "COMPLETE": "COMPLETE",
                "EXECUTED": "COMPLETE",
                "OPEN": "OPEN",
                "PENDING": "OPEN",
                "CANCELLED": "CANCELLED",
                "REJECTED": "REJECTED",
                "TRIGGER PENDING": "TRIGGER PENDING",
            }
            status = status_map.get(raw_status, raw_status)

            qty_str = str(ord.get("qty", ord.get("quantity", "0")))
            try:
                quantity = int(qty_str)
            except ValueError:
                quantity = 0

            pend_qty_str = str(ord.get("unFldSz", ord.get("pending_quantity", "0")))
            try:
                pending_quantity = int(pend_qty_str)
            except ValueError:
                pending_quantity = 0

            price_str = str(ord.get("prc", ord.get("price", "0")))
            try:
                price = float(price_str)
            except ValueError:
                price = 0.0

            result.append({
                "order_id": str(ord.get("nOrdNo", ord.get("order_id", ""))),
                "tradingsymbol": ord.get("trdSym", ord.get("tradingsymbol", "")),
                "transaction_type": str(ord.get("trnsTp", ord.get("transaction_type", ""))).upper(),
                "quantity": quantity,
                "pending_quantity": pending_quantity,
                "price": price,
                "order_type": str(ord.get("prcTp", ord.get("order_type", "MARKET"))).upper(),
                "status": status,
                "product": str(ord.get("prod", ord.get("product", "NRML"))).upper(),
                "exchange": str(ord.get("exch", ord.get("exchange", "NFO"))).upper(),
                "parent_order_id": str(ord.get("refLmtPrc", ord.get("parent_order_id", ""))) or None,
                "status_message": str(ord.get("rejRsn", ord.get("status_message", ""))),
            })

        return result

    def get_margins(self, account_key: str) -> dict[str, Any]:
        """Fetch Kotak Neo margin summary."""
        client = self._clients.get(account_key)
        if not client or not self.is_authenticated(account_key):
            return {"net": None, "cash": None, "collateral": None}

        try:
            resp = client.limits()
            if isinstance(resp, dict):
                data = resp.get("data", resp)
                net = data.get("Net", data.get("net"))
                cash = data.get("cash", data.get("Cash"))
                collateral = data.get("Collateral", data.get("collateral"))
                try:
                    net = float(net) if net is not None else None
                    cash = float(cash) if cash is not None else None
                    collateral = float(collateral) if collateral is not None else None
                except (ValueError, TypeError):
                    pass
                return {"net": net, "cash": cash, "collateral": collateral}
        except Exception as exc:
            logger.warning("Kotak get_margins failed for key=%s…: %s", account_key[:8], exc)

        return {"net": None, "cash": None, "collateral": None}

    # ── order management ───────────────────────────────────────────────────────

    def place_order(
        self,
        api_key: str,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        order_type: str,
        price: float | None = None,
        trigger_price: float | None = None,
        product: str = "NRML",
    ) -> list[str]:
        """Place an order on Kotak Neo.

        Kotak Neo's freeze-qty limit differs from Zerodha's.  Splitting is
        applied if quantity > 1800 (conservative Kotak NFO freeze limit).
        """
        account_key = api_key  # alias for clarity
        client = self._clients.get(account_key)
        if not client:
            raise ValueError(f"Kotak account not found: key={account_key[:8]}…")
        if not self.is_authenticated(account_key):
            raise RuntimeError(f"Kotak account not authenticated: key={account_key[:8]}…")

        transaction_type = transaction_type.upper()
        order_type = order_type.upper()
        product = product.upper()
        exchange = exchange.upper()
        tradingsymbol = tradingsymbol.upper()

        # Map transaction_type to Kotak Neo expected values ('B' or 'S')
        transaction_type_map = {
            "BUY": "B",
            "SELL": "S",
        }
        kotak_transaction_type = transaction_type_map.get(transaction_type, transaction_type)

        KOTAK_FREEZE_LIMIT = 1800
        legs: list[int] = []
        remaining = quantity
        while remaining > 0:
            leg_qty = min(remaining, KOTAK_FREEZE_LIMIT)
            legs.append(leg_qty)
            remaining -= leg_qty

        # Map order_type to Kotak Neo strings
        order_type_map = {
            "MARKET": "MKT",
            "LIMIT": "L",
            "SL": "SL",
            "SL-M": "SL-M",
        }
        kotak_order_type = order_type_map.get(order_type, order_type)

        order_ids: list[str] = []
        for leg_qty in legs:
            params = {
                "exchange_segment": f"{exchange}_EQ" if exchange in ("NSE", "BSE") else exchange,
                "product": product,
                "price": str(price or 0),
                "order_type": kotak_order_type,
                "quantity": str(leg_qty),
                "validity": "DAY",
                "trading_symbol": tradingsymbol,
                "transaction_type": kotak_transaction_type,
                "amo": "NO",
                "disclosed_quantity": "0",
                "market_protection": "0",
                "pf": "N",
                "trigger_price": str(trigger_price or 0),
                "tag": None,
            }
            try:
                resp = client.place_order(**params)
                logger.info("Kotak place_order raw response: %s", resp)
                _check_neo_error(resp)
                if isinstance(resp, dict):
                    order_id = str(
                        resp.get("nOrdNo")
                        or resp.get("norenordno")
                        or resp.get("order_id")
                        or resp.get("orderId")
                        or resp.get("data", {}).get("nOrdNo")
                        or resp.get("data", {}).get("norenordno")
                        or resp.get("data", {}).get("order_id")
                        or resp.get("data", {}).get("orderId")
                        or ""
                    )
                else:
                    order_id = str(resp)
                order_ids.append(order_id)
                logger.info(
                    "Kotak order placed: %s %s %d %s → %s",
                    transaction_type, order_type, leg_qty, tradingsymbol, order_id,
                )
            except Exception as exc:
                logger.error("Kotak place_order failed: %s", exc)
                raise RuntimeError(f"Kotak place_order failed: {exc}") from exc

        return order_ids

    def modify_order(
        self,
        api_key: str,
        order_id: str,
        quantity: int | None = None,
        price: float | None = None,
        order_type: str | None = None,
        trigger_price: float | None = None,
    ) -> str:
        """Modify an open Kotak Neo order."""
        account_key = api_key
        client = self._clients.get(account_key)
        if not client:
            raise ValueError(f"Kotak account not found: key={account_key[:8]}…")

        order_type_map = {"MARKET": "MKT", "LIMIT": "L", "SL": "SL", "SL-M": "SL-M"}
        params = {
            "order_id": order_id,
            "price": str(price or 0),
            "quantity": str(quantity or 0),
            "validity": "DAY",
            "disclosed_quantity": "0",
            "trigger_price": str(trigger_price or 0),
            "order_type": order_type_map.get((order_type or "").upper(), order_type or "L"),
        }
        try:
            resp = client.modify_order(**params)
            _check_neo_error(resp)
            return str(
                resp.get("nOrdNo", order_id) if isinstance(resp, dict) else order_id
            )
        except Exception as exc:
            raise RuntimeError(f"Kotak modify_order failed: {exc}") from exc

    def cancel_order(self, api_key: str, order_id: str) -> str:
        """Cancel an open Kotak Neo order."""
        account_key = api_key
        client = self._clients.get(account_key)
        if not client:
            raise ValueError(f"Kotak account not found: key={account_key[:8]}…")

        try:
            resp = client.cancel_order(order_id=order_id)
            _check_neo_error(resp)
            return str(
                resp.get("nOrdNo", order_id) if isinstance(resp, dict) else order_id
            )
        except Exception as exc:
            raise RuntimeError(f"Kotak cancel_order failed: {exc}") from exc

    def exit_positions(
        self,
        account_key: str,
        tradingsymbol: str | None = None,
        price: float | None = None,
    ) -> list[dict[str, Any]]:
        """Square off Kotak Neo positions."""
        positions = self.get_positions(account_key)
        orders_placed = []

        for pos in positions:
            qty = pos.get("quantity", 0)
            if qty == 0:
                continue

            sym = pos.get("tradingsymbol", "")
            if tradingsymbol and sym.upper() != tradingsymbol.upper():
                continue

            transaction_type = "SELL" if qty > 0 else "BUY"
            exit_qty = abs(qty)
            product = pos.get("product", "NRML")
            exchange = pos.get("exchange", "NFO")
            order_type = "LIMIT" if price is not None else "MARKET"

            try:
                order_ids = self.place_order(
                    api_key=account_key,
                    tradingsymbol=sym,
                    exchange=exchange,
                    transaction_type=transaction_type,
                    quantity=exit_qty,
                    order_type=order_type,
                    price=price,
                    product=product,
                )
                for oid in order_ids:
                    orders_placed.append({
                        "tradingsymbol": sym,
                        "quantity": exit_qty,
                        "product": product,
                        "order_id": oid,
                    })
            except Exception as exc:
                logger.error("Kotak exit failed for %s: %s", sym, exc)
                if tradingsymbol:
                    raise RuntimeError(f"Kotak exit failed for {sym}: {exc}") from exc

        return orders_placed

    def create_ticker(self, account_key: str):
        """Create a KotakTicker WebSocket connection wrapper for this account."""
        client = self._clients.get(account_key)
        if not client:
            raise ValueError(f"Kotak account not found: key={account_key[:8]}…")
        return KotakTicker(account_key, self.get_access_token(account_key), client)


# ── KotakTicker WebSocket Wrapper ─────────────────────────────────────────────

class KotakTicker:
    """Compatibility wrapper that mimics Zerodha's KiteTicker using Kotak Neo's SDK WebSocket."""

    MODE_LTP = "ltp"
    MODE_QUOTE = "quote"
    MODE_FULL = "full"

    def __init__(self, api_key: str, access_token: str | None, client: Any) -> None:
        self.api_key = api_key
        self.access_token = access_token
        self.client = client

        self.on_connect = None
        self.on_ticks = None
        self.on_order_update = None
        self.on_close = None
        self.on_error = None

    def connect(self, **kwargs) -> None:
        """Register callbacks and connect the Kotak Neo WebSocket threads."""
        self.client.on_open = self._on_open
        self.client.on_message = self._on_message
        self.client.on_error = self._on_error
        self.client.on_close = self._on_close

        logger.info("Connecting Kotak Neo WebSocket (api_key=%s…)...", self.api_key[:8])
        try:
            # Subscribing to the order feed starts the background WS threads in the Kotak SDK
            self.client.subscribe_to_orderfeed()
        except Exception as e:
            logger.error("Failed to connect Kotak Neo order feed: %s", e)
            if self.on_error:
                self.on_error(self, 0, str(e))

    def close(self) -> None:
        """Disconnect and clean up Kotak Neo WebSocket connections."""
        logger.info("Closing Kotak Neo WebSocket (api_key=%s…)...", self.api_key[:8])
        neo_ws = getattr(self.client, "NeoWebSocket", None)
        if neo_ws:
            if getattr(neo_ws, "hsWebsocket", None):
                try:
                    neo_ws.hsWebsocket.close()
                except Exception:
                    pass
            if getattr(neo_ws, "hsiWebsocket", None):
                try:
                    neo_ws.hsiWebsocket.close()
                except Exception:
                    pass
            neo_ws.is_hsw_open = 0
            neo_ws.is_hsi_open = 0

    def subscribe(self, tokens: list[int]) -> None:
        """Mock method for KiteTicker compatibility — position ticks are routed via Zerodha."""
        pass

    def unsubscribe(self, tokens: list[int]) -> None:
        """Mock method for KiteTicker compatibility."""
        pass

    def set_mode(self, mode: str, tokens: list[int]) -> None:
        """Mock method for KiteTicker compatibility."""
        pass

    # ── Internal SDK WebSocket Callback Mappings ──────────────────────────────

    def _on_open(self, message: Any = None) -> None:
        logger.info("Kotak Neo WebSocket connected: %s", message or "Session opened")
        if self.on_connect:
            self.on_connect(self, None)

    def _on_close(self, message: Any = None) -> None:
        logger.info("Kotak Neo WebSocket closed: %s", message or "Session closed")
        if self.on_close:
            self.on_close(self, 1000, str(message or "Closed"))

    def _on_error(self, error: Any) -> None:
        logger.error("Kotak Neo WebSocket error: %s", error)
        if self.on_error:
            self.on_error(self, 0, str(error))

    def _on_message(self, message: Any) -> None:
        if not isinstance(message, dict):
            return

        msg_type = message.get("type")
        data = message.get("data")

        if msg_type == "stock_feed":
            # Convert Kotak's stock_feed data to Zerodha/Kite-like tick structure
            # Kotak feed format: [{'tk': '26000', 'ltp': '24444.90', ...}]
            ticks = []
            if isinstance(data, list):
                for item in data:
                    tk = item.get("tk")
                    ltp = item.get("ltp")
                    if tk is not None:
                        try:
                            ticks.append({
                                "instrument_token": int(tk),
                                "last_price": float(ltp) if ltp is not None else 0.0,
                            })
                        except (ValueError, TypeError):
                            continue
            if ticks and self.on_ticks:
                self.on_ticks(self, ticks)

        elif msg_type == "order_feed":
            # Convert Kotak's order_feed update to standard Zerodha/Kite-like order update schema.
            # Parse the string payload if necessary.
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    pass

            if isinstance(data, dict):
                # Ignore connection acknowledgements and heartbeats
                if data.get("type") == "cn" or data.get("task") == "cn":
                    return

                # Normalise status strings
                status_map = {
                    "COMPLETE": "COMPLETE",
                    "EXECUTED": "COMPLETE",
                    "OPEN": "OPEN",
                    "PENDING": "OPEN",
                    "CANCELLED": "CANCELLED",
                    "REJECTED": "REJECTED",
                    "TRIGGER PENDING": "TRIGGER PENDING",
                }
                raw_status = str(data.get("ordSt", data.get("status", ""))).upper()
                status = status_map.get(raw_status, raw_status)

                try:
                    qty = int(data.get("qty", data.get("quantity", 0)))
                except (ValueError, TypeError):
                    qty = 0

                try:
                    pending = int(data.get("unFldSz", data.get("pending_quantity", qty)))
                except (ValueError, TypeError):
                    pending = qty

                try:
                    price = float(data.get("prc", data.get("price", 0.0)))
                except (ValueError, TypeError):
                    price = 0.0

                norm_data = {
                    "order_id": str(data.get("nOrdNo", data.get("order_id", ""))),
                    "tradingsymbol": data.get("trdSym", data.get("tradingsymbol", "")),
                    "transaction_type": str(data.get("trnsTp", data.get("transaction_type", ""))).upper(),
                    "quantity": qty,
                    "filled_quantity": max(0, qty - pending),
                    "price": price,
                    "status": status,
                    "status_message": str(data.get("rejRsn", data.get("status_message", ""))),
                }

                if self.on_order_update:
                    self.on_order_update(self, norm_data)

