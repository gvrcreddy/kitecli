"""
Local KiteCLI client.

Wraps broker-specific account managers (KiteAccountManager for Zerodha,
KotakAccountManager for Kotak) via a unified registry.  Public API is
identical to the previous single-broker version so that cli/main.py and
cli/live_session.py require zero changes.
"""

from concurrent.futures import ThreadPoolExecutor
from cli.kite_manager import KiteAccountManager


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
                "Install it with: pip install neo-api-client"
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
                )
                _account_manager_map[account_key] = mgr
                acct.setdefault("api_key", account_key)
                # Attempt auto-login for Kotak
                if not mgr.is_authenticated(account_key):
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

                if _kite_manager.is_authenticated(api_key):
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

    def get_positions(self, api_keys: list[str]) -> dict:
        """Fetch open positions for the given accounts in parallel."""
        keys = api_keys or _kite_manager.get_all_api_keys()

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
                total_pnl = sum(p.get("pnl", 0.0) for p in positions)
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "positions": positions,
                    "total_pnl": total_pnl,
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

        return {"accounts": results}

    def get_status(self) -> dict:
        """Get authentication status for all accounts."""
        accounts = []
        for api_key in _kite_manager.get_all_api_keys():
            accounts.append(_kite_manager.get_account_info(api_key))
        if _kotak_manager is not None:
            for key in _kotak_manager.get_all_account_keys():
                accounts.append(_kotak_manager.get_account_info(key))
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
        keys = api_keys or _kite_manager.get_all_api_keys()

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
        keys = api_keys or _kite_manager.get_all_api_keys()

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
        keys = api_keys or _kite_manager.get_all_api_keys()

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
        keys = api_keys or _kite_manager.get_all_api_keys()

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
