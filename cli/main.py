"""
KiteCLI — Multi-account Kite Connect positions viewer.

Entry-point module that defines all ``kcli`` commands using Typer.
"""

from typing import Optional

import typer

import asyncio

from cli.api_client import KCLIClient, KCLIClientError
from cli.config import CONFIG_FILE, create_default_config, load_config
from cli.display import (
    console,
    display_banner,
    display_error,
    display_info,
    display_login_urls,
    display_positions,
    display_status,
    display_success,
)
from cli.live_session import KCLILiveSession


app = typer.Typer(
    name="kcli",
    help="[bold cyan]Kite Connect CLI[/bold cyan] — Multi-account trading positions viewer 🪁",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ── helpers ────────────────────────────────────────────────────────

def _load_config_or_exit() -> dict:
    """Load config and exit with a helpful message if missing."""
    config = load_config()
    if config is None:
        display_error(
            f"Config file not found at {CONFIG_FILE}\n"
            "  Run [bold]kcli config --init[/bold] to create one."
        )
        raise typer.Exit(code=1)
    return config


def _build_client(config: dict) -> KCLIClient:
    """Construct a local KCLIClient from the loaded config."""
    accounts = config.get("accounts", [])
    return KCLIClient(accounts)


def _ensure_accounts_initialized(client: KCLIClient, config: dict) -> None:
    """Show per-account init status; in local mode accounts are init'd in KCLIClient.__init__."""
    status_resp = client.get_status()
    for acct in status_resp.get("accounts", []):
        name = acct.get("name", "Account")
        if acct.get("authenticated"):
            display_success(f"{name}: session active ✓")
        else:
            display_info(f"{name}: not authenticated — run [bold]kcli init[/bold]")


def _mask_secret(value: str, visible: int = 4) -> str:
    """Mask a secret string, showing only the last ``visible`` chars."""
    if len(value) <= visible:
        return "*" * len(value)
    return "*" * (len(value) - visible) + value[-visible:]


# ── commands ───────────────────────────────────────────────────────

@app.command()
def init() -> None:
    """[bold]Initialise accounts[/bold] — authenticate with Kite Connect.

    Sends account credentials to the server, shows login URLs,
    then completes the auth callback for each account.
    """
    display_banner()

    config = _load_config_or_exit()
    accounts = config.get("accounts", [])
    if not accounts:
        display_error("No accounts configured. Edit your config file to add accounts.")
        raise typer.Exit(code=1)
    client = _build_client(config)
    console.print()

    accounts = config.get("accounts", [])
    if not accounts:
        display_error("No accounts configured. Edit your config file to add accounts.")
        raise typer.Exit(code=1)

    # Init accounts on server (auto-login is attempted server-side)
    try:
        display_info("Initialising accounts on server…")
        result = client.init_accounts(accounts)
        console.print()
    except KCLIClientError as exc:
        display_error(str(exc))
        raise typer.Exit(code=1)

    login_accounts = result.get("accounts", [])

    # Separate auto-logged-in accounts from those needing manual login
    auto_logged = [a for a in login_accounts if a.get("auto_logged_in")]
    manual_needed = [a for a in login_accounts if not a.get("auto_logged_in")]

    # Show auto-login results
    for acct in auto_logged:
        display_success(f"{acct.get('name', 'Account')}: Auto-login successful! ✓")
        console.print()

    # Handle accounts that need manual login
    if manual_needed:
        display_login_urls(manual_needed)
        console.print()

        # Show fallback messages for failed auto-logins
        for acct in manual_needed:
            msg = acct.get("message", "")
            if msg:
                display_info(f"{acct.get('name', 'Account')}: {msg}")

        console.print(
            "  [bold yellow]After logging in, enter the request_token from the "
            "redirect URL for each account.[/bold yellow]\n"
        )

        for acct in manual_needed:
            name = acct.get("name", "Account")
            api_key = acct.get("api_key", "")

            request_token = typer.prompt(
                f"  🔑 request_token for {name}",
            )

            try:
                resp = client.complete_callback(api_key, request_token.strip())
                if resp.get("status") == "error":
                    display_error(f"{name}: {resp.get('message', 'Callback failed')}")
                else:
                    display_success(f"{name}: Authenticated successfully! ✓")
            except KCLIClientError as exc:
                display_error(f"{name}: {exc}")

            console.print()

    display_success("Initialisation complete. Run [bold]kcli positions[/bold] to view positions.")


@app.command()
def positions() -> None:
    """[bold]View positions[/bold] — fetch and display open positions across all accounts."""
    display_banner()

    config = _load_config_or_exit()
    client = _build_client(config)

    accounts = config.get("accounts", [])
    api_keys = [acct.get("api_key", "") for acct in accounts]

    try:
        display_info("Fetching positions…")
        console.print()
        result = client.get_positions(api_keys)
    except KCLIClientError as exc:
        display_error(str(exc))
        raise typer.Exit(code=1)

    accounts_data = result.get("accounts", [])
    if not accounts_data:
        display_info("No position data returned from server.")
        raise typer.Exit()

    display_positions(accounts_data)


@app.command()
def status() -> None:
    """[bold]Account status[/bold] — check authentication state of all accounts."""
    display_banner()

    config = _load_config_or_exit()
    client = _build_client(config)
    result = client.get_status()

    status_accounts = result.get("accounts", [])
    if not status_accounts:
        display_info("No account status returned from server.")
        raise typer.Exit()

    display_status(status_accounts)


@app.command()
def live(
    refresh: int = typer.Option(5, "--refresh", "-r", help="Auto-refresh interval in seconds."),
) -> None:
    """[bold]Live dashboard[/bold] — Interactive positions monitor and order terminal."""
    config = _load_config_or_exit()
    accounts = config.get("accounts", [])
    if not accounts:
        display_error("No accounts configured. Edit your config file to add accounts.")
        raise typer.Exit(code=1)
    client = _build_client(config)

    # Launch interactive session
    session = KCLILiveSession(client, accounts, refresh_interval=refresh)
    try:
        asyncio.run(session.run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        display_error(f"TUI Error: {exc}")


@app.command("config")
def config_cmd(
    init: bool = typer.Option(False, "--init", help="Create a default config file."),
    show: bool = typer.Option(False, "--show", help="Display the current configuration."),
    path: bool = typer.Option(False, "--path", help="Print the config file path."),
) -> None:
    """[bold]Configuration[/bold] — manage the kcli config file."""
    display_banner()

    if path:
        console.print(f"  📁 Config file: [bold]{CONFIG_FILE}[/bold]")
        return

    if init:
        if CONFIG_FILE.exists():
            overwrite = typer.confirm(
                f"  Config already exists at {CONFIG_FILE}. Overwrite?",
                default=False,
            )
            if not overwrite:
                display_info("Aborted. Existing config left unchanged.")
                return

        create_default_config()
        display_success(f"Default config created at {CONFIG_FILE}")
        display_info(
            "Edit the config file with your Kite Connect credentials:\n"
            f"      [bold]{CONFIG_FILE}[/bold]"
        )
        return

    # Default behaviour: --show (also the fallback when no flags given)
    config = load_config()
    if config is None:
        display_error(
            f"No config found at {CONFIG_FILE}\n"
            "  Run [bold]kcli config --init[/bold] to create one."
        )
        raise typer.Exit(code=1)

    # Pretty-print config with masked secrets
    console.print()
    console.print("  [bold cyan]Accounts[/bold cyan]")
    for i, acct in enumerate(config.get("accounts", []), start=1):
        console.print(f"    [bold]{i}. {acct.get('name', 'Account')}[/bold]")
        console.print(f"       api_key    : {acct.get('api_key', 'N/A')}")
        console.print(
            f"       api_secret : [dim]{_mask_secret(acct.get('api_secret', ''))}[/dim]"
        )
        if acct.get("user_id"):
            console.print(f"       user_id    : {acct.get('user_id', 'N/A')}")
        if acct.get("password"):
            console.print(
                f"       password   : [dim]{_mask_secret(acct.get('password', ''))}[/dim]"
            )
        if acct.get("totp_secret"):
            console.print(
                f"       totp_secret: [dim]{_mask_secret(acct.get('totp_secret', ''))}[/dim]"
            )
    console.print()


# ── entry point ────────────────────────────────────────────────────

def main() -> None:
    """CLI entry point."""
    app()


if __name__ == "__main__":
    main()
