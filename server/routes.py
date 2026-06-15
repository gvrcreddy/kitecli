"""FastAPI route definitions for the KiteCLI server API."""

import logging

from fastapi import APIRouter

from models import (
    AccountLoginInfo,
    AccountPositions,
    CallbackRequest,
    CallbackResponse,
    InitRequest,
    InitResponse,
    Position,
    PositionsRequest,
    PositionsResponse,
    StatusResponse,
    OrderRequest,
    OrderResponse,
    OrderResult,
    ExitRequest,
    ExitResponse,
    ExitResult,
    ExitPositionDetail,
    OptionChainRequest,
    OptionChainResponse,
    OptionChainEntry,
    OptionChainExpiry,
    OrdersRequest,
    OrdersResponse,
    AccountOrders,
    OrderEntry,
    MarketIndicesResponse,
)
from kite_manager import KiteAccountManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# Module-level manager shared across all route handlers.
manager = KiteAccountManager()


@router.post("/init", response_model=InitResponse)
async def init_accounts(request: InitRequest) -> InitResponse:
    """Initialize Kite accounts and return login URLs.

    Registers each account with the KiteAccountManager.  When an account
    includes ``user_id``, ``password``, and ``totp_secret``, the server
    will attempt an automated login using pyotp.  If auto-login
    succeeds the account is marked as authenticated immediately;
    otherwise it falls back to returning the manual login URL.
    """
    account_logins: list[AccountLoginInfo] = []

    for account in request.accounts:
        login_url = manager.init_account(
            api_key=account.api_key,
            api_secret=account.api_secret,
            name=account.name,
            proxy=account.proxy,
        )

        # Check if session was restored from saved token
        if manager.is_authenticated(account.api_key):
            account_logins.append(
                AccountLoginInfo(
                    name=account.name,
                    api_key=account.api_key,
                    login_url=login_url,
                    auto_logged_in=True,
                    message="Session restored from saved token",
                )
            )
            continue

        # Try auto-login when full credentials are provided
        if account.user_id and account.password and account.totp_secret:
            logger.info(
                "Attempting auto-login for '%s' (user_id=%s)",
                account.name,
                account.user_id,
            )
            success = manager.auto_login(
                api_key=account.api_key,
                user_id=account.user_id,
                password=account.password,
                totp_secret=account.totp_secret,
            )
            if success:
                account_logins.append(
                    AccountLoginInfo(
                        name=account.name,
                        api_key=account.api_key,
                        login_url=login_url,
                        auto_logged_in=True,
                        message="Auto-login successful",
                    )
                )
                continue
            else:
                logger.warning(
                    "Auto-login failed for '%s', falling back to manual login",
                    account.name,
                )
                account_logins.append(
                    AccountLoginInfo(
                        name=account.name,
                        api_key=account.api_key,
                        login_url=login_url,
                        auto_logged_in=False,
                        message="Auto-login failed — use the login URL instead",
                    )
                )
                continue

        # No auto-login credentials — return manual login URL
        account_logins.append(
            AccountLoginInfo(
                name=account.name,
                api_key=account.api_key,
                login_url=login_url,
            )
        )

    logger.info("Initialized %d account(s)", len(account_logins))
    return InitResponse(accounts=account_logins)


@router.post("/callback", response_model=CallbackResponse)
async def handle_callback(request: CallbackRequest) -> CallbackResponse:
    """Complete OAuth login for an account using the request token.

    Calls KiteConnect.generate_session to exchange the request token
    for an access token and marks the account as authenticated.
    """
    success = manager.complete_login(
        api_key=request.api_key,
        request_token=request.request_token,
    )

    if success:
        logger.info("Callback successful for api_key=%s…", request.api_key[:8])
        return CallbackResponse(
            api_key=request.api_key,
            status="success",
            message="Login completed successfully",
        )
    else:
        logger.warning("Callback failed for api_key=%s…", request.api_key[:8])
        return CallbackResponse(
            api_key=request.api_key,
            status="error",
            message="Login failed — check request token or api_secret",
        )


@router.post("/positions", response_model=PositionsResponse)
async def get_positions(request: PositionsRequest) -> PositionsResponse:
    """Fetch open positions for the requested accounts.

    For each api_key, attempts to retrieve positions from the Kite API.
    If an account fails (e.g. session expired), it returns status='error'
    with an empty positions list instead of failing the whole request.
    """
    account_positions_list: list[AccountPositions] = []

    for api_key in request.api_keys:
        account_info = manager.get_account_info(api_key)
        name = account_info.get("name", api_key)

        try:
            raw_positions = manager.get_positions(api_key)
            positions = [Position(**pos) for pos in raw_positions]
            total_pnl = sum(pos.pnl for pos in positions)

            account_positions_list.append(
                AccountPositions(
                    name=name,
                    api_key=api_key,
                    positions=positions,
                    total_pnl=total_pnl,
                    status="success",
                )
            )
        except Exception as exc:
            logger.exception(
                "Error fetching positions for api_key=%s…", api_key[:8]
            )
            account_positions_list.append(
                AccountPositions(
                    name=name,
                    api_key=api_key,
                    positions=[],
                    total_pnl=0.0,
                    status=f"error: {exc}",
                )
            )

    return PositionsResponse(accounts=account_positions_list)


@router.get("/status", response_model=StatusResponse)
async def get_status() -> StatusResponse:
    """Return authentication status of all registered accounts."""
    accounts = [
        manager.get_account_info(api_key)
        for api_key in manager.get_all_api_keys()
    ]
    return StatusResponse(accounts=accounts)


@router.post("/order", response_model=OrderResponse)
async def place_multi_account_order(request: OrderRequest) -> OrderResponse:
    """Place an order across multiple authenticated accounts."""
    results: list[OrderResult] = []

    # Determine which accounts to place the order in
    target_keys = request.api_keys if request.api_keys else [
        k for k in manager.get_all_api_keys() if manager.is_authenticated(k)
    ]

    if not target_keys:
        logger.warning("No authenticated accounts available for placing order.")
        # If specific keys were requested, return errors for them
        requested = request.api_keys if request.api_keys else ["None available"]
        for key in requested:
            results.append(
                OrderResult(
                    name=key,
                    api_key=key,
                    status="error",
                    message="No authenticated trading sessions found.",
                )
            )
        return OrderResponse(results=results)

    for api_key in target_keys:
        acct_info = manager.get_account_info(api_key)
        name = acct_info.get("name", api_key)

        try:
            order_id = manager.place_order(
                api_key=api_key,
                tradingsymbol=request.tradingsymbol,
                exchange=request.exchange,
                transaction_type=request.transaction_type,
                quantity=request.quantity,
                order_type=request.order_type,
                price=request.price,
                trigger_price=request.trigger_price,
                product=request.product,
            )
            results.append(
                OrderResult(
                    name=name,
                    api_key=api_key,
                    status="success",
                    order_id=order_id,
                    message="Order placed successfully.",
                )
            )
        except Exception as exc:
            logger.exception(
                "Order placement failed in account %s (api_key=%s…): %s",
                name,
                api_key[:8],
                exc,
            )
            results.append(
                OrderResult(
                    name=name,
                    api_key=api_key,
                    status="error",
                    message=str(exc),
                )
            )

    return OrderResponse(results=results)


@router.post("/exit", response_model=ExitResponse)
async def exit_multi_account_positions(request: ExitRequest) -> ExitResponse:
    """Exit open positions across multiple authenticated accounts."""
    results: list[ExitResult] = []

    # Determine which accounts to exit positions in
    target_keys = request.api_keys if request.api_keys else [
        k for k in manager.get_all_api_keys() if manager.is_authenticated(k)
    ]

    if not target_keys:
        logger.warning("No authenticated accounts available for exit.")
        requested = request.api_keys if request.api_keys else ["None available"]
        for key in requested:
            results.append(
                ExitResult(
                    name=key,
                    api_key=key,
                    status="error",
                    message="No authenticated trading sessions found.",
                    orders_placed=[],
                )
            )
        return ExitResponse(results=results)

    for api_key in target_keys:
        acct_info = manager.get_account_info(api_key)
        name = acct_info.get("name", api_key)

        try:
            raw_exits = manager.exit_positions(
                api_key=api_key,
                tradingsymbol=request.tradingsymbol,
            )
            placed_details = [
                ExitPositionDetail(
                    tradingsymbol=item["tradingsymbol"],
                    quantity=item["quantity"],
                    product=item["product"],
                    order_id=item["order_id"],
                )
                for item in raw_exits
            ]
            msg = (
                f"Successfully placed {len(placed_details)} exit order(s)."
                if placed_details
                else "No active positions found to exit."
            )
            results.append(
                ExitResult(
                    name=name,
                    api_key=api_key,
                    status="success",
                    message=msg,
                    orders_placed=placed_details,
                )
            )
        except Exception as exc:
            logger.exception(
                "Exit positions failed in account %s (api_key=%s…): %s",
                name,
                api_key[:8],
                exc,
            )
            results.append(
                ExitResult(
                    name=name,
                    api_key=api_key,
                    status="error",
                    message=str(exc),
                    orders_placed=[],
                )
            )

    return ExitResponse(results=results)


@router.post("/oc", response_model=OptionChainResponse)
async def get_option_chain(request: OptionChainRequest) -> OptionChainResponse:
    """Fetch option chain for a specific underlying and expiry.

    Uses kite.instruments("NFO") under the hood, filtered to the requested
    underlying name and expiry week (or exact expiry_date). LTP prices are
    fetched via a single batch kite.ltp() call.
    """
    try:
        result = manager.get_option_chain(
            api_key=request.api_key,
            underlying=request.underlying,
            expiry_week=request.expiry_week,
            expiry_date=request.expiry_date,
        )
        return OptionChainResponse(
            underlying=result["underlying"],
            expiry=result["expiry"],
            strikes=[OptionChainEntry(**s) for s in result["strikes"]],
            available_expiries=[OptionChainExpiry(**e) for e in result["available_expiries"]],
            status=result["status"],
            message=result.get("message", ""),
        )
    except Exception as exc:
        logger.exception("Option chain fetch failed: %s", exc)
        return OptionChainResponse(
            underlying=request.underlying,
            expiry="",
            strikes=[],
            available_expiries=[],
            status="error",
            message=str(exc),
        )


@router.post("/orders", response_model=OrdersResponse)
async def get_orders(request: OrdersRequest) -> OrdersResponse:
    """Fetch today's order book for specified accounts.

    Returns all orders (both pending and executed) grouped by account.
    Filter by status client-side: OPEN/TRIGGER PENDING = pending, COMPLETE = executed.
    If api_keys is empty, fetches from all authenticated accounts.
    """
    target_keys = request.api_keys if request.api_keys else [
        k for k in manager.get_all_api_keys() if manager.is_authenticated(k)
    ]

    accounts: list[AccountOrders] = []
    for api_key in target_keys:
        acct_info = manager.get_account_info(api_key)
        name = acct_info.get("name", api_key)
        try:
            raw_orders = manager.get_orders(api_key)
            orders = []
            for o in raw_orders:
                orders.append(OrderEntry(
                    order_id=str(o.get("order_id", "")),
                    tradingsymbol=o.get("tradingsymbol", ""),
                    exchange=o.get("exchange", ""),
                    transaction_type=o.get("transaction_type", ""),
                    quantity=int(o.get("quantity") or 0),
                    filled_quantity=int(o.get("filled_quantity") or 0),
                    pending_quantity=int(o.get("pending_quantity") or 0),
                    price=float(o.get("price") or 0),
                    average_price=float(o.get("average_price") or 0),
                    trigger_price=float(o.get("trigger_price") or 0),
                    order_type=o.get("order_type", ""),
                    status=o.get("status", ""),
                    product=o.get("product", ""),
                    status_message=(o.get("status_message") or ""),
                ))
            accounts.append(AccountOrders(
                name=name, api_key=api_key, orders=orders, status="success"
            ))
        except Exception as exc:
            logger.exception("Error fetching orders for api_key=%s…", api_key[:8])
            accounts.append(AccountOrders(
                name=name, api_key=api_key, orders=[], status=f"error: {exc}"
            ))

    return OrdersResponse(accounts=accounts)


@router.get("/indices", response_model=MarketIndicesResponse)
async def get_market_indices() -> MarketIndicesResponse:
    """Fetch live Nifty, Sensex, and India VIX LTP."""
    result = manager.get_market_indices()
    return MarketIndicesResponse(**result)
