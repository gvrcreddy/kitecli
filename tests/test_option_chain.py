"""
Unit Tests for Option Chain Matrix with Greeks, OI, PCR, ATM Straddle, and Interactive Clicks.
"""
import unittest
from unittest.mock import MagicMock
from datetime import date, timedelta
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType, MouseButton, MouseModifier

from cli.greeks import enrich_option_chain_greeks
from cli.live_session import KCLILiveSession


class TestOptionChainGreeksAndMatrix(unittest.TestCase):

    def test_enrich_option_chain_greeks(self):
        spot_price = 24850.0
        expiry_date = (date.today() + timedelta(days=2)).isoformat()
        strikes = [
            {
                "strike": 24700.0,
                "ce_symbol": "NIFTY2690324700CE",
                "ce_ltp": 180.0,
                "ce_oi": 100000,
                "pe_symbol": "NIFTY2690324700PE",
                "pe_ltp": 30.0,
                "pe_oi": 50000,
            },
            {
                "strike": 24800.0,
                "ce_symbol": "NIFTY2690324800CE",
                "ce_ltp": 110.0,
                "ce_oi": 200000,
                "pe_symbol": "NIFTY2690324800PE",
                "pe_ltp": 55.0,
                "pe_oi": 150000,
            },
            {
                "strike": 24850.0,
                "ce_symbol": "NIFTY2690324850CE",
                "ce_ltp": 75.0,
                "ce_oi": 300000,
                "pe_symbol": "NIFTY2690324850PE",
                "pe_ltp": 70.0,
                "pe_oi": 300000,
            },
            {
                "strike": 24900.0,
                "ce_symbol": "NIFTY2690324900CE",
                "ce_ltp": 45.0,
                "ce_oi": 250000,
                "pe_symbol": "NIFTY2690324900PE",
                "pe_ltp": 115.0,
                "pe_oi": 100000,
            },
        ]

        summary = enrich_option_chain_greeks(strikes, spot_price, expiry_date)

        # ATM Strike
        self.assertEqual(summary["atm_strike"], 24850.0)
        # ATM Straddle: 75.0 + 70.0 = 145.0
        self.assertEqual(summary["atm_straddle"], 145.0)
        # Total CE OI: 100k + 200k + 300k + 250k = 850k
        # Total PE OI: 50k + 150k + 300k + 100k = 600k
        self.assertEqual(summary["total_ce_oi"], 850000)
        self.assertEqual(summary["total_pe_oi"], 600000)
        # PCR: 600k / 850k ≈ 0.71
        self.assertAlmostEqual(summary["pcr"], 0.71, places=2)
        # Max Pain should be near ATM
        self.assertIn(summary["max_pain"], [24800.0, 24850.0, 24900.0])

        # Verify Delta and IV calculated on ATM strike
        atm_entry = strikes[2]
        self.assertIsNotNone(atm_entry.get("ce_delta"))
        self.assertIsNotNone(atm_entry.get("pe_delta"))
        self.assertGreater(atm_entry["ce_delta"], 0.40)
        self.assertLess(atm_entry["ce_delta"], 0.60)
        self.assertLess(atm_entry["pe_delta"], -0.40)
        self.assertGreater(atm_entry["pe_delta"], -0.60)
        self.assertIsNotNone(atm_entry.get("ce_iv"))
        self.assertIsNotNone(atm_entry.get("pe_iv"))

    def test_render_oc_matrix_text(self):
        client = MagicMock()
        accounts = [{"name": "ZK8719", "api_key": "api_zk", "user_id": "ZK8719"}]
        session = KCLILiveSession(client, accounts)

        session.oc_data = {
            "underlying": "NIFTY",
            "expiry": "2026-09-03",
            "spot_price": 24852.10,
            "summary": {
                "atm_strike": 24850.0,
                "atm_straddle": 147.0,
                "pcr": 1.15,
                "max_pain": 24850.0,
            },
            "strikes": [
                {
                    "strike": 24800.0,
                    "ce_symbol": "NIFTY2690324800CE",
                    "ce_ltp": 115.0,
                    "ce_delta": 0.58,
                    "ce_iv": 13.8,
                    "ce_oi": 2400000,
                    "pe_symbol": "NIFTY2690324800PE",
                    "pe_ltp": 65.4,
                    "pe_delta": -0.42,
                    "pe_iv": 14.8,
                    "pe_oi": 1800000,
                },
                {
                    "strike": 24850.0,
                    "ce_symbol": "NIFTY2690324850CE",
                    "ce_ltp": 72.2,
                    "ce_delta": 0.50,
                    "ce_iv": 13.5,
                    "ce_oi": 3100000,
                    "pe_symbol": "NIFTY2690324850PE",
                    "pe_ltp": 74.8,
                    "pe_delta": -0.50,
                    "pe_iv": 14.5,
                    "pe_oi": 2900000,
                },
            ],
            "available_expiries": [
                {"week_label": "Current Week", "expiry": "2026-09-03"},
                {"week_label": "Next Week", "expiry": "2026-09-10"},
            ],
        }

        rendered = session._render_oc_text()
        self.assertIn("NIFTY Spot: 24,852.10", rendered)
        self.assertIn("PCR: 1.15", rendered)
        self.assertIn("ATM Str: ₹147.0", rendered)
        self.assertIn("Range: 24703-24997", rendered)
        self.assertIn("24850", rendered)  # ATM strike
        self.assertIn("24.0L", rendered)  # Formatted OI
        self.assertIn("13.5%", rendered)  # Formatted IV
        self.assertIn("0.50", rendered)   # Delta

    def test_oc_interactive_click_handlers(self):
        client = MagicMock()
        accounts = [{"name": "ZK8719", "api_key": "api_zk", "user_id": "ZK8719"}]
        session = KCLILiveSession(client, accounts)

        session.oc_data = {
            "underlying": "NIFTY",
            "expiry": "2026-09-03",
            "spot_price": 24850.0,
            "summary": {
                "atm_strike": 24850.0,
                "atm_straddle": 147.0,
                "pcr": 1.15,
                "max_pain": 24850.0,
            },
            "strikes": [
                {
                    "strike": 24850.0,
                    "ce_symbol": "NIFTY2690324850CE",
                    "ce_ltp": 72.2,
                    "ce_delta": 0.50,
                    "ce_iv": 13.5,
                    "ce_oi": 3100000,
                    "pe_symbol": "NIFTY2690324850PE",
                    "pe_ltp": 74.8,
                    "pe_delta": -0.50,
                    "pe_iv": 14.5,
                    "pe_oi": 2900000,
                },
            ],
            "available_expiries": [
                {"week_label": "Current Week", "expiry": "2026-09-03"},
                {"week_label": "Next Week", "expiry": "2026-09-10"},
            ],
        }

        frags = session._build_oc_formatted_fragments()
        self.assertTrue(len(frags) > 0)

        # Find Call fragment with mouse handler
        ce_frag = None
        for frag in frags:
            if len(frag) == 3 and callable(frag[2]) and "72.20" in frag[1]:
                ce_frag = frag
                break

        self.assertIsNotNone(ce_frag)
        # Set a mock input_field on session
        session.input_field = MagicMock()
        session.input_field.buffer = MagicMock()

        # Simulate click on Call side
        mock_mouse = MouseEvent(
            position=MagicMock(),
            event_type=MouseEventType.MOUSE_UP,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
        ce_frag[2](mock_mouse)
        # Should keep prompt clean and populate buffer with explicit sell command
        self.assertEqual(session.input_field.buffer.text, "sell NIFTY2690324850CE 27L 72.20")

    def test_oc_all_strikes_rendered(self):
        client = MagicMock()
        accounts = [{"name": "ZK8719", "api_key": "api_zk", "user_id": "ZK8719"}]
        session = KCLILiveSession(client, accounts)

        # Generate 50 strikes from 22000 to 27000
        strikes = []
        for strike in range(22000, 27000, 100):
            strikes.append({
                "strike": float(strike),
                "ce_symbol": f"NIFTY{strike}CE",
                "ce_ltp": 50.0,
                "pe_symbol": f"NIFTY{strike}PE",
                "pe_ltp": 50.0,
            })

        session.oc_data = {
            "underlying": "NIFTY",
            "expiry": "2026-09-03",
            "spot_price": 24050.0,
            "summary": {"atm_strike": 24000.0, "atm_straddle": 100.0, "pcr": 1.0, "max_pain": 24000.0},
            "strikes": strikes,
            "available_expiries": [
                {"week_label": "Current Week", "expiry": "2026-09-03", "is_monthly": False},
                {"week_label": "Next Week", "expiry": "2026-09-10", "is_monthly": False},
                {"week_label": "Far Week", "expiry": "2026-09-17", "is_monthly": False},
                {"week_label": "Sep Monthly", "expiry": "2026-09-24", "is_monthly": True},
            ],
        }

        # All strikes (both deep ITM 22000 and deep OTM 26900) should be in text
        rendered = session._render_oc_text()
        self.assertIn("22000", rendered)
        self.assertIn("24000", rendered)
        self.assertIn("26900", rendered)

    def test_oc_top_expiry_bar_and_dropdown(self):
        client = MagicMock()
        accounts = [{"name": "ZK8719", "api_key": "api_zk", "user_id": "ZK8719"}]
        session = KCLILiveSession(client, accounts)

        session.oc_data = {
            "underlying": "NIFTY",
            "expiry": "2026-09-03",
            "spot_price": 24050.0,
            "summary": {"atm_strike": 24000.0, "atm_straddle": 100.0, "pcr": 1.0, "max_pain": 24000.0},
            "strikes": [],
            "available_expiries": [
                {"week_label": "Current Week", "expiry": "2026-09-03", "is_monthly": False},
                {"week_label": "Next Week", "expiry": "2026-09-10", "is_monthly": False},
                {"week_label": "Far Week", "expiry": "2026-09-17", "is_monthly": False},
                {"week_label": "Sep Monthly", "expiry": "2026-09-24", "is_monthly": True},
            ],
        }

        # 1. Weekly mode rendering
        session.oc_dropdown_open = False
        frags_closed = session._build_oc_formatted_fragments()
        closed_text = "".join(f[1] for f in frags_closed)
        self.assertIn("Current: 03-Sep", closed_text)
        self.assertIn("Next: 10-Sep", closed_text)
        self.assertIn("Far: 17-Sep", closed_text)
        self.assertIn("Expiries (4)", closed_text)
        self.assertNotIn("Select Expiry (Click to switch)", closed_text)

        # 2. Toggle dropdown open
        session.oc_dropdown_open = True
        frags_open = session._build_oc_formatted_fragments()
        open_text = "".join(f[1] for f in frags_open)
        self.assertIn("Select Expiry (Click to switch)", open_text)
        self.assertIn("Sep Monthly", open_text)
        self.assertIn("Close Dropdown", open_text)

    def test_get_option_chain_monthly_expiry_resolution(self):
        from cli.kite_manager import KiteAccountManager
        mgr = KiteAccountManager()
        mock_kite = MagicMock()
        mgr._clients["api_zk"] = mock_kite
        mgr._authenticated["api_zk"] = True

        today = date.today()
        # Mock 4 expiries in current month and 1 in next month
        exp1 = today + timedelta(days=2)
        exp2 = today + timedelta(days=9)
        exp3_monthly = today + timedelta(days=23)

        mock_kite.instruments.return_value = [
            {"name": "NIFTY", "instrument_type": "CE", "strike": 24000, "expiry": exp1, "tradingsymbol": "NIFTY_W1_CE", "lot_size": 50},
            {"name": "NIFTY", "instrument_type": "PE", "strike": 24000, "expiry": exp1, "tradingsymbol": "NIFTY_W1_PE", "lot_size": 50},
            {"name": "NIFTY", "instrument_type": "CE", "strike": 24000, "expiry": exp3_monthly, "tradingsymbol": "NIFTY_M_CE", "lot_size": 50},
            {"name": "NIFTY", "instrument_type": "PE", "strike": 24000, "expiry": exp3_monthly, "tradingsymbol": "NIFTY_M_PE", "lot_size": 50},
        ]
        mock_kite.quote.return_value = {
            "NSE:NIFTY 50": {"last_price": 24050.0},
            "NFO:NIFTY_M_CE": {"last_price": 120.0, "instrument_token": 101, "oi": 50000, "volume": 1000},
            "NFO:NIFTY_M_PE": {"last_price": 80.0, "instrument_token": 102, "oi": 40000, "volume": 1000},
        }

        # Query with expiry_date="monthly"
        resp = mgr.get_option_chain("api_zk", "NIFTY", expiry_date="monthly")
        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["expiry"], exp3_monthly.isoformat())
        self.assertTrue(any(e.get("is_monthly") for e in resp["available_expiries"]))

    def test_oc_visual_cues_rendering(self):
        client = MagicMock()
        accounts = [{"name": "ZK8719", "api_key": "api_zk", "user_id": "ZK8719"}]
        session = KCLILiveSession(client, accounts)

        session.oc_data = {
            "underlying": "NIFTY",
            "expiry": "2026-09-08",
            "spot_price": 24055.80,
            "summary": {
                "atm_strike": 24050.0,
                "atm_straddle": 147.0,
                "pcr": 0.86,
                "max_pain": 24000.0,
            },
            "strikes": [
                {
                    "strike": 23800.0,
                    "ce_symbol": "NIFTY2690823800CE",
                    "ce_ltp": 253.60,
                    "ce_delta": 0.72,
                    "ce_iv": 14.8,
                    "ce_oi": 571000,
                    "pe_symbol": "NIFTY2690823800PE",
                    "pe_ltp": 57.80,
                    "pe_delta": -0.28,
                    "pe_iv": 15.2,
                    "pe_oi": 3023000,
                },
                {
                    "strike": 24000.0,
                    "ce_symbol": "NIFTY2690824000CE",
                    "ce_ltp": 130.20,
                    "ce_delta": 0.55,
                    "ce_iv": 13.8,
                    "ce_oi": 5852000,
                    "pe_symbol": "NIFTY2690824000PE",
                    "pe_ltp": 130.15,
                    "pe_delta": -0.45,
                    "pe_iv": 14.0,
                    "pe_oi": 5398000,
                },
                {
                    "strike": 24050.0,
                    "ce_symbol": "NIFTY2690824050CE",
                    "ce_ltp": 107.30,
                    "ce_delta": 0.50,
                    "ce_iv": 13.5,
                    "ce_oi": 2171000,
                    "pe_symbol": "NIFTY2690824050PE",
                    "pe_ltp": 158.95,
                    "pe_delta": -0.50,
                    "pe_iv": 13.8,
                    "pe_oi": 1301000,
                },
                {
                    "strike": 24200.0,
                    "ce_symbol": "NIFTY2690824200CE",
                    "ce_ltp": 53.15,
                    "ce_delta": 0.35,
                    "ce_iv": 13.9,
                    "ce_oi": 7449000,
                    "pe_symbol": "NIFTY2690824200PE",
                    "pe_delta": -0.65,
                    "pe_iv": 14.5,
                    "pe_oi": 3488000,
                },
            ],
            "available_expiries": [
                {"week_label": "Current Week", "expiry": "2026-09-08"},
            ],
        }

        rendered = session._render_oc_text()
        self.assertIn("Range: 23903-24197", rendered)
        self.assertIn("24200 R", rendered)  # Call Wall Resistance
        self.assertIn("24000 S", rendered)  # Put Wall Support
        self.assertIn("[24050 ]", rendered) # ATM strike
        self.assertIn("▇", rendered)        # OI Spark Bar

        # Verify formatted fragments include proper styles
        frags = session._build_oc_formatted_fragments()
        styles = [f[0] for f in frags]
        self.assertIn("class:oc.bar.call", styles)
        self.assertIn("class:oc.bar.put", styles)
        self.assertIn("class:oc.wall.call", styles)
        self.assertIn("class:oc.wall.put", styles)
        self.assertIn("class:oc.ce.itm", styles)
        self.assertIn("class:oc.pe.itm", styles)


    def test_black76_forward_greeks_sensibull_matching(self):
        from cli.greeks import calculate_greeks

        # 8-Sep expiry (7 DTE) at Forward F = 24000
        # 24000 CE (Price: 130.20) -> Delta = 0.50, IV ~ 9.8%, Theta ~ -9.3
        g_8sep = calculate_greeks(
            spot_price=24055.80,
            strike_price=24000.0,
            expiry_date=(date.today() + timedelta(days=7)).isoformat(),
            option_type="CE",
            option_price=130.20,
            forward_price=24000.0,
        )
        self.assertEqual(g_8sep["delta"], 0.50)
        self.assertAlmostEqual(g_8sep["iv"], 9.8, delta=0.6)
        self.assertAlmostEqual(g_8sep["theta"], -9.3, delta=1.0)

        # 15-Sep expiry (14 DTE) at Forward F = 24028.50
        # 24050 CE (Price: 168.50) -> Delta = 0.48, IV ~ 9.5%, Theta ~ -6.4
        g_15sep = calculate_greeks(
            spot_price=24055.80,
            strike_price=24050.0,
            expiry_date=(date.today() + timedelta(days=14)).isoformat(),
            option_type="CE",
            option_price=168.50,
            forward_price=24028.50,
        )
        self.assertEqual(g_15sep["delta"], 0.48)
        self.assertAlmostEqual(g_15sep["iv"], 9.5, delta=0.6)
        self.assertAlmostEqual(g_15sep["theta"], -6.4, delta=1.0)

    def test_oc_dual_rendering_and_alignment(self):
        session = KCLILiveSession(client=MagicMock(), accounts=[{"name": "TestAcct", "api_key": "k1"}])
        session.cmd_buffer = MagicMock()
        session.input_field = MagicMock()

        oc1 = {
            "expiry": "2026-09-08",
            "spot_price": 24055.80,
            "summary": {"atm_strike": 24050.0, "atm_straddle": 147.0, "pcr": 0.86, "max_pain": 24000.0},
            "strikes": [
                {"strike": 24000.0, "ce_symbol": "NIFTY2690824000CE", "ce_ltp": 130.20, "ce_iv": 9.8, "ce_delta": 0.50, "ce_theta": -9.3, "pe_symbol": "NIFTY2690824000PE", "pe_ltp": 130.15, "pe_iv": 9.8, "pe_delta": -0.50, "pe_theta": -9.3},
                {"strike": 24050.0, "ce_symbol": "NIFTY2690824050CE", "ce_ltp": 107.30, "ce_iv": 9.9, "ce_delta": 0.52, "ce_theta": -9.2, "pe_symbol": "NIFTY2690824050PE", "pe_ltp": 158.95, "pe_iv": 9.9, "pe_delta": -0.48, "pe_theta": -9.2},
            ],
        }
        oc2 = {
            "expiry": "2026-09-15",
            "spot_price": 24055.80,
            "summary": {"atm_strike": 24050.0, "atm_straddle": 210.0, "pcr": 0.75, "max_pain": 24100.0},
            "strikes": [
                {"strike": 24000.0, "ce_symbol": "NIFTY2691524000CE", "ce_ltp": 193.05, "ce_iv": 9.5, "ce_delta": 0.48, "ce_theta": -6.4, "pe_symbol": "NIFTY2691524000PE", "pe_ltp": 166.00, "pe_iv": 9.6, "pe_delta": -0.52, "pe_theta": -6.4},
                {"strike": 24050.0, "ce_symbol": "NIFTY2691524050CE", "ce_ltp": 168.50, "ce_iv": 9.5, "ce_delta": 0.50, "ce_theta": -6.5, "pe_symbol": "NIFTY2691524050PE", "pe_ltp": 190.70, "pe_iv": 9.5, "pe_delta": -0.50, "pe_theta": -6.5},
            ],
        }
        session.oc_dual_data = {
            "underlying": "NIFTY",
            "oc1": oc1,
            "oc2": oc2,
            "available_expiries": [
                {"week_label": "Current Week", "expiry": "2026-09-08"},
                {"week_label": "Next Week", "expiry": "2026-09-15"},
            ],
            "exp1_idx": 0,
            "exp2_idx": 1,
        }

        rendered = session._render_oc_dual_text()
        self.assertIn("Expiry 1: 08-Sep", rendered)
        self.assertIn("Expiry 2: 15-Sep", rendered)
        self.assertIn("│   LTP1   LTP2 │  IV1    IV2 │  Δ1   Δ2  │  θ1   θ2  │  STRIKE  │   LTP1   LTP2 │  IV1    IV2 │  Δ1   Δ2  │  θ1   θ2  │", rendered)
        self.assertIn("│ 130.20  193.05│  9.8%   9.5%│ 0.50  0.48│ -9.3  -6.4│  24000   │ 130.15  166.00│  9.8%   9.6%│-0.50 -0.52│ -9.3  -6.4│", rendered)
        self.assertIn("│ 107.30  168.50│  9.9%   9.5%│ 0.52  0.50│ -9.2  -6.5│[ 24050  ]│ 158.95  190.70│  9.9%   9.5%│-0.48 -0.50│ -9.2  -6.5│", rendered)

        # Test Formatted fragments & Click-to-Trade staging
        frags = session._build_oc_dual_formatted_fragments()
        self.assertTrue(len(frags) > 0)

        # Find fragment for 193.05 (OC2 24000 CE)
        oc2_ce_frag = next((f for f in frags if "193.05" in str(f[1])), None)
        self.assertIsNotNone(oc2_ce_frag)
        self.assertEqual(oc2_ce_frag[0], "class:oc.set2.ce")
        
        click_handler = oc2_ce_frag[2]
        mock_event = MagicMock()
        mock_event.event_type = MouseEventType.MOUSE_UP
        click_handler(mock_event)

        # Staged sell command for Expiry 2 contract
        self.assertEqual(session.input_field.buffer.text, "sell NIFTY2691524000CE 27L 193.05")

        # Find fragment for 130.15 (OC1 24000 PE)
        oc1_pe_frag = next((f for f in frags if "130.15" in str(f[1])), None)
        self.assertIsNotNone(oc1_pe_frag)
        self.assertEqual(oc1_pe_frag[0], "class:oc.set1.pe")
        
        click_handler_pe = oc1_pe_frag[2]
        click_handler_pe(mock_event)
        self.assertEqual(session.input_field.buffer.text, "sell NIFTY2690824000PE 27L 130.15")

    def test_oc_dual_live_tick_streaming_updates(self):
        client = MagicMock()
        accounts = [{"name": "ZK8719", "api_key": "api_1", "broker": "zerodha"}]
        session = KCLILiveSession(client, accounts)
        session.app = MagicMock()
        session.app.loop = MagicMock()

        oc1 = {
            "underlying": "NIFTY",
            "expiry": "2026-09-08",
            "spot_price": 24000.0,
            "summary": {"atm_strike": 24000.0, "pcr": 0.95, "max_pain": 24000, "atm_straddle": 260.0},
            "strikes": [
                {"strike": 24000.0, "ce_token": 1001, "pe_token": 1002, "ce_symbol": "NIFTY2690824000CE", "ce_ltp": 130.20, "pe_symbol": "NIFTY2690824000PE", "pe_ltp": 130.15},
            ],
        }
        oc2 = {
            "underlying": "NIFTY",
            "expiry": "2026-09-15",
            "spot_price": 24000.0,
            "summary": {"atm_strike": 24000.0, "pcr": 0.90, "max_pain": 24000, "atm_straddle": 350.0},
            "strikes": [
                {"strike": 24000.0, "ce_token": 2001, "pe_token": 2002, "ce_symbol": "NIFTY2691524000CE", "ce_ltp": 193.05, "pe_symbol": "NIFTY2691524000PE", "pe_ltp": 166.00},
            ],
        }
        session.oc_dual_data = {
            "underlying": "NIFTY",
            "oc1": oc1,
            "oc2": oc2,
            "available_expiries": [
                {"week_label": "Current Week", "expiry": "2026-09-08"},
                {"week_label": "Next Week", "expiry": "2026-09-15"},
            ],
            "exp1_idx": 0,
            "exp2_idx": 1,
        }
        session.info_mode = "oc_dual"

        # 1. Update subscriptions
        all_strikes = oc1["strikes"] + oc2["strikes"]
        session._update_oc_subscriptions(all_strikes)

        # Verify oc_token_map has mapped both OC1 and OC2 tokens
        self.assertIn(1001, session.oc_token_map)
        self.assertIn(1002, session.oc_token_map)
        self.assertIn(2001, session.oc_token_map)
        self.assertIn(2002, session.oc_token_map)

        # 2. Simulate streaming ticks arriving via WebSocket
        ticks = [
            {"instrument_token": 1001, "last_price": 145.50},  # OC1 24000 CE moves from 130.20 -> 145.50
            {"instrument_token": 2002, "last_price": 178.25},  # OC2 24000 PE moves from 166.00 -> 178.25
        ]
        session._on_ticks(None, ticks)

        # Verify strike dictionaries were directly updated
        self.assertEqual(oc1["strikes"][0]["ce_ltp"], 145.50)
        self.assertEqual(oc2["strikes"][0]["pe_ltp"], 178.25)

        # 3. Simulate spot tick update
        session.index_tokens = {256265: "nifty"}
        spot_tick = [{"instrument_token": 256265, "last_price": 24080.50}]
        session._on_ticks(None, spot_tick)

        self.assertEqual(oc1["spot_price"], 24080.50)
        self.assertEqual(oc2["spot_price"], 24080.50)

        # 4. Trigger _update_oc_and_invalidate and verify rendering reflects new prices
        session._update_oc_and_invalidate()
        rendered = session._render_oc_dual_text()
        self.assertIn("145.50", rendered)
        self.assertIn("178.25", rendered)


if __name__ == "__main__":
    unittest.main()

