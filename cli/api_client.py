"""
Local KiteCLI client.

Wraps broker-specific account managers (KiteAccountManager for Zerodha,
KotakAccountManager for Kotak) via a unified registry.  Public API is
identical to the previous single-broker version so that cli/main.py and
cli/live_session.py require zero changes.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from cli.kite_manager import KiteAccountManager
from cli.config import remove_session

logger = logging.getLogger(__name__)


class KCLIClientError(Exception):
    """Raised when a broker API call fails."""


# Broker-specific singleton managers (shared across KCLIClient instances)
_kite_manager = KiteAccountManager()
# Kotak manager is imported lazily so that projects without neo-api-client
# installed still work fine as pure-Zerodha setups.
_kotak_manager = None  # type: ignore[assignment]


def _get_kotak_manager():
    """Return the KotakAccountManager singleton, importing it on first use."""
    global _kotak_manager
    if _kotak_manager is None:
        try:
            from cli.kotak_manager import KotakAccountManager
            _kotak_manager = KotakAccountManager()
        except ImportError as exc:
            raise ImportError(
                "Kotak Neo support requires the 'neo-api-client' package. "
                "Install it with: pip install \"git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.2#egg=neo_api_client\""
            ) from exc
    return _kotak_manager


# Module-level map: account_key → manager instance (populated in KCLIClient.__init__)
_account_manager_map: dict = {}


def _manager_for(account_key: str):
    """Return the broker manager responsible for the given account key."""
    return _account_manager_map.get(account_key, _kite_manager)


# Legacy compatibility alias used by live_session.py for WebSocket init
_manager = _kite_manager


class KCLIClient:
    """Local client that delegates to per-broker account managers.

    Args:
        accounts: List of account dicts from config.  Each account may
                  include a ``"broker"`` key (``"zerodha"`` or ``"kotak"``;
                  defaults to ``"zerodha"`` when omitted).  Accounts are
                  initialised eagerly in parallel so that session tokens are
                  restored before the first command runs.
    """

    def __init__(self, accounts: list[dict]) -> None:
        self._accounts = accounts
        self.accounts = accounts
        # Determine the account_key field per account (api_key for Zerodha,
        # consumer_key for Kotak).
        self._api_keys = []  # Zerodha api_keys (used by WebSocket layer)

        def init_one(acct):
            broker = acct.get("broker", "zerodha").lower()
            if broker == "kotak":
                mgr = _get_kotak_manager()
                account_key = acct.get("consumer_key", acct.get("api_key", ""))
                mgr.init_account_kotak(
                    consumer_key=account_key,
                    consumer_secret=acct.get("consumer_secret", ""),
                    mobile_number=acct.get("mobile_number", ""),
                    password=acct.get("password", ""),
                    mpin=acct.get("mpin", ""),
                    ucc=acct.get("ucc", ""),
                    name=acct.get("name", ""),
                    totp_secret=acct.get("totp_secret", ""),
                    proxy=acct.get("proxy"),
                )
                _account_manager_map[account_key] = mgr
                # Ensure api_key field is populated for downstream code
                acct.setdefault("api_key", account_key)
            else:
                # Default: Zerodha
                api_key = acct.get("api_key", "")
                _kite_manager.init_account_kite(
                    api_key=api_key,
                    api_secret=acct.get("api_secret", ""),
                    name=acct.get("name", ""),
                    proxy=acct.get("proxy"),
                )
                _account_manager_map[api_key] = _kite_manager
                self._api_keys.append(api_key)

        with ThreadPoolExecutor(max_workers=max(1, len(accounts))) as executor:
            list(executor.map(init_one, accounts))


    # ── compatibility helpers ──────────────────────────────────────

    def health_check(self) -> bool:
        """Always True — no server to ping in local mode."""
        return True

    # ── public API (mirrors old KCLIClient exactly, but runs in parallel) ──

    def init_accounts(self, accounts: list[dict]) -> dict:
        """Initialise (or re-initialise) accounts and attempt auto-login in parallel."""
        def init_one(acct):
            broker = acct.get("broker", "zerodha").lower()
            if broker == "kotak":
                mgr = _get_kotak_manager()
                account_key = acct.get("consumer_key", acct.get("api_key", ""))
                name = acct.get("name", account_key)
                mgr.init_account_kotak(
                    consumer_key=account_key,
                    consumer_secret=acct.get("consumer_secret", ""),
                    mobile_number=acct.get("mobile_number", ""),
                    password=acct.get("password", ""),
                    mpin=acct.get("mpin", ""),
                    ucc=acct.get("ucc", ""),
                    name=name,
                    totp_secret=acct.get("totp_secret", ""),
                    proxy=acct.get("proxy"),
                )
                _account_manager_map[account_key] = mgr
                acct.setdefault("api_key", account_key)

                # Validation: verify token with a REST query (limits)
                is_valid = False
                if mgr.is_authenticated(account_key):
                    try:
                        resp = mgr._clients[account_key].limits()
                        from cli.kotak_manager import _check_neo_error
                        _check_neo_error(resp)
                        is_valid = True
                    except Exception as exc:
                        msg = str(exc).lower()
                        is_auth_error = any(x in msg for x in ["100008", "100022", "1037", "unauthorized", "invalid token", "invalid session", "session expired", "session has been closed", "session closed"])
                        if is_auth_error:
                            logger.info("Kotak validation failed (expired/invalid) for %s: %s", name, exc)
                            mgr._authenticated[account_key] = False
                            remove_session(f"kotak:{account_key}")
                        else:
                            # Network/timeout error: fallback to assuming token is valid
                            logger.warning("Kotak validation query failed due to network/other issue. Assuming valid. Error: %s", exc)
                            is_valid = True

                # Attempt auto-login for Kotak
                if not is_valid:
                    success = mgr.auto_login(account_key)
                    return {
                        "name": name, "api_key": account_key,
                        "login_url": "",
                        "auto_logged_in": success,
                        "message": "Auto-login successful" if success else "Auto-login failed",
                    }
                return {
                    "name": name, "api_key": account_key,
                    "login_url": "", "auto_logged_in": True,
                    "message": "Session restored",
                }
            else:
                # Zerodha
                api_key = acct.get("api_key", "")
                name = acct.get("name", api_key)
                login_url = _kite_manager.init_account_kite(
                    api_key=api_key,
                    api_secret=acct.get("api_secret", ""),
                    name=name,
                    proxy=acct.get("proxy"),
                )
                _account_manager_map[api_key] = _kite_manager

                # Validation: verify token with a REST query (profile)
                is_valid = False
                if _kite_manager.is_authenticated(api_key):
                    try:
                        _kite_manager._clients[api_key].profile()
                        is_valid = True
                    except Exception as exc:
                        try:
                            from kiteconnect.exceptions import TokenException, PermissionException
                        except Exception:
                            TokenException = PermissionException = ()
                        msg = str(exc).lower()
                        is_auth_error = isinstance(exc, (TokenException, PermissionException)) or any(x in msg for x in ["403", "401", "unauthorized", "token", "session"])
                        if is_auth_error:
                            logger.info("Zerodha validation failed (expired/invalid) for %s: %s", name, exc)
                            _kite_manager._authenticated[api_key] = False
                            remove_session(api_key)
                        else:
                            # Network/timeout error: fallback to assuming token is valid
                            logger.warning("Zerodha validation query failed due to network/other issue. Assuming valid. Error: %s", exc)
                            is_valid = True

                if is_valid:
                    return {
                        "name": name, "api_key": api_key,
                        "login_url": login_url, "auto_logged_in": True,
                        "message": "Session restored from saved token",
                    }

                user_id = acct.get("user_id")
                password = acct.get("password")
                totp_secret = acct.get("totp_secret")
                if user_id and password and totp_secret:
                    success = _kite_manager.auto_login_kite(
                        api_key=api_key, user_id=user_id,
                        password=password, totp_secret=totp_secret,
                    )
                    return {
                        "name": name, "api_key": api_key,
                        "login_url": login_url, "auto_logged_in": success,
                        "message": "Auto-login successful" if success else "Auto-login failed. Use manual login URL.",
                    }
                return {
                    "name": name, "api_key": api_key,
                    "login_url": login_url, "auto_logged_in": False,
                    "message": "Credentials incomplete — manual login required.",
                }

        with ThreadPoolExecutor(max_workers=max(1, len(accounts))) as executor:
            results = list(executor.map(init_one, accounts))

        return {"accounts": results}


    def is_authenticated(self, api_key: str) -> bool:
        """Check if the given account is authenticated."""
        return _manager_for(api_key).is_authenticated(api_key)

    def login(self, api_key: str, request_token: str) -> dict:
        """Complete Kite OAuth login with a request token (Zerodha only)."""
        try:
            success = _kite_manager.complete_login(api_key, request_token.strip())
            if success:
                return {"status": "success", "message": "Login successful"}
            return {"status": "error", "message": "Login failed — check request_token"}
        except Exception as exc:
            raise KCLIClientError(str(exc)) from exc

    def complete_callback(self, api_key: str, request_token: str) -> dict:
        """Complete Kite OAuth login with a request token (Zerodha only)."""
        try:
            success = _kite_manager.complete_login(api_key, request_token.strip())
            if success:
                return {"status": "success", "message": "Login successful"}
            return {"status": "error", "message": "Login failed — check request_token"}
        except Exception as exc:
            raise KCLIClientError(str(exc)) from exc

    def get_order_margin(
        self,
        api_key: str,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
        price: float | None = None,
        product: str = "NRML",
        exchange: str = "NFO",
        order_type: str = "LIMIT",
    ) -> dict:
        """Calculate margin required for a proposed order on a specific account."""
        mgr = _manager_for(api_key)
        return mgr.get_order_margin(
            account_key=api_key,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            price=price,
            product=product,
            exchange=exchange,
            order_type=order_type,
        )

    def get_positions(self, api_keys: list[str]) -> dict:
        """Fetch open positions for the given accounts in parallel."""
        keys = api_keys or [a.get("api_key") for a in self._accounts if a.get("api_key")]

        def fetch_one(api_key):
            mgr = _manager_for(api_key)
            info = mgr.get_account_info(api_key)
            if not info.get("authenticated"):
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "positions": [],
                    "total_pnl": 0.0,
                    "status": "unauthenticated",
                }
            try:
                positions = mgr.get_positions(api_key)
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "positions": positions,
                    "total_pnl": 0.0,
                    "status": "success",
                }
            except Exception as exc:
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "positions": [],
                    "total_pnl": 0.0,
                    "status": f"error: {exc}",
                }

        with ThreadPoolExecutor(max_workers=max(1, len(keys))) as executor:
            results = list(executor.map(fetch_one, keys))

        # Check if there are any positions needing instrument_token/LTP resolution
        symbols_to_resolve = set()
        for res in results:
            if res.get("status") == "success":
                for pos in res.get("positions", []):
                    if not pos.get("instrument_token") and pos.get("tradingsymbol"):
                        symbols_to_resolve.add(pos["tradingsymbol"])

        if symbols_to_resolve:
            # Find the first available authenticated Zerodha account
            zerodha_api_key = None
            for key in self._api_keys:
                if self.is_authenticated(key):
                    zerodha_api_key = key
                    break

            if zerodha_api_key:
                try:
                    ltp_map = self.get_ltp_and_tokens(zerodha_api_key, list(symbols_to_resolve))
                    if ltp_map:
                        for res in results:
                            if res.get("status") == "success":
                                for pos in res.get("positions", []):
                                    if not pos.get("instrument_token") and pos.get("tradingsymbol"):
                                        sym = pos["tradingsymbol"]
                                        if sym in ltp_map:
                                            pos["instrument_token"] = ltp_map[sym].get("instrument_token")
                                            ltp = ltp_map[sym].get("last_price")
                                            if ltp is not None:
                                                pos["last_price"] = ltp
                                                qty = pos.get("quantity", 0)
                                                avg = pos.get("average_price", 0.0)
                                                pnl = (ltp - avg) * qty if qty != 0 else pos.get("realised", 0.0)
                                                pos["pnl"] = pnl
                                                pos["unrealised"] = (ltp - avg) * qty if qty != 0 else 0.0
                                                if avg > 0 and qty != 0:
                                                    pos["pnl_pct"] = (pnl / (avg * abs(qty))) * 100
                                                else:
                                                    pos["pnl_pct"] = 0.0
                except Exception:
                    pass

        # Calculate final total_pnl for each account
        for res in results:
            if res.get("status") == "success":
                res["total_pnl"] = sum(p.get("pnl", 0.0) for p in res.get("positions", []))

        return {"accounts": results}

    def get_status(self) -> dict:
        """Get authentication status for all accounts."""
        accounts = []
        for acct in self._accounts:
            key = acct.get("api_key")
            if key:
                accounts.append(_manager_for(key).get_account_info(key))
        return {"accounts": accounts}

    def place_order(
        self,
        api_keys: list[str],
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        order_type: str,
        price: float | None = None,
        trigger_price: float | None = None,
        product: str = "NRML",
    ) -> dict:
        """Place an order across specified accounts in parallel."""
        keys = api_keys or [a.get("api_key") for a in self._accounts if a.get("api_key")]

        def place_one(api_key):
            mgr = _manager_for(api_key)
            info = mgr.get_account_info(api_key)
            if not info.get("authenticated"):
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "status": "error",
                    "order_id": None,
                    "message": "Account not authenticated",
                }
            try:
                order_ids = mgr.place_order(
                    api_key=api_key,
                    tradingsymbol=tradingsymbol,
                    exchange=exchange,
                    transaction_type=transaction_type,
                    quantity=quantity,
                    order_type=order_type,
                    price=price,
                    trigger_price=trigger_price,
                    product=product,
                )
                ids_str = ", ".join(order_ids)
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "status": "success",
                    "order_id": ids_str,
                    "order_ids": order_ids,
                    "legs": len(order_ids),
                    "message": f"Order placed: {ids_str}" if len(order_ids) == 1 else f"Order split into {len(order_ids)} legs: {ids_str}",
                }
            except Exception as exc:
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "status": "error",
                    "order_id": None,
                    "message": str(exc),
                }

        with ThreadPoolExecutor(max_workers=max(1, len(keys))) as executor:
            results = list(executor.map(place_one, keys))

        return {"results": results}

    def exit_positions(
        self,
        api_keys: list[str],
        tradingsymbol: str | None = None,
        price: float | None = None,
    ) -> dict:
        """Exit positions across specified accounts in parallel."""
        keys = api_keys or [a.get("api_key") for a in self._accounts if a.get("api_key")]

        def exit_one(api_key):
            mgr = _manager_for(api_key)
            info = mgr.get_account_info(api_key)
            if not info.get("authenticated"):
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "status": "error",
                    "message": "Account not authenticated",
                    "orders_placed": [],
                }
            try:
                orders = mgr.exit_positions(api_key, tradingsymbol, price)
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "status": "success",
                    "message": f"Exited {len(orders)} position(s)",
                    "orders_placed": orders,
                }
            except Exception as exc:
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "status": "error",
                    "message": str(exc),
                    "orders_placed": [],
                }

        with ThreadPoolExecutor(max_workers=max(1, len(keys))) as executor:
            results = list(executor.map(exit_one, keys))

        return {"results": results}

    def get_option_chain(
        self,
        api_key: str,
        underlying: str,
        expiry_week: int = 0,
        expiry_date: str | None = None,
    ) -> dict:
        """Fetch option chain for a specific underlying and expiry."""
        try:
            return _manager.get_option_chain(
                api_key=api_key,
                underlying=underlying,
                expiry_week=expiry_week,
                expiry_date=expiry_date,
            )
        except Exception as exc:
            raise KCLIClientError(str(exc)) from exc

    def get_orders(self, api_keys: list[str]) -> dict:
        """Fetch today's order book for specified accounts in parallel."""
        keys = api_keys or [a.get("api_key") for a in self._accounts if a.get("api_key")]

        def fetch_one(api_key):
            mgr = _manager_for(api_key)
            info = mgr.get_account_info(api_key)
            if not info.get("authenticated"):
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "orders": [],
                    "status": "unauthenticated",
                }
            try:
                orders = mgr.get_orders(api_key)
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "orders": orders,
                    "status": "success",
                }
            except Exception as exc:
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "orders": [],
                    "status": f"error: {exc}",
                }

        with ThreadPoolExecutor(max_workers=max(1, len(keys))) as executor:
            results = list(executor.map(fetch_one, keys))

        return {"accounts": results}

    def modify_order(
        self,
        api_key: str,
        order_id: str,
        quantity: int | None = None,
        price: float | None = None,
        order_type: str | None = None,
        trigger_price: float | None = None,
    ) -> dict:
        """Modify an order on a specific account."""
        try:
            mgr = _manager_for(api_key)
            res_id = mgr.modify_order(
                api_key=api_key,
                order_id=order_id,
                quantity=quantity,
                price=price,
                order_type=order_type,
                trigger_price=trigger_price,
            )
            return {"status": "success", "order_id": res_id, "message": f"Order modified: {res_id}"}
        except Exception as exc:
            return {"status": "error", "order_id": None, "message": str(exc)}

    def cancel_order(
        self,
        api_key: str,
        order_id: str,
    ) -> dict:
        """Cancel an order on a specific account."""
        try:
            mgr = _manager_for(api_key)
            res_id = mgr.cancel_order(api_key=api_key, order_id=order_id)
            return {"status": "success", "order_id": res_id, "message": f"Order cancelled: {res_id}"}
        except Exception as exc:
            return {"status": "error", "order_id": None, "message": str(exc)}

    def get_nfo_lot_sizes(self) -> dict:
        """Fetch NFO tradingsymbol → lot_size map (one-shot, cache at startup)."""
        return _kite_manager.get_nfo_lot_sizes()

    def get_market_indices(self) -> dict:
        """Fetch live Nifty, Sensex, and India VIX."""
        return _kite_manager.get_market_indices()

    def get_margins(self, api_keys: list[str]) -> dict:
        """Fetch equity margin summary for each account in parallel."""
        keys = api_keys or [a.get("api_key") for a in self._accounts if a.get("api_key")]

        def fetch_one(api_key):
            mgr = _manager_for(api_key)
            result = mgr.get_margins(api_key)
            return {
                "api_key": api_key,
                "net": result["net"],
                "cash": result["cash"],
                "collateral": result.get("collateral")
            }

        with ThreadPoolExecutor(max_workers=max(1, len(keys))) as executor:
            results = list(executor.map(fetch_one, keys))

        return {"accounts": results}

    def get_access_token(self, api_key: str) -> str | None:
        """Get the access token for an authenticated account."""
        return _manager_for(api_key).get_access_token(api_key)

    def create_ticker(self, api_key: str):
        """Create a WebSocket ticker for the broker associated with api_key."""
        mgr = _manager_for(api_key)
        if hasattr(mgr, "create_ticker"):
            return mgr.create_ticker(api_key)
        return None

    def check_token(self, api_key: str) -> dict:
        """Validate an account's access token. Returns status/detail dict."""
        # check_token is Zerodha-specific (kite.profile()). For non-Zerodha accounts
        # return a simplified status based on is_authenticated.
        mgr = _manager_for(api_key)
        if mgr.broker_name == "zerodha":
            return _kite_manager.check_token(api_key)
        # Generic fallback for other brokers
        authenticated = mgr.is_authenticated(api_key)
        info = mgr.get_account_info(api_key)
        return {
            "name": info.get("name", api_key),
            "api_key": api_key,
            "status": "valid" if authenticated else "no_token",
            "detail": "authenticated" if authenticated else "not authenticated",
        }

    def get_ltp_and_tokens(self, api_key: str, symbols: list[str]) -> dict:
        """Fetch LTP and instrument tokens for the given symbols using the designated manager."""
        mgr = _manager_for(api_key)
        if hasattr(mgr, "get_ltp_and_tokens"):
            return mgr.get_ltp_and_tokens(api_key, symbols)
        return {}

