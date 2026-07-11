"""
Abstract base class for broker account managers.

Defines the standard contract that every concrete broker implementation
(KiteAccountManager, KotakAccountManager, …) must satisfy so the rest of
the KiteCLI stack (api_client, live_session) remains broker-agnostic.

Normalised field names
----------------------
Positions
    tradingsymbol  – exchange-standardised instrument symbol
    quantity       – net quantity (negative = short)
    average_price  – average trade price
    last_price     – last known market price
    pnl            – unrealised P&L in ₹
    realised       – realised P&L in ₹
    unrealised     – unrealised P&L in ₹
    pnl_pct        – unrealised P&L as a % of cost
    product        – product type (NRML, MIS, CNC)
    exchange       – exchange code (NFO, NSE, BSE …)
    instrument_token – broker-specific integer token for WS subscription
                       (None for brokers that don't expose this)

Orders
    order_id, tradingsymbol, transaction_type, quantity,
    pending_quantity, price, order_type, status, product,
    exchange, parent_order_id, status_message
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseBrokerManager(ABC):
    """Abstract base class for per-broker account managers."""

    @property
    @abstractmethod
    def broker_name(self) -> str:
        """Short identifier string, e.g. ``"zerodha"`` or ``"kotak"``."""

    # ── lifecycle ─────────────────────────────────────────────────────────────

    @abstractmethod
    def init_account(self, account_key: str, **credentials) -> str:
        """Register an account. Returns a login URL (empty if not applicable)."""

    @abstractmethod
    def auto_login(self, account_key: str, **credentials) -> bool:
        """Attempt a fully automated login. Returns True on success."""

    @abstractmethod
    def is_authenticated(self, account_key: str) -> bool:
        """Return True if the account has a valid live session."""

    @abstractmethod
    def get_account_info(self, account_key: str) -> dict[str, Any]:
        """Return {name, account_key, authenticated} for the account."""

    @abstractmethod
    def get_access_token(self, account_key: str) -> str | None:
        """Return the live access/session token, or None."""

    @abstractmethod
    def get_all_account_keys(self) -> list[str]:
        """Return the list of all registered account keys."""

    # ── market data ───────────────────────────────────────────────────────────

    @abstractmethod
    def get_positions(self, account_key: str) -> list[dict[str, Any]]:
        """Fetch net open positions using the normalised schema."""

    @abstractmethod
    def get_orders(self, account_key: str) -> list[dict[str, Any]]:
        """Fetch today's full order book using the normalised schema."""

    @abstractmethod
    def get_margins(self, account_key: str) -> dict[str, Any]:
        """Return {"net": float|None, "cash": float|None, "collateral": float|None}."""

    # ── order management ──────────────────────────────────────────────────────

    @abstractmethod
    def place_order(
        self,
        account_key: str,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        order_type: str,
        price: float | None = None,
        trigger_price: float | None = None,
        product: str = "NRML",
    ) -> list[str]:
        """Place an order and return a list of order IDs."""

    @abstractmethod
    def modify_order(
        self,
        account_key: str,
        order_id: str,
        quantity: int | None = None,
        price: float | None = None,
        order_type: str | None = None,
        trigger_price: float | None = None,
    ) -> str:
        """Modify an open order. Returns the order ID."""

    @abstractmethod
    def cancel_order(self, account_key: str, order_id: str) -> str:
        """Cancel an open order. Returns the order ID."""

    @abstractmethod
    def exit_positions(
        self,
        account_key: str,
        tradingsymbol: str | None = None,
        price: float | None = None,
    ) -> list[dict[str, Any]]:
        """Square off open positions. Returns placed exit order details."""

    # ── optional broker-specific capabilities ─────────────────────────────────

    def supports_websocket(self) -> bool:
        """Return True if this broker provides a real-time WebSocket stream.

        Zerodha returns True; Kotak (in this integration) returns False —
        live LTPs for Kotak positions are delivered via the primary Zerodha
        ticker using symbol-based matching in live_session._on_ticks.
        """
        return False

    def get_nfo_lot_sizes(self) -> dict[str, int]:
        """Return tradingsymbol → lot_size map for NFO instruments.
        Zerodha fetches this from kite.instruments("NFO"). Default: empty dict.
        """
        return {}

    def get_market_indices(self) -> dict[str, Any]:
        """Return Nifty / Sensex / India VIX snapshot. Default: not supported."""
        return {"status": "error", "message": "not supported by this broker"}
