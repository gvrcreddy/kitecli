"""
Rich-based display helpers for KiteCLI.

All terminal output—banners, tables, panels, status messages—goes
through the functions in this module so the CLI has a consistent,
polished look.
"""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

# ── Banner ─────────────────────────────────────────────────────────

BANNER = r"""
  ╦╔═╔═╗╦  ╦
  ╠╩╗║  ║  ║
  ╩ ╩╚═╝╩═╝╩
  Kite Connect CLI
"""


def display_banner() -> None:
    """Print the KiteCLI banner in bold cyan."""
    console.print(Text(BANNER, style="bold cyan"))


# ── Positions ──────────────────────────────────────────────────────

def _format_currency(value: float) -> str:
    """Format a number as ₹ with commas (Indian locale style)."""
    sign = "-" if value < 0 else ""
    abs_val = abs(value)
    # Split into integer and decimal parts
    int_part = int(abs_val)
    dec_part = f"{abs_val - int_part:.2f}"[1:]  # ".xx"

    # Indian grouping: last 3 digits, then groups of 2
    s = str(int_part)
    if len(s) > 3:
        last3 = s[-3:]
        rest = s[:-3]
        groups = []
        while rest:
            groups.append(rest[-2:])
            rest = rest[:-2]
        groups.reverse()
        formatted = ",".join(groups) + "," + last3
    else:
        formatted = s

    return f"{sign}₹{formatted}{dec_part}"


def _pnl_style(value: float) -> str:
    """Return 'green' for positive, 'red' for negative, dim otherwise."""
    if value > 0:
        return "green"
    elif value < 0:
        return "red"
    return "dim"


def _format_pnl_pct(value: float) -> str:
    """Format P&L percentage with + / - prefix."""
    prefix = "+" if value > 0 else ""
    return f"{prefix}{value:.2f}%"


def display_positions(accounts_data: list[dict]) -> None:
    """Render positions tables for every account.

    Args:
        accounts_data: List of dicts, each with keys ``name``,
            ``total_pnl``, and ``positions`` (list of position dicts).
    """
    grand_total_pnl = 0.0

    for account in accounts_data:
        name = account.get("name", "Unknown")
        total_pnl = float(account.get("total_pnl", 0))
        positions = account.get("positions", [])
        grand_total_pnl += total_pnl

        pnl_color = _pnl_style(total_pnl)
        header_text = Text.assemble(
            (f" {name} ", "bold white"),
            (" │ ", "dim"),
            ("P&L: ", "bold"),
            (f"{_format_currency(total_pnl)}", f"bold {pnl_color}"),
        )

        if not positions:
            panel = Panel(
                Text("  No open positions", style="dim italic"),
                title=header_text,
                border_style=pnl_color,
                padding=(1, 2),
            )
            console.print(panel)
            console.print()
            continue

        table = Table(
            show_header=True,
            header_style="bold cyan",
            border_style="dim",
            row_styles=["", "dim"],
            pad_edge=True,
            expand=True,
        )
        table.add_column("Symbol", style="bold white", no_wrap=True)
        table.add_column("Qty", justify="right")
        table.add_column("Avg Price", justify="right")
        table.add_column("LTP", justify="right")
        table.add_column("P&L", justify="right")
        table.add_column("P&L %", justify="right")

        for pos in positions:
            pnl = float(pos.get("pnl", 0))
            pnl_pct = float(pos.get("pnl_pct", 0))
            style = _pnl_style(pnl)

            table.add_row(
                str(pos.get("tradingsymbol", "")),
                str(pos.get("quantity", 0)),
                _format_currency(float(pos.get("average_price", 0))),
                _format_currency(float(pos.get("last_price", 0))),
                Text(_format_currency(pnl), style=style),
                Text(_format_pnl_pct(pnl_pct), style=style),
            )

        panel = Panel(
            table,
            title=header_text,
            border_style=pnl_color,
            padding=(0, 1),
        )
        console.print(panel)
        console.print()

    # Grand total summary
    gt_style = _pnl_style(grand_total_pnl)
    summary = Text.assemble(
        ("  Grand Total P&L  ", "bold"),
        (f"{_format_currency(grand_total_pnl)}", f"bold {gt_style}"),
    )
    console.print(
        Panel(
            summary,
            border_style=gt_style,
            padding=(1, 2),
        )
    )


def render_positions_to_string(accounts_data: list[dict], width: int = 80, show_indices: bool = False) -> str:
    """Render positions to a string with ANSI color codes.

    Identical to display_positions, but returns the string instead of printing.
    """
    from io import StringIO
    from rich.console import Console

    # Create an in-memory console capturing output
    capture_console = Console(
        file=StringIO(),
        force_terminal=True,
        color_system="truecolor",
        width=width,
    )

    grand_total_pnl = 0.0
    pos_idx = 1

    for account in accounts_data:
        name = account.get("name", "Unknown")
        total_pnl = float(account.get("total_pnl", 0))
        positions = account.get("positions", [])
        grand_total_pnl += total_pnl

        pnl_color = _pnl_style(total_pnl)
        header_text = Text.assemble(
            (f" {name} ", "bold white"),
            (" │ ", "dim"),
            ("P&L: ", "bold"),
            (f"{_format_currency(total_pnl)}", f"bold {pnl_color}"),
        )

        if not positions:
            panel = Panel(
                Text("  No open positions", style="dim italic"),
                title=header_text,
                border_style=pnl_color,
                padding=(1, 2),
            )
            capture_console.print(panel)
            capture_console.print()
            continue

        table = Table(
            show_header=True,
            header_style="bold cyan",
            border_style="dim",
            row_styles=["", "dim"],
            pad_edge=True,
            expand=True,
        )
        table.add_column("Symbol", style="bold white", no_wrap=True)
        table.add_column("Qty", justify="right")
        table.add_column("Avg Price", justify="right")
        table.add_column("LTP", justify="right")
        table.add_column("P&L", justify="right")
        table.add_column("P&L %", justify="right")

        for pos in positions:
            pnl = float(pos.get("pnl", 0))
            pnl_pct = float(pos.get("pnl_pct", 0))
            style = _pnl_style(pnl)

            symbol = str(pos.get("tradingsymbol", ""))
            if show_indices:
                symbol = f"[{pos_idx}] {symbol}"
                pos_idx += 1

            table.add_row(
                symbol,
                str(pos.get("quantity", 0)),
                _format_currency(float(pos.get("average_price", 0))),
                _format_currency(float(pos.get("last_price", 0))),
                Text(_format_currency(pnl), style=style),
                Text(_format_pnl_pct(pnl_pct), style=style),
            )


        panel = Panel(
            table,
            title=header_text,
            border_style=pnl_color,
            padding=(0, 1),
        )
        capture_console.print(panel)
        capture_console.print()

    # Grand total summary
    gt_style = _pnl_style(grand_total_pnl)
    summary_text = Text.assemble(
        ("  Grand Total P&L  ", "bold"),
        (f"{_format_currency(grand_total_pnl)}", f"bold {gt_style}"),
    )
    capture_console.print(
        Panel(
            summary_text,
            border_style=gt_style,
            padding=(1, 2),
        )
    )

    return capture_console.file.getvalue()


# ── Login URLs ─────────────────────────────────────────────────────

def display_login_urls(accounts: list[dict]) -> None:
    """Show Kite login URLs for each account.

    Args:
        accounts: List of dicts with ``name`` and ``login_url`` keys.
    """
    lines = Text()
    for acct in accounts:
        lines.append("  ✓ ", style="bold green")
        lines.append(f"{acct.get('name', 'Account')}", style="bold white")
        lines.append("\n    ", style="")
        lines.append(f"{acct.get('login_url', 'N/A')}", style="underline cyan")
        lines.append("\n\n")

    lines.append(
        "  Open each URL in your browser to authorize the account.\n",
        style="dim italic",
    )

    console.print(
        Panel(
            lines,
            title="[bold cyan]🔗 Login URLs[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


# ── Status ─────────────────────────────────────────────────────────

def display_status(accounts: list[dict]) -> None:
    """Show per-account authentication status.

    Args:
        accounts: List of dicts with ``name`` and ``authenticated`` keys.
    """
    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        pad_edge=True,
    )
    table.add_column("Account", style="bold white")
    table.add_column("Status", justify="center")

    for acct in accounts:
        name = acct.get("name", "Unknown")
        authed = acct.get("authenticated", False)
        if authed:
            status = Text("✓ Authenticated", style="bold green")
        else:
            status = Text("✗ Not Authenticated", style="bold red")
        table.add_row(name, status)

    console.print(
        Panel(
            table,
            title="[bold cyan]📊 Account Status[/bold cyan]",
            border_style="cyan",
            padding=(1, 1),
        )
    )


# ── Simple messages ────────────────────────────────────────────────

def display_error(message: str) -> None:
    """Print a styled error message."""
    console.print(
        Panel(
            Text(f"  {message}", style="bold red"),
            title="[bold red]✗ Error[/bold red]",
            border_style="red",
            padding=(0, 1),
        )
    )


def display_success(message: str) -> None:
    """Print a styled success message."""
    console.print(
        Panel(
            Text(f"  {message}", style="bold green"),
            title="[bold green]✓ Success[/bold green]",
            border_style="green",
            padding=(0, 1),
        )
    )


def display_info(message: str) -> None:
    """Print a styled informational message."""
    console.print(
        Panel(
            Text(f"  {message}", style="bold cyan"),
            title="[bold cyan]ℹ Info[/bold cyan]",
            border_style="cyan",
            padding=(0, 1),
        )
    )
