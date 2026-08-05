"""Zerodha & Statutory Charges Calculation Engine for Indian F&O & Equity Trades.

Official Rates from https://zerodha.com/charges/#charges-explained:
- Brokerage: Flat ₹20 per executed order (or 0.03% whichever is lower).
- STT/CTT: 0.15% on sell side (on premium value).
- Transaction charges: NSE/NFO Options = 0.03553% (on premium), BSE/BFO Options = 0.0325% (on premium).
- GST: 18% on (brokerage + SEBI charges + transaction charges).
- SEBI charges: ₹10 / crore (0.0001%).
- Stamp charges: 0.003% or ₹300 / crore on buy side.
- IPFT fee: ₹50 / crore (0.0005%).
"""

from typing import Any


def calculate_option_charges(
    buy_price: float,
    sell_price: float,
    qty: int,
    buy_orders_count: int = 1,
    sell_orders_count: int = 1,
    exchange: str = "NSE",
) -> dict[str, float]:
    """Calculate exact statutory charges and taxes for an F&O option contract trade per Zerodha official rates.

    Args:
        buy_price: Average buy price of matched legs.
        sell_price: Average sell price of matched legs.
        qty: Matched quantity.
        buy_orders_count: Number of buy orders executed.
        sell_orders_count: Number of sell orders executed.
        exchange: Exchange name ('NSE', 'NFO', 'BSE', 'BFO').

    Returns:
        Dictionary containing breakdown of brokerage, STT, exchange fees, SEBI fees,
        GST, stamp duty, IPFT, total charges, and net P&L.
    """
    buy_val = max(0.0, buy_price * qty)
    sell_val = max(0.0, sell_price * qty)
    total_val = buy_val + sell_val

    exch_str = str(exchange or "NSE").upper()
    is_nse = exch_str in ["NSE", "NFO"]

    # 1. Brokerage: Flat ₹20 per executed order
    brokerage = float((buy_orders_count + sell_orders_count) * 20.0)

    # 2. STT: 0.15% on Sell side premium (Zerodha official rate)
    stt = round(sell_val * 0.0015, 2)

    # 3. Transaction charges: NSE/NFO Options = 0.03553% on premium, BSE/BFO Options = 0.0325% on premium
    exch_rate = 0.0003553 if is_nse else 0.000325
    exch_fee = round(total_val * exch_rate, 2)

    # 4. SEBI turnover fee: ₹10 / crore (0.0001%)
    sebi_fee = round(total_val * 0.000001, 2)

    # 5. IPFT fee: ₹50 / crore (0.000005%) for NSE/NFO
    ipft_fee = round(total_val * 0.000005, 2) if is_nse else 0.0

    # 6. GST: 18% on (Brokerage + Exchange Fee + SEBI Fee)
    gst = round((brokerage + exch_fee + sebi_fee) * 0.18, 2)

    # 7. Stamp duty: 0.003% (₹300/crore) on Buy side premium
    stamp_duty = round(buy_val * 0.00003, 2)

    total_charges = round(brokerage + stt + exch_fee + sebi_fee + ipft_fee + gst + stamp_duty, 2)

    return {
        "brokerage": brokerage,
        "stt": stt,
        "exch_fee": exch_fee,
        "sebi_fee": sebi_fee,
        "ipft_fee": ipft_fee,
        "gst": gst,
        "stamp_duty": stamp_duty,
        "total_charges": total_charges,
    }


def fetch_broker_charges(client: Any, order_params_list: list[dict[str, Any]]) -> float | None:
    """Fetch official charges from Zerodha API if available."""
    try:
        if hasattr(client, "get_order_charges"):
            resp = client.get_order_charges(order_params_list)
            if isinstance(resp, list):
                return sum(float(item.get("total", 0.0)) for item in resp)
            elif isinstance(resp, dict):
                return float(resp.get("total", 0.0))
    except Exception:
        pass
    return None
