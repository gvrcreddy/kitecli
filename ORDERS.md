# Order Types and TUI Commands Reference

This reference guide documents the syntax, usage, and interactive workflows for placing, modifying, cancelling, and exiting orders in the `kitecli` live TUI dashboard (`kcli live`).

---

## 💡 Quick Action Bar Shortcuts

The action bar at the bottom of the screen dynamically updates based on your current selection to pre-fill the input box with the correct command structure:

1. **Nothing Selected**: Displays **BUY**, **SELL**, and **REFRESH** buttons. Pre-fills a chained command to select an account and place an order:
   * `account <name> && buy <symbol> <qty> [price] [product]`
2. **Account Selected**: Displays **BUY**, **SELL**, and **REFRESH** buttons. Pre-fills:
   * `buy <symbol> <qty> [price] [product]`
3. **Position & Account Selected**: Displays **BUY**, **SELL**, and **REFRESH** buttons. Automatically resolves the active position's quantity and current last traded price (LTP) to pre-fill:
   * `buy <qty> <price> ` (e.g. `buy 150 240.25 `)
4. **Pending Order Selected**: Switches the action bar to **MODIFY**, **CANCEL**, and **REFRESH** buttons:
   * **MODIFY** pre-fills the input with: `order <full_id> <current_qty> <current_price>`
   * **CANCEL** pre-fills the input with: `cancel <full_id>`

* **REFRESH Button**: The **REFRESH** button is always present at the end of the action bar. Clicking it immediately triggers a manual sync/refresh of all active positions, pending/executed orders, margins, and indices across all accounts, without needing to type the command manually.

---

## 🛒 1. Order Placement (BUY / SELL)

Place new orders on the selected account context or directly on a specific account.

### Syntax
```
buy  [symbol|position_id] <quantity|lotsL> [price] [product]
sell [symbol|position_id] <quantity|lotsL> [price] [product]
```

### Parameters
* **`symbol|position_id`** *(Optional)*: 
  * If omitted, targets the currently selected position.
  * Can be a position ID from the left pane (e.g., `1`, `2`, `3`) or a direct trading symbol (e.g., `SBIN`, `NIFTY2662322500PE`).
* **`quantity|lotsL`** *(Required)*: 
  * Specify as a raw integer (e.g. `75`) or using lot size notation (e.g. `1L`, `3L`).
* **`price`** *(Optional)*: 
  * Specify a limit price (e.g. `145.50`). 
  * If omitted (or set to `0`), the order is submitted as a **MARKET** order.
* **`product`** *(Optional)*: 
  * Defaults to `NRML`. Can be `MIS` or `CNC`.
  * For equity symbols on NSE, if `NRML` is used, the system automatically switches it to `CNC`.

### Examples
* `buy 2L` — Buy 2 lots of the currently selected position at **MARKET** price.
* `sell 3 2L` — Sell 2 lots of position ID `3` at **MARKET** price.
* `buy NIFTY2662322500PE 1L 145.50` — Place a **LIMIT** buy order for 1 lot of the symbol at `145.50`.
* `sell SBIN 100 0 MIS` — Place a **MARKET** sell order for 100 shares of SBIN under `MIS` product type.

---

## ✏️ 2. Pending Order Modification

Modify any open/pending order using its ID or suffix.

### Syntax
```
order <id_suffix|full_id> <quantity|lotsL> <price>
```

### Parameters
* **`id_suffix|full_id`**: The full order ID or the last few digits (e.g., last 6 digits displayed in the pending orders pane).
* **`quantity|lotsL`**: The new target quantity (supports raw numbers or lot notation `L`).
* **`price`**: The new limit price. If set to `0`, it modifies the order to a **MARKET** order.

### Examples
* `order 348921 100 150.00` — Modifies pending order ending in `348921` to a quantity of `100` at a limit price of `150.00`.
* `order 348921 2L 148.20` — Modifies pending order ending in `348921` to a quantity of 2 lots at a limit price of `148.20`.

---

## ❌ 3. Pending Order Cancellation

Cancel any open/pending order.

### Syntax
```
cancel [id_suffix|full_id]
```

### Parameters
* **`id_suffix|full_id`** *(Optional)*:
  * The full order ID or the last few digits of the target order.
  * If omitted, it will attempt to cancel the currently selected pending order.

### Examples
* `cancel 348921` — Cancels the pending order ending in `348921`.
* `cancel` — Cancels the currently selected pending order.

---

## 🚪 4. Position Exits

Square off existing open positions.

### Syntax
```
exit [symbol|position_id]
exit all
```

### Parameters
* **`symbol|position_id`** *(Optional)*:
  * Triggers exit for the specific position by ID or symbol.
  * If omitted, exits the currently selected position context.
* **`exit all`**: 
  * Triggers square-off for **ALL** open positions across the selected account (or all accounts if no account is selected).

### Examples
* `exit` — Exit the currently selected position.
* `exit 3` — Exit position ID `3`.
* `exit SBIN` — Exit all positions matching `SBIN`.
* `exit all` — Exit all open positions.

---

## 🔗 5. Command Chaining (`&&`)

You can chain multiple commands together on a single line using `&&` to run them sequentially. This is especially useful for setting the account context and placing/modifying/cancelling an order in a single keystroke.

### Examples
* `account SS1009 && buy SBIN 2L` — Selects account context `SS1009` first, and then places a market buy order for 2 lots of SBIN on it.
* `select order 348921 && cancel` — Selects the pending order ending in `348921` and immediately prepares a cancellation confirmation prompt.

---

## 🛡️ Double Confirmation Safeties
All order actions (**Place, Modify, Cancel, Exit**) invoke a secondary prompt asking for confirmation before making requests to the Zerodha API.
* **Prompt**: `Confirm MODIFY order 348921 to 100 @ 150.00? (y/n)> `
* Type **`y`** or **`yes`** and press **Enter** to execute.
* Type any other key or press **Enter** to cancel the action safety.
