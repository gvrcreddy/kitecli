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

from cli.base_manager import BaseBrokerManager

logger = logging.getLogger(__name__)

SESSIONS_FILE = Path.home() / ".kcli" / "sessions.json"


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
        """Kotak Neo REST-only integration; no WebSocket subscription."""
        return False

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

        Returns:
            Empty string (Kotak uses app-based / OTP auth, no browser URL).
        """
        try:
            from neo_api_client import NeoAPI
        except ImportError as exc:
            raise ImportError(
                "Kotak Neo support requires the 'neo-api-client' package. "
                "Install it with: pip install neo-api-client"
            ) from exc

        client = NeoAPI(environment="prod", consumer_key=consumer_key)
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
                client.limits()
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

        result = []
        for pos in raw_positions:
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

            last_price_str = str(pos.get("ltp", pos.get("last_price", "0")))
            try:
                last_price = float(last_price_str)
            except ValueError:
                last_price = avg_price

            pnl_str = str(pos.get("realizedPL", pos.get("pnl", "0")))
            try:
                pnl = float(pnl_str)
            except ValueError:
                pnl = (last_price - avg_price) * qty if qty != 0 else 0.0

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
                "realised": pnl,
                "unrealised": (last_price - avg_price) * qty if qty != 0 else 0.0,
                "pnl_pct": pnl_pct,
                "product": pos.get("prod", pos.get("product", "NRML")),
                "exchange": pos.get("exch", pos.get("exchange", "NFO")),
                # No instrument_token from Kotak — live_session will use symbol matching
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
                "transaction_type": transaction_type,
                "amo": "NO",
                "disclosed_quantity": "0",
                "market_protection": "0",
                "pf": "N",
                "trigger_price": str(trigger_price or 0),
                "tag": None,
            }
            try:
                resp = client.place_order(**params)
                if isinstance(resp, dict):
                    order_id = str(
                        resp.get("nOrdNo")
                        or resp.get("order_id")
                        or resp.get("data", {}).get("nOrdNo", "")
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
