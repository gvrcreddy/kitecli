"""
Technical indicators and daily Nifty candle cache manager for KiteCLI.

Manages ~/.kcli/nifty_daily.json and calculates Daily EMA-9, SMA-20, SMA-50, and RSI-14.
"""

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger("kitecli.indicators")

CACHE_PATH = Path.home() / ".kcli" / "nifty_daily.json"


def get_cached_daily_closes() -> tuple[Optional[str], List[float]]:
    """Read cached daily close prices from ~/.kcli/nifty_daily.json.

    Returns:
        (last_updated_date, daily_closes_list)
    """
    if not CACHE_PATH.exists():
        return None, []

    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            last_date = data.get("last_updated_date")
            closes = data.get("daily_closes", [])
            return last_date, closes
    except Exception as exc:
        logger.warning("Failed to read nifty_daily.json cache: %s", exc)
        return None, []


def save_cached_daily_closes(daily_closes: List[float]) -> None:
    """Save daily close prices to ~/.kcli/nifty_daily.json with today's date."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    today_str = date.today().isoformat()
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "last_updated_date": today_str,
                "daily_closes": daily_closes,
            }, f, indent=2)
        logger.info("Saved %d daily closes to %s for date %s", len(daily_closes), CACHE_PATH, today_str)
    except Exception as exc:
        logger.error("Failed to save nifty_daily.json cache: %s", exc)


def fetch_or_get_daily_closes(client: Any) -> List[float]:
    """Fetch 100 daily closing prices from Zerodha if missing or outdated, else return cached values."""
    today_str = date.today().isoformat()
    last_date, closes = get_cached_daily_closes()

    if last_date == today_str and len(closes) >= 20:
        logger.debug("Using cached daily closes for date %s (len=%d)", today_str, len(closes))
        return closes

    # First order of the day: fetch 100 daily candles from Zerodha REST API
    logger.info("First completed order of the day (%s): fetching Nifty daily candles from Zerodha...", today_str)
    try:
        # NIFTY 50 instrument token = 256265
        token = 256265
        to_date = date.today()
        from_date = to_date - timedelta(days=365)

        # Retrieve a valid authenticated Zerodha client instance
        kite = None
        if hasattr(client, "_account_manager_map"):
            for key, mgr in getattr(client, "_account_manager_map", {}).items():
                if hasattr(mgr, "_clients") and key in mgr._clients:
                    kite = mgr._clients[key]
                    break
        elif hasattr(client, "_clients"):
            for key, client_inst in getattr(client, "_clients", {}).items():
                kite = client_inst
                break

        if not kite:
            try:
                from cli.api_client import _account_manager_map, _kite_manager
                for key, mgr in _account_manager_map.items():
                    if getattr(mgr, "broker_name", "") == "zerodha":
                        if hasattr(mgr, "_clients") and key in mgr._clients and mgr.is_authenticated(key):
                            kite = mgr._clients[key]
                            break
                if not kite:
                    for key, client_inst in getattr(_kite_manager, "_clients", {}).items():
                        if _kite_manager.is_authenticated(key):
                            kite = client_inst
                            break
            except Exception as exc:
                logger.warning("Failed to locate Zerodha client: %s", exc)

        if not kite:
            logger.warning("No authenticated Zerodha client available to fetch daily candles.")
            return closes

        records = kite.historical_data(
            instrument_token=token,
            from_date=from_date.strftime("%Y-%m-%d"),
            to_date=to_date.strftime("%Y-%m-%d"),
            interval="day",
        )
        if records:
            fetched_closes = [float(r["close"]) for r in records][-200:]
            if fetched_closes:
                save_cached_daily_closes(fetched_closes)
                return fetched_closes
    except Exception as exc:
        logger.error("Failed to fetch Nifty daily historical candles: %s", exc)

    return closes


def calculate_daily_indicators(
    client: Any, live_nifty_spot: float
) -> Dict[str, Optional[float]]:
    """Compute Daily EMA-9, SMA-20, SMA-50, SMA-200, RSI-14, MACD, Bollinger Bands, Distance %, and Returns."""
    empty_res = {
        "nifty_ema_9": None, "nifty_sma_20": None, "nifty_sma_50": None, "nifty_sma_200": None, "nifty_rsi_14": None,
        "nifty_macd_line": None, "nifty_macd_signal": None, "nifty_macd_hist": None,
        "nifty_bb_upper": None, "nifty_bb_lower": None, "nifty_bb_pct_b": None, "nifty_bb_width": None,
        "nifty_dist_sma_20_pct": None, "nifty_dist_sma_50_pct": None, "nifty_dist_sma_200_pct": None,
        "nifty_ret_1d_pct": None, "nifty_ret_5d_pct": None, "nifty_ret_20d_pct": None,
    }
    historical_closes = fetch_or_get_daily_closes(client)
    if not historical_closes:
        return empty_res

    if live_nifty_spot <= 0:
        live_nifty_spot = historical_closes[-1]

    if live_nifty_spot <= 0:
        return empty_res

    # Append live Nifty spot as today's forming daily close
    all_closes = historical_closes + [live_nifty_spot]
    df = pd.DataFrame({"close": all_closes})

    # 1. EMAs & SMAs
    df["ema_9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema_12"] = df["close"].ewm(span=12, adjust=False).mean()
    df["ema_26"] = df["close"].ewm(span=26, adjust=False).mean()
    df["sma_20"] = df["close"].rolling(window=min(20, len(df))).mean()
    df["sma_50"] = df["close"].rolling(window=min(50, len(df))).mean()
    df["sma_200"] = df["close"].rolling(window=min(200, len(df))).mean()

    # 2. RSI 14
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0.0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=14).mean()
    rs = gain / loss
    df["rsi_14"] = 100.0 - (100.0 / (1.0 + rs))

    # 3. MACD (12, 26, 9)
    df["macd_line"] = df["ema_12"] - df["ema_26"]
    df["macd_signal"] = df["macd_line"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd_line"] - df["macd_signal"]

    # 4. Bollinger Bands (20, 2)
    std_20 = df["close"].rolling(window=min(20, len(df))).std()
    df["bb_upper"] = df["sma_20"] + (2.0 * std_20)
    df["bb_lower"] = df["sma_20"] - (2.0 * std_20)
    bb_range = df["bb_upper"] - df["bb_lower"]
    df["bb_pct_b"] = (df["close"] - df["bb_lower"]) / bb_range.replace(0, 1.0)
    df["bb_width"] = bb_range / df["sma_20"].replace(0, 1.0)

    # 5. Normalized Distance % to Moving Averages (Stationary ML features)
    df["dist_sma_20_pct"] = ((df["close"] - df["sma_20"]) / df["sma_20"].replace(0, 1.0)) * 100.0
    df["dist_sma_50_pct"] = ((df["close"] - df["sma_50"]) / df["sma_50"].replace(0, 1.0)) * 100.0
    df["dist_sma_200_pct"] = ((df["close"] - df["sma_200"]) / df["sma_200"].replace(0, 1.0)) * 100.0

    # 6. Multi-period Percentage Returns
    df["ret_1d_pct"] = df["close"].pct_change(1) * 100.0
    df["ret_5d_pct"] = df["close"].pct_change(5) * 100.0
    df["ret_20d_pct"] = df["close"].pct_change(20) * 100.0

    latest = df.iloc[-1]

    def _clean_val(v, decimals=2):
        return round(float(v), decimals) if pd.notna(v) else None

    return {
        "nifty_ema_9": _clean_val(latest["ema_9"]),
        "nifty_sma_20": _clean_val(latest["sma_20"]),
        "nifty_sma_50": _clean_val(latest["sma_50"]),
        "nifty_sma_200": _clean_val(latest["sma_200"]),
        "nifty_rsi_14": _clean_val(latest["rsi_14"]),
        "nifty_macd_line": _clean_val(latest["macd_line"]),
        "nifty_macd_signal": _clean_val(latest["macd_signal"]),
        "nifty_macd_hist": _clean_val(latest["macd_hist"]),
        "nifty_bb_upper": _clean_val(latest["bb_upper"]),
        "nifty_bb_lower": _clean_val(latest["bb_lower"]),
        "nifty_bb_pct_b": _clean_val(latest["bb_pct_b"], 4),
        "nifty_bb_width": _clean_val(latest["bb_width"], 4),
        "nifty_dist_sma_20_pct": _clean_val(latest["dist_sma_20_pct"]),
        "nifty_dist_sma_50_pct": _clean_val(latest["dist_sma_50_pct"]),
        "nifty_dist_sma_200_pct": _clean_val(latest["dist_sma_200_pct"]),
        "nifty_ret_1d_pct": _clean_val(latest["ret_1d_pct"]),
        "nifty_ret_5d_pct": _clean_val(latest["ret_5d_pct"]),
        "nifty_ret_20d_pct": _clean_val(latest["ret_20d_pct"]),
    }
