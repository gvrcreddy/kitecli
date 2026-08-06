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
    ) -> None:
        """Enqueue an executed order with market context (Greeks, Margins, Indicators, Positions, Charges)."""
        self._queue.put({
            "type": "executed_order_context",
            "timestamp": datetime.now().isoformat(),
            "user_id": user_id,
            "account_name": account_name,
            "order_data": order_data,
            "margins": margins,
            "greeks": greeks,
            "indicators": indicators,
            "index_values": index_values,
            "positions": positions or [],
            "charges": charges or {},
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
                }
        except Exception as exc:
            logger.warning("Schema migration for executed_order_context skipped: %s", exc)

        try:
            conn.execute("UPDATE executed_order_context SET transaction_type = 'BUY' WHERE UPPER(transaction_type) = 'B';")
            conn.execute("UPDATE executed_order_context SET transaction_type = 'SELL' WHERE UPPER(transaction_type) = 'S';")
        except Exception:
            pass

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
                pos_json = json.dumps(pos_list) if pos_list else None
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
                        total_charges, brokerage, stt, gst
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    ),
                )
        conn.commit()
