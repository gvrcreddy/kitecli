# KCLI — Kite Connect CLI

A multi-account Zerodha Kite Connect positions viewer with a beautiful terminal interface.

```
  ╦╔═╔═╗╦  ╦
  ╠╩╗║  ║  ║
  ╩ ╩╚═╝╩═╝╩
  Kite Connect CLI
```

## Architecture

```
┌─────────────────┐       HTTPS       ┌──────────────────┐      Kite API      ┌─────────────────┐
│   kcli (CLI)    │ ──────────────── │  FastAPI Server  │ ──────────────────│  Zerodha Kite   │
│   Your Machine  │                   │  Google Cloud    │                    │  Connect API    │
└─────────────────┘                   └──────────────────┘                    └─────────────────┘
        │
        ▼
  ~/.kcli/config.yaml
```

- **CLI (`kcli`)**: Runs on your machine. Beautiful color-coded terminal UI.
- **Server**: Runs on Google Cloud (Cloud Run). Proxies Kite API calls.
- **Config**: Multi-account config stored locally at `~/.kcli/config.yaml`.

## Quick Start

### 1. Deploy the Server

```bash
# Build and deploy to Cloud Run
cd server
gcloud run deploy kcli-server \
  --source . \
  --region asia-south1 \
  --allow-unauthenticated \
  --set-env-vars AUTH_TOKEN=your-secret-token
```

### 2. Install the CLI

```bash
# From the project root
pip install -e .
```

### 3. Initialize Config

```bash
# Create the default config file
kcli config --init

# Edit the config with your accounts
# Open ~/.kcli/config.yaml and add your Kite API credentials
```

**Config file format (`~/.kcli/config.yaml`):**

```yaml
server:
  url: "https://your-cloud-run-url.run.app"
  auth_token: "your-secret-token"

accounts:
  - name: "Trading Account 1"
    api_key: "your_api_key_1"
    api_secret: "your_api_secret_1"
  - name: "Trading Account 2"
    api_key: "your_api_key_2"
    api_secret: "your_api_secret_2"
```

### 4. Login to Kite

```bash
# Initialize and authenticate all accounts
kcli init
```

This will:
1. Send your account configs to the server
2. Display login URLs for each account
3. Prompt you to paste the `request_token` after logging in via browser

> **Note:** Kite access tokens expire daily (~6 AM IST). You need to run `kcli init` once each trading day.

### 5. View Positions

```bash
# View positions across all accounts
kcli positions
```

## Commands

| Command | Description |
|---|---|
| `kcli init` | Authenticate all accounts (daily) |
| `kcli positions` | View positions across all accounts |
| `kcli status` | Check authentication status |
| `kcli config --init` | Create default config file |
| `kcli config --show` | Show current config (secrets masked) |
| `kcli config --path` | Print config file path |

## Development

### Run Server Locally

```bash
cd server
pip install -r requirements.txt
AUTH_TOKEN=test-token uvicorn main:app --reload --port 8080
```

### Run CLI

```bash
pip install -e .
kcli --help
```

## Getting Kite API Credentials

1. Go to [Kite Developer Console](https://developers.kite.trade/)
2. Create a new app
3. Note your **API Key** and **API Secret**
4. Set the **Redirect URL** to your server's callback URL

## License

MIT
