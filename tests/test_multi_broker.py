"""
Tests for the multi-broker architecture: BaseBrokerManager ABC,
KiteAccountManager (Zerodha), KotakAccountManager, and api_client routing.
"""

import unittest
from unittest.mock import MagicMock, patch


# ── BaseBrokerManager ABC tests ───────────────────────────────────────────────

class TestBaseBrokerManagerABC(unittest.TestCase):
    """Verify the ABC cannot be instantiated directly."""

    def test_cannot_instantiate_abc(self):
        from cli.base_manager import BaseBrokerManager
        with self.assertRaises(TypeError):
            BaseBrokerManager()

    def test_abc_has_required_methods(self):
        from cli.base_manager import BaseBrokerManager
        import inspect
        abstract_methods = {
            name for name, val in inspect.getmembers(BaseBrokerManager)
            if getattr(val, "__isabstractmethod__", False)
        }
        required = {
            "broker_name", "init_account", "auto_login", "is_authenticated",
            "get_account_info", "get_access_token", "get_all_account_keys",
            "get_positions", "get_orders", "get_margins",
            "place_order", "modify_order", "cancel_order", "exit_positions",
        }
        self.assertTrue(
            required.issubset(abstract_methods),
            f"Missing abstract methods: {required - abstract_methods}",
        )


# ── KiteAccountManager tests ──────────────────────────────────────────────────

class TestKiteAccountManagerIsSubclass(unittest.TestCase):
    def test_kite_manager_is_base_subclass(self):
        from cli.base_manager import BaseBrokerManager
        from cli.kite_manager import KiteAccountManager
        self.assertTrue(issubclass(KiteAccountManager, BaseBrokerManager))

    def test_broker_name_is_zerodha(self):
        from cli.kite_manager import KiteAccountManager
        mgr = KiteAccountManager()
        self.assertEqual(mgr.broker_name, "zerodha")

    def test_supports_websocket_true(self):
        from cli.kite_manager import KiteAccountManager
        mgr = KiteAccountManager()
        self.assertTrue(mgr.supports_websocket())


class TestKiteAccountManagerInit(unittest.TestCase):
    """Test KiteAccountManager account initialisation without network calls."""

    def _make_manager_with_mock_kite(self):
        """Return a manager with a mocked KiteConnect and a registered account."""
        from cli.kite_manager import KiteAccountManager
        mgr = KiteAccountManager()
        mock_kite = MagicMock()
        mock_kite.access_token = None

        with patch("cli.kite_manager.KiteConnect", return_value=mock_kite), \
             patch("cli.kite_manager._load_sessions", return_value={}):
            mgr.init_account_kite(
                api_key="testapikey123",
                api_secret="secret",
                name="TestAccount",
            )
        return mgr, mock_kite

    def test_init_registers_account(self):
        mgr, _ = self._make_manager_with_mock_kite()
        self.assertIn("testapikey123", mgr.get_all_api_keys())

    def test_not_authenticated_initially(self):
        mgr, _ = self._make_manager_with_mock_kite()
        self.assertFalse(mgr.is_authenticated("testapikey123"))

    def test_get_account_info_structure(self):
        mgr, _ = self._make_manager_with_mock_kite()
        info = mgr.get_account_info("testapikey123")
        self.assertEqual(info["name"], "TestAccount")
        self.assertFalse(info["authenticated"])
        self.assertIn("api_key", info)

    def test_get_all_account_keys_delegates(self):
        mgr, _ = self._make_manager_with_mock_kite()
        self.assertEqual(mgr.get_all_account_keys(), ["testapikey123"])


class TestKiteGetPositions(unittest.TestCase):
    """Test KiteAccountManager.get_positions() normalisation."""

    def test_positions_normalised_fields(self):
        from cli.kite_manager import KiteAccountManager
        mgr = KiteAccountManager()
        mock_kite = MagicMock()
        mock_kite.positions.return_value = {
            "net": [
                {
                    "tradingsymbol": "NIFTY24JAN21000CE",
                    "quantity": 50,
                    "average_price": 120.0,
                    "last_price": 130.0,
                    "pnl": 500.0,
                    "realised": 0.0,
                    "unrealised": 500.0,
                    "product": "NRML",
                    "exchange": "NFO",
                    "instrument_token": 12345,
                }
            ]
        }
        mgr._clients["k1"] = mock_kite
        mgr._authenticated["k1"] = True

        positions = mgr.get_positions("k1")
        self.assertEqual(len(positions), 1)
        pos = positions[0]
        self.assertEqual(pos["tradingsymbol"], "NIFTY24JAN21000CE")
        self.assertEqual(pos["quantity"], 50)
        self.assertAlmostEqual(pos["pnl_pct"], 8.33, places=2)
        self.assertEqual(pos["instrument_token"], 12345)

    def test_order_margin_calculation(self):
        from cli.kite_manager import KiteAccountManager
        mgr = KiteAccountManager()
        mock_kite = MagicMock()
        mock_kite.order_margins.return_value = [
            {"total": 457.5, "span": 0.0, "exposure": 0.0, "option_premium": 457.5}
        ]
        mgr._clients["k1"] = mock_kite
        mgr._authenticated["k1"] = True

        m_info = mgr.get_order_margin("k1", "NIFTY26JUL22200PE", "BUY", 150, price=3.05)
        self.assertEqual(m_info["status"], "success")
        self.assertEqual(m_info["total"], 457.5)


# ── KotakAccountManager tests ─────────────────────────────────────────────────

class TestKotakAccountManagerIsSubclass(unittest.TestCase):
    def test_kotak_manager_is_base_subclass(self):
        from cli.base_manager import BaseBrokerManager
        from cli.kotak_manager import KotakAccountManager
        self.assertTrue(issubclass(KotakAccountManager, BaseBrokerManager))

    def test_broker_name_is_kotak(self):
        from cli.kotak_manager import KotakAccountManager
        mgr = KotakAccountManager()
        self.assertEqual(mgr.broker_name, "kotak")

    def test_supports_websocket(self):
        from cli.kotak_manager import KotakAccountManager
        mgr = KotakAccountManager()
        self.assertTrue(mgr.supports_websocket())


class TestKotakAccountManagerInit(unittest.TestCase):
    """Test KotakAccountManager initialisation with a mock NEOClient."""

    def _make_manager_with_mock_neo(self):
        from cli.kotak_manager import KotakAccountManager
        mgr = KotakAccountManager()
        mock_neo = MagicMock()
        mock_neo.configuration.edit_token = None

        with patch("cli.kotak_manager.NeoAPI", return_value=mock_neo, create=True), \
             patch("cli.kotak_manager._load_sessions", return_value={}):
            # Directly call init_account_kotak, patching the import
            with patch.dict("sys.modules", {"neo_api_client": MagicMock(NeoAPI=MagicMock(return_value=mock_neo))}):
                mgr.init_account_kotak(
                    consumer_key="kotak_key_123",
                    consumer_secret="secret",
                    mobile_number="+91999",
                    name="KotakTest",
                )
        return mgr, mock_neo

    def test_kotak_init_registers_account(self):
        mgr, _ = self._make_manager_with_mock_neo()
        self.assertIn("kotak_key_123", mgr.get_all_account_keys())

    def test_kotak_not_authenticated_initially(self):
        mgr, _ = self._make_manager_with_mock_neo()
        self.assertFalse(mgr.is_authenticated("kotak_key_123"))

    def test_kotak_account_info_has_broker_field(self):
        mgr, _ = self._make_manager_with_mock_neo()
        info = mgr.get_account_info("kotak_key_123")
        self.assertEqual(info.get("broker"), "kotak")
        self.assertFalse(info["authenticated"])


class TestKotakPositionNormalisation(unittest.TestCase):
    """Test that KotakAccountManager.get_positions() returns None for instrument_token."""

    def test_positions_have_no_instrument_token(self):
        from cli.kotak_manager import KotakAccountManager
        mgr = KotakAccountManager()
        mock_neo = MagicMock()
        mock_neo.positions.return_value = {
            "data": [
                {
                    "trdSym": "NIFTY24JAN21000CE",
                    "netQty": "50",
                    "avgPrice": "120.0",
                    "ltp": "130.0",
                    "realizedPL": "500.0",
                    "prod": "NRML",
                    "exch": "NFO",
                }
            ]
        }
        mgr._clients["k1"] = mock_neo
        mgr._authenticated["k1"] = True

        positions = mgr.get_positions("k1")
        self.assertEqual(len(positions), 1)
        pos = positions[0]
        self.assertIsNone(pos["instrument_token"], "Kotak positions must have instrument_token=None")
        self.assertEqual(pos["tradingsymbol"], "NIFTY24JAN21000CE")
        self.assertEqual(pos["quantity"], 50)

    def test_positions_merge_holdings_average_price(self):
        from cli.kotak_manager import KotakAccountManager
        mgr = KotakAccountManager()
        mock_neo = MagicMock()
        
        # Mock positions with detailed carry-forward fields
        mock_neo.positions.return_value = {
            "data": [
                {
                    "tok": "51441",
                    "trdSym": "NIFTY2671425200CE",
                    "cfSellQty": "1755",
                    "cfSellAmt": "1755.00",
                    "prod": "NRML",
                    "exch": "NFO",
                }
            ]
        }
        
        # Mock holdings showing the true averagePrice of 1.6926
        mock_neo.holdings.return_value = {
            "data": [
                {
                    "exchangeIdentifier": "51441",
                    "quantity": -1755,
                    "averagePrice": 1.6926,
                }
            ]
        }
        
        mgr._clients["k1"] = mock_neo
        mgr._authenticated["k1"] = True

        positions = mgr.get_positions("k1")
        self.assertEqual(len(positions), 1)
        pos = positions[0]
        self.assertEqual(pos["tradingsymbol"], "NIFTY2671425200CE")
        self.assertEqual(pos["quantity"], -1755)
        # Average price should be 1.6926 (from holdings), not 1.00 (from positions)
        self.assertAlmostEqual(pos["average_price"], 1.6926)
# ── api_client routing tests ──────────────────────────────────────────────────

class TestApiClientBrokerRouting(unittest.TestCase):
    """Test that KCLIClient correctly routes Zerodha vs Kotak accounts."""

    def setUp(self):
        # Patch at the module level to prevent real KiteConnect instantiation
        self.kite_patch = patch("cli.kite_manager.KiteConnect")
        self.mock_kite_cls = self.kite_patch.start()
        self.mock_kite = MagicMock()
        self.mock_kite.access_token = None
        self.mock_kite_cls.return_value = self.mock_kite

        self.sessions_patch = patch("cli.kite_manager._load_sessions", return_value={})
        self.sessions_patch.start()

    def tearDown(self):
        self.kite_patch.stop()
        self.sessions_patch.stop()
        # Reset global state in api_client between tests
        import cli.api_client as ac
        ac._account_manager_map.clear()

    def test_zerodha_account_routes_to_kite_manager(self):
        import cli.api_client as ac
        from cli.kite_manager import KiteAccountManager

        client = ac.KCLIClient([{
            "name": "ZK",
            "broker": "zerodha",
            "api_key": "zk_key",
            "api_secret": "secret",
        }])

        mgr = ac._manager_for("zk_key")
        self.assertIsInstance(mgr, KiteAccountManager)

    def test_kotak_account_routes_to_kotak_manager(self):
        from unittest.mock import patch as _patch
        import sys
        import cli.api_client as ac

        mock_neo_client = MagicMock()
        mock_neo_client.configuration.edit_token = None
        mock_neo_module = MagicMock()
        mock_neo_module.NeoAPI.return_value = mock_neo_client

        with _patch.dict(sys.modules, {"neo_api_client": mock_neo_module}), \
             _patch("cli.kotak_manager._load_sessions", return_value={}):
            client = ac.KCLIClient([{
                "name": "KotakAcc",
                "broker": "kotak",
                "consumer_key": "kt_key",
                "consumer_secret": "secret",
            }])

        mgr = ac._manager_for("kt_key")
        self.assertEqual(mgr.broker_name, "kotak")

    def test_get_positions_resolves_kotak_tokens(self):
        from unittest.mock import patch as _patch
        import sys
        import cli.api_client as ac

        mock_neo_client = MagicMock()
        mock_neo_client.configuration.edit_token = None
        mock_neo_module = MagicMock()
        mock_neo_module.NeoAPI.return_value = mock_neo_client
        
        # Mock Kotak positions returning one position without instrument_token
        mock_neo_client.positions.return_value = {
            "data": [
                {
                    "trdSym": "NIFTY2671425200CE",
                    "netQty": "-1755",
                    "avgPrice": "1.0",
                    "ltp": "1.0",
                    "realizedPL": "0.0",
                    "prod": "NRML",
                    "exch": "NFO",
                }
            ]
        }

        # Mock Zerodha client ltp call
        self.mock_kite.ltp.return_value = {
            "NFO:NIFTY2671425200CE": {
                "instrument_token": 98765,
                "last_price": 142.5
            }
        }

        with _patch.dict(sys.modules, {"neo_api_client": mock_neo_module}), \
             _patch("cli.kotak_manager._load_sessions", return_value={}):
            client = ac.KCLIClient([
                {
                    "name": "ZK",
                    "broker": "zerodha",
                    "api_key": "zk_key",
                    "api_secret": "secret",
                },
                {
                    "name": "KotakAcc",
                    "broker": "kotak",
                    "consumer_key": "kt_key",
                    "consumer_secret": "secret",
                }
            ])
            # Pretend both are authenticated
            ac._account_manager_map["zk_key"]._authenticated["zk_key"] = True
            ac._account_manager_map["kt_key"]._authenticated["kt_key"] = True

            # Call get_positions
            res = client.get_positions(["zk_key", "kt_key"])
            
            # Find the Kotak account result
            kotak_res = next(a for a in res["accounts"] if a["api_key"] == "kt_key")
            self.assertEqual(len(kotak_res["positions"]), 1)
            pos = kotak_res["positions"][0]
            
            # Assertions on resolved values
            self.assertEqual(pos["instrument_token"], 98765)
            self.assertEqual(pos["last_price"], 142.5)
            self.assertAlmostEqual(pos["pnl"], -248332.5)

    def test_kotak_place_order_transaction_type_mapping_and_errors(self):
        from cli.kotak_manager import KotakAccountManager
        mgr = KotakAccountManager()
        mock_neo = MagicMock()
        mgr._clients["k1"] = mock_neo
        mgr._authenticated["k1"] = True

        # Test successful order placement maps transaction type
        mock_neo.place_order.return_value = {"nOrdNo": "123456"}
        order_ids = mgr.place_order(
            api_key="k1",
            tradingsymbol="NIFTY26JUL26200CE",
            exchange="NFO",
            transaction_type="BUY",
            quantity=100,
            order_type="LIMIT",
            price=1.0,
            product="NRML"
        )
        self.assertEqual(order_ids, ["123456"])
        # Verify it mapped "BUY" to "B" when calling place_order
        mock_neo.place_order.assert_called_with(
            exchange_segment="NFO",
            product="NRML",
            price="1.0",
            order_type="L",
            quantity="100",
            validity="DAY",
            trading_symbol="NIFTY26JUL26200CE",
            transaction_type="B",
            amo="NO",
            disclosed_quantity="0",
            market_protection="0",
            pf="N",
            trigger_price="0",
            tag=None
        )

        # Test that returning an SDK-level error dictionary raises a RuntimeError
        mock_neo.place_order.return_value = {"Error": "Some validation error"}
        with self.assertRaises(RuntimeError) as ctx:
            mgr.place_order(
                api_key="k1",
                tradingsymbol="NIFTY26JUL26200CE",
                exchange="NFO",
                transaction_type="BUY",
                quantity=100,
                order_type="LIMIT",
                price=1.0,
                product="NRML"
            )
        self.assertIn("Some validation error", str(ctx.exception))

        # Test that returning a backend-level error dictionary raises a RuntimeError
        mock_neo.place_order.return_value = {"stCode": 100008, "errMsg": "unauthorized", "stat": "Not_Ok"}
        with self.assertRaises(RuntimeError) as ctx:
            mgr.place_order(
                api_key="k1",
                tradingsymbol="NIFTY26JUL26200CE",
                exchange="NFO",
                transaction_type="BUY",
                quantity=100,
                order_type="LIMIT",
                price=1.0,
                product="NRML"
            )
        self.assertIn("unauthorized", str(ctx.exception))
        self.assertIn("100008", str(ctx.exception))

        # Test that returning 'No Data' (code 5203) does not raise any exceptions
        mock_neo.place_order.return_value = {"stCode": 5203, "errMsg": "No Data", "stat": "Not_Ok"}
        # This should return a list with an empty order_id (from fallback str(resp)) or similar, but not raise an error
        try:
            mgr.place_order(
                api_key="k1",
                tradingsymbol="NIFTY26JUL26200CE",
                exchange="NFO",
                transaction_type="BUY",
                quantity=100,
                order_type="LIMIT",
                price=1.0,
                product="NRML"
            )
        except Exception as exc:
            self.fail(f"place_order raised an exception on No Data: {exc}")

    def test_kotak_ticker_reconnect_flow(self):
        from cli.kotak_manager import KotakTicker
        
        mock_client = MagicMock()
        ticker = KotakTicker("kt_key", "token", mock_client, reconnect=True, reconnect_max_tries=3)
        
        # Mock callbacks
        on_close_called = False
        def mock_on_close(ws, code, reason):
            nonlocal on_close_called
            on_close_called = True
        ticker.on_close = mock_on_close
        
        # Patch Timer so we don't actually wait
        timer_mocks = []
        class MockTimer:
            def __init__(self, delay, fn, *args, **kwargs):
                self.delay = delay
                self.fn = fn
                timer_mocks.append(self)
            def start(self):
                pass
            def cancel(self):
                pass

        import threading
        original_timer = threading.Timer
        threading.Timer = MockTimer
        try:
            # Simulate _on_close
            ticker._on_close("Session closed")
            self.assertTrue(on_close_called)
            self.assertEqual(ticker._reconnect_attempt, 1)
            self.assertEqual(len(timer_mocks), 1)
            self.assertEqual(timer_mocks[0].delay, 2) # first backoff = 2 seconds
            
            # Reset timer_mocks and check close prevents reconnection
            timer_mocks.clear()
            ticker.close()
            self.assertTrue(ticker._stop_reconnect)
            ticker._on_close("Session closed")
            # reconnect_attempt should NOT increment because _stop_reconnect is True
            self.assertEqual(ticker._reconnect_attempt, 1)
            self.assertEqual(len(timer_mocks), 0)
        finally:
            threading.Timer = original_timer

    def test_kotak_ticker_error_code_mapping(self):
        from cli.kotak_manager import KotakTicker
        
        mock_client = MagicMock()
        ticker = KotakTicker("kt_key", "token", mock_client, reconnect=False)
        
        last_error_code = None
        def mock_on_error(ws, code, reason):
            nonlocal last_error_code
            last_error_code = code
        ticker.on_error = mock_on_error
        
        # Test regular error -> code 0
        ticker._on_error("Connection timed out")
        self.assertEqual(last_error_code, 0)
        
        # Test auth error -> code 403
        ticker._on_error("unauthorized session")
        self.assertEqual(last_error_code, 403)


if __name__ == "__main__":
    unittest.main()
