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
    """Print the KiteCLI banner in bold blue/cyan."""
    console.print(Text(BANNER, style="bold #58a6ff"))


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
    """Return soft green/red/dim colors for positive/negative P&L."""
    if value > 0:
        return "#3fb950"
    elif value < 0:
        return "#f85149"
    return "#8b949e"


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
            (f" {name} ", "bold #e6edf3"),
            (" │ ", "#8b949e"),
            ("P&L: ", "bold"),
            (f"{_format_currency(total_pnl)}", f"bold {pnl_color}"),
        )

        if not positions:
            panel = Panel(
                Text("  No open positions", style="#8b949e italic"),
                title=header_text,
                border_style=pnl_color,
                padding=(1, 2),
            )
            console.print(panel)
            console.print()
            continue

        table = Table(
            show_header=True,
            header_style="bold #58a6ff",
            border_style="#30363d",
            row_styles=["", "dim"],
            pad_edge=True,
            expand=True,
        )
        table.add_column("Symbol", style="bold #e6edf3", no_wrap=True)
        table.add_column("Qty", justify="right")
        table.add_column("Avg Price", justify="right")
        table.add_column("LTP", justify="right")
        table.add_column("P&L", justify="right")

        for pos in positions:
            pnl = float(pos.get("pnl", 0))
            style = _pnl_style(pnl)

            table.add_row(
                str(pos.get("tradingsymbol", "")),
                str(pos.get("quantity", 0)),
                _format_currency(float(pos.get("average_price", 0))),
                _format_currency(float(pos.get("last_price", 0))),
                Text(_format_currency(pnl), style=style),
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

        margin_net  = account.get("margin_net")
        margin_cash = account.get("margin_cash")

        header_parts: list[tuple[str, str]] = [
            (f" {name} ", "bold #e6edf3"),
            (" │ ", "#8b949e"),
            ("P&L: ", "bold"),
            (f"{_format_currency(total_pnl)}", f"bold {pnl_color}"),
        ]
        if margin_net is not None:
            header_parts += [
                ("  │ ", "#8b949e"),
                ("Net: ", "#8b949e"),
                (_format_currency(float(margin_net)), "bold #58a6ff"),
            ]
        if margin_cash is not None:
            header_parts += [
                ("  │ ", "#8b949e"),
                ("Cash: ", "#8b949e"),
                (_format_currency(float(margin_cash)), "bold #3fb950"),
            ]

        header_text = Text.assemble(*header_parts)


        if not positions:
            panel = Panel(
                Text("  No open positions", style="#8b949e italic"),
                title=header_text,
                border_style=pnl_color,
                padding=(1, 2),
            )
            capture_console.print(panel)
            capture_console.print()
            continue

        table = Table(
            show_header=True,
            header_style="bold #58a6ff",
            border_style="#30363d",
            row_styles=["", "dim"],
            pad_edge=True,
            expand=True,
        )
        table.add_column("Symbol", style="bold #e6edf3", no_wrap=True)
        table.add_column("Lots/Qty", justify="right")
        table.add_column("Avg Price", justify="right")
        table.add_column("LTP", justify="right")
        table.add_column("P&L", justify="right")

        for pos in positions:
            pnl = float(pos.get("pnl", 0))
            style = _pnl_style(pnl)

            symbol = str(pos.get("tradingsymbol", ""))
            if show_indices:
                symbol = f"[{pos_idx}] {symbol}"
                pos_idx += 1

            lot_size = pos.get("lot_size", 1) or 1
            qty = pos.get("quantity", 0)
            if lot_size > 1:
                lots = qty / lot_size
                # Show as integer lots if whole number, else 1 decimal
                qty_display = f"{int(lots)}L" if lots == int(lots) else f"{lots:.1f}L"
            else:
                qty_display = str(qty)

            table.add_row(
                symbol,
                qty_display,
                _format_currency(float(pos.get("average_price", 0))),
                _format_currency(float(pos.get("last_price", 0))),
                Text(_format_currency(pnl), style=style),
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
        lines.append("  ✓ ", style="bold #3fb950")
        lines.append(f"{acct.get('name', 'Account')}", style="bold #e6edf3")
        lines.append("\n    ", style="")
        lines.append(f"{acct.get('login_url', 'N/A')}", style="underline #58a6ff")
        lines.append("\n\n")

    lines.append(
        "  Open each URL in your browser to authorize the account.\n",
        style="#8b949e italic",
    )

    console.print(
        Panel(
            lines,
            title="[bold #58a6ff]🔗 Login URLs[/bold #58a6ff]",
            border_style="#58a6ff",
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
        header_style="bold #58a6ff",
        border_style="#30363d",
        pad_edge=True,
    )
    table.add_column("Account", style="bold #e6edf3")
    table.add_column("Status", justify="center")

    for acct in accounts:
        name = acct.get("name", "Unknown")
        authed = acct.get("authenticated", False)
        if authed:
            status = Text("✓ Authenticated", style="bold #3fb950")
        else:
            status = Text("✗ Not Authenticated", style="bold #f85149")
        table.add_row(name, status)

    console.print(
        Panel(
            table,
            title="[bold #58a6ff]📊 Account Status[/bold #58a6ff]",
            border_style="#58a6ff",
            padding=(1, 1),
        )
    )


# ── Simple messages ────────────────────────────────────────────────

def display_error(message: str) -> None:
    """Print a styled error message."""
    console.print(
        Panel(
            Text(f"  {message}", style="bold #f85149"),
            title="[bold #f85149]✗ Error[/bold #f85149]",
            border_style="#f85149",
            padding=(0, 1),
        )
    )


def display_success(message: str) -> None:
    """Print a styled success message."""
    console.print(
        Panel(
            Text(f"  {message}", style="bold #3fb950"),
            title="[bold #3fb950]✓ Success[/bold #3fb950]",
            border_style="#3fb950",
            padding=(0, 1),
        )
    )


def display_info(message: str) -> None:
    """Print a styled informational message."""
    console.print(
        Panel(
            Text(f"  {message}", style="bold #58a6ff"),
            title="[bold #58a6ff]ℹ Info[/bold #58a6ff]",
            border_style="#58a6ff",
            padding=(0, 1),
        )
    )
