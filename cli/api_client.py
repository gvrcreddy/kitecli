"""
Local KiteCLI client.

Wraps KiteAccountManager directly — no HTTP server needed.
Public API is identical to the old HTTP-based KCLIClient so that
cli/main.py and cli/live_session.py require zero changes.
"""

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

    # ── public API (mirrors old KCLIClient exactly) ────────────────

    def init_accounts(self, accounts: list[dict]) -> dict:
        """Initialise (or re-initialise) accounts and attempt auto-login."""
        result_accounts = []

        for acct in accounts:
            api_key = acct.get("api_key", "")
            name = acct.get("name", api_key)

            login_url = _manager.init_account(
                api_key=api_key,
                api_secret=acct.get("api_secret", ""),
                name=name,
                proxy=acct.get("proxy"),
            )

            if _manager.is_authenticated(api_key):
                result_accounts.append({
                    "name": name,
                    "api_key": api_key,
                    "login_url": login_url,
                    "auto_logged_in": True,
                    "message": "Session restored from saved token",
                })
                continue

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
                    result_accounts.append({
                        "name": name,
                        "api_key": api_key,
                        "login_url": login_url,
                        "auto_logged_in": True,
                        "message": "Auto-login successful",
                    })
                    continue
                else:
                    result_accounts.append({
                        "name": name,
                        "api_key": api_key,
                        "login_url": login_url,
                        "auto_logged_in": False,
                        "message": "Auto-login failed. Use manual login URL.",
                    })
            else:
                result_accounts.append({
                    "name": name,
                    "api_key": api_key,
                    "login_url": login_url,
                    "auto_logged_in": False,
                    "message": "Credentials incomplete — manual login required.",
                })

        return {"accounts": result_accounts}

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
        """Fetch open positions for the given accounts."""
        result_accounts = []
        keys = api_keys or _manager.get_all_api_keys()

        for api_key in keys:
            info = _manager.get_account_info(api_key)
            if not info.get("authenticated"):
                result_accounts.append({
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "positions": [],
                    "total_pnl": 0.0,
                    "status": "unauthenticated",
                })
                continue
            try:
                positions = _manager.get_positions(api_key)
                total_pnl = sum(p.get("pnl", 0.0) for p in positions)
                result_accounts.append({
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "positions": positions,
                    "total_pnl": total_pnl,
                    "status": "success",
                })
            except Exception as exc:
                result_accounts.append({
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "positions": [],
                    "total_pnl": 0.0,
                    "status": f"error: {exc}",
                })

        return {"accounts": result_accounts}

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
        """Place an order across specified accounts."""
        keys = api_keys or _manager.get_all_api_keys()
        results = []

        for api_key in keys:
            info = _manager.get_account_info(api_key)
            if not info.get("authenticated"):
                results.append({
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "status": "error",
                    "order_id": None,
                    "message": "Account not authenticated",
                })
                continue
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
                results.append({
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "status": "success",
                    "order_id": str(order_id),
                    "message": f"Order placed: {order_id}",
                })
            except Exception as exc:
                results.append({
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "status": "error",
                    "order_id": None,
                    "message": str(exc),
                })

        return {"results": results}

    def exit_positions(
        self,
        api_keys: list[str],
        tradingsymbol: str | None = None,
    ) -> dict:
        """Exit positions across specified accounts."""
        keys = api_keys or _manager.get_all_api_keys()
        results = []

        for api_key in keys:
            info = _manager.get_account_info(api_key)
            if not info.get("authenticated"):
                results.append({
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "status": "error",
                    "message": "Account not authenticated",
                    "orders_placed": [],
                })
                continue
            try:
                orders = _manager.exit_positions(api_key, tradingsymbol)
                results.append({
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "status": "success",
                    "message": f"Exited {len(orders)} position(s)",
                    "orders_placed": orders,
                })
            except Exception as exc:
                results.append({
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "status": "error",
                    "message": str(exc),
                    "orders_placed": [],
                })

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
        """Fetch today's order book for specified accounts."""
        keys = api_keys or _manager.get_all_api_keys()
        result_accounts = []

        for api_key in keys:
            info = _manager.get_account_info(api_key)
            if not info.get("authenticated"):
                result_accounts.append({
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "orders": [],
                    "status": "unauthenticated",
                })
                continue
            try:
                orders = _manager.get_orders(api_key)
                result_accounts.append({
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "orders": orders,
                    "status": "success",
                })
            except Exception as exc:
                result_accounts.append({
                    "name": info.get("name", api_key),
                    "api_key": api_key,
                    "orders": [],
                    "status": f"error: {exc}",
                })

        return {"accounts": result_accounts}

    def get_market_indices(self) -> dict:
        """Fetch live Nifty, Sensex, and India VIX."""
        return _manager.get_market_indices()
