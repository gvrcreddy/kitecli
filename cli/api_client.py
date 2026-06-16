"""
Local KiteCLI client.

Wraps KiteAccountManager directly — no HTTP server needed.
Public API is identical to the old HTTP-based KCLIClient so that
cli/main.py and cli/live_session.py require zero changes.
"""

from concurrent.futures import ThreadPoolExecutor
from cli.kite_manager import KiteAccountManager


class KCLIClientError(Exception):
    """Raised when a Kite API call fails."""


# Module-level singleton so session state persists across calls within the process.
_manager = KiteAccountManager()


class KCLIClient:
    """Local client that delegates directly to KiteAccountManager.

    Args:
        accounts: List of account dicts from config (name, api_key, api_secret,
                  user_id, password, totp_secret, proxy).  Accounts are
                  initialised eagerly on construction so that session tokens are
                  restored before the first command runs.
    """

    def __init__(self, accounts: list[dict]) -> None:
        self._accounts = accounts
        self._api_keys = [a.get("api_key", "") for a in accounts]
        # Eagerly init all accounts (restores saved sessions if available)
        for acct in accounts:
            _manager.init_account(
                api_key=acct.get("api_key", ""),
                api_secret=acct.get("api_secret", ""),
                name=acct.get("name", ""),
                proxy=acct.get("proxy"),
            )

    # ── compatibility helpers ──────────────────────────────────────

    def health_check(self) -> bool:
        """Always True — no server to ping in local mode."""
        return True

    # ── public API (mirrors old KCLIClient exactly, but runs in parallel) ──

    def init_accounts(self, accounts: list[dict]) -> dict:
        """Initialise (or re-initialise) accounts and attempt auto-login in parallel."""
        def init_one(acct):
            api_key = acct.get("api_key", "")
            name = acct.get("name", api_key)

            login_url = _manager.init_account(
                api_key=api_key,
                api_secret=acct.get("api_secret", ""),
                name=name,
                proxy=acct.get("proxy"),
            )

            if _manager.is_authenticated(api_key):
                return {
                    "name": name,
                    "api_key": api_key,
                    "login_url": login_url,
                    "auto_logged_in": True,
                    "message": "Session restored from saved token",
                }

            user_id = acct.get("user_id")
            password = acct.get("password")
            totp_secret = acct.get("totp_secret")
            if user_id and password and totp_secret:
                success = _manager.auto_login(
                    api_key=api_key,
                    user_id=user_id,
                    password=password,
                    totp_secret=totp_secret,
                )
                if success:
                    return {
                        "name": name,
                        "api_key": api_key,
                        "login_url": login_url,
                        "auto_logged_in": True,
                        "message": "Auto-login successful",
                    }
                else:
                    return {
                        "name": name,
                        "api_key": api_key,
                        "login_url": login_url,
                        "auto_logged_in": False,
                        "message": "Auto-login failed. Use manual login URL.",
                    }
            else:
                return {
                    "name": name,
                    "api_key": api_key,
                    "login_url": login_url,
                    "auto_logged_in": False,
                    "message": "Credentials incomplete — manual login required.",
                }

        with ThreadPoolExecutor(max_workers=max(1, len(accounts))) as executor:
            results = list(executor.map(init_one, accounts))

        return {"accounts": results}

    def complete_callback(self, api_key: str, request_token: str) -> dict:
        """Complete Kite OAuth login with a request token."""
        try:
            success = _manager.complete_login(api_key, request_token.strip())
            if success:
                return {"status": "success", "message": "Login successful"}
            return {"status": "error", "message": "Login failed — check request_token"}
        except Exception as exc:
            raise KCLIClientError(str(exc)) from exc

    def get_positions(self, api_keys: list[str]) -> dict:
        """Fetch open positions for the given accounts in parallel."""
        keys = api_keys or _manager.get_all_api_keys()

        def fetch_one(api_key):
            info = _manager.get_account_info(api_key)
            if not info.get("authenticated"):
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "positions": [],
                    "total_pnl": 0.0,
                    "status": "unauthenticated",
                }
            try:
                positions = _manager.get_positions(api_key)
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
        accounts = [
            _manager.get_account_info(api_key)
            for api_key in _manager.get_all_api_keys()
        ]
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
        keys = api_keys or _manager.get_all_api_keys()

        def place_one(api_key):
            info = _manager.get_account_info(api_key)
            if not info.get("authenticated"):
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "status": "error",
                    "order_id": None,
                    "message": "Account not authenticated",
                }
            try:
                order_id = _manager.place_order(
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
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "status": "success",
                    "order_id": str(order_id),
                    "message": f"Order placed: {order_id}",
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
    ) -> dict:
        """Exit positions across specified accounts in parallel."""
        keys = api_keys or _manager.get_all_api_keys()

        def exit_one(api_key):
            info = _manager.get_account_info(api_key)
            if not info.get("authenticated"):
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "status": "error",
                    "message": "Account not authenticated",
                    "orders_placed": [],
                }
            try:
                orders = _manager.exit_positions(api_key, tradingsymbol)
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
        keys = api_keys or _manager.get_all_api_keys()

        def fetch_one(api_key):
            info = _manager.get_account_info(api_key)
            if not info.get("authenticated"):
                return {
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "orders": [],
                    "status": "unauthenticated",
                }
            try:
                orders = _manager.get_orders(api_key)
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

    def get_market_indices(self) -> dict:
        """Fetch live Nifty, Sensex, and India VIX."""
        return _manager.get_market_indices()

    def get_margins(self, api_keys: list[str]) -> dict:
        """Fetch equity margin summary for each account in parallel.

        Returns ``{"accounts": [{"api_key": ..., "net": ..., "cash": ...}, ...]}``
        where ``net`` is buying power after F&O deductions and ``cash`` is raw
        cash balance. Both are ``None`` if the account is unavailable.
        """
        keys = api_keys or _manager.get_all_api_keys()

        def fetch_one(api_key):
            result = _manager.get_margins(api_key)
            return {"api_key": api_key, "net": result["net"], "cash": result["cash"]}

        with ThreadPoolExecutor(max_workers=max(1, len(keys))) as executor:
            results = list(executor.map(fetch_one, keys))

        return {"accounts": results}

    def get_access_token(self, api_key: str) -> str | None:
        """Get the access token for an authenticated account."""
        return _manager.get_access_token(api_key)

    def check_token(self, api_key: str) -> dict:
        """Validate an account's access token against the REST API.

        Returns a dict with keys: name, api_key, status
        ("valid"/"no_token"/"expired"/"forbidden"/"error"), and detail.
        """
        return _manager.check_token(api_key)
