"""
highlighter.py — Live Syntax Highlighting, Auto-Completion, and Typo Correction for KiteCLI.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Generator

from prompt_toolkit.auto_suggest import AutoSuggest, AutoSuggestFromHistory, Suggestion
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.lexers import Lexer

if TYPE_CHECKING:
    from cli.live_session import KCLILiveSession


# Action verb definitions
BUY_VERBS = {"buy", "b"}
SELL_VERBS = {"sell", "s"}
EXIT_VERBS = {"exit", "x"}
MODIFY_VERBS = {"modify", "m", "order"}
CANCEL_VERBS = {"cancel", "c"}
SELECT_VERBS = {"select", "sel", "deselect", "clear"}

VIEW_VERBS = {
    "oc", "oc comp", "occomp", "ocdual", "oc dual", "oc side", "oc2", "optionchain", "chain",
    "pnl", "pl", "holdings", "holding", "portfolio", "port", "hld",
    "md", "depth", "marketdepth", "orders", "ord", "pending", "executed", "ex",
    "positions", "pos", "advisor", "adv", "ai", "status", "st", "health",
    "sched", "scheduled", "schedule", "so", "cond", "conditional", "co",
    "help", "?", "quit", "q"
}

PRODUCTS = {"NRML", "MIS", "CNC"}
FLAGS = {"all", "deep", "summary", "sum", "detail", "details", "week", "last_week", "month", "custom", "none"}
TIME_KEYWORDS = {"at", "on"}
CHAIN_KEYWORDS = {"then", "->", "=>", "&&"}


class KiteCLILexer(Lexer):
    """Real-time syntax highlighter for the KiteCLI interactive command input."""

    def __init__(self, session: KCLILiveSession | None = None) -> None:
        super().__init__()
        self.session = session

    def lex_document(self, document: Document) -> Callable[[int], list[tuple[str, str]]]:
        def get_line(lineno: int) -> list[tuple[str, str]]:
            line = document.lines[lineno]
            if not line:
                return [("", "")]
            return self._tokenize_line(line)
        return get_line

    def _tokenize_line(self, line: str) -> list[tuple[str, str]]:
        fragments: list[tuple[str, str]] = []
        # Tokenize by whitespace and chaining delimiters while preserving exact spaces
        tokens = re.split(r"(\s+|&&|->|=>)", line)

        # Contextual state within command segment (reset on && or then/->)
        state = "EXPECT_VERB"  # EXPECT_VERB, IN_COMMAND

        for token in tokens:
            if not token:
                continue

            # Whitespace
            if re.match(r"^\s+$", token):
                fragments.append(("", token))
                continue

            # Chaining operators
            if token in ("&&", "->", "=>"):
                fragments.append(("class:cmd.chain", token))
                state = "EXPECT_VERB"
                continue

            t_lower = token.lower()
            t_upper = token.upper()

            # Chaining keyword 'then'
            if t_lower == "then":
                fragments.append(("class:cmd.chain", token))
                state = "EXPECT_VERB"
                continue

            # Account tags (e.g. @ZK8719 or account/acct)
            if token.startswith("@"):
                fragments.append(("class:cmd.account", token))
                continue

            # Time clauses
            if t_lower in TIME_KEYWORDS:
                fragments.append(("class:cmd.keyword", token))
                continue

            if re.match(r"^\d{1,2}:\d{2}(?::\d{2})?(?:[aApP][mM])?$", token) or re.match(r"^\d{4}-\d{2}-\d{2}$|^\d{2}-\d{2}-\d{4}$", token):
                fragments.append(("class:cmd.time", token))
                continue

            # Check verbs when expecting a command verb
            if state == "EXPECT_VERB":
                if t_lower in BUY_VERBS:
                    fragments.append(("class:cmd.buy", token))
                    state = "IN_BUY"
                    continue
                elif t_lower in SELL_VERBS:
                    fragments.append(("class:cmd.sell", token))
                    state = "IN_SELL"
                    continue
                elif t_lower in EXIT_VERBS:
                    fragments.append(("class:cmd.exit", token))
                    state = "IN_EXIT"
                    continue
                elif t_lower in MODIFY_VERBS:
                    fragments.append(("class:cmd.modify", token))
                    state = "IN_MODIFY"
                    continue
                elif t_lower in CANCEL_VERBS:
                    fragments.append(("class:cmd.cancel", token))
                    state = "IN_CANCEL"
                    continue
                elif t_lower in SELECT_VERBS:
                    fragments.append(("class:cmd.select", token))
                    state = "IN_SELECT"
                    continue
                elif t_lower in ("account", "acct", "a"):
                    fragments.append(("class:cmd.select", token))
                    state = "IN_ACCOUNT"
                    continue
                elif t_lower in VIEW_VERBS:
                    fragments.append(("class:cmd.view", token))
                    state = "IN_VIEW"
                    continue
                elif token.isdigit():
                    # Shorthand position selection by index (e.g. '1', '2')
                    fragments.append(("class:cmd.id", token))
                    state = "IN_SELECT"
                    continue

            # In-command token styling
            if t_upper in PRODUCTS:
                fragments.append(("class:cmd.product", token))
            elif t_lower in FLAGS:
                fragments.append(("class:cmd.flag", token))
            elif re.match(r"^\d+[Ll]$", token):
                # Lot quantity (e.g. 27L, 2l)
                fragments.append(("class:cmd.qty", token))
            elif re.match(r"^\d+\.\d+$", token):
                # Float price (e.g. 130.20, 1.40)
                fragments.append(("class:cmd.price", token))
            elif token.isdigit():
                # Digits could be quantity, price, or ID
                if state in ("IN_BUY", "IN_SELL"):
                    fragments.append(("class:cmd.qty", token))
                elif state in ("IN_MODIFY", "IN_CANCEL"):
                    fragments.append(("class:cmd.id", token))
                else:
                    fragments.append(("class:cmd.qty", token))
            elif re.match(r"^[A-Z0-9_-]+$", token) and any(c.isalpha() for c in token) and len(token) >= 3:
                # Trading symbol or Account name
                if state == "IN_ACCOUNT":
                    fragments.append(("class:cmd.account", token))
                else:
                    fragments.append(("class:cmd.symbol", token))
            elif token.startswith("/") and len(token) > 1:
                # NLI natural language command (e.g. /buy 100 nifty call)
                fragments.append(("class:cmd.nli", token))
            else:
                fragments.append(("class:cmd.text", token))

        return fragments


class KiteCLICompleter(Completer):
    """Dynamic Tab Completer offering contextual completions for KiteCLI."""

    def __init__(self, session: KCLILiveSession | None = None) -> None:
        super().__init__()
        self.session = session

    def get_completions(self, document: Document, complete_event: Any) -> Generator[Completion, None, None]:
        text_before_cursor = document.text_before_cursor
        if not text_before_cursor:
            return

        # Split into sub-commands if chained
        sub_cmds = text_before_cursor.split("&&")
        cur_sub = sub_cmds[-1].lstrip()

        words = cur_sub.split()
        word_under_cursor = document.get_word_before_cursor(WORD=True)
        plain_word = document.get_word_before_cursor()

        # 1. First word completions (Command verbs and primary shortcuts)
        if len(words) == 0 or (len(words) == 1 and not cur_sub.endswith(" ")):
            prefix = plain_word.lower()
            all_cmds = [
                ("buy", "Place BUY order (e.g. buy 27L 1.40)"),
                ("sell", "Place SELL order (e.g. sell 27L 1.40)"),
                ("exit", "Exit positions (e.g. exit all, exit 1, exit 1.40)"),
                ("modify", "Modify open order (e.g. modify <id> 27L 1.50)"),
                ("cancel", "Cancel order (e.g. cancel all, cancel co all)"),
                ("oc", "Switch to Option Chain view"),
                ("oc comp", "Switch to Dual Option Chain comparison"),
                ("pnl", "Switch to P&L Analytics view"),
                ("holdings", "Switch to Holdings & Portfolio view"),
                ("md", "Switch to Market Depth view"),
                ("orders", "View Pending Orders"),
                ("executed", "View Executed Orders"),
                ("positions", "View Active Positions"),
                ("advisor", "View Tuesday Expiry Advisor"),
                ("status", "Check multi-broker connection health"),
                ("account", "Switch active account (e.g. account ZK8719)"),
                ("sched", "View active scheduled orders"),
                ("cond", "View active conditional orders"),
                ("help", "View Help and shortcuts"),
                ("clear", "Clear active selection"),
                ("quit", "Exit application"),
            ]
            for cmd, desc in all_cmds:
                if cmd.startswith(prefix):
                    yield Completion(cmd, start_position=-len(plain_word), display=cmd, display_meta=desc)
            return

        first_verb = words[0].lower()

        # 2. Account selection completions
        if first_verb in ("account", "acct", "a") or word_under_cursor.startswith("@"):
            is_at = word_under_cursor.startswith("@")
            prefix = word_under_cursor.lstrip("@").lower()
            start_pos = -len(word_under_cursor) if is_at else -len(plain_word)
            if self.session and hasattr(self.session, "accounts"):
                for acct in self.session.accounts:
                    name = acct.get("name", "")
                    if name.lower().startswith(prefix):
                        rep = f"@{name}" if is_at else name
                        yield Completion(rep, start_position=start_pos, display=f"@{name}", display_meta=f"{acct.get('broker', 'broker').title()}")
                if "all".startswith(prefix):
                    yield Completion("all", start_position=start_pos, display="all", display_meta="All configured accounts")
            return

        # 3. Holdings completions
        if first_verb in ("holdings", "holding", "portfolio", "port", "hld"):
            prefix = word_under_cursor.lower()
            for opt, desc in [("summary", "Summary portfolio view"), ("deep", "Deep constituent view"), ("all", "View all accounts")]:
                if opt.startswith(prefix):
                    yield Completion(opt, start_position=-len(word_under_cursor), display=opt, display_meta=desc)
            return

        # 4. P&L completions
        if first_verb in ("pnl", "pl"):
            prefix = word_under_cursor.lower()
            for opt, desc in [("week", "Current options week (Wed-Tue)"), ("last_week", "Previous week"), ("month", "Current calendar month"), ("custom", "Custom date range")]:
                if opt.startswith(prefix):
                    yield Completion(opt, start_position=-len(word_under_cursor), display=opt, display_meta=desc)
            return

        # 5. Cancel completions
        if first_verb in ("cancel", "c"):
            prefix = word_under_cursor.lower()
            for opt, desc in [("all", "Cancel all open orders"), ("sched", "Cancel scheduled orders"), ("co", "Cancel conditional orders")]:
                if opt.startswith(prefix):
                    yield Completion(opt, start_position=-len(word_under_cursor), display=opt, display_meta=desc)
            return

        # 6. Exit completions
        if first_verb in ("exit", "x"):
            prefix = word_under_cursor.lower()
            if "all".startswith(prefix):
                yield Completion("all", start_position=-len(word_under_cursor), display="all", display_meta="Exit all open positions")

        # 7. Symbol completions for BUY / SELL / EXIT / MD / SELECT
        if first_verb in ("buy", "b", "sell", "s", "exit", "x", "select", "sel", "md", "depth"):
            prefix = word_under_cursor.upper()
            symbols_seen = set()

            # Active positions
            if self.session and hasattr(self.session, "active_positions"):
                for idx, pos in enumerate(self.session.active_positions, start=1):
                    sym = pos.get("tradingsymbol", "").upper()
                    if sym and sym not in symbols_seen:
                        symbols_seen.add(sym)
                        if sym.startswith(prefix):
                            yield Completion(sym, start_position=-len(word_under_cursor), display=sym, display_meta=f"Position #{idx} ({pos.get('quantity', 0)} qty)")

            # Option chain strikes
            if self.session and hasattr(self.session, "oc_data") and self.session.oc_data:
                for strike_dict in self.session.oc_data.get("strikes", []):
                    for side in ("ce", "pe"):
                        sym = str(strike_dict.get(f"{side}_symbol", "")).upper()
                        ltp = strike_dict.get(f"{side}_ltp", 0.0)
                        if sym and sym not in symbols_seen:
                            symbols_seen.add(sym)
                            if sym.startswith(prefix):
                                yield Completion(sym, start_position=-len(word_under_cursor), display=sym, display_meta=f"OC Strike ₹{strike_dict.get('strike')} {side.upper()} (LTP: ₹{ltp:.2f})")

            # Dual Option chain strikes
            if self.session and hasattr(self.session, "oc_dual_data") and self.session.oc_dual_data:
                for oc_k in ("oc1", "oc2"):
                    chain = self.session.oc_dual_data.get(oc_k)
                    if chain:
                        for strike_dict in chain.get("strikes", []):
                            for side in ("ce", "pe"):
                                sym = str(strike_dict.get(f"{side}_symbol", "")).upper()
                                if sym and sym not in symbols_seen:
                                    symbols_seen.add(sym)
                                    if sym.startswith(prefix):
                                        yield Completion(sym, start_position=-len(word_under_cursor), display=sym, display_meta=f"OC-Comp Strike ₹{strike_dict.get('strike')} {side.upper()}")


# Typo auto-correction mappings
TYPO_VERB_MAP = {
    "by": "buy",
    "bu": "buy",
    "buyy": "buy",
    "biuy": "buy",
    "sel": "sell",
    "sl": "sell",
    "selll": "sell",
    "sll": "sell",
    "sle": "sell",
    "ext": "exit",
    "eixt": "exit",
    "exitt": "exit",
    "exi": "exit",
    "xt": "exit",
    "cancle": "cancel",
    "cncl": "cancel",
    "canc": "cancel",
    "cancl": "cancel",
    "cance": "cancel",
    "mod": "modify",
    "mdfy": "modify",
    "modif": "modify",
    "modifyy": "modify",
    "selct": "select",
    "slect": "select",
    "slec": "select",
    "slct": "select",
    "hldngs": "holdings",
    "hldng": "holdings",
    "holdin": "holdings",
    "adviser": "advisor",
    "advisr": "advisor",
    "advsr": "advisor",
    "opchain": "oc",
    "occomp": "oc comp",
    "oc-comp": "oc comp",
    "ocdual": "oc comp",
    "oc-dual": "oc comp",
}


def correct_command_typos(raw_text: str, session: KCLILiveSession | None = None) -> str | None:
    """Analyze and auto-correct common typographical errors in user command input.

    Returns:
        Corrected command string if a valid correction was made, or None.
    """
    if not raw_text or not raw_text.strip():
        return None

    cleaned = raw_text.strip()
    original = cleaned

    # 1. Normalize chained sub-commands
    if "&&" in cleaned:
        sub_cmds = [c.strip() for c in cleaned.split("&&") if c.strip()]
        corrected_subs = []
        any_sub_corrected = False
        for sub in sub_cmds:
            c = correct_command_typos(sub, session)
            if c:
                corrected_subs.append(c)
                any_sub_corrected = True
            else:
                corrected_subs.append(sub)
        return " && ".join(corrected_subs) if any_sub_corrected else None

    # 2. Fix conditional arrows and spacing
    cleaned = re.sub(r"\s*->\s*", " THEN ", cleaned)
    cleaned = re.sub(r"\s*=>\s*", " THEN ", cleaned)

    # 3. Split attached action and quantity (e.g. 'b27L 1.40' -> 'b 27L 1.40', 's100 1.40' -> 's 100 1.40')
    m_attached = re.match(r"^([bBsSeExXmM])(\d+[Ll]?)(.*)$", cleaned)
    if m_attached:
        action_char, qty_part, rest = m_attached.group(1), m_attached.group(2), m_attached.group(3)
        cleaned = f"{action_char} {qty_part} {rest.strip()}".strip()

    # 4. Split attached action and symbol (e.g. 'bNIFTY...' -> 'b NIFTY...')
    m_attached_sym = re.match(r"^([bBsSeExXmM])([A-Za-z]{3,}\d+.*)$", cleaned)
    if m_attached_sym:
        action_char, sym_part = m_attached_sym.group(1), m_attached_sym.group(2)
        cleaned = f"{action_char} {sym_part}"

    tokens = cleaned.split()
    if not tokens:
        return None

    first = tokens[0].lower()

    # 5. Fix primary verb typo from dictionary
    if first in TYPO_VERB_MAP:
        tokens[0] = TYPO_VERB_MAP[first]

    # 6. Clean up invalid '@' in limit price (e.g. 'buy 27L @1.40' -> 'buy 27L 1.40')
    for i in range(1, len(tokens)):
        if tokens[i].startswith("@") and len(tokens[i]) > 1:
            val_no_at = tokens[i][1:]
            # Only strip '@' if it's a numeric price, NOT an account name (like @ZK8719)
            if re.match(r"^\d+(?:\.\d+)?$", val_no_at):
                tokens[i] = val_no_at

    # 7. Normalize lowercase trading symbols to uppercase
    for i in range(len(tokens)):
        tok = tokens[i]
        if re.match(r"^[A-Za-z]+[0-9]+[A-Za-z0-9_-]*$", tok) and any(c.islower() for c in tok):
            # Looks like an option symbol e.g. nifty2690824000ce -> NIFTY2690824000CE
            tokens[i] = tok.upper()

    # 8. Normalize lowercase lot suffix (e.g. '27l' -> '27L')
    for i in range(len(tokens)):
        if re.match(r"^\d+l$", tokens[i]):
            tokens[i] = tokens[i].upper()

    corrected = " ".join(tokens).strip()

    if corrected != original:
        return corrected
    return None
