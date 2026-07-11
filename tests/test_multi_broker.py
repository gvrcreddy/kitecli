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
        self.assertIn("pnl_pct", pos)
        self.assertEqual(pos["instrument_token"], 12345)


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

    def test_no_websocket(self):
        from cli.kotak_manager import KotakAccountManager
        mgr = KotakAccountManager()
        self.assertFalse(mgr.supports_websocket())


class TestKotakAccountManagerInit(unittest.TestCase):
    """Test KotakAccountManager initialisation with a mock NEOClient."""

    def _make_manager_with_mock_neo(self):
        from cli.kotak_manager import KotakAccountManager
        mgr = KotakAccountManager()
        mock_neo = MagicMock()
        mock_neo.configuration.access_token = None

        with patch("cli.kotak_manager.NEOClient", return_value=mock_neo, create=True), \
             patch("cli.kotak_manager._load_sessions", return_value={}):
            # Directly call init_account_kotak, patching the import
            with patch.dict("sys.modules", {"neo_api_client": MagicMock(NEOClient=MagicMock(return_value=mock_neo))}):
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
        mock_neo_client.configuration.access_token = None
        mock_neo_module = MagicMock()
        mock_neo_module.NEOClient.return_value = mock_neo_client

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


if __name__ == "__main__":
    unittest.main()
