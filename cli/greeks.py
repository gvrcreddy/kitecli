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
    spot_price: float, strike_price: float, T: float, option_price: float, is_call: bool, r: float = 0.07
) -> float | None:
    """Solve for Implied Volatility (IV %) from option price using Newton-Raphson."""
    if option_price <= 0 or T <= 0:
        return None
    sigma = 0.25
    for _ in range(100):
        d1 = (math.log(spot_price / strike_price) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if is_call:
            price = spot_price * _norm_cdf(d1) - strike_price * math.exp(-r * T) * _norm_cdf(d2)
        else:
            price = strike_price * math.exp(-r * T) * _norm_cdf(-d2) - spot_price * _norm_cdf(-d1)

        diff = price - option_price
        if abs(diff) < 1e-4:
            return sigma * 100.0

        vega = spot_price * math.sqrt(T) * _norm_pdf(d1)
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
    risk_free_rate: float = 0.07,
    option_price: float | None = None,
) -> Dict[str, Optional[float]]:
    """Calculate Black-Scholes Option Greeks.

    Args:
        spot_price: Current spot price of underlying index/stock (e.g. 24500.0)
        strike_price: Strike price of option (e.g. 24500.0)
        expiry_date: Expiry date string (YYYY-MM-DD), date, or datetime object
        option_type: 'CE' / 'CALL' or 'PE' / 'PUT'
        vix_pct: Baseline Volatility percentage (e.g. 15.2 for 15.2% India VIX)
        risk_free_rate: Annual risk-free interest rate (default 7% = 0.07)
        option_price: Optional option market price (LTP) to solve for exact IV.

    Returns:
        Dict containing delta, gamma, theta, vega, and iv.
    """
    if spot_price <= 0 or strike_price <= 0:
        return {"delta": None, "gamma": None, "theta": None, "vega": None, "iv": None}

    # Calculate time to expiry in years
    if isinstance(expiry_date, str):
        try:
            exp_dt = datetime.strptime(expiry_date, "%Y-%m-%d")
        except ValueError:
            exp_dt = datetime.now() + timedelta(days=7)
    elif isinstance(expiry_date, date) and not isinstance(expiry_date, datetime):
        exp_dt = datetime.combine(expiry_date, datetime.min.time())
    else:
        exp_dt = expiry_date

    now = datetime.now()
    days_to_exp = max((exp_dt - now).total_seconds() / 86400.0, 0.25)
    T = days_to_exp / 365.0

    opt_type_upper = option_type.upper()
    is_call = "CE" in opt_type_upper or "CALL" in opt_type_upper

    eff_vix = vix_pct
    if option_price and option_price > 0:
        solved_iv = implied_volatility(spot_price, strike_price, T, option_price, is_call, risk_free_rate)
        if not solved_iv or solved_iv <= 0:
            return {"delta": None, "gamma": None, "theta": None, "vega": None, "iv": None}
        eff_vix = solved_iv

    sigma = max(eff_vix / 100.0, 0.01)

    # Black-Scholes d1 and d2 calculations
    d1 = (math.log(spot_price / strike_price) + (risk_free_rate + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    opt_type_upper = option_type.upper()
    is_call = "CE" in opt_type_upper or "CALL" in opt_type_upper

    # 1. Delta
    if is_call:
        delta = _norm_cdf(d1)
    else:
        delta = _norm_cdf(d1) - 1.0

    # 2. Gamma
    gamma = _norm_pdf(d1) / (spot_price * sigma * math.sqrt(T))

    # 3. Theta (daily decay)
    term1 = -(spot_price * _norm_pdf(d1) * sigma) / (2.0 * math.sqrt(T))
    if is_call:
        term2 = -risk_free_rate * strike_price * math.exp(-risk_free_rate * T) * _norm_cdf(d2)
    else:
        term2 = risk_free_rate * strike_price * math.exp(-risk_free_rate * T) * _norm_cdf(-d2)
    theta = (term1 + term2) / 365.0

    # 4. Vega (per 1% change in volatility)
    vega = (spot_price * math.sqrt(T) * _norm_pdf(d1)) / 100.0

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 2),
        "vega": round(vega, 2),
        "iv": round(eff_vix, 2),
    }
