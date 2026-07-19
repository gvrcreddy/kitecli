# Kite Connect CLI (kitecli)

A multi-account Zerodha Kite Connect trading positions viewer with a beautiful interactive terminal user interface (TUI).

```
  ╦╔═╔═╗╦  ╦
  ╠╩╗║  ║  ║
  ╩ ╩╚═╝╩═╝╩
  Kite Connect CLI
```

---

## Key Features

- 🔒 **Local-Only Architecture**: No server layer, no database, no cloud deployment. Your Zerodha API credentials and session tokens stay strictly on your local machine.
- 👥 **Multi-Account**: View and manage open positions and order books from multiple Zerodha accounts in a single consolidated screen.
- ⚡ **Parallelized Requests**: All network calls (positions, orders, initialization) are executed concurrently in a thread pool, keeping updates extremely fast and fluid.
- 🔑 **Auto-Login**: Session tokens are cached securely in `~/.kcli/sessions.json`. Using your credentials (`user_id`, `password`, `totp_secret`), `kcli` automatically handles authentication and daily OTP generation in the background.
- 🌐 **Proxy Routing**: Map different HTTP/HTTPS proxies to each account individually to comply with Zerodha API connection requirements.
- 📊 **Interactive TUI Dashboard**: Launch the live dashboard to view:
  - Consolidated active positions with soft-color styling.
  - Live indices panel (**NIFTY 50**, **SENSEX**, and **INDIA VIX**).
  - Info Pane to view Pending Orders, Executed Orders, or Option Chains (`F1`, `F2`, `F3`).
  - Active logs with color-coded alerts and focus highlights for simple navigation.
- 📡 **Live WebSocket Streaming**: Position LTPs/P&L, the market indices panel, and the option chain all update in real time over the Kite WebSocket (`KiteTicker`) — no manual refresh needed. Order fills push an instant positions/orders re-sync.
- 🎯 **Primary Streaming Account**: Market data (indices, option chain, and position prices) is streamed through a single designated *primary* account instead of redundantly subscribing on every account. Mark one account with `primary: true` in the config, or let `kcli` auto-select the first streaming-capable account. Per-account positions, orders, and P&L remain fully independent.
- 🩺 **Streaming Diagnostics**: On startup, `kcli` probes each account's WebSocket authentication. Accounts whose `api_key` lacks an active streaming subscription (REST works but the ticker is rejected with `403`) are reported clearly and skipped, preventing reconnect-error storms.
- 🤖 **Gemini Natural Language Interface (NLI) [Very Basic]**: Prefix commands with `/` (e.g., `/exit weekly options on zk`) to translate conversational queries into explicit chained `kcli` orders. Runs 100% offline for symbol lookup using active positions, and is powered by Google's Gemini 2.5 Flash Cloud API. *(Requires setting `gemini_api_key` in config.yaml)*.
- 🧹 **Action Bar SQUAREOFF Button**: Dedicated purple action button that pre-fills the `exit near-week` command, letting you exit all near-week weekly options on selected or all accounts concurrently with double confirmation.
- 💾 **SQLite Session Recorder**: Logs positions, orders, and market indices snapshots context to a local `~/.kcli/data.db` database in the background for trading performance analytics.
- 📈 **Tuesday Strangle Advisor**: Displays capital allocations, lot sizes, and copy-paste execution stages on Tuesdays in the Info Pane (`F4`).

---

## Installation

Install the package via `pip`:

```bash
pip install kitecli
```

### Kotak Neo Support (Optional)

Since the official Kotak Neo Python SDK is not hosted on PyPI, if you want to use Kotak accounts, you must install the SDK directly from Kotak's official GitHub repository first:

```bash
pip install "git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.2#egg=neo_api_client"
```

*(For local development or installing from source)*:
```bash
git clone https://github.com/chandu389/kitecli.git
cd kitecli
pip install -e .
```

---

## Quick Start

### 1. Initialize Configuration

Create a default configuration template:

```bash
kcli config --init
```

This generates a config file at `~/.kcli/config.yaml`.

### 2. Configure Accounts

Open `~/.kcli/config.yaml` in your text editor and add your accounts. Include your login credentials and TOTP secrets to enable auto-login:

```yaml
accounts:
  - name: "Account 1"
    api_key: "your_api_key_1"
    api_secret: "your_api_secret_1"
    user_id: "your_zerodha_user_id_1"
    password: "your_zerodha_password_1"
    totp_secret: "your_totp_secret_1"
    proxy: "http://username:password@ip:port"  # Optional per-account proxy
    primary: true                              # Optional: use this account for streaming

  - name: "Account 2"
    api_key: "your_api_key_2"
    api_secret: "your_api_secret_2"
    user_id: "your_zerodha_user_id_2"
    password: "your_zerodha_password_2"
    totp_secret: "your_totp_secret_2"
    proxy: "http://username:password@ip:port"
```

**The `primary` flag** (optional) designates which account streams the shared
market data — the indices panel, option chain, and position prices. Because an
instrument's price is the same regardless of which account holds it, streaming
it once through a single primary account avoids redundant duplicate
subscriptions. If `primary` is omitted (or the flagged account can't stream),
`kcli` automatically falls back to the first streaming-capable account.

> **Note on streaming:** Live WebSocket streaming requires that the account's
> Kite Connect app has an active streaming subscription. An account can read
> positions over REST yet still be rejected by the WebSocket (`403`) if its app
> lacks streaming access. `kcli` detects this on startup and reports it in the
> Status Logs.

### 3. Log In & Authenticate

Authenticate and start your sessions (auto-login will run in the background for accounts with complete credentials):

```bash
kcli init
```

### 4. Run commands

- **Interactive Dashboard**:
  ```bash
  kcli live
  ```
- **Positions Snapshot**:
  ```bash
  kcli positions
  ```
- **Status Check**:
  ```bash
  kcli status
  ```

---

## CLI Command Reference

| Command | Description |
|---|---|
| `kcli live` | Launch the interactive live TUI dashboard |
| `kcli init` | Initialize and authenticate account sessions |
| `kcli positions` | Print a quick snapshot of active positions |
| `kcli status` | Check authentication status of configured accounts |
| `kcli config --init` | Generate a default configuration file |
| `kcli config --show` | Display current configuration (secrets masked) |
| `kcli config --path` | Print the configuration file path |

---

## License

MIT

---

## Changelog

### 0.1.0b13 — 2026-06-30

**Bug Fixes:**
- **Global IPv4 DNS Resolution Patch**: Moved and applied the IPv4 DNS resolution override globally at the package initialization level in `cli/__init__.py`. This forces all Zerodha connections (including direct, non-proxied account calls like `SS1009`) to resolve and connect strictly over IPv4, preventing IP mismatch errors ("IP not allowed") caused by transient macOS IPv6 routing.

---

### 0.1.0b12 — 2026-06-29

**Enhancements:**
- **Explicit Symbol Resolution on SQUAREOFF Click**: Clicking the `SQUAREOFF` action bar button now resolves near-week options using live open positions, pre-filling the exact command chain (e.g. `exit NIFTY26JUN22300PE`) directly in the input bar for full transparency before execution, rather than displaying generic `exit near-week` text.
- **Price Format Correction in NLI**: Added strict system instructions and formatting rules to ensure the LLM outputs prices as raw numeric values (e.g. `1.4`) and explicitly forbids prefixing them with `@` (e.g. `@1.4`), which was generating invalid kcli commands.
- **Large Quantity Parsing Fix**: Fixed a bug where any order quantity >= 1000 (e.g., 2665, 2405) was skipped by the CLI parser because it was assumed to be an option strike price.
- **Auto-Splitting on Position Exit**: Modified position exits/square-offs to route through `self.place_order` so that exit orders exceeding the exchange freeze limit (e.g., 1800 for Nifty) are automatically sliced into separate child orders instead of getting rejected by Zerodha.
- **Smooth TUI Scrolling & Sizing Fix**: Rebuilt the scroll bindings for the Tuesday Advisor and Option Chain panes to delegate vertical scrolling directly to prompt-toolkit's native buffer cursor movement. This unifies mouse wheel and keyboard navigation under a single native mechanism, resolving the scroll-lock and viewport snap-back issues permanently.

---

### 0.1.0b11 — 2026-06-28

**New Features:**
- **Action Bar SQUAREOFF Button**: Purple button pre-filling the `exit near-week` command to square off near-week weekly options on selected or all accounts concurrently.
- **Natural Language Interface (NLI) [Very Basic]**: Translates slash (`/`) command requests into kcli chained orders. Runs 100% offline for symbol lookup using active positions, and routes queries through the Gemini 2.5 Flash API.
- **SQLite Session Recorder**: Background thread records order executions, positions, and market indices snapshots metadata to `~/.kcli/data.db` for trade performance reporting.
- **Tuesday Option Strangle Advisor**: Dynamic capital allocation and lot sizing plan mapped to F4 key.

**Bug Fixes:**
- **Proxy Bypass & IPv4 DNS lookup patch**: Patched the NLI network call to force IPv4 DNS queries and bypass local terminal proxies, reducing Gemini API lookup latency from 2400ms to 2ms on macOS.
- **Spacious 3-Row Prompt Height**: Expanded the command input row statically to 3 lines with text wrapping enabled, so long chained commands and confirmations are fully readable.
- **Authentication Client Method**: Added the missing `is_authenticated` helper method to the `KCLIClient` wrapper.
- **Advisor Singleton Import path**: Resolved the `_manager` import exception in `cli/advisor.py` by directing it to the correct singleton location in `cli/api_client.py`.

---

### 0.1.0b10 — 2026-06-25

**Bug Fixes:**
- **WebSocket reconnect crash on startup**: `reconnect` and `reconnect_max_tries` were incorrectly passed to `ticker.connect()`, which does not accept them. These params now correctly go to the `KiteTicker()` constructor. Fixes: `KiteTicker.connect() got an unexpected keyword argument 'reconnect'`.
- **Reduced reconnect attempts to 5**: Max reconnect retries tuned down from 50 to 5 (~60s recovery window with exponential backoff) to stop faster on persistent failures.

---

### 0.1.0b9 — 2026-06-24

**Bug Fixes:**
- **WebSocket auto-reconnect on network drops**: The `KiteTicker` was previously started without reconnect settings, so a transient TCP drop (error 1006 — peer closed connection) would silently kill the WebSocket permanently, freezing NIFTY indices, position LTPs, and all live data. Reconnect is now enabled with up to **10 attempts** and exponential backoff.
- **Auth-failure reconnect storm prevention**: If the WebSocket fails due to a 403 / expired token, the ticker now immediately stops reconnecting and shows a clear message (`Run kcli init to re-authenticate`) instead of hammering Zerodha indefinitely.

---

### 0.1.0b8 — 2026-06-24

**Bug Fixes:**
- **Account-aware order routing**: Fixed a bug where clicking an account (e.g. `@SS1009`) correctly updated the TUI context, but placing an order for a symbol that also existed in another account routed the order to that other account. Symbol resolution and action bar position matching are now scoped to the selected account context.
- **Improved login/auto-login error logging**: `complete_login` and `auto_login` in `kite_manager.py` now log the full exception message and stack trace, making proxy and token failures much easier to diagnose.

**Enhancements:**
- **Filled quantity display in orders pane**: Both pending (F1) and executed (F2) orders now always show `filled/total` format (e.g. `0/910`, `130/910`, `910/910`) so you can track partial fills at a glance.
- **Live order update messages**: The WebSocket order update log in the status pane now shows `filled/total` quantity (e.g. `SELL 130/910 NIFTY25JUN25800PE -> OPEN`) in real time as fills arrive.

---

### 0.1.0b7 — 2026-06-19

**Bug Fixes:**
- **Position price updates via WebSocket**: Position LTPs were not updating in the TUI because `instrument_token` was missing from the dict returned by `get_positions()`. Adding the key allows the WebSocket ticker to correctly map tick data to positions.

**New Features:**
- **Pending order modification**: Select a pending order (`select order <id>` or `s o <id>`) and use `order <id> <qty> <price>` to modify it, with a double-confirmation prompt before execution.
- **Pending order cancellation**: Use `cancel [id]` (or click CANCEL after selecting an order) to cancel a pending order with confirmation.
- **REFRESH button**: A `REFRESH` button on the TUI quick action bar immediately triggers a full sync of positions, orders, margins, and indices across all accounts.
- **Context-aware MODIFY/CANCEL buttons**: The quick action bar swaps BUY/SELL for MODIFY/CANCEL buttons when a pending order is selected.
- **ORDERS.md documentation**: Added a comprehensive reference guide for all order types, syntax, lot notation (`L`), command chaining (`&&`), and keyboard shortcuts.

---

### 0.1.0b6 — 2026-06-15

**New Features:**
- **Command chaining (`&&`)**: Chain multiple commands in a single input, e.g. `account SS1009 && buy SBIN 10`.
- **Context-aware BUY/SELL buttons**: Action bar buttons dynamically pre-fill order syntax based on whether an account, position, or nothing is selected.
- **Lot-size notation**: Specify quantities in lots using `L` suffix (e.g. `2L` for 2 lots).
- **Position ID shortcuts**: Reference positions by their row index number instead of full symbol name.

