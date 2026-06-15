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

---

## Installation

Install the package via `pip`:

```bash
pip install kitecli
```

*(For local development or installing from source)*:
```bash
git clone https://github.com/vgolugur/kitecli.git
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
```

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
