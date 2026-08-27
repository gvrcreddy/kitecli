"""
SQLite-backed data recording subsystem for KiteCLI.

Records order updates, position snapshots, and user commands asynchronously
on a background thread to prevent TUI rendering blocks.
"""

import json
import logging
import queue
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("kitecli.recorder")


def parse_to_iso_timestamp(ts_val: Any) -> str:
    """Normalize any timestamp string (ISO, Kotak 'DD-Mon-YYYY HH:MM:SS', or standard space) to ISO format."""
    if not ts_val:
        return datetime.now().isoformat()
    if hasattr(ts_val, "isoformat"):
        return ts_val.isoformat()
    s = str(ts_val).strip()
    if not s:
        return datetime.now().isoformat()

    formats = [
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.isoformat()
        except ValueError:
            pass
    return s


class DataRecorder:
    """Manages asynchronous batch persistence of trading logs to SQLite."""

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            self.db_path = Path.home() / ".kcli" / "data.db"
        else:
            self.db_path = Path(db_path)

        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        """Start the background recorder thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="KCLIDataRecorder",
            daemon=True,
        )
        self._thread.start()
        logger.info("DataRecorder thread started.")

    def stop(self) -> None:
        """Gracefully stop the background recorder thread and flush pending items."""
        if not self._running:
            return

        self._running = False
        # Send sentinel to unblock worker
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("DataRecorder thread stopped.")

    def enqueue_order(self, order_data: dict[str, Any], index_values: dict[str, Any], user_id: str) -> None:
        """Enqueue an order update for logging."""
        self._queue.put({
            "type": "order",
            "timestamp": datetime.now().isoformat(),
            "user_id": user_id,
            "order_data": order_data,
            "index_values": index_values,
        })

    def enqueue_positions(self, positions: list[dict[str, Any]], index_values: dict[str, Any], user_id: str) -> None:
        """Enqueue a list of positions for logging."""
        self._queue.put({
            "type": "positions",
            "timestamp": datetime.now().isoformat(),
            "user_id": user_id,
            "positions": positions,
            "index_values": index_values,
        })

    def enqueue_executed_order_context(
        self,
        user_id: str,
        order_data: dict[str, Any],
        margins: dict[str, Any],
        greeks: dict[str, Any],
        indicators: dict[str, Any],
        index_values: dict[str, Any],
        positions: list[dict[str, Any]] | None = None,
        account_name: str | None = None,
        charges: dict[str, Any] | None = None,
        pos_intent: str | None = None,
        is_settled_externally: int = 0,
    ) -> None:
        """Enqueue an executed order with market context (Greeks, Margins, Indicators, Positions, Charges)."""
        ord_ts = order_data.get("order_timestamp") or order_data.get("exchange_timestamp")
        ts_str = parse_to_iso_timestamp(ord_ts)

        self._queue.put({
            "type": "executed_order_context",
            "timestamp": ts_str,
            "user_id": user_id,
            "account_name": account_name,
            "order_data": order_data,
            "margins": margins,
            "greeks": greeks,
            "indicators": indicators,
            "index_values": index_values,
            "positions": positions or [],
            "charges": charges or {},
            "pos_intent": pos_intent,
            "is_settled_externally": is_settled_externally,
        })

    def enqueue_command(
        self,
        command_text: str,
        parsed_action: str,
        status: str,
        result_message: str | None,
        index_values: dict[str, Any],
        user_id: str | None = None,
    ) -> None:
        """Enqueue a user command execution log."""
        self._queue.put({
            "type": "command",
            "timestamp": datetime.now().isoformat(),
            "user_id": user_id,
            "command_text": command_text,
            "parsed_action": parsed_action,
            "status": status,
            "result_message": result_message,
            "index_values": index_values,
        })

    def _init_db(self, conn: sqlite3.Connection) -> None:
        """Initialise database tables and enable WAL mode."""
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        # Create orders table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                parent_order_id TEXT,
                tradingsymbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                transaction_type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                order_type TEXT NOT NULL,
                price REAL,
                trigger_price REAL,
                average_price REAL,
                status TEXT NOT NULL,
                status_message TEXT,
                nifty REAL,
                sensex REAL,
                vix REAL
            );
        """)

        # Create position snapshots table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS position_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_id TEXT NOT NULL,
                tradingsymbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                average_price REAL NOT NULL,
                last_price REAL NOT NULL,
                pnl REAL NOT NULL,
                realised REAL NOT NULL,
                unrealised REAL NOT NULL,
                nifty REAL,
                sensex REAL,
                vix REAL
            );
        """)

        # Create user commands table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_id TEXT,
                command_text TEXT NOT NULL,
                parsed_action TEXT NOT NULL,
                status TEXT NOT NULL,
                result_message TEXT,
                nifty REAL,
                sensex REAL,
                vix REAL
            );
        """)

        # Create executed order context table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS executed_order_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_id TEXT NOT NULL,
                account_name TEXT,
                order_id TEXT NOT NULL,
                tradingsymbol TEXT NOT NULL,
                exchange TEXT,
                transaction_type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                status TEXT NOT NULL,

                -- Option Greeks
                delta REAL,
                gamma REAL,
                theta REAL,
                vega REAL,
                iv REAL,

                -- Account Margins
                available_net_margin REAL,
                available_cash REAL,

                -- Technical & Market Context
                nifty_spot REAL,
                nifty_vix REAL,
                nifty_ema_9 REAL,
                nifty_sma_20 REAL,
                nifty_sma_50 REAL,
                nifty_sma_200 REAL,
                nifty_rsi_14 REAL,

                -- ML Indicators (MACD, Bollinger Bands, Distances, Returns)
                nifty_macd_line REAL,
                nifty_macd_signal REAL,
                nifty_macd_hist REAL,
                nifty_bb_upper REAL,
                nifty_bb_lower REAL,
                nifty_bb_pct_b REAL,
                nifty_bb_width REAL,
                nifty_dist_sma_20_pct REAL,
                nifty_dist_sma_50_pct REAL,
                nifty_dist_sma_200_pct REAL,
                nifty_ret_1d_pct REAL,
                nifty_ret_5d_pct REAL,
                nifty_ret_20d_pct REAL,

                -- Open Positions Context at Execution Time
                positions_snapshot TEXT,
                open_positions_count INTEGER,
                total_unrealised_pnl REAL,
                total_realised_pnl REAL,
                grand_total_pnl REAL,

                -- Order Statutory Charges & Taxes
                total_charges REAL,
                brokerage REAL,
                stt REAL,
                gst REAL
            );
        """)

        # Ensure schema migrations for executed_order_context if table pre-existed with fewer columns
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(executed_order_context);")
            existing_cols = {row[1] for row in cursor.fetchall()}
            if existing_cols:
                required_cols = {
                    "account_name": "TEXT",
                    "nifty_sma_200": "REAL",
                    "nifty_macd_line": "REAL",
                    "nifty_macd_signal": "REAL",
                    "nifty_macd_hist": "REAL",
                    "nifty_bb_upper": "REAL",
                    "nifty_bb_lower": "REAL",
                    "nifty_bb_pct_b": "REAL",
                    "nifty_bb_width": "REAL",
                    "nifty_dist_sma_20_pct": "REAL",
                    "nifty_dist_sma_50_pct": "REAL",
                    "nifty_dist_sma_200_pct": "REAL",
                    "nifty_ret_1d_pct": "REAL",
                    "nifty_ret_5d_pct": "REAL",
                    "nifty_ret_20d_pct": "REAL",
                    "positions_snapshot": "TEXT",
                    "open_positions_count": "INTEGER",
                    "total_unrealised_pnl": "REAL",
                    "total_realised_pnl": "REAL",
                    "grand_total_pnl": "REAL",
                    "total_charges": "REAL",
                    "brokerage": "REAL",
                    "stt": "REAL",
                    "gst": "REAL",
                    "is_settled_externally": "INTEGER DEFAULT 0",
                    "pos_intent": "TEXT",
                }
                for col_name, col_type in required_cols.items():
                    if col_name not in existing_cols:
                        try:
                            cursor.execute(f"ALTER TABLE executed_order_context ADD COLUMN {col_name} {col_type};")
                        except Exception:
                            pass
        except Exception as exc:
            logger.warning("Schema migration for executed_order_context skipped: %s", exc)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS conditional_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                user_id TEXT,
                account_key TEXT,
                account_name TEXT,
                parent_order_ids TEXT NOT NULL,
                parent_symbol TEXT NOT NULL,
                parent_action TEXT NOT NULL,
                parent_qty INTEGER NOT NULL,
                parent_price REAL,
                child_command_raw TEXT NOT NULL,
                child_action TEXT NOT NULL,
                child_symbol TEXT,
                child_qty TEXT,
                child_price REAL,
                child_product TEXT DEFAULT 'NRML',
                status TEXT NOT NULL DEFAULT 'PENDING',
                triggered_at TEXT,
                child_order_ids TEXT,
                failure_reason TEXT
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cond_orders_lookup ON conditional_orders(trade_date, status);")

        conn.commit()

    def _worker_loop(self) -> None:
        """Background thread worker loop that processes and batch-saves queued logs."""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path))
            self._init_db(conn)
        except Exception as exc:
            logger.error("Failed to initialize database: %s", exc, exc_info=True)
            return

        try:
            while True:
                # Blocks until an item is available
                item = self._queue.get()
                if item is None:
                    # Sentinel received, exit thread
                    self._queue.task_done()
                    break

                # Gather any other immediately available items to write in a single batch
                batch = [item]
                while not self._queue.empty():
                    try:
                        next_item = self._queue.get_nowait()
                        if next_item is None:
                            # Sentinel found, we should stop after this batch
                            self._queue.task_done()
                            self._running = False
                            break
                        batch.append(next_item)
                    except queue.Empty:
                        break

                # Process the gathered batch within a single transaction
                try:
                    self._write_batch(conn, batch)
                except Exception as exc:
                    logger.error("Error writing batch to DB: %s", exc, exc_info=True)

                # Mark all processed items as done
                for _ in range(len(batch)):
                    self._queue.task_done()

                if not self._running:
                    break

        finally:
            try:
                # Final flush of any items that arrived right during shutdown
                final_batch = []
                while not self._queue.empty():
                    try:
                        next_item = self._queue.get_nowait()
                        if next_item is not None:
                            final_batch.append(next_item)
                        self._queue.task_done()
                    except queue.Empty:
                        break
                if final_batch:
                    self._write_batch(conn, final_batch)
            except Exception as exc:
                logger.error("Error during final database flush: %s", exc, exc_info=True)
            finally:
                conn.close()

    def _write_batch(self, conn: sqlite3.Connection, batch: list[dict[str, Any]]) -> None:
        """Write a batch of items to the database in a transaction."""
        cursor = conn.cursor()
        for item in batch:
            item_type = item.get("type")
            ts = item.get("timestamp")
            user_id = item.get("user_id")
            indices = item.get("index_values", {})
            nifty = indices.get("nifty")
            sensex = indices.get("sensex")
            vix = indices.get("vix")

            if item_type == "order":
                o = item.get("order_data", {})
                cursor.execute(
                    """
                    INSERT INTO orders (
                        timestamp, user_id, order_id, parent_order_id, tradingsymbol, exchange,
                        transaction_type, quantity, order_type, price, trigger_price,
                        average_price, status, status_message, nifty, sensex, vix
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        user_id,
                        str(o.get("order_id", "")),
                        o.get("parent_order_id"),
                        o.get("tradingsymbol", ""),
                        o.get("exchange", ""),
                        o.get("transaction_type", ""),
                        int(o.get("quantity", 0)),
                        o.get("order_type", ""),
                        o.get("price"),
                        o.get("trigger_price"),
                        o.get("average_price"),
                        o.get("status", ""),
                        o.get("status_message"),
                        nifty,
                        sensex,
                        vix,
                    ),
                )

            elif item_type == "positions":
                positions_list = item.get("positions", [])
                for p in positions_list:
                    cursor.execute(
                        """
                        INSERT INTO position_snapshots (
                            timestamp, user_id, tradingsymbol, exchange, quantity,
                            average_price, last_price, pnl, realised, unrealised, nifty, sensex, vix
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ts,
                            user_id,
                            p.get("tradingsymbol", ""),
                            p.get("exchange", ""),
                            int(p.get("quantity", 0)),
                            float(p.get("average_price", 0.0)),
                            float(p.get("last_price", 0.0)),
                            float(p.get("pnl", 0.0)),
                            float(p.get("realised", 0.0)),
                            float(p.get("unrealised", 0.0)),
                            nifty,
                            sensex,
                            vix,
                        ),
                    )

            elif item_type == "executed_order_context":
                o = item.get("order_data", {})
                m = item.get("margins", {})
                g = item.get("greeks", {})
                ind = item.get("indicators", {})
                pos_list = item.get("positions", [])
                pos_json = json.dumps(pos_list, default=str) if pos_list else None
                open_cnt = len([p for p in pos_list if p.get("quantity", 0) != 0])
                total_unrealised = sum(float(p.get("unrealised", 0.0)) for p in pos_list if p.get("quantity", 0) != 0)
                total_realised = sum(float(p.get("realised", 0.0)) for p in pos_list)
                grand_total = total_realised + total_unrealised

                account_name = item.get("account_name")

                chg = item.get("charges", {})
                oid_str = str(o.get("order_id", "")).strip()
                if oid_str:
                    cursor.execute("DELETE FROM executed_order_context WHERE order_id = ?;", (oid_str,))

                cursor.execute(
                    """
                    INSERT INTO executed_order_context (
                        timestamp, user_id, account_name, order_id, tradingsymbol, exchange,
                        transaction_type, quantity, price, status,
                        delta, gamma, theta, vega, iv,
                        available_net_margin, available_cash,
                        nifty_spot, nifty_vix, nifty_ema_9, nifty_sma_20, nifty_sma_50, nifty_sma_200, nifty_rsi_14,
                        nifty_macd_line, nifty_macd_signal, nifty_macd_hist,
                        nifty_bb_upper, nifty_bb_lower, nifty_bb_pct_b, nifty_bb_width,
                        nifty_dist_sma_20_pct, nifty_dist_sma_50_pct, nifty_dist_sma_200_pct,
                        nifty_ret_1d_pct, nifty_ret_5d_pct, nifty_ret_20d_pct,
                        positions_snapshot, open_positions_count, total_unrealised_pnl, total_realised_pnl, grand_total_pnl,
                        total_charges, brokerage, stt, gst,
                        pos_intent, is_settled_externally
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        user_id,
                        account_name,
                        str(o.get("order_id", "")),
                        o.get("tradingsymbol", ""),
                        o.get("exchange", ""),
                        ("BUY" if str(o.get("transaction_type", "")).upper() in ("B", "BUY") else ("SELL" if str(o.get("transaction_type", "")).upper() in ("S", "SELL") else str(o.get("transaction_type", "")).upper())),
                        int(o.get("quantity", 0)),
                        float(o.get("average_price") or o.get("price") or 0.0),
                        o.get("status", "COMPLETE"),
                        g.get("delta"),
                        g.get("gamma"),
                        g.get("theta"),
                        g.get("vega"),
                        g.get("iv"),
                        m.get("margin_net"),
                        m.get("margin_cash"),
                        nifty,
                        vix,
                        ind.get("nifty_ema_9"),
                        ind.get("nifty_sma_20"),
                        ind.get("nifty_sma_50"),
                        ind.get("nifty_sma_200"),
                        ind.get("nifty_rsi_14"),
                        ind.get("nifty_macd_line"),
                        ind.get("nifty_macd_signal"),
                        ind.get("nifty_macd_hist"),
                        ind.get("nifty_bb_upper"),
                        ind.get("nifty_bb_lower"),
                        ind.get("nifty_bb_pct_b"),
                        ind.get("nifty_bb_width"),
                        ind.get("nifty_dist_sma_20_pct"),
                        ind.get("nifty_dist_sma_50_pct"),
                        ind.get("nifty_dist_sma_200_pct"),
                        ind.get("nifty_ret_1d_pct"),
                        ind.get("nifty_ret_5d_pct"),
                        ind.get("nifty_ret_20d_pct"),
                        pos_json,
                        open_cnt,
                        total_unrealised,
                        total_realised,
                        grand_total,
                        chg.get("total_charges"),
                        chg.get("brokerage"),
                        chg.get("stt"),
                        chg.get("gst"),
                        item.get("pos_intent"),
                        int(item.get("is_settled_externally", 0)),
                    ),
                )
        conn.commit()

    def get_connection(self) -> sqlite3.Connection:
        """Return a configured SQLite connection with WAL mode enabled."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def save_conditional_order(
        self,
        trade_date: str,
        parent_order_ids: list[str],
        parent_symbol: str,
        parent_action: str,
        parent_qty: int,
        parent_price: float | None,
        child_command_raw: str,
        child_action: str,
        child_symbol: str | None,
        child_qty: str | None,
        child_price: float | None,
        child_product: str = "NRML",
        user_id: str | None = None,
        account_key: str | None = None,
        account_name: str | None = None,
    ) -> int:
        """Synchronously persist a new conditional order to SQLite and return its inserted ID."""
        parent_ids_json = json.dumps([str(oid) for oid in parent_order_ids])
        now_iso = datetime.now().isoformat()
        with self.get_connection() as conn:
            self._init_db(conn)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO conditional_orders (
                    trade_date, created_at, user_id, account_key, account_name,
                    parent_order_ids, parent_symbol, parent_action, parent_qty, parent_price,
                    child_command_raw, child_action, child_symbol, child_qty, child_price,
                    child_product, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING');
                """,
                (
                    trade_date,
                    now_iso,
                    user_id or "UNKNOWN",
                    account_key,
                    account_name,
                    parent_ids_json,
                    parent_symbol,
                    parent_action.upper(),
                    parent_qty,
                    parent_price,
                    child_command_raw,
                    child_action.upper(),
                    child_symbol,
                    child_qty,
                    child_price,
                    child_product.upper() if child_product else "NRML",
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def get_pending_conditional_orders(self, today_str: str) -> list[dict[str, Any]]:
        """Retrieve all active pending conditional orders for today."""
        with self.get_connection() as conn:
            self._init_db(conn)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM conditional_orders WHERE trade_date = ? AND status = 'PENDING' ORDER BY id ASC;",
                (today_str,),
            )
            rows = cursor.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                try:
                    d["parent_order_ids"] = json.loads(d.get("parent_order_ids", "[]"))
                except Exception:
                    d["parent_order_ids"] = [d.get("parent_order_ids", "")]
                try:
                    d["child_order_ids"] = json.loads(d.get("child_order_ids", "[]")) if d.get("child_order_ids") else []
                except Exception:
                    d["child_order_ids"] = [d.get("child_order_ids", "")] if d.get("child_order_ids") else []
                results.append(d)
            return results

    def get_all_conditional_orders_today(self, today_str: str) -> list[dict[str, Any]]:
        """Retrieve all conditional orders for today (all statuses)."""
        with self.get_connection() as conn:
            self._init_db(conn)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM conditional_orders WHERE trade_date = ? ORDER BY id DESC;",
                (today_str,),
            )
            rows = cursor.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                try:
                    d["parent_order_ids"] = json.loads(d.get("parent_order_ids", "[]"))
                except Exception:
                    d["parent_order_ids"] = [d.get("parent_order_ids", "")]
                try:
                    d["child_order_ids"] = json.loads(d.get("child_order_ids", "[]")) if d.get("child_order_ids") else []
                except Exception:
                    d["child_order_ids"] = [d.get("child_order_ids", "")] if d.get("child_order_ids") else []
                results.append(d)
            return results

    def update_conditional_order_status(
        self,
        cond_id: int,
        status: str,
        child_order_ids: list[str] | str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """Update status and metadata of a conditional order."""
        child_ids_str = None
        if child_order_ids is not None:
            if isinstance(child_order_ids, list):
                child_ids_str = json.dumps([str(x) for x in child_order_ids])
            else:
                child_ids_str = json.dumps([str(child_order_ids)])

        now_iso = datetime.now().isoformat() if status in ("TRIGGERED", "COMPLETE") else None

        with self.get_connection() as conn:
            self._init_db(conn)
            cursor = conn.cursor()
            if status in ("TRIGGERED", "COMPLETE"):
                cursor.execute(
                    """
                    UPDATE conditional_orders
                    SET status = ?, triggered_at = COALESCE(triggered_at, ?), child_order_ids = COALESCE(?, child_order_ids), failure_reason = ?
                    WHERE id = ?;
                    """,
                    (status, now_iso, child_ids_str, failure_reason, cond_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE conditional_orders
                    SET status = ?, failure_reason = ?, child_order_ids = COALESCE(?, child_order_ids)
                    WHERE id = ?;
                    """,
                    (status, failure_reason, child_ids_str, cond_id),
                )
            conn.commit()

    def expire_old_conditional_orders(self, today_str: str) -> int:
        """Expire all pending conditional orders from previous trading sessions."""
        with self.get_connection() as conn:
            self._init_db(conn)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE conditional_orders SET status = 'EXPIRED', failure_reason = 'Day ended / session rollover' WHERE trade_date != ? AND status = 'PENDING';",
                (today_str,),
            )
            count = cursor.rowcount
            conn.commit()
            return count

    def cancel_conditional_order(self, cond_id: int) -> bool:
        """Cancel a specific pending conditional order by ID."""
        with self.get_connection() as conn:
            self._init_db(conn)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE conditional_orders SET status = 'CANCELLED', failure_reason = 'Cancelled by user' WHERE id = ? AND status = 'PENDING';",
                (cond_id,),
            )
            count = cursor.rowcount
            conn.commit()
            return count > 0

    def cancel_conditional_orders_by_parent(self, parent_order_id: str) -> list[int]:
        """Cancel any pending conditional orders linked to a parent order ID."""
        target_oid = str(parent_order_id).strip()
        cancelled_ids = []
        with self.get_connection() as conn:
            self._init_db(conn)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, parent_order_ids FROM conditional_orders WHERE status = 'PENDING';"
            )
            rows = cursor.fetchall()
            for r in rows:
                cid = r["id"]
                p_ids_raw = r["parent_order_ids"]
                try:
                    p_ids = json.loads(p_ids_raw)
                except Exception:
                    p_ids = [p_ids_raw]
                if target_oid in [str(x) for x in p_ids]:
                    cursor.execute(
                        "UPDATE conditional_orders SET status = 'CANCELLED', failure_reason = 'Parent order cancelled' WHERE id = ?;",
                        (cid,),
                    )
                    cancelled_ids.append(cid)
            conn.commit()
        return cancelled_ids
