"""
Configuration management for KiteCLI.

Handles reading, writing, and creating default configuration files
stored at ~/.kcli/config.yaml.
"""

from pathlib import Path
from typing import Optional

import yaml

CONFIG_DIR = Path.home() / ".kcli"
CONFIG_FILE = CONFIG_DIR / "config.yaml"

DEFAULT_CONFIG = {
    "accounts": [
        {
            # --- Zerodha account (default) ---
            "name": "Account 1",
            "broker": "zerodha",          # optional; "zerodha" is the default
            "api_key": "your_api_key",
            "api_secret": "your_api_secret",
            "user_id": "your_zerodha_user_id",
            "password": "your_zerodha_password",
            "totp_secret": "your_totp_secret",
            "proxy": "http://user:pass@host:port",
            # Optional: mark one account as the primary streaming account. It is
            # used for market index (NIFTY/SENSEX/INDIA VIX) and option-chain
            # streaming so those instruments aren't subscribed redundantly on
            # every account. If omitted (or the flagged account can't stream),
            # the first stream-capable account is chosen automatically.
            "primary": True,
        },
        # --- Kotak Neo account (optional) ---
        # Uncomment and fill in to add a Kotak Neo account.
        # {
        #     "name": "Kotak Account",
        #     "broker": "kotak",
        #     "consumer_key": "your_kotak_consumer_key",
        #     "consumer_secret": "your_kotak_consumer_secret",
        #     "mobile_number": "+919876543210",
        #     "password": "your_kotak_password",
        #     "mpin": "your_kotak_mpin",
        #     "ucc": "your_kotak_ucc",
        #     "totp_secret": "your_kotak_totp_secret",
        # },
    ],
}


def load_config() -> Optional[dict]:
    """Load configuration from ~/.kcli/config.yaml.

    Returns:
        dict with configuration values, or None if the config file
        does not exist.
    """
    if not CONFIG_FILE.exists():
        return None

    with open(CONFIG_FILE, "r") as f:
        config = yaml.safe_load(f)

    # Automatically migrate/remove legacy server config if present
    if config and "server" in config:
        del config["server"]
        save_config(config)

    return config


def save_config(config: dict) -> None:
    """Write configuration to ~/.kcli/config.yaml.

    Creates the config directory if it does not already exist.

    Args:
        config: Configuration dictionary to persist.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def create_default_config() -> dict:
    """Create and save a default template configuration.

    The template contains placeholder values that the user should
    replace with their own credentials.

    Returns:
        The default configuration dictionary that was saved.
    """
    save_config(DEFAULT_CONFIG)
    return DEFAULT_CONFIG
