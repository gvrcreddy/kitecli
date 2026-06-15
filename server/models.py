"""Pydantic models for KiteCLI server API request and response payloads."""

from pydantic import BaseModel


class AccountConfig(BaseModel):
    """Configuration for a single Kite trading account."""

    name: str
    api_key: str
    api_secret: str
    user_id: str | None = None
    password: str | None = None
    totp_secret: str | None = None
    proxy: str | None = None


class InitRequest(BaseModel):
    """Request body for initializing multiple Kite accounts."""

    accounts: list[AccountConfig]


class AccountLoginInfo(BaseModel):
    """Login information returned for a single account after initialization."""

    name: str
    api_key: str
    login_url: str
    auto_logged_in: bool = False
    message: str = ""


class InitResponse(BaseModel):
    """Response body containing login URLs for all initialized accounts."""

    accounts: list[AccountLoginInfo]


class CallbackRequest(BaseModel):
    """Request body for completing OAuth login with a request token."""

    api_key: str
    request_token: str


class CallbackResponse(BaseModel):
    """Response body after attempting to complete login for an account."""

    api_key: str
    status: str
    message: str


class PositionsRequest(BaseModel):
    """Request body specifying which accounts to fetch positions for."""

    api_keys: list[str]


class Position(BaseModel):
    """A single open trading position."""

    tradingsymbol: str
    quantity: int
    average_price: float
    last_price: float
    pnl: float
    product: str
    exchange: str


class AccountPositions(BaseModel):
    """Positions and PnL summary for a single account."""

    name: str
    api_key: str
    positions: list[Position]
    total_pnl: float
    status: str


class PositionsResponse(BaseModel):
    """Response body containing positions across all requested accounts."""

    accounts: list[AccountPositions]


class StatusResponse(BaseModel):
    """Response body showing authentication status of all registered accounts.

    Each dict in the list contains: name, api_key, authenticated (bool).
    """

    accounts: list[dict]


# ── Order and Exit Models ───────────────────────────────────────────

class OrderRequest(BaseModel):
    """Request to place an order across specified accounts."""

    api_keys: list[str] = []  # If empty, place order in all authenticated accounts
    tradingsymbol: str
    exchange: str = "NFO"
    transaction_type: str  # BUY or SELL
    quantity: int
    order_type: str  # MARKET, LIMIT, SL, SL-M
    price: float | None = None
    trigger_price: float | None = None
    product: str = "NRML"  # MIS, NRML, CNC


class OrderResult(BaseModel):
    """Order placement result for a single account."""

    name: str
    api_key: str
    status: str  # success, error
    order_id: str | None = None
    message: str


class OrderResponse(BaseModel):
    """Response containing results of order placement across accounts."""

    results: list[OrderResult]


class ExitRequest(BaseModel):
    """Request to exit positions across specified accounts."""

    api_keys: list[str] = []  # If empty, exit in all authenticated accounts
    tradingsymbol: str | None = None  # If None/empty, exit ALL positions


class ExitPositionDetail(BaseModel):
    """Detail of a single squared-off position."""

    tradingsymbol: str
    quantity: int
    product: str
    order_id: str | None = None


class ExitResult(BaseModel):
    """Exit result for a single account."""

    name: str
    api_key: str
    status: str  # success, error
    message: str
    orders_placed: list[ExitPositionDetail] = []


class ExitResponse(BaseModel):
    """Response containing exit results across all specified accounts."""

    results: list[ExitResult]


# ── Option Chain Models ─────────────────────────────────────────────

class OptionChainRequest(BaseModel):
    """Request to fetch option chain for a specific underlying and expiry."""

    api_key: str                    # Any one authenticated account's api_key to use
    underlying: str                 # e.g. NIFTY, BANKNIFTY, FINNIFTY
    expiry_week: int = 0            # 0 = current/nearest week, 1 = next week, etc.
    expiry_date: str | None = None  # e.g. "2024-06-27" — overrides expiry_week if provided


class OptionChainEntry(BaseModel):
    """A single strike row in the option chain (CE + PE side)."""

    strike: float
    ce_symbol: str | None = None
    ce_lot_size: int | None = None
    ce_ltp: float | None = None
    pe_symbol: str | None = None
    pe_lot_size: int | None = None
    pe_ltp: float | None = None


class OptionChainExpiry(BaseModel):
    """Available expiry dates for a given underlying."""

    expiry: str        # ISO date string, e.g. "2024-06-27"
    week_label: str    # e.g. "Current Week", "Next Week", "Week +2"


class OptionChainResponse(BaseModel):
    """Response for option chain lookup."""

    underlying: str
    expiry: str
    strikes: list[OptionChainEntry]
    available_expiries: list[OptionChainExpiry]
    status: str
    message: str = ""


# ── Orders Fetch Models ─────────────────────────────────────────────

class OrdersRequest(BaseModel):
    """Request to fetch orders for specified accounts."""

    api_keys: list[str] = []  # If empty, fetch from all authenticated accounts


class OrderEntry(BaseModel):
    """A single order entry from the order book."""

    order_id: str
    tradingsymbol: str
    exchange: str
    transaction_type: str  # BUY or SELL
    quantity: int
    filled_quantity: int = 0
    pending_quantity: int = 0
    price: float = 0.0
    average_price: float = 0.0
    trigger_price: float = 0.0
    order_type: str  # MARKET, LIMIT, SL, SL-M
    status: str
    product: str
    status_message: str = ""


class AccountOrders(BaseModel):
    """Orders for a single account."""

    name: str
    api_key: str
    orders: list[OrderEntry]
    status: str


class OrdersResponse(BaseModel):
    """Response containing orders across all specified accounts."""

    accounts: list[AccountOrders]


# ── Market Indices Model ────────────────────────────────────────────

class MarketIndicesResponse(BaseModel):
    """Response containing Nifty, Sensex, and India VIX LTP."""

    status: str
    nifty: float | None = None
    sensex: float | None = None
    vix: float | None = None
    message: str = ""

