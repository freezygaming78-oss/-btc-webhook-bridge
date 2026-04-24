"""
Performance Logger
Tracks win/loss, profit factor, drawdown per strategy + portfolio equity curve
Uses SQLite for persistent storage
"""

import logging
import sqlite3
import json
from datetime import datetime
from typing import Optional

logger = logging.getLogger("performance_logger")


CREATE_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        TEXT UNIQUE,
    strategy        TEXT NOT NULL,
    entry_price     REAL,
    stop_loss       REAL,
    take_profit     REAL,
    exit_price      REAL,
    position_size   REAL,
    pnl             REAL,
    pnl_pct         REAL,
    outcome         TEXT,   -- 'win' | 'loss' | 'open'
    entry_time      TEXT,
    exit_time       TEXT,
    fees_paid       REAL,
    notes           TEXT
);
"""

CREATE_EQUITY_TABLE = """
CREATE TABLE IF NOT EXISTS equity_curve (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT,
    equity      REAL,
    drawdown    REAL,
    peak_equity REAL
);
"""


class PerformanceLogger:
    FEES_PCT = 0.0005  # 0.05% per side

    def __init__(self, db_path: str = "logs/performance.db"):
        self.db_path = db_path
        self._init_db()
        self._peak_equity: Optional[float] = None

    def _init_db(self):
        with self._conn() as conn:
            conn.execute(CREATE_TRADES_TABLE)
            conn.execute(CREATE_EQUITY_TABLE)

    def _conn(self):
        return sqlite3.connect(self.db_path)

    # ─────────────────────────────────
    # TRADE LIFECYCLE
    # ─────────────────────────────────
    def record_entry(
        self,
        strategy:      str,
        entry_price:   float,
        stop_loss:     float,
        take_profit:   float,
        position_size: float,
        trade_id:      str,
        notes:         str = "",
    ):
        fees = entry_price * position_size * self.FEES_PCT
        with self._conn() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO trades
                  (trade_id, strategy, entry_price, stop_loss, take_profit,
                   position_size, outcome, entry_time, fees_paid, notes)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
            """, (trade_id, strategy, entry_price, stop_loss, take_profit,
                  position_size, datetime.utcnow().isoformat(), fees, notes))
        logger.info("Trade entry recorded", extra={"trade_id": trade_id, "strategy": strategy})

    def record_exit(
        self,
        trade_id:   str,
        exit_price: float,
        reason:     str = "",   # "tp" | "sl" | "manual"
    ):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT entry_price, position_size, strategy FROM trades WHERE trade_id=?",
                (trade_id,)
            ).fetchone()

            if not row:
                logger.warning("Trade not found for exit", extra={"trade_id": trade_id})
                return

            entry_price, position_size, strategy = row
            fees        = exit_price * position_size * self.FEES_PCT
            pnl         = (exit_price - entry_price) * position_size - fees
            pnl_pct     = ((exit_price - entry_price) / entry_price) * 100
            outcome     = "win" if pnl > 0 else "loss"

            conn.execute("""
                UPDATE trades SET
                    exit_price=?, pnl=?, pnl_pct=?, outcome=?,
                    exit_time=?, fees_paid=fees_paid+?, notes=notes||?
                WHERE trade_id=?
            """, (exit_price, pnl, pnl_pct, outcome,
                  datetime.utcnow().isoformat(), fees,
                  f" | exit:{reason}", trade_id))

        logger.info("Trade exit recorded", extra={
            "trade_id": trade_id,
            "outcome":  outcome,
            "pnl":      round(pnl, 2),
        })

    # ─────────────────────────────────
    # EQUITY CURVE
    # ─────────────────────────────────
    def snapshot_equity(self, current_equity: float):
        if self._peak_equity is None or current_equity > self._peak_equity:
            self._peak_equity = current_equity
        drawdown = (self._peak_equity - current_equity) / self._peak_equity * 100

        with self._conn() as conn:
            conn.execute("""
                INSERT INTO equity_curve (timestamp, equity, drawdown, peak_equity)
                VALUES (?, ?, ?, ?)
            """, (datetime.utcnow().isoformat(), current_equity, drawdown, self._peak_equity))

    # ─────────────────────────────────
    # ANALYTICS
    # ─────────────────────────────────
    def get_summary(self) -> dict:
        with self._conn() as conn:
            total  = self._strategy_stats(conn, None)
            by_strat = {}
            for strat in ["breakout", "divergence", "mean_reversion"]:
                by_strat[strat] = self._strategy_stats(conn, strat)

            equity_rows = conn.execute(
                "SELECT equity, drawdown FROM equity_curve ORDER BY id DESC LIMIT 100"
            ).fetchall()

        return {
            "portfolio":    total,
            "strategies":   by_strat,
            "equity_curve": [{"equity": r[0], "drawdown": r[1]} for r in reversed(equity_rows)],
        }

    def _strategy_stats(self, conn: sqlite3.Connection, strategy: Optional[str]) -> dict:
        where  = "WHERE strategy=? AND outcome != 'open'" if strategy else "WHERE outcome != 'open'"
        params = (strategy,) if strategy else ()

        rows = conn.execute(
            f"SELECT outcome, pnl, pnl_pct FROM trades {where}", params
        ).fetchall()

        if not rows:
            return {"trades": 0, "win_rate": 0, "profit_factor": 0, "avg_pnl": 0, "max_drawdown": 0}

        wins        = [r for r in rows if r[0] == "win"]
        losses      = [r for r in rows if r[0] == "loss"]
        total_win   = sum(r[1] for r in wins)
        total_loss  = abs(sum(r[1] for r in losses)) or 0.0001

        # Max drawdown calculation
        pnls     = [r[1] for r in rows]
        peak     = 0.0
        max_dd   = 0.0
        cumulative = 0.0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = (peak - cumulative) / max(peak, 1) * 100
            max_dd = max(max_dd, dd)

        return {
            "trades":         len(rows),
            "wins":           len(wins),
            "losses":         len(losses),
            "win_rate":       round(len(wins) / len(rows) * 100, 1),
            "profit_factor":  round(total_win / total_loss, 3),
            "avg_pnl":        round(sum(r[1] for r in rows) / len(rows), 2),
            "total_pnl":      round(sum(r[1] for r in rows), 2),
            "max_drawdown":   round(max_dd, 2),
        }

    def get_open_trades(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT trade_id, strategy, entry_price, position_size, entry_time "
                "FROM trades WHERE outcome='open'"
            ).fetchall()
        return [
            {"trade_id": r[0], "strategy": r[1], "entry_price": r[2],
             "position_size": r[3], "entry_time": r[4]}
            for r in rows
        ]
