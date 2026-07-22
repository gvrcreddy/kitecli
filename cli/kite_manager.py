import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pyotp
import requests as http_requests
from kiteconnect import KiteConnect

from cli.base_manager import BaseBrokerManager

logger = logging.getLogger(__name__)

from cli.config import load_sessions as _load_sessions, save_session as _save_session


class KiteAccountManager(BaseBrokerManager):
    """Manages multiple KiteConnect instances keyed by api_key.

    Stores KiteConnect client objects, api_secrets, account names,
    and tracks authentication state for each registered account.
    """

    # ── BaseBrokerManager identity ────────────────────────────────────────────

    @property
    def broker_name(self) -> str:
        return "zerodha"

    def supports_websocket(self) -> bool:
        return True

    # BaseBrokerManager abstract interface helpers (delegate to api_key methods)
    def init_account(self, account_key: str, **credentials) -> str:  # type: ignore[override]
        """ABC entry point — delegates to the api_key-based overload."""
        return self._init_account_by_key(
            api_key=account_key,
            api_secret=credentials.get("api_secret", ""),
            name=credentials.get("name", ""),
            proxy=credentials.get("proxy"),
        )

    def auto_login(self, account_key: str, **credentials) -> bool:  # type: ignore[override]
        """ABC entry point — delegates to the api_key-based auto_login."""
        return self._auto_login_by_key(
            api_key=account_key,
            user_id=credentials.get("user_id", ""),
            password=credentials.get("password", ""),
            totp_secret=credentials.get("totp_secret", ""),
        )

    def get_all_account_keys(self) -> list[str]:
        return self.get_all_api_keys()

    def get_account_info(self, account_key: str) -> dict[str, Any]:  # type: ignore[override]
        return self._get_account_info_by_key(account_key)

    def get_access_token(self, account_key: str) -> str | None:  # type: ignore[override]
        return self._get_access_token_by_key(account_key)

    # ── internal init state ───────────────────────────────────────────────────

    def __init__(self) -> None:
        self._clients: dict[str, KiteConnect] = {}
        self._api_secrets: dict[str, str] = {}
        self._account_names: dict[str, str] = {}
        self._authenticated: dict[str, bool] = {}
        self._proxies: dict[str, dict] = {}  # per-account proxy dicts for requests.Session
        self._nfo_lot_size_cache: dict[str, int] = {}  # fetched once, reused across calls

    # ── public init helpers (named with _by_key suffix to avoid ABC clash) ────

    def _init_account_by_key(self, api_key: str, api_secret: str, name: str = "", proxy: str = None) -> str:
        """Core account initialisation (used both directly and via ABC)."""
        proxies = {"http": proxy, "https": proxy} if proxy else None
        kite = KiteConnect(api_key=api_key, proxies=proxies)
        
        # Increase connection pool size and timeouts to support slow proxies
        from requests.adapters import HTTPAdapter
        adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100)
        kite.reqsession.mount("https://", adapter)
        kite.reqsession.mount("http://", adapter)
        kite.timeout = 25

        self._clients[api_key] = kite
        self._api_secrets[api_key] = api_secret
        self._account_names[api_key] = name or api_key
        self._authenticated[api_key] = False
        self._proxies[api_key] = proxies or {}

        # Monkeypatch reqsession.request to bypass proxy for all non-order endpoints
        orig_request = kite.reqsession.request
        def proxied_request(method, url, *args, **kwargs):
            method_upper = method.upper()
            url_lower = url.lower()
            # Only apply proxies for order placement, modification, or cancellation
            is_order_api = (
                method_upper in ("POST", "PUT", "DELETE") and
                ("/orders" in url_lower or "/gtt" in url_lower)
            )
            if not is_order_api:
                # Remove proxies for both the individual request kwargs and session-level config
                kwargs["proxies"] = {}
                orig_proxies = kite.reqsession.proxies
                kite.reqsession.proxies = {}
                try:
                    return orig_request(method, url, *args, **kwargs)
                finally:
                    kite.reqsession.proxies = orig_proxies
            else:
                return orig_request(method, url, *args, **kwargs)

        kite.reqsession.request = proxied_request

        sessions = _load_sessions()
        saved_token = sessions.get(api_key)
        if saved_token:
            logger.debug("Found saved session token for '%s' (api_key=%s…). Assuming valid.", name, api_key[:8])
            kite.set_access_token(saved_token)
            self._authenticated[api_key] = True

        login_url = f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"
        logger.info("Initialized account '%s' (api_key=%s…)", name, api_key[:8])
        return login_url

    def _get_account_info_by_key(self, api_key: str) -> dict[str, Any]:
        return {
            "name": self._account_names.get(api_key, api_key),
            "api_key": api_key,
            "account_key": api_key,
            "authenticated": self.is_authenticated(api_key),
        }

    def _get_access_token_by_key(self, api_key: str) -> str | None:
        kite = self._clients.get(api_key)
        if kite:
            return getattr(kite, "access_token", None)
        return None

    def _auto_login_by_key(self, api_key: str, user_id: str, password: str, totp_secret: str) -> bool:
        """Internal helper for auto_login; calls the public auto_login(api_key, ...) below."""
        return self.auto_login_kite(api_key, user_id, password, totp_secret)

    def init_account_kite(self, api_key: str, api_secret: str, name: str = "", proxy: str = None) -> str:
        """Initialize a KiteConnect instance and return the login URL.

        This is the Zerodha-specific entry point used by api_client.py.
        The ABC ``init_account`` above delegates here via ``_init_account_by_key``.
        """
        return self._init_account_by_key(api_key=api_key, api_secret=api_secret, name=name, proxy=proxy)

    def complete_login(self, api_key: str, request_token: str) -> bool:
        """Complete the OAuth login flow by generating a session.

        Args:
            api_key: The Kite Connect API key.
            request_token: The request token received from the OAuth callback.

        Returns:
            True if login succeeded, False otherwise.
        """
        kite = self._clients.get(api_key)
        api_secret = self._api_secrets.get(api_key)

        if not kite or not api_secret:
            logger.error("Account not found for api_key=%s…", api_key[:8])
            return False

        try:
            data = kite.generate_session(request_token, api_secret=api_secret)
            access_token = data["access_token"]
            kite.set_access_token(access_token)
            self._authenticated[api_key] = True
            _save_session(api_key, access_token)
            logger.info(
                "Login successful for account '%s' (api_key=%s…)",
                self._account_names.get(api_key, api_key),
                api_key[:8],
            )
            return True
        except Exception as exc:
            logger.error(
                "Login failed for account '%s' (api_key=%s…): %s",
                self._account_names.get(api_key, api_key),
                api_key[:8],
                exc,
                exc_info=True,
            )
            self._authenticated[api_key] = False
            return False

    def get_positions(self, api_key: str) -> list[dict[str, Any]]:
        """Fetch open positions for an account.

        Only positions with a non-zero quantity are returned.

        Args:
            api_key: The Kite Connect API key.

        Returns:
            A list of position dicts with keys matching the Position model.

        Raises:
            ValueError: If the account is not found.
            RuntimeError: If the Kite API call fails.
        """
        kite = self._clients.get(api_key)
        if not kite:
            raise ValueError(f"Account not found for api_key={api_key[:8]}…")

        try:
            positions_data = kite.positions()
            # Combine day and net positions; net is the primary view
            all_positions = positions_data.get("net", [])

            ret_positions = []
            for pos in all_positions:
                qty = pos.get("quantity", 0)
                avg_price = pos.get("average_price", 0.0)
                pnl = pos.get("pnl", 0.0)
                pnl_pct = (pnl / (avg_price * abs(qty))) * 100 if avg_price > 0 and qty != 0 else 0.0

                ret_positions.append(
                    {
                        "tradingsymbol": pos.get("tradingsymbol", ""),
                        "quantity": qty,
                        "average_price": avg_price,
                        "last_price": pos.get("last_price", 0.0),
                        "pnl": pnl,
                        "realised": pos.get("realised", 0.0),
                        "unrealised": pos.get("unrealised", 0.0),
                        "pnl_pct": pnl_pct,
                        "product": pos.get("product", ""),
                        "exchange": pos.get("exchange", ""),
                        "instrument_token": pos.get("instrument_token"),
                    }
                )

            logger.info(
                "Fetched %d net positions for api_key=%s…",
                len(ret_positions),
                api_key[:8],
            )
            return ret_positions
        except Exception as exc:
            logger.error(
                "Failed to fetch positions for api_key=%s…", api_key[:8]
            )
            raise RuntimeError(f"Failed to fetch positions: {exc}") from exc

    def is_authenticated(self, api_key: str) -> bool:
        """Check if the given account has completed authentication.

        Args:
            api_key: The Kite Connect API key.

        Returns:
            True if the account is authenticated, False otherwise.
        """
        return self._authenticated.get(api_key, False)

    def get_account_info(self, api_key: str) -> dict[str, Any]:
        """Get summary info for a registered account.

        Args:
            api_key: The Kite Connect API key.

        Returns:
            A dict with name, api_key, and authenticated status.
        """
        return {
            "name": self._account_names.get(api_key, api_key),
            "api_key": api_key,
            "authenticated": self.is_authenticated(api_key),
        }

    def get_access_token(self, api_key: str) -> str | None:
        """Get the access token for an authenticated account.

        Args:
            api_key: The Kite Connect API key.

        Returns:
            The access token string, or None if not authenticated/found.
        """
        kite = self._clients.get(api_key)
        if kite:
            return getattr(kite, "access_token", None)
        return None

    def check_token(self, api_key: str) -> dict[str, Any]:
        """Validate the stored access token for an account against the REST API.

        Calls ``kite.profile()`` and classifies the outcome so callers can tell
        the difference between a valid token, an expired/invalid token, and other
        failures (network/proxy). This is the same credential pair used for the
        WebSocket ticker, so it explains 403 handshake failures.

        Returns a dict with keys:
            name: account display name
            api_key: the api_key
            status: one of "valid", "no_token", "expired", "forbidden", "error"
            detail: human-readable message
        """
        name = self._account_names.get(api_key, api_key)
        kite = self._clients.get(api_key)
        if kite is None:
            return {"name": name, "api_key": api_key, "status": "error",
                    "detail": "account not registered"}

        token = getattr(kite, "access_token", None)
        if not token:
            return {"name": name, "api_key": api_key, "status": "no_token",
                    "detail": "no access token (login required)"}

        try:
            from kiteconnect.exceptions import TokenException, PermissionException
        except Exception:
            TokenException = PermissionException = ()

        try:
            kite.profile()
            return {"name": name, "api_key": api_key, "status": "valid",
                    "detail": "token valid"}
        except TokenException as exc:
            return {"name": name, "api_key": api_key, "status": "expired",
                    "detail": f"token expired/invalid: {exc}"}
        except PermissionException as exc:
            return {"name": name, "api_key": api_key, "status": "forbidden",
                    "detail": f"permission denied: {exc}"}
        except Exception as exc:
            msg = str(exc)
            low = msg.lower()
            if "403" in msg or "forbidden" in low:
                status = "forbidden"
            elif "token" in low or "session" in low:
                status = "expired"
            else:
                status = "error"
            return {"name": name, "api_key": api_key, "status": status,
                    "detail": msg}

    def get_all_api_keys(self) -> list[str]:
        """Return a list of all registered api_keys."""
        return list(self._clients.keys())

    def auto_login_kite(
        self,
        api_key: str,
        user_id: str,
        password: str,
        totp_secret: str,
    ) -> bool:
        """Automate the full Kite login flow using credentials + TOTP.

        This is the Zerodha-specific implementation.  The ABC ``auto_login``
        above delegates here via ``_auto_login_by_key``.

        Args:
            api_key: The Kite Connect API key (must already be init'd).
            user_id: Zerodha client/user ID (e.g. ``AB1234``).
            password: Zerodha login password.
            totp_secret: Base32-encoded TOTP secret from Zerodha 2FA setup.

        Returns:
            True if auto-login succeeded, False otherwise.
        """
        kite = self._clients.get(api_key)
        api_secret = self._api_secrets.get(api_key)
        if not kite or not api_secret:
            logger.error("Account not initialized for api_key=%s…", api_key[:8])
            return False

        # Build a requests.Session and apply the per-account proxy so that
        # login and 2FA calls also flow through the proxy (not just KiteConnect SDK calls).
        session = http_requests.Session()
        from requests.adapters import HTTPAdapter
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        # Zerodha login does not enforce whitelisting, direct is faster and more reliable
        # session.proxies.update(account_proxies)
        # logger.info("auto_login: using proxy for %s (api_key=%s…)", user_id, api_key[:8])

        try:
            # Step 1: Login with user_id + password
            login_resp = session.post(
                "https://kite.zerodha.com/api/login",
                data={"user_id": user_id, "password": password},
                timeout=25,
            )
            login_data = login_resp.json()

            if login_data.get("status") != "success":
                logger.error(
                    "Login failed for %s: %s",
                    user_id,
                    login_data.get("message", "Unknown error"),
                )
                return False

            request_id = login_data["data"]["request_id"]
            logger.info("Login step 1 passed for %s", user_id)

            # Step 2: Generate TOTP
            totp = pyotp.TOTP(totp_secret)
            otp_value = totp.now()

            # Step 3: Submit 2FA
            twofa_resp = session.post(
                "https://kite.zerodha.com/api/twofa",
                data={
                    "request_id": request_id,
                    "twofa_value": otp_value,
                    "user_id": user_id,
                    "twofa_type": "totp",
                },
                timeout=25,
            )
            twofa_data = twofa_resp.json()

            if twofa_data.get("status") != "success":
                logger.error(
                    "2FA failed for %s: %s",
                    user_id,
                    twofa_data.get("message", "Unknown error"),
                )
                return False

            logger.info("2FA passed for %s", user_id)

            # Step 4: Visit Kite Connect login URL to capture request_token.
            # We follow redirects manually hop-by-hop because the final redirect target
            # (the app's redirect URL, e.g. localhost or custom domain) might not be
            # reachable from the server, causing requests to raise a ConnectionError
            # if we allow automatic redirects.
            current_url = f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"
            request_token = None

            for hop in range(1, 11):
                logger.info("Auto-login hop %d: %s", hop, current_url)

                # Check if current_url already contains the request_token BEFORE requesting it
                parsed = urlparse(current_url)
                query_params = parse_qs(parsed.query)
                token = query_params.get("request_token", [None])[0]
                if token:
                    request_token = token
                    break

                try:
                    resp = session.get(current_url, allow_redirects=False, timeout=25)
                except http_requests.exceptions.RequestException as exc:
                    logger.warning("Network error/unreachable during redirect hop %d: %s", hop, exc)
                    break

                location = resp.headers.get("Location")
                if not location:
                    break

                # Resolve relative redirect URLs
                if location.startswith("/"):
                    parsed_prev = urlparse(current_url)
                    location = f"{parsed_prev.scheme}://{parsed_prev.netloc}{location}"

                current_url = location

                # If target URL is not a Zerodha domain (e.g. localhost or client app domain),
                # do not actually request it (it may not be reachable or could trigger external side-effects),
                # and we've already checked if it has the request_token.
                parsed_loc = urlparse(current_url)
                if parsed_loc.netloc and not parsed_loc.netloc.endswith(".zerodha.com") and "zerodha.com" not in parsed_loc.netloc:
                    logger.info("Reached non-Zerodha redirect target: %s", current_url)
                    # We will extract the token at the start of the next iteration
                    pass

            if not request_token:
                logger.error("Could not extract request_token from redirect chain for %s", user_id)
                return False

            logger.info("Captured request_token for %s", user_id)

            # Step 5: Exchange request_token for access_token
            return self.complete_login(api_key, request_token)

        except Exception as exc:
            logger.error(
                "Auto-login failed for %s (api_key=%s…): %s",
                user_id,
                api_key[:8],
                exc,
                exc_info=True,
            )
            return False

    # Maximum quantity per single order leg.  Zerodha rejects NFO orders
    # above the exchange-defined freeze quantity (e.g. 1800 for NIFTY).
    # We use a slightly conservative default so the split fires before the
    # exchange-level rejection.
    FREEZE_QTY_LIMIT: int = 1755

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
        """Place an order for a specific account.

        If *quantity* exceeds ``FREEZE_QTY_LIMIT`` the order is automatically
        split into multiple legs of at most ``FREEZE_QTY_LIMIT`` each.

        Args:
            api_key: The Kite Connect API key.
            tradingsymbol: The symbol to trade (e.g. INFY, NIFTY2662325000CE).
            exchange: The exchange (e.g. NSE, NFO).
            transaction_type: BUY or SELL.
            quantity: Total order quantity (will be split if necessary).
            order_type: MARKET, LIMIT, SL, SL-M.
            price: Order price (required for LIMIT/SL orders).
            trigger_price: Trigger price (required for SL/SL-M orders).
            product: MIS, NRML, CNC.

        Returns:
            A list of order IDs (one per leg).
        """
        kite = self._clients.get(api_key)
        if not kite:
            raise ValueError(f"Account not found for api_key={api_key[:8]}…")

        if not self.is_authenticated(api_key):
            raise RuntimeError(
                f"Account '{self._account_names.get(api_key, api_key)}' is not authenticated."
            )

        # Normalize parameters (uppercase strings)
        transaction_type = transaction_type.upper()
        order_type = order_type.upper()
        product = product.upper()
        exchange = exchange.upper()
        tradingsymbol = tradingsymbol.upper()

        # Split quantity into legs of at most FREEZE_QTY_LIMIT
        legs: list[int] = []
        remaining = quantity
        while remaining > 0:
            leg_qty = min(remaining, self.FREEZE_QTY_LIMIT)
            legs.append(leg_qty)
            remaining -= leg_qty

        total_legs = len(legs)
        if total_legs > 1:
            logger.info(
                "Splitting %s %s order for %d %s into %d legs: %s (freeze limit=%d)",
                order_type, transaction_type, quantity, tradingsymbol,
                total_legs, legs, self.FREEZE_QTY_LIMIT,
            )

        order_ids: list[str] = []
        for i, leg_qty in enumerate(legs):
            # Build order parameters
            params = {
                "variety": kite.VARIETY_REGULAR,
                "exchange": exchange,
                "tradingsymbol": tradingsymbol,
                "transaction_type": transaction_type,
                "quantity": leg_qty,
                "product": product,
                "order_type": order_type,
                "validity": kite.VALIDITY_DAY,
            }

            if order_type == "MARKET":
                params["market_protection"] = 5.0

            if price is not None and price > 0:
                params["price"] = price
            if trigger_price is not None and trigger_price > 0:
                params["trigger_price"] = trigger_price

            leg_desc = f" (leg {i + 1}/{total_legs})" if total_legs > 1 else ""
            logger.info(
                "Placing %s %s order for %d %s on %s (%s) for account '%s'%s",
                order_type,
                transaction_type,
                leg_qty,
                tradingsymbol,
                exchange,
                product,
                self._account_names.get(api_key, api_key),
                leg_desc,
            )

            try:
                order_id = kite.place_order(**params)
                order_ids.append(str(order_id))
            except Exception as exc:
                logger.error(
                    "Failed to place order%s for api_key=%s…: %s",
                    leg_desc, api_key[:8], exc,
                )
                raise RuntimeError(
                    f"Failed to place order{leg_desc}: {exc}"
                    + (f" ({len(order_ids)}/{total_legs} legs placed successfully)" if total_legs > 1 else "")
                ) from exc

        return order_ids

    def exit_positions(
        self,
        api_key: str,
        tradingsymbol: str | None = None,
        price: float | None = None,
    ) -> list[dict[str, Any]]:
        """Exit/square off open positions for a specific account.

        Args:
            api_key: The Kite Connect API key.
            tradingsymbol: If specified, only square off positions matching this symbol.
                           If None, square off all open positions.
            price: If specified, place limit orders at this price. If None, place market orders.

        Returns:
            A list of details of placed exit orders:
            [{'tradingsymbol': str, 'quantity': int, 'product': str, 'order_id': str}]
        """
        kite = self._clients.get(api_key)
        if not kite:
            raise ValueError(f"Account not found for api_key={api_key[:8]}…")

        if not self.is_authenticated(api_key):
            raise RuntimeError(f"Account '{self._account_names.get(api_key, api_key)}' is not authenticated.")

        # Normalize target symbol
        if tradingsymbol:
            tradingsymbol = tradingsymbol.upper()

        # 1. Fetch current positions
        try:
            positions_data = kite.positions()
            net_positions = positions_data.get("net", [])
        except Exception as exc:
            logger.error("Failed to fetch positions during exit for api_key=%s…", api_key[:8])
            raise RuntimeError(f"Failed to fetch positions: {exc}") from exc

        orders_placed = []

        # 2. Iterate and square off open positions
        for pos in net_positions:
            qty = pos.get("quantity", 0)
            if qty == 0:
                continue

            symbol = pos.get("tradingsymbol", "")
            if tradingsymbol and symbol != tradingsymbol:
                # If a specific symbol was requested, skip others
                continue

            # Square-off logic:
            # - If long (qty > 0): SELL
            # - If short (qty < 0): BUY
            transaction_type = kite.TRANSACTION_TYPE_SELL if qty > 0 else kite.TRANSACTION_TYPE_BUY
            exit_qty = abs(qty)
            product = pos.get("product", "")
            exchange = pos.get("exchange", "")

            logger.info(
                "Square off position for account '%s': %s %d %s (%s)",
                self._account_names.get(api_key, api_key),
                transaction_type,
                exit_qty,
                symbol,
                product,
            )

            try:
                order_ids = self.place_order(
                    api_key=api_key,
                    tradingsymbol=symbol,
                    exchange=exchange,
                    transaction_type=transaction_type,
                    quantity=exit_qty,
                    order_type=kite.ORDER_TYPE_LIMIT if price is not None else kite.ORDER_TYPE_MARKET,
                    price=price,
                    product=product,
                )
                for order_id in order_ids:
                    orders_placed.append({
                        "tradingsymbol": symbol,
                        "quantity": exit_qty,
                        "product": product,
                        "order_id": order_id
                    })
            except Exception as exc:
                logger.error(
                    "Failed to place exit order for %s in account %s: %s",
                    symbol,
                    self._account_names.get(api_key, api_key),
                    exc,
                )
                # Raise an error only if the targeted symbol exit failed completely
                if tradingsymbol:
                    raise RuntimeError(f"Failed to place exit order for {symbol}: {exc}") from exc


        return orders_placed

    def modify_order(
        self,
        api_key: str,
        order_id: str,
        quantity: int | None = None,
        price: float | None = None,
        order_type: str | None = None,
        trigger_price: float | None = None,
    ) -> str:
        """Modify an open order.

        Returns:
            The order ID.
        """
        kite = self._clients.get(api_key)
        if not kite:
            raise ValueError(f"Account not found for api_key={api_key[:8]}…")

        if not self.is_authenticated(api_key):
            raise RuntimeError(f"Account '{self._account_names.get(api_key, api_key)}' is not authenticated.")

        params = {
            "variety": kite.VARIETY_REGULAR,
            "order_id": order_id,
        }
        if quantity is not None:
            params["quantity"] = quantity
        if price is not None:
            params["price"] = price
        if order_type is not None:
            norm_type = order_type.upper()
            params["order_type"] = norm_type
            if norm_type == "MARKET":
                params["market_protection"] = 5.0
        if trigger_price is not None:
            params["trigger_price"] = trigger_price

        try:
            return kite.modify_order(**params)
        except Exception as exc:
            logger.error("Failed to modify order %s for api_key=%s…", order_id, api_key[:8])
            raise RuntimeError(f"Failed to modify order: {exc}") from exc

    def cancel_order(
        self,
        api_key: str,
        order_id: str,
    ) -> str:
        """Cancel an open order.

        Returns:
            The order ID.
        """
        kite = self._clients.get(api_key)
        if not kite:
            raise ValueError(f"Account not found for api_key={api_key[:8]}…")

        if not self.is_authenticated(api_key):
            raise RuntimeError(f"Account '{self._account_names.get(api_key, api_key)}' is not authenticated.")

        try:
            return kite.cancel_order(
                variety=kite.VARIETY_REGULAR,
                order_id=order_id,
            )
        except Exception as exc:
            logger.error("Failed to cancel order %s for api_key=%s…", order_id, api_key[:8])
            raise RuntimeError(f"Failed to cancel order: {exc}") from exc

    def get_option_chain(
        self,
        api_key: str,
        underlying: str,
        expiry_week: int = 0,
        expiry_date: str | None = None,
    ) -> dict:
        """Fetch option chain for a specific underlying and expiry week.

        Uses kite.instruments("NFO") to get the full instrument list, then
        filters locally by the underlying name and the requested expiry.

        Args:
            api_key: Any one authenticated account's api_key.
            underlying: Underlying index/stock name (e.g. NIFTY, BANKNIFTY).
            expiry_week: 0 = current/nearest weekly expiry, 1 = next week, etc.
            expiry_date: ISO date string (YYYY-MM-DD) to target a specific expiry.
                         If provided, expiry_week is ignored.

        Returns:
            Dict with keys: underlying, expiry, strikes, available_expiries, status, message.
        """
        import datetime

        kite = self._clients.get(api_key)
        if not kite:
            raise ValueError(f"Account not found for api_key={api_key[:8]}…")

        if not self.is_authenticated(api_key):
            raise RuntimeError(
                f"Account '{self._account_names.get(api_key, api_key)}' is not authenticated."
            )

        underlying = underlying.upper().strip()
        logger.info("Fetching NFO instruments for option chain (%s)…", underlying)

        # Fetch full NFO instrument list
        instruments = kite.instruments("NFO")

        # Filter to options (CE/PE) for the requested underlying
        options = [
            inst for inst in instruments
            if inst.get("name", "").upper() == underlying
            and inst.get("instrument_type") in ("CE", "PE")
        ]

        if not options:
            return {
                "underlying": underlying,
                "expiry": "",
                "strikes": [],
                "available_expiries": [],
                "status": "error",
                "message": f"No options found for underlying '{underlying}' in NFO. "
                           f"Check the name (e.g. NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY).",
            }

        # Collect all unique expiry dates (sorted ascending)
        today = datetime.date.today()
        all_expiries = sorted(
            set(
                inst["expiry"] for inst in options
                if isinstance(inst.get("expiry"), datetime.date) and inst["expiry"] >= today
            )
        )

        if not all_expiries:
            return {
                "underlying": underlying,
                "expiry": "",
                "strikes": [],
                "available_expiries": [],
                "status": "error",
                "message": "No active expiry dates found.",
            }

        # Build available_expiries labels
        available_expiries = []
        for i, exp in enumerate(all_expiries):
            if i == 0:
                label = "Current Week"
            elif i == 1:
                label = "Next Week"
            else:
                label = f"Week +{i}"
            available_expiries.append({"expiry": exp.isoformat(), "week_label": label})

        # Resolve target expiry
        if expiry_date:
            try:
                target_expiry = datetime.date.fromisoformat(expiry_date)
            except ValueError:
                return {
                    "underlying": underlying,
                    "expiry": "",
                    "strikes": [],
                    "available_expiries": available_expiries,
                    "status": "error",
                    "message": f"Invalid expiry_date format '{expiry_date}'. Use YYYY-MM-DD.",
                }
            if target_expiry not in all_expiries:
                return {
                    "underlying": underlying,
                    "expiry": expiry_date,
                    "strikes": [],
                    "available_expiries": available_expiries,
                    "status": "error",
                    "message": f"Expiry date '{expiry_date}' not found for {underlying}.",
                }
        else:
            idx = min(expiry_week, len(all_expiries) - 1)
            target_expiry = all_expiries[idx]

        # Filter to selected expiry and group by strike
        strike_map: dict[float, dict] = {}
        for inst in options:
            if inst.get("expiry") != target_expiry:
                continue
            strike = float(inst.get("strike", 0))
            inst_type = inst.get("instrument_type")  # CE or PE
            tradingsymbol = inst.get("tradingsymbol", "")
            lot_size = int(inst.get("lot_size", 0))

            if strike not in strike_map:
                strike_map[strike] = {"strike": strike}

            if inst_type == "CE":
                strike_map[strike]["ce_symbol"] = tradingsymbol
                strike_map[strike]["ce_lot_size"] = lot_size
            elif inst_type == "PE":
                strike_map[strike]["pe_symbol"] = tradingsymbol
                strike_map[strike]["pe_lot_size"] = lot_size

        strikes = sorted(strike_map.values(), key=lambda x: x["strike"])

        # Batch-fetch LTP for all option symbols in one call
        all_symbols = []
        for s in strikes:
            if s.get("ce_symbol"):
                all_symbols.append(f"NFO:{s['ce_symbol']}")
            if s.get("pe_symbol"):
                all_symbols.append(f"NFO:{s['pe_symbol']}")

        ltp_data: dict = {}
        if all_symbols:
            try:
                # kite.ltp() accepts up to 500 symbols at once
                for i in range(0, len(all_symbols), 500):
                    chunk = all_symbols[i : i + 500]
                    ltp_data.update(kite.ltp(chunk))
            except Exception as exc:
                logger.warning("Failed to fetch LTP for option chain: %s", exc)

        # Enrich each strike entry with live LTP and instrument tokens. The
        # tokens let the live TUI subscribe to these options on the WebSocket so
        # the option-chain LTPs can stream rather than being a one-shot snapshot.
        for s in strikes:
            ce_key = f"NFO:{s.get('ce_symbol', '')}"
            pe_key = f"NFO:{s.get('pe_symbol', '')}"
            if ce_key in ltp_data:
                s["ce_ltp"] = ltp_data[ce_key].get("last_price")
                s["ce_token"] = ltp_data[ce_key].get("instrument_token")
            if pe_key in ltp_data:
                s["pe_ltp"] = ltp_data[pe_key].get("last_price")
                s["pe_token"] = ltp_data[pe_key].get("instrument_token")

        logger.info(
            "Option chain: %s, expiry=%s, strikes=%d",
            underlying,
            target_expiry.isoformat(),
            len(strikes),
        )

        return {
            "underlying": underlying,
            "expiry": target_expiry.isoformat(),
            "strikes": strikes,
            "available_expiries": available_expiries,
            "status": "success",
            "message": "",
        }

    def get_orders(self, api_key: str) -> list[dict]:
        """Fetch today's order book for a specific authenticated account.

        Args:
            api_key: The API key of the account to query.

        Returns:
            List of order dicts as returned by KiteConnect.orders().
        """
        kite = self._clients.get(api_key)
        if not kite:
            raise ValueError(f"Account not found for api_key={api_key[:8]}…")
        if not self.is_authenticated(api_key):
            raise RuntimeError(
                f"Account '{self._account_names.get(api_key, api_key)}' is not authenticated."
            )
        return kite.orders()

    def get_margins(self, api_key: str) -> dict[str, Any]:
        """Fetch equity margin summary for an account.

        Returns a dict with:
          - ``net``   — total available buying power after SPAN/exposure blocked
                        for open F&O positions (``equity.net``).
          - ``cash``  — current available cash balance (``equity.available.live_balance``).
                        This differs from ``available.cash`` / ``available.opening_balance``
                        which reflect the ledger balance at day start, not the live figure.

        Both values are ``None`` if the account is not authenticated or the
        call fails (callers should treat ``None`` as "unavailable").
        """
        kite = self._clients.get(api_key)
        if not kite or not self.is_authenticated(api_key):
            return {"net": None, "cash": None}
        try:
            data = kite.margins(segment="equity")
            net = data.get("net")
            # live_balance is the real-time available cash, not the stale opening_balance
            cash = data.get("available", {}).get("live_balance")
            collateral = data.get("available", {}).get("collateral")
            return {"net": net, "cash": cash, "collateral": collateral}
        except Exception as exc:
            logger.warning("get_margins failed for api_key=%s…: %s", api_key[:8], exc)
            return {"net": None, "cash": None, "collateral": None}

    def get_nfo_lot_sizes(self) -> dict[str, int]:
        """Return the cached NFO tradingsymbol → lot_size map.

        Fetches from Zerodha on the first call, then returns the in-memory
        cache on all subsequent calls — so live_session can call this freely
        at every REST refresh without triggering repeated API requests.

        Returns an empty dict if no authenticated account is available or the
        initial fetch fails.
        """
        if self._nfo_lot_size_cache:
            return self._nfo_lot_size_cache
        for api_key in self.get_all_api_keys():
            if self.is_authenticated(api_key):
                kite = self._clients.get(api_key)
                if kite:
                    try:
                        instruments = kite.instruments("NFO")
                        self._nfo_lot_size_cache = {
                            inst["tradingsymbol"]: int(inst.get("lot_size", 1) or 1)
                            for inst in instruments
                            if inst.get("tradingsymbol")
                        }
                        return self._nfo_lot_size_cache
                    except Exception as exc:
                        logger.warning("get_nfo_lot_sizes failed: %s", exc)
                        return {}
        return {}

    def get_market_indices(self) -> dict[str, Any]:
        # 1. Try Kite ohlc first for last_price and yesterday's close
        for api_key in self.get_all_api_keys():
            if self.is_authenticated(api_key):
                kite = self._clients.get(api_key)
                if kite:
                    try:
                        data = kite.ohlc(["NSE:NIFTY 50", "BSE:SENSEX", "NSE:INDIA VIX"])
                        
                        def parse_item(item_data):
                            if not isinstance(item_data, dict):
                                return None, None
                            last = item_data.get("last_price")
                            ohlc = item_data.get("ohlc", {})
                            close = ohlc.get("close") if isinstance(ohlc, dict) else None
                            change = (last - close) if (last is not None and close is not None) else None
                            return last, change

                        nifty_last, nifty_change = parse_item(data.get("NSE:NIFTY 50"))
                        sensex_last, sensex_change = parse_item(data.get("BSE:SENSEX"))
                        vix_last, vix_change = parse_item(data.get("NSE:INDIA VIX"))

                        if nifty_last or sensex_last or vix_last:
                            return {
                                "status": "success",
                                "nifty": nifty_last,
                                "nifty_change": nifty_change,
                                "sensex": sensex_last,
                                "sensex_change": sensex_change,
                                "vix": vix_last,
                                "vix_change": vix_change,
                            }
                    except Exception as exc:
                        logger.warning("Kite indices fetch failed for api_key=%s…: %s", api_key[:8], exc)

        return {"status": "error", "message": "All authenticated Zerodha account indices fetches failed."}

    def get_ltp_and_tokens(self, api_key: str, symbols: list[str]) -> dict:
        """Fetch LTP and instrument tokens for the given symbols using Zerodha ltp()."""
        kite = self._clients.get(api_key)
        if not kite:
            return {}
        try:
            prefixed = []
            for s in symbols:
                if ":" not in s:
                    if any(x in s for x in ["CE", "PE"]):
                        prefixed.append(f"NFO:{s}")
                    else:
                        prefixed.append(f"NSE:{s}")
                else:
                    prefixed.append(s)
            res = kite.ltp(prefixed)
            normalized = {}
            for k, v in res.items():
                sym = k.split(":")[-1]
                normalized[sym] = {
                    "instrument_token": v.get("instrument_token"),
                    "last_price": v.get("last_price"),
                }
            return normalized
        except Exception as exc:
            logger.warning("Failed to fetch LTP/tokens from Zerodha: %s", exc)
            return {}

    def get_order_margin(
        self,
        account_key: str,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
        price: float | None = None,
        product: str = "NRML",
        exchange: str = "NFO",
        order_type: str = "LIMIT",
    ) -> dict[str, Any]:
        """Calculate margin required using Zerodha kite.order_margins()."""
        kite = self._clients.get(account_key)
        if not kite or not self.is_authenticated(account_key):
            return {"status": "error", "message": "Account not authenticated"}

        try:
            p_val = price if price is not None else 0.0
            order_param = {
                "exchange": exchange,
                "tradingsymbol": tradingsymbol,
                "transaction_type": transaction_type.upper(),
                "variety": "regular",
                "product": product.upper(),
                "order_type": order_type.upper() if price is not None else "MARKET",
                "quantity": abs(quantity),
                "price": p_val,
                "trigger_price": 0,
            }

            margin_resp = kite.order_margins([order_param])
            if isinstance(margin_resp, list) and len(margin_resp) > 0:
                m_info = margin_resp[0]
                total_margin = m_info.get("total")
                if total_margin is None:
                    total_margin = m_info.get("margin_required", 0.0)
                return {
                    "status": "success",
                    "total": float(total_margin),
                    "span": float(m_info.get("span", 0.0)),
                    "exposure": float(m_info.get("exposure", 0.0)),
                    "option_premium": float(m_info.get("option_premium", 0.0)),
                    "detail": m_info,
                }
            return {"status": "error", "message": "Empty margin response from Zerodha"}
        except Exception as exc:
            logger.warning("Zerodha order_margins fetch failed for %s: %s", tradingsymbol, exc)
            if price and price > 0 and transaction_type.upper() == "BUY":
                return {
                    "status": "success",
                    "total": round(abs(quantity) * price, 2),
                    "is_estimated": True,
                }
            return {"status": "error", "message": str(exc)}

