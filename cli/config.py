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
    "server": {
        "url": "http://localhost:8080",
        "auth_token": "your-secret-token",
    },
    "accounts": [
        {
            "name": "Account 1",
            "api_key": "your_api_key",
            "api_secret": "your_api_secret",
            "user_id": "your_zerodha_user_id",
            "password": "your_zerodha_password",
            "totp_secret": "your_totp_secret",
        },
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
        return yaml.safe_load(f)


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
