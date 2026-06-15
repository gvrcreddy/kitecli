import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pyotp
import requests as http_requests
from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)

SESSIONS_FILE = Path.home() / ".kcli" / "sessions.json"


def _load_sessions() -> dict[str, str]:
    if not SESSIONS_FILE.exists():
        return {}
    try:
        with open(SESSIONS_FILE, "r") as f:
            return json.load(f)
    except Exception as exc:
        logger.error("Failed to load sessions: %s", exc)
        return {}


def _save_session(api_key: str, access_token: str) -> None:
    sessions = _load_sessions()
    sessions[api_key] = access_token
    try:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SESSIONS_FILE, "w") as f:
            json.dump(sessions, f, indent=2)
    except Exception as exc:
        logger.error("Failed to save session for %s: %s", api_key[:8], exc)


class KiteAccountManager:
    """Manages multiple KiteConnect instances keyed by api_key.

    Stores KiteConnect client objects, api_secrets, account names,
    and tracks authentication state for each registered account.
    """

    def __init__(self) -> None:
        self._clients: dict[str, KiteConnect] = {}
        self._api_secrets: dict[str, str] = {}
        self._account_names: dict[str, str] = {}
        self._authenticated: dict[str, bool] = {}

    def init_account(self, api_key: str, api_secret: str, name: str = "", proxy: str = None) -> str:
        """Initialize a KiteConnect instance and return the login URL.

        Args:
            api_key: The Kite Connect API key.
            api_secret: The Kite Connect API secret.
            name: A human-readable name for the account.
            proxy: Optional HTTP/HTTPS proxy string for routing requests.

        Returns:
            The Kite login URL for user authorization.
        """
        proxies = {"http": proxy, "https": proxy} if proxy else None
        kite = KiteConnect(api_key=api_key, proxies=proxies)
        self._clients[api_key] = kite
        self._api_secrets[api_key] = api_secret
        self._account_names[api_key] = name or api_key
        self._authenticated[api_key] = False

        # Try to restore session from saved token
        sessions = _load_sessions()
        saved_token = sessions.get(api_key)
        if saved_token:
            logger.info("Found saved session token for '%s' (api_key=%s…). Verifying...", name, api_key[:8])
            try:
                kite.set_access_token(saved_token)
                # Verify token by calling profile
                kite.profile()
                self._authenticated[api_key] = True
                logger.info("Successfully restored valid session for '%s' (api_key=%s…)", name, api_key[:8])
            except Exception as exc:
                logger.warning(
                    "Saved session token for '%s' (api_key=%s…) is invalid or expired: %s",
                    name,
                    api_key[:8],
                    exc,
                )
                self._authenticated[api_key] = False

        login_url = f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"
        logger.info("Initialized account '%s' (api_key=%s…)", name, api_key[:8])
        return login_url

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
        except Exception:
            logger.exception(
                "Login failed for account '%s' (api_key=%s…)",
                self._account_names.get(api_key, api_key),
                api_key[:8],
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

            open_positions = []
            for pos in all_positions:
                if pos.get("quantity", 0) != 0:
                    open_positions.append(
                        {
                            "tradingsymbol": pos.get("tradingsymbol", ""),
                            "quantity": pos.get("quantity", 0),
                            "average_price": pos.get("average_price", 0.0),
                            "last_price": pos.get("last_price", 0.0),
                            "pnl": pos.get("pnl", 0.0),
                            "product": pos.get("product", ""),
                            "exchange": pos.get("exchange", ""),
                        }
                    )

            logger.info(
                "Fetched %d open positions for api_key=%s…",
                len(open_positions),
                api_key[:8],
            )
            return open_positions
        except Exception as exc:
            logger.exception(
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

    def get_all_api_keys(self) -> list[str]:
        """Return a list of all registered api_keys."""
        return list(self._clients.keys())

    def auto_login(
        self,
        api_key: str,
        user_id: str,
        password: str,
        totp_secret: str,
    ) -> bool:
        """Automate the full Kite login flow using credentials + TOTP.

        Steps:
            1. POST user_id + password to Kite login endpoint.
            2. Generate a TOTP code using the shared secret via pyotp.
            3. POST the TOTP for two-factor authentication.
            4. Visit the Kite Connect login URL to capture the request_token.
            5. Exchange the request_token for an access_token.

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

        session = http_requests.Session()

        try:
            # Step 1: Login with user_id + password
            login_resp = session.post(
                "https://kite.zerodha.com/api/login",
                data={"user_id": user_id, "password": password},
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
                try:
                    resp = session.get(current_url, allow_redirects=False)
                except http_requests.exceptions.RequestException as exc:
                    logger.warning("Network error/unreachable during redirect hop %d: %s", hop, exc)
                    # Check if the url itself contained the token before network error
                    parsed = urlparse(current_url)
                    query_params = parse_qs(parsed.query)
                    token = query_params.get("request_token", [None])[0]
                    if token:
                        request_token = token
                    break

                # Check if current_url already contains the request_token
                parsed = urlparse(current_url)
                query_params = parse_qs(parsed.query)
                token = query_params.get("request_token", [None])[0]
                if token:
                    request_token = token
                    break

                location = resp.headers.get("Location")
                if not location:
                    break

                # Resolve relative redirect URLs
                if location.startswith("/"):
                    parsed_prev = urlparse(current_url)
                    location = f"{parsed_prev.scheme}://{parsed_prev.netloc}{location}"

                current_url = location

                # Check if target redirect URL contains the request_token
                parsed = urlparse(current_url)
                query_params = parse_qs(parsed.query)
                token = query_params.get("request_token", [None])[0]
                if token:
                    request_token = token
                    break

                # If target URL is not a Zerodha domain (e.g. localhost or client app domain),
                # do not actually request it (it may not be reachable or could trigger external side-effects),
                # and we've already checked if it has the request_token.
                parsed_loc = urlparse(current_url)
                if parsed_loc.netloc and not parsed_loc.netloc.endswith(".zerodha.com") and "zerodha.com" not in parsed_loc.netloc:
                    logger.info("Reached non-Zerodha redirect target: %s", current_url)
                    break

            if not request_token:
                logger.error("Could not extract request_token from redirect chain for %s", user_id)
                return False

            logger.info("Captured request_token for %s", user_id)

            # Step 5: Exchange request_token for access_token
            return self.complete_login(api_key, request_token)

        except Exception:
            logger.exception("Auto-login failed for %s (api_key=%s…)", user_id, api_key[:8])
            return False

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
    ) -> str:
        """Place an order for a specific account.

        Args:
            api_key: The Kite Connect API key.
            tradingsymbol: The symbol to trade (e.g. INFY, NIFTY2662325000CE).
            exchange: The exchange (e.g. NSE, NFO).
            transaction_type: BUY or SELL.
            quantity: Order quantity.
            order_type: MARKET, LIMIT, SL, SL-M.
            price: Order price (required for LIMIT/SL orders).
            trigger_price: Trigger price (required for SL/SL-M orders).
            product: MIS, NRML, CNC.

        Returns:
            The order ID.
        """
        kite = self._clients.get(api_key)
        if not kite:
            raise ValueError(f"Account not found for api_key={api_key[:8]}…")

        if not self.is_authenticated(api_key):
            raise RuntimeError(f"Account '{self._account_names.get(api_key, api_key)}' is not authenticated.")

        # Normalize parameters (uppercase strings)
        transaction_type = transaction_type.upper()
        order_type = order_type.upper()
        product = product.upper()
        exchange = exchange.upper()
        tradingsymbol = tradingsymbol.upper()

        # place_order arguments
        params = {
            "variety": kite.VARIETY_REGULAR,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "product": product,
            "order_type": order_type,
            "validity": kite.VALIDITY_DAY,
        }

        if price is not None and price > 0:
            params["price"] = price
        if trigger_price is not None and trigger_price > 0:
            params["trigger_price"] = trigger_price

        logger.info(
            "Placing %s %s order for %d %s on %s (%s) for account '%s'",
            order_type,
            transaction_type,
            quantity,
            tradingsymbol,
            exchange,
            product,
            self._account_names.get(api_key, api_key),
        )

        try:
            order_id = kite.place_order(**params)
            return order_id
        except Exception as exc:
            logger.exception("Failed to place order for api_key=%s…", api_key[:8])
            raise RuntimeError(f"Failed to place order: {exc}") from exc

    def exit_positions(
        self,
        api_key: str,
        tradingsymbol: str | None = None,
    ) -> list[dict[str, Any]]:
        """Exit/square off open positions for a specific account.

        Args:
            api_key: The Kite Connect API key.
            tradingsymbol: If specified, only square off positions matching this symbol.
                           If None, square off all open positions.

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
            logger.exception("Failed to fetch positions during exit for api_key=%s…", api_key[:8])
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
                order_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=exchange,
                    tradingsymbol=symbol,
                    transaction_type=transaction_type,
                    quantity=exit_qty,
                    product=product,
                    order_type=kite.ORDER_TYPE_MARKET,
                    validity=kite.VALIDITY_DAY,
                )
                orders_placed.append({
                    "tradingsymbol": symbol,
                    "quantity": exit_qty,
                    "product": product,
                    "order_id": order_id
                })
            except Exception as exc:
                logger.exception(
                    "Failed to place exit order for %s in account %s: %s",
                    symbol,
                    self._account_names.get(api_key, api_key),
                    exc,
                )
                # Raise an error only if the targeted symbol exit failed completely
                if tradingsymbol:
                    raise RuntimeError(f"Failed to place exit order for {symbol}: {exc}") from exc


        return orders_placed

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

        # Enrich each strike entry with live LTP
        for s in strikes:
            ce_key = f"NFO:{s.get('ce_symbol', '')}"
            pe_key = f"NFO:{s.get('pe_symbol', '')}"
            if ce_key in ltp_data:
                s["ce_ltp"] = ltp_data[ce_key].get("last_price")
            if pe_key in ltp_data:
                s["pe_ltp"] = ltp_data[pe_key].get("last_price")

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

    def get_market_indices(self) -> dict[str, Any]:
        """Fetch Nifty, Sensex, and India VIX LTP using first authenticated account, or fallback to Yahoo Finance."""
        # 1. Try Kite first
        for api_key in self.get_all_api_keys():
            if self.is_authenticated(api_key):
                kite = self._clients.get(api_key)
                if kite:
                    try:
                        data = kite.ltp(["NSE:NIFTY 50", "BSE:SENSEX", "NSE:INDIA VIX"])
                        nifty = data.get("NSE:NIFTY 50", {}).get("last_price")
                        sensex = data.get("BSE:SENSEX", {}).get("last_price")
                        vix = data.get("NSE:INDIA VIX", {}).get("last_price")
                        if nifty or sensex or vix:
                            return {
                                "status": "success",
                                "nifty": nifty,
                                "sensex": sensex,
                                "vix": vix,
                            }
                    except Exception as exc:
                        logger.warning("Kite indices fetch failed for api_key=%s…: %s", api_key[:8], exc)

        # 2. Fallback to Yahoo Finance chart API
        try:
            logger.info("Fetching indices from Yahoo Finance fallback...")
            import requests as http_requests
            headers = {"User-Agent": "Mozilla/5.0"}

            # Fetch Nifty (^NSEI)
            n_resp = http_requests.get("https://query1.finance.yahoo.com/v8/finance/chart/^NSEI?interval=1m&range=1d", headers=headers, timeout=5)
            n_price = n_resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]

            # Fetch Sensex (^BSESN)
            s_resp = http_requests.get("https://query1.finance.yahoo.com/v8/finance/chart/^BSESN?interval=1m&range=1d", headers=headers, timeout=5)
            s_price = s_resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]

            # Fetch India VIX (^INDIAVIX)
            v_resp = http_requests.get("https://query1.finance.yahoo.com/v8/finance/chart/^INDIAVIX?interval=1m&range=1d", headers=headers, timeout=5)
            v_price = v_resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]

            return {
                "status": "success",
                "nifty": float(n_price),
                "sensex": float(s_price),
                "vix": float(v_price),
            }
        except Exception as exc:
            logger.error("Yahoo Finance fallback failed: %s", exc)
            return {"status": "error", "message": f"Kite call had insufficient permission, and Yahoo fallback failed: {exc}"}
