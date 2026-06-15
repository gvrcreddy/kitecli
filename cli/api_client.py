"""
HTTP API client for KiteCLI.

Provides a synchronous wrapper around the KiteCLI server endpoints
using httpx with proper authentication and error handling.
"""

import httpx


class KCLIClientError(Exception):
    """Raised when an API request fails or the server is unreachable."""


class KCLIClient:
    """Synchronous HTTP client for the KiteCLI server.

    Args:
        server_url: Base URL of the KiteCLI server (e.g. ``http://localhost:8080``).
        auth_token: Secret token sent via the ``X-Auth-Token`` header.
    """

    def __init__(self, server_url: str, auth_token: str) -> None:
        self.base_url = server_url.rstrip("/")
        self.auth_token = auth_token
        self._timeout = 10.0  # seconds

    # ── internal helpers ───────────────────────────────────────────

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Auth-Token": self.auth_token}

    def _get(self, path: str) -> httpx.Response:
        """Issue a GET request and return the response.

        Raises:
            KCLIClientError: On connection or HTTP errors.
        """
        url = f"{self.base_url}{path}"
        try:
            resp = httpx.get(url, headers=self._headers, timeout=self._timeout)
            resp.raise_for_status()
            return resp
        except httpx.ConnectError as exc:
            raise KCLIClientError(
                f"Could not connect to server at {self.base_url}. "
                "Is the server running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise KCLIClientError(
                f"Request to {url} timed out after {self._timeout}s."
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise KCLIClientError(
                f"Server returned {exc.response.status_code} for {url}: "
                f"{exc.response.text}"
            ) from exc

    def _post(self, path: str, json: dict | list | None = None) -> httpx.Response:
        """Issue a POST request and return the response.

        Raises:
            KCLIClientError: On connection or HTTP errors.
        """
        url = f"{self.base_url}{path}"
        try:
            resp = httpx.post(
                url, headers=self._headers, json=json, timeout=self._timeout
            )
            resp.raise_for_status()
            return resp
        except httpx.ConnectError as exc:
            raise KCLIClientError(
                f"Could not connect to server at {self.base_url}. "
                "Is the server running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise KCLIClientError(
                f"Request to {url} timed out after {self._timeout}s."
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise KCLIClientError(
                f"Server returned {exc.response.status_code} for {url}: "
                f"{exc.response.text}"
            ) from exc

    # ── public API ─────────────────────────────────────────────────

    def health_check(self) -> bool:
        """Check whether the server is reachable.

        Returns:
            True if the /health endpoint responds successfully.
        """
        try:
            self._get("/health")
            return True
        except KCLIClientError:
            return False

    def init_accounts(self, accounts: list[dict]) -> dict:
        """Initialise accounts on the server.

        Args:
            accounts: List of account dicts (name, api_key, api_secret).

        Returns:
            JSON response from the server containing login URLs.
        """
        resp = self._post("/api/init", json={"accounts": accounts})
        return resp.json()

    def complete_callback(self, api_key: str, request_token: str) -> dict:
        """Complete the Kite login callback for an account.

        Args:
            api_key: The Kite Connect API key.
            request_token: The request token from the redirect URL.

        Returns:
            JSON response from the server.
        """
        resp = self._post(
            "/api/callback",
            json={"api_key": api_key, "request_token": request_token},
        )
        return resp.json()

    def get_positions(self, api_keys: list[str]) -> dict:
        """Fetch open positions for the given accounts.

        Args:
            api_keys: List of Kite Connect API keys.

        Returns:
            JSON response containing positions per account.
        """
        resp = self._post("/api/positions", json={"api_keys": api_keys})
        return resp.json()

    def get_status(self) -> dict:
        """Get authentication status for all accounts.

        Returns:
            JSON response with per-account auth status.
        """
        resp = self._get("/api/status")
        return resp.json()

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
        """Place an order across specified accounts.

        Args:
            api_keys: List of account API keys. If empty, places in all authenticated accounts.
            tradingsymbol: The symbol to trade (e.g. INFY, NIFTY2662325000CE).
            exchange: Exchange name (e.g. NFO, NSE).
            transaction_type: BUY or SELL.
            quantity: Quantity to buy or sell.
            order_type: MARKET, LIMIT, SL, SL-M.
            price: Optional price (for LIMIT/SL orders).
            trigger_price: Optional trigger price (for SL/SL-M orders).
            product: MIS, NRML, CNC.

        Returns:
            JSON response from the server with order IDs/statuses.
        """
        payload = {
            "api_keys": api_keys,
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "order_type": order_type,
            "product": product,
        }
        if price is not None:
            payload["price"] = price
        if trigger_price is not None:
            payload["trigger_price"] = trigger_price

        resp = self._post("/api/order", json=payload)
        return resp.json()

    def exit_positions(
        self,
        api_keys: list[str],
        tradingsymbol: str | None = None,
    ) -> dict:
        """Exit positions across specified accounts.

        Args:
            api_keys: List of account API keys. If empty, exits in all authenticated accounts.
            tradingsymbol: Symbol to exit, or None to exit all positions.

        Returns:
            JSON response from the server with exit statuses.
        """
        payload = {
            "api_keys": api_keys,
            "tradingsymbol": tradingsymbol,
        }
        resp = self._post("/api/exit", json=payload)
        return resp.json()

    def get_option_chain(
        self,
        api_key: str,
        underlying: str,
        expiry_week: int = 0,
        expiry_date: str | None = None,
    ) -> dict:
        """Fetch option chain for a specific underlying and expiry.

        Args:
            api_key: Any one authenticated account's api_key.
            underlying: Underlying name (e.g. NIFTY, BANKNIFTY, FINNIFTY).
            expiry_week: 0 = current week, 1 = next week, 2 = week after, etc.
            expiry_date: Specific expiry date as ISO string (YYYY-MM-DD).
                         If provided, overrides expiry_week.

        Returns:
            JSON response with option chain strikes, expiries, and symbols.
        """
        payload: dict = {
            "api_key": api_key,
            "underlying": underlying,
            "expiry_week": expiry_week,
        }
        if expiry_date:
            payload["expiry_date"] = expiry_date
        resp = self._post("/api/oc", json=payload)
        return resp.json()

    def get_orders(self, api_keys: list[str]) -> dict:
        """Fetch today's order book for specified accounts.

        Args:
            api_keys: List of account API keys. If empty, fetches from all
                      authenticated accounts.

        Returns:
            JSON response with orders grouped by account.
        """
        resp = self._post("/api/orders", json={"api_keys": api_keys})
        return resp.json()

    def get_market_indices(self) -> dict:
        """Fetch live Nifty, Sensex, and India VIX LTP.

        Returns:
            JSON response containing index prices.
        """
        resp = self._get("/api/indices")
        return resp.json()
