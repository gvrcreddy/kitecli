import calendar
import math
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
MONTH_CODE_MAP = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
    "7": 7, "8": 8, "9": 9, "O": 10, "N": 11, "D": 12,
    "01": 1, "02": 2, "03": 3, "04": 4, "05": 5, "06": 6,
    "07": 7, "08": 8, "09": 9, "10": 10, "11": 11, "12": 12,
}

# Standard Normal Cumulative Distribution Function
def _norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


# Standard Normal Probability Density Function
def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def parse_nfo_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    """Parse NFO option tradingsymbol into strike, option_type, and estimated expiry date."""
    symbol = symbol.strip().upper()
    if not (symbol.endswith("CE") or symbol.endswith("PE")):
        return None

    opt_type = "PE" if symbol.endswith("PE") else "CE"
    core = symbol[:-2]

    # Pattern A: Monthly (e.g. NIFTY26JUL22500 or BANKNIFTY26AUG50000)
    m_monthly = re.match(r"^([A-Z]+)(\d{2})([A-Z]{3})(\d+(?:\.\d+)?)$", core)
    if m_monthly:
        underlying, yy, mon_str, strike_str = m_monthly.groups()
        year = 2000 + int(yy)
        month = MONTH_MAP.get(mon_str, date.today().month)
        strike = float(strike_str)
        cal = calendar.monthcalendar(year, month)
        thursdays = [week[calendar.THURSDAY] for week in cal if week[calendar.THURSDAY] != 0]
        expiry_day = thursdays[-1] if thursdays else 28
        exp_date = date(year, month, expiry_day)
        return {"underlying": underlying, "option_type": opt_type, "strike": strike, "expiry_date": exp_date}

    # Pattern B: Weekly (e.g. NIFTY2672522500 -> YY=26, M=7, DD=25, STRIKE=22500, or NIFTY2690122800 -> YY=26, M=9, DD=01, STRIKE=22800)
    m_weekly = re.match(r"^([A-Z]+)(\d{2})(0[1-9]|1[0-2]|[1-9OND])(\d{2})(\d+(?:\.\d+)?)$", core)
    if m_weekly:
        underlying, yy, m_code, dd_str, strike_str = m_weekly.groups()
        year = 2000 + int(yy)
        month = MONTH_CODE_MAP.get(m_code, date.today().month)
        day = int(dd_str)
        strike = float(strike_str)
        try:
            exp_date = date(year, month, day)
        except ValueError:
            exp_date = date.today() + timedelta(days=7)
        return {"underlying": underlying, "option_type": opt_type, "strike": strike, "expiry_date": exp_date}

    # Fallback pattern if non-standard string:
    nums = re.findall(r"\d+", core)
    if nums:
        strike = float(nums[-1])
        exp_date = date.today() + timedelta(days=7)
        return {"underlying": "NIFTY", "option_type": opt_type, "strike": strike, "expiry_date": exp_date}

    return None


def get_position_expiry_info(pos: Dict[str, Any]) -> Dict[str, Any]:
    """Extract expiry date, strike, option type, and underlying from a position dict.

    Returns a dict with:
      - 'expiry_date': date object or None (for equity/non-expiring)
      - 'underlying': str
      - 'strike': float (0.0 for non-options)
      - 'option_type': 'CE' | 'PE' | 'FUT' | 'EQ'
      - 'is_option': bool
      - 'is_future': bool
    """
    sym = str(pos.get("tradingsymbol", "")).strip().upper()

    # 1. Check if pos has a direct expiry field
    raw_exp = pos.get("expiry")
    exp_date = None
    if isinstance(raw_exp, date) and not isinstance(raw_exp, datetime):
        exp_date = raw_exp
    elif isinstance(raw_exp, datetime):
        exp_date = raw_exp.date()
    elif isinstance(raw_exp, str) and raw_exp:
        try:
            exp_date = datetime.strptime(raw_exp[:10], "%Y-%m-%d").date()
        except Exception:
            pass

    # 2. If it's an option (ends with CE or PE)
    if sym.endswith("CE") or sym.endswith("PE"):
        parsed = parse_nfo_symbol(sym)
        if parsed:
            return {
                "expiry_date": exp_date or parsed.get("expiry_date"),
                "underlying": parsed.get("underlying", sym),
                "strike": float(parsed.get("strike", 0.0)),
                "option_type": parsed.get("option_type", "CE"),
                "is_option": True,
                "is_future": False,
            }
        opt_type = "PE" if sym.endswith("PE") else "CE"
        return {
            "expiry_date": exp_date,
            "underlying": sym,
            "strike": 0.0,
            "option_type": opt_type,
            "is_option": True,
            "is_future": False,
        }

    # 3. If it's a future (ends with FUT)
    if sym.endswith("FUT"):
        m_fut = re.match(r"^([A-Z]+)(\d{2})([A-Z]{3})FUT$", sym)
        if m_fut:
            underlying, yy, mon_str = m_fut.groups()
            year = 2000 + int(yy)
            month = MONTH_MAP.get(mon_str, date.today().month)
            cal = calendar.monthcalendar(year, month)
            thursdays = [week[calendar.THURSDAY] for week in cal if week[calendar.THURSDAY] != 0]
            expiry_day = thursdays[-1] if thursdays else 28
            fut_exp = date(year, month, expiry_day)
            return {
                "expiry_date": exp_date or fut_exp,
                "underlying": underlying,
                "strike": 0.0,
                "option_type": "FUT",
                "is_option": False,
                "is_future": True,
            }
        return {
            "expiry_date": exp_date,
            "underlying": sym,
            "strike": 0.0,
            "option_type": "FUT",
            "is_option": False,
            "is_future": True,
        }

    # 4. Equity / Other
    return {
        "expiry_date": exp_date,
        "underlying": sym,
        "strike": 0.0,
        "option_type": "EQ",
        "is_option": False,
        "is_future": False,
    }


def format_expiry_header(exp_date: Optional[date], today: Optional[date] = None) -> tuple[str, str]:
    """Return (title_label, dte_label).
    e.g. ('28 AUG 2026', 'Today • 0 DTE') or ('04 SEP 2026', 'Next Week • 7 DTE') or ('EQUITY / OTHER', '')
    """
    if exp_date is None:
        return "EQUITY / OTHER", ""

    if today is None:
        today = date.today()

    dte = (exp_date - today).days
    date_str = exp_date.strftime("%d %b %Y").upper()

    if dte == 0:
        dte_str = "Today • 0 DTE"
    elif dte == 1:
        dte_str = "Tomorrow • 1 DTE"
    elif 2 <= dte <= 7:
        dte_str = f"This Week • {dte} DTE"
    elif 8 <= dte <= 14:
        dte_str = f"Next Week • {dte} DTE"
    elif dte > 14:
        dte_str = f"{dte} DTE"
    else:
        dte_str = f"Expired ({abs(dte)}d ago)"

    return date_str, dte_str


def format_strike_display(info: Dict[str, Any]) -> str:
    """Format human-readable strike/type string like '22800 PE', '51500 CE', 'FUT', 'EQ'."""
    if info.get("is_option"):
        strike = info.get("strike", 0.0)
        strike_str = f"{int(strike)}" if strike == int(strike) else f"{strike:.1f}"
        opt_type = info.get("option_type", "")
        return f"{strike_str} {opt_type}"
    elif info.get("is_future"):
        return "FUT"
    return "EQ"


def implied_volatility(
    spot_price: float,
    strike_price: float,
    T: float,
    option_price: float,
    is_call: bool,
    r: float = 0.0,
    forward_price: float | None = None,
) -> float | None:
    """Solve for Implied Volatility (IV %) from option price using Black-76 Newton-Raphson."""
    F = forward_price if (forward_price and forward_price > 0) else spot_price
    if option_price <= 0 or T <= 0 or F <= 0 or strike_price <= 0:
        return None

    intrinsic = max(0.0, (F - strike_price) if is_call else (strike_price - F))
    if option_price < intrinsic - 1e-4:
        return None

    sigma = 0.20
    df = math.exp(-r * T)
    for _ in range(100):
        d1 = (math.log(F / strike_price) + (0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if is_call:
            price = df * (F * _norm_cdf(d1) - strike_price * _norm_cdf(d2))
        else:
            price = df * (strike_price * _norm_cdf(-d2) - F * _norm_cdf(-d1))

        diff = price - option_price
        if abs(diff) < 1e-4:
            return sigma * 100.0

        vega = df * F * math.sqrt(T) * _norm_pdf(d1)
        if vega < 1e-8:
            break
        sigma -= diff / vega
        if sigma <= 0.001 or sigma > 5.0:
            break
    return None


def calculate_greeks_for_symbol(
    symbol: str, spot_price: float, vix_pct: float = 15.0, option_price: float | None = None
) -> Dict[str, Optional[float]]:
    """Helper to calculate Option Greeks directly from a tradingsymbol."""
    parsed = parse_nfo_symbol(symbol)
    if not parsed:
        return {"delta": None, "gamma": None, "theta": None, "vega": None, "iv": None}

    return calculate_greeks(
        spot_price=spot_price,
        strike_price=parsed["strike"],
        expiry_date=parsed["expiry_date"],
        option_type=parsed["option_type"],
        vix_pct=vix_pct,
        option_price=option_price,
    )


def calculate_greeks(
    spot_price: float,
    strike_price: float,
    expiry_date: str | datetime | date,
    option_type: str,
    vix_pct: float = 15.0,
    risk_free_rate: float = 0.0,
    option_price: float | None = None,
    forward_price: float | None = None,
) -> Dict[str, Optional[float]]:
    """Calculate Black-76 Option Greeks using Forward / Synthetic Future price.

    Args:
        spot_price: Current spot price of underlying index/stock (e.g. 24055.80)
        strike_price: Strike price of option (e.g. 24000.0)
        expiry_date: Expiry date string (YYYY-MM-DD), date, or datetime object
        option_type: 'CE' / 'CALL' or 'PE' / 'PUT'
        vix_pct: Baseline Volatility percentage (e.g. 15.2 for 15.2% India VIX)
        risk_free_rate: Annual risk-free interest rate (default 0.0 for Forward pricing)
        option_price: Optional option market price (LTP) to solve for exact IV.
        forward_price: Optional Synthetic Forward price (F) from Put-Call Parity.

    Returns:
        Dict containing delta, gamma, theta, vega, and iv.
    """
    F = forward_price if (forward_price and forward_price > 0) else spot_price
    if F <= 0 or strike_price <= 0:
        return {"delta": None, "gamma": None, "theta": None, "vega": None, "iv": None}

    # Calculate time to expiry in years targeting 15:30 IST market close
    from datetime import time as _time
    if isinstance(expiry_date, str):
        try:
            exp_d = datetime.strptime(expiry_date[:10], "%Y-%m-%d").date()
        except ValueError:
            exp_d = date.today() + timedelta(days=7)
    elif isinstance(expiry_date, datetime):
        exp_d = expiry_date.date()
    elif isinstance(expiry_date, date):
        exp_d = expiry_date
    else:
        exp_d = date.today() + timedelta(days=7)

    exp_dt = datetime.combine(exp_d, _time(15, 30))
    now = datetime.now()
    seconds = max((exp_dt - now).total_seconds(), 0.0)
    # Minimum 0.05 days (~1.2 hours) for numerical stability on expiry day close
    days_to_exp = max(seconds / 86400.0, 0.05)
    T = days_to_exp / 365.0

    opt_type_upper = option_type.upper()
    is_call = "CE" in opt_type_upper or "CALL" in opt_type_upper

    eff_vix = vix_pct
    if option_price and option_price > 0:
        solved_iv = implied_volatility(
            spot_price=spot_price,
            strike_price=strike_price,
            T=T,
            option_price=option_price,
            is_call=is_call,
            r=risk_free_rate,
            forward_price=F,
        )
        if solved_iv and solved_iv > 0:
            eff_vix = solved_iv

    sigma = max(eff_vix / 100.0, 0.01)
    df = math.exp(-risk_free_rate * T)

    # Black-76 d1 and d2 calculations
    d1 = (math.log(F / strike_price) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    # 1. Delta
    if is_call:
        delta = df * _norm_cdf(d1)
    else:
        delta = df * (_norm_cdf(d1) - 1.0)

    # 2. Gamma
    gamma = (df * _norm_pdf(d1)) / (F * sigma * math.sqrt(T))

    # 3. Theta (daily decay)
    theta = -(F * _norm_pdf(d1) * sigma) / (2.0 * math.sqrt(T) * 365.0)

    # 4. Vega (per 1% change in volatility)
    vega = (df * F * math.sqrt(T) * _norm_pdf(d1)) / 100.0

    return {
        "delta": round(delta, 2),
        "gamma": round(gamma, 6),
        "theta": round(theta, 2),
        "vega": round(vega, 2),
        "iv": round(eff_vix, 2),
    }


def enrich_option_chain_greeks(
    strikes: list[dict],
    spot_price: float,
    expiry_date: str | datetime | date,
    vix_pct: float = 15.0,
    risk_free_rate: float = 0.0,
) -> Dict[str, Any]:
    """Calculate Greeks for every strike in the option chain using Black-76 forward model.

    Derives the Synthetic Forward Price (F) from Put-Call parity across liquid pairs.

    Returns a summary dict with:
      - 'atm_strike': float
      - 'atm_straddle': float | None
      - 'pcr': float
      - 'max_pain': float
      - 'total_ce_oi': int
      - 'total_pe_oi': int
      - 'forward_price': float
    """
    if not strikes or spot_price <= 0:
        return {
            "atm_strike": 0.0,
            "atm_straddle": None,
            "pcr": 0.0,
            "max_pain": 0.0,
            "total_ce_oi": 0,
            "total_pe_oi": 0,
            "forward_price": 0.0,
        }

    # 1. Derive Forward Price (F) from liquid strike pairs via Put-Call Parity:
    # F_i = K_i + CE_ltp_i - PE_ltp_i
    fwd_estimates = []
    for s in strikes:
        k = float(s.get("strike", 0.0))
        ce = s.get("ce_ltp")
        pe = s.get("pe_ltp")
        if ce is not None and pe is not None and ce > 0 and pe > 0:
            if spot_price <= 0 or abs(k - spot_price) / spot_price <= 0.05:
                fwd_estimates.append(k + ce - pe)

    if fwd_estimates:
        fwd_estimates.sort()
        mid = len(fwd_estimates) // 2
        forward_price = (
            fwd_estimates[mid]
            if len(fwd_estimates) % 2 == 1
            else (fwd_estimates[mid - 1] + fwd_estimates[mid]) / 2.0
        )
    else:
        forward_price = spot_price

    total_ce_oi = 0
    total_pe_oi = 0
    atm_strike = None
    min_fwd_diff = float("inf")
    atm_straddle = None

    for s in strikes:
        strike = float(s.get("strike", 0.0))
        ce_ltp = s.get("ce_ltp")
        pe_ltp = s.get("pe_ltp")
        ce_oi = int(s.get("ce_oi") or 0)
        pe_oi = int(s.get("pe_oi") or 0)
        total_ce_oi += ce_oi
        total_pe_oi += pe_oi

        # Find ATM strike (closest strike to Forward Price)
        diff = abs(strike - forward_price)
        if diff < min_fwd_diff:
            min_fwd_diff = diff
            atm_strike = strike
            if ce_ltp is not None and pe_ltp is not None:
                atm_straddle = round(ce_ltp + pe_ltp, 2)
            else:
                atm_straddle = None

        # Calculate CE Greeks
        if ce_ltp is not None and ce_ltp > 0:
            ce_g = calculate_greeks(
                spot_price=spot_price,
                strike_price=strike,
                expiry_date=expiry_date,
                option_type="CE",
                vix_pct=vix_pct,
                risk_free_rate=risk_free_rate,
                option_price=ce_ltp,
                forward_price=forward_price,
            )
            s["ce_delta"] = ce_g.get("delta")
            s["ce_iv"] = ce_g.get("iv")
            s["ce_theta"] = ce_g.get("theta")
        else:
            s["ce_delta"] = None
            s["ce_iv"] = None
            s["ce_theta"] = None

        # Calculate PE Greeks
        if pe_ltp is not None and pe_ltp > 0:
            pe_g = calculate_greeks(
                spot_price=spot_price,
                strike_price=strike,
                expiry_date=expiry_date,
                option_type="PE",
                vix_pct=vix_pct,
                risk_free_rate=risk_free_rate,
                option_price=pe_ltp,
                forward_price=forward_price,
            )
            s["pe_delta"] = pe_g.get("delta")
            s["pe_iv"] = pe_g.get("iv")
            s["pe_theta"] = pe_g.get("theta")
        else:
            s["pe_delta"] = None
            s["pe_iv"] = None
            s["pe_theta"] = None

    # Calculate PCR
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0.0

    # Calculate Max Pain
    max_pain = atm_strike or 0.0
    min_total_payout = float("inf")
    for target in strikes:
        k_target = float(target.get("strike", 0.0))
        total_payout = 0.0
        for s in strikes:
            k = float(s.get("strike", 0.0))
            ce_oi = int(s.get("ce_oi") or 0)
            pe_oi = int(s.get("pe_oi") or 0)
            if k_target > k:
                total_payout += (k_target - k) * ce_oi
            elif k_target < k:
                total_payout += (k - k_target) * pe_oi
        if total_payout < min_total_payout:
            min_total_payout = total_payout
            max_pain = k_target

    return {
        "atm_strike": atm_strike,
        "atm_straddle": atm_straddle,
        "pcr": pcr,
        "max_pain": max_pain,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "forward_price": round(forward_price, 2),
    }

