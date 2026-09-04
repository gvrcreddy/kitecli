"""
Tests for syntax highlighting, tab auto-completion, and typo auto-correction in KiteCLI.
"""
import sys
import unittest
from unittest.mock import MagicMock

# Ensure project root is in path
sys.path.append("/Users/vgolugur/Documents/Projects/kitecli")

from prompt_toolkit.document import Document
from cli.highlighter import KiteCLILexer, KiteCLICompleter, correct_command_typos
from cli.live_session import KCLILiveSession


class TestHighlighterAndAutoCorrection(unittest.TestCase):

    def test_lexer_tokenizes_buy_command(self):
        lexer = KiteCLILexer()
        doc = Document("buy NIFTY2690824000CE 27L 130.20 NRML at 09:20")
        get_line = lexer.lex_document(doc)
        tokens = get_line(0)

        classes = [cls for cls, text in tokens if cls]
        self.assertIn("class:cmd.buy", classes)
        self.assertIn("class:cmd.symbol", classes)
        self.assertIn("class:cmd.qty", classes)
        self.assertIn("class:cmd.price", classes)
        self.assertIn("class:cmd.product", classes)
        self.assertIn("class:cmd.keyword", classes)
        self.assertIn("class:cmd.time", classes)

    def test_lexer_tokenizes_sell_exit_and_chaining(self):
        lexer = KiteCLILexer()
        doc = Document("sell 100 1.40 THEN exit all && oc comp")
        get_line = lexer.lex_document(doc)
        tokens = get_line(0)

        classes = [cls for cls, text in tokens if cls]
        self.assertIn("class:cmd.sell", classes)
        self.assertIn("class:cmd.chain", classes)
        self.assertIn("class:cmd.exit", classes)
        self.assertIn("class:cmd.flag", classes)
        self.assertIn("class:cmd.view", classes)

    def test_lexer_tokenizes_account_and_modify(self):
        lexer = KiteCLILexer()
        doc = Document("modify 123456 27L 1.50 @ZK8719")
        get_line = lexer.lex_document(doc)
        tokens = get_line(0)

        classes = [cls for cls, text in tokens if cls]
        self.assertIn("class:cmd.modify", classes)
        self.assertIn("class:cmd.id", classes)
        self.assertIn("class:cmd.qty", classes)
        self.assertIn("class:cmd.price", classes)
        self.assertIn("class:cmd.account", classes)

    def test_typo_correction_rules(self):
        # Verb typos
        self.assertEqual(correct_command_typos("by 27L 1.40"), "buy 27L 1.40")
        self.assertEqual(correct_command_typos("sel 100 1.40"), "sell 100 1.40")
        self.assertEqual(correct_command_typos("ext all"), "exit all")
        self.assertEqual(correct_command_typos("cancle all"), "cancel all")
        self.assertEqual(correct_command_typos("hldngs deep"), "holdings deep")
        self.assertEqual(correct_command_typos("adviser"), "advisor")
        self.assertEqual(correct_command_typos("occomp"), "oc comp")

        # Attached action and qty
        self.assertEqual(correct_command_typos("b27L 1.40"), "b 27L 1.40")
        self.assertEqual(correct_command_typos("s100 1.40"), "s 100 1.40")

        # @ in price
        self.assertEqual(correct_command_typos("buy 27L @1.40"), "buy 27L 1.40")
        self.assertEqual(correct_command_typos("exit all @0.05"), "exit all 0.05")

        # Lowercase symbol and lot suffix
        self.assertEqual(correct_command_typos("buy nifty2690824000ce 27l 1.40"), "buy NIFTY2690824000CE 27L 1.40")

        # Chained commands with typo
        self.assertEqual(correct_command_typos("by 27L 1.40 && ext all"), "buy 27L 1.40 && exit all")

    def test_completer_suggests_commands_and_accounts(self):
        session = MagicMock()
        session.accounts = [
            {"name": "ZK8719", "broker": "zerodha"},
            {"name": "WVV135", "broker": "kotak"},
        ]
        session.active_positions = [
            {"tradingsymbol": "NIFTY2690824000CE", "quantity": 100}
        ]
        session.oc_data = {
            "strikes": [
                {"strike": 24000, "ce_symbol": "NIFTY2690824000CE", "ce_ltp": 130.20, "pe_symbol": "NIFTY2690824000PE", "pe_ltp": 85.10}
            ]
        }

        completer = KiteCLICompleter(session=session)

        # 1. First word command completion
        doc1 = Document("bu")
        completions1 = [c.text for c in completer.get_completions(doc1, None)]
        self.assertIn("buy", completions1)

        doc2 = Document("oc")
        completions2 = [c.text for c in completer.get_completions(doc2, None)]
        self.assertIn("oc", completions2)
        self.assertIn("oc comp", completions2)

        # 2. Account completion
        doc3 = Document("account z")
        completions3 = [c.text for c in completer.get_completions(doc3, None)]
        self.assertIn("ZK8719", completions3)

        doc4 = Document("buy 27L @w")
        completions4 = [c.text for c in completer.get_completions(doc4, None)]
        self.assertIn("@WVV135", completions4)

        # 3. Position / OC strike symbol completion
        doc5 = Document("buy NIFTY2690824")
        completions5 = [c.text for c in completer.get_completions(doc5, None)]
        self.assertIn("NIFTY2690824000CE", completions5)

    def test_tab_and_right_arrow_fill_suggestion(self):
        from prompt_toolkit.auto_suggest import Suggestion
        from prompt_toolkit.buffer import Buffer

        buf = Buffer()
        buf.set_document(Document("buy NIFTY26", cursor_position=11), bypass_readonly=True)
        buf.suggestion = Suggestion("90824000CE 27L 130.20")

        # Tab acceptance
        if buf.complete_state:
            buf.complete_next()
        elif buf.suggestion and buf.suggestion.text:
            buf.insert_text(buf.suggestion.text)

        self.assertEqual(buf.text, "buy NIFTY2690824000CE 27L 130.20")
        self.assertEqual(buf.cursor_position, len(buf.text))


if __name__ == "__main__":
    unittest.main()
