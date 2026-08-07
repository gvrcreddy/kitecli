# 📊 Indian F&O Options Statutory Charges & Taxes Guide

This document outlines the exact mathematical formulas, statutory tax breakdown, and code implementations used in **KiteCLI** to compute brokerage, government taxes, and exchange fees for F&O Option trades.

---

## 📚 1. Official Regulatory Standards

KiteCLI implements the official statutory rates defined by **SEBI**, **NSE/BSE**, **Ministry of Finance**, and **Broker Tariff Sheets**:

- **Brokerage**: Flat **₹20** per executed order (Buy/Sell).
- **STT (Securities Transaction Tax)**: **0.15%** on Sell-side option premium value.
- **Exchange Turnover Charges**: **0.03553%** on total option premium value (NSE/NFO).
- **SEBI Turnover Charges**: **₹10 per Crore** (0.0001%) on total premium value.
- **IPFT Fee (Investor Protection Fund)**: **₹0.01 per Crore** (0.0000001%) on total premium value (NSE/NFO).
- **GST (Goods and Services Tax)**: **18%** applied on *(Brokerage + Exchange Fee + SEBI Fee)*.
- **Stamp Duty**: **0.003%** (₹300 per Crore) on Buy-side option premium value.

---

## 🧮 2. Mathematical Formulas

### Base Turnovers
$$\text{Buy Turnover} = \text{Buy Price} \times \text{Quantity}$$

$$\text{Sell Turnover} = \text{Sell Price} \times \text{Quantity}$$

$$\text{Total Turnover} = \text{Buy Turnover} + \text{Sell Turnover}$$

---

### Charge-by-Charge Formulas

| Tax / Charge | Regulatory Rate | Formula |
| :--- | :--- | :--- |
| **1. Brokerage** | Flat ₹20 / executed order | $(\text{Buy Orders} + \text{Sell Orders}) \times 20.00$ |
| **2. STT (Securities Tax)** | 0.15% on Sell side | $\text{Sell Turnover} \times 0.0015$ |
| **3. Exchange Fee** | 0.03553% (NSE/NFO) | $\text{Total Turnover} \times 0.0003553$ |
| **4. SEBI Fee** | ₹10 / Crore (0.0001%) | $\text{Total Turnover} \times 0.000001$ |
| **5. IPFT Fee** | ₹0.01 / Crore (0.0000001%) | $\text{Total Turnover} \times 0.000000001$ |
| **6. GST** | 18% on (Brokerage + Exch + SEBI) | $(\text{Brokerage} + \text{Exch Fee} + \text{SEBI Fee}) \times 0.18$ |
| **7. Stamp Duty** | 0.003% on Buy side | $\text{Buy Turnover} \times 0.00003$ |

---

### Total Statutory Charges Formula

$$\text{Total Statutory Charges} = \text{Brokerage} + \text{STT} + \text{Exchange Fee} + \text{SEBI Fee} + \text{IPFT Fee} + \text{GST} + \text{Stamp Duty}$$

$$\text{Net Realised P\&L} = \text{Gross Realised P\&L} - \text{Total Statutory Charges}$$

---

## 📝 3. Concrete Calculation Example

### Trade Parameters
- **Option Contract**: NIFTY 22,600 PE (27 Lots = 1,755 Qty)
- **Buy Leg**: 1,755 Qty @ ₹2.35 $\rightarrow$ Buy Turnover = **₹4,124.25**
- **Sell Leg**: 1,755 Qty @ ₹2.55 $\rightarrow$ Sell Turnover = **₹4,475.25**
- **Total Turnover**: **₹8,599.50**

### Breakdown:
1. **Brokerage**: 2 orders × ₹20 = **₹40.00**
2. **STT**: ₹4,475.25 × 0.15% = **₹6.71**
3. **Exchange Fee**: ₹8,599.50 × 0.03553% = **₹3.06**
4. **SEBI Fee**: ₹8,599.50 × 0.0001% = **₹0.01**
5. **IPFT Fee**: ₹8,599.50 × ₹0.01/Cr = **₹0.00** (rounds to ₹0.00)
6. **GST**: (₹40.00 + ₹3.06 + ₹0.01) × 18% = **₹7.75**
7. **Stamp Duty**: ₹4,124.25 × 0.003% = **₹0.12**

$$\mathbf{\text{Total Statutory Charges}} = 40.00 + 6.71 + 3.06 + 0.01 + 0.00 + 7.75 + 0.12 = \mathbf{₹57.65}$$

$$\text{Gross P\&L} = (2.55 - 2.35) \times 1,755 = +\text{₹351.00}$$

$$\mathbf{\text{Net P\&L}} = 351.00 - 57.65 = \mathbf{+\text{₹293.35}}$$

---

## 💻 4. Code Architecture

- **Calculation Engine**: [`cli/charges.py`](file:///Users/vgolugur/Documents/Projects/kitecli/cli/charges.py)
- **Database Persistence**: Stored per fill in SQLite table `executed_order_context` under columns `brokerage`, `stt`, `gst`, `total_charges`.
- **TUI P&L Rendering**: Included dynamically in `_render_pnl_pane()` in [`cli/live_session.py`](file:///Users/vgolugur/Documents/Projects/kitecli/cli/live_session.py).
