"""
BloFin Execution Engine
CCXT-based execution for BTCUSDT perpetual futures on BloFin
Handles order placement, position sizing, SL/TP, leverage control
"""

import logging
import os
import asyncio
from typing import Optional

import ccxt.async_support as ccxt

logger = logging.getLogger("execution_engine")


class BloFinExecutionEngine:
    """
    Production execution engine for BloFin perpetual futures.

    Responsibilities:
    - Connect to BloFin via CCXT
    - Set leverage & margin mode on startup
    - Execute market entries with SL/TP
    - Track open positions across strategies
    - Provide account balance and margin info
    """

    SYMBOL        = "BTC/USDT:USDT"   # BloFin perp symbol
    SYMBOL_CLEAN  = "BTCUSDT"
    DEFAULT_LEV   = int(os.getenv("DEFAULT_LEVERAGE", 7))
    MARGIN_MODE   = "cross"            # or "isolated"
    FEES_PCT      = 0.0005             # 0.05% maker/taker
    SLIPPAGE_PCT  = 0.0005             # 0.05% assumed

    def __init__(self):
        self.exchange = ccxt.blofin({
            "apiKey":    os.getenv("BLOFIN_API_KEY", ""),
            "secret":    os.getenv("BLOFIN_SECRET",  ""),
            "password":  os.getenv("BLOFIN_PASSPHRASE", ""),  # BloFin requires passphrase
            "options":   {
                "defaultType": "swap",   # perpetual futures
            },
            "enableRateLimit": True,
        })
        self._open_positions: dict[str, dict] = {}
        self._initialized = False

    # ─────────────────────────────────
    # INIT / TEARDOWN
    # ─────────────────────────────────
    async def initialize(self):
        """Call once on startup to set leverage and confirm connection."""
        if self._initialized:
            return
        try:
            await self.exchange.load_markets()
            await self._set_leverage(self.DEFAULT_LEV)
            self._initialized = True
            logger.info("BloFin engine initialized", extra={
                "symbol":   self.SYMBOL,
                "leverage": self.DEFAULT_LEV,
                "margin":   self.MARGIN_MODE,
            })
        except Exception as e:
            logger.error("BloFin initialization failed", extra={"error": str(e)})
            raise

    async def close(self):
        await self.exchange.close()

    # ─────────────────────────────────
    # CORE EXECUTION
    # ─────────────────────────────────
    async def execute_signal(
        self,
        signal,                      # TradingSignal pydantic model
        risk_allocation_pct: float,  # fraction of total 1% risk
        reason: str = "signal",
    ) -> dict:
        """
        Main entry point for executing a trading signal.
        1. Fetch account balance
        2. Compute position size
        3. Place market order
        4. Place SL/TP orders
        5. Track position
        """
        await self._ensure_initialized()

        try:
            # Account info
            balance     = await self._get_usdt_balance()
            ticker      = await self.exchange.fetch_ticker(self.SYMBOL)
            market_price = float(ticker["last"])

            # Use signal price but validate against market
            entry_price = signal.price
            price_drift = abs(entry_price - market_price) / market_price
            if price_drift > 0.005:   # > 0.5% drift
                logger.warning("Price drift detected, using market price", extra={
                    "signal_price": entry_price,
                    "market_price": market_price,
                    "drift_pct":    round(price_drift * 100, 3),
                })
                entry_price = market_price

            # Position sizing
            from risk_manager import RiskManager
            rm = RiskManager()
            sizing = rm.compute_position_size(
                account_balance=balance,
                entry_price=entry_price,
                stop_loss_price=signal.stop_loss,
                risk_allocation=risk_allocation_pct,
                leverage=self.DEFAULT_LEV,
            )

            if sizing.get("qty", 0) <= 0:
                return {"success": False, "error": sizing.get("error", "qty=0")}

            qty = sizing["qty"]

            # Margin check
            required_margin = (qty * entry_price) / self.DEFAULT_LEV
            available_margin = await self._get_available_margin()
            if available_margin < required_margin * 1.1:
                logger.warning("MARGIN INSUFFICIENT", extra={
                    "required":  required_margin,
                    "available": available_margin,
                })
                return {"success": False, "error": "insufficient_margin"}

            # Market entry
            order = await self.exchange.create_order(
                symbol=self.SYMBOL,
                type="market",
                side=signal.action,        # "buy" | "sell"
                amount=qty,
                params={
                    "tdMode":    "cross",
                    "posSide":   "long" if signal.action == "buy" else "short",
                }
            )
            order_id = order["id"]

            logger.info("Market order placed", extra={
                "order_id":  order_id,
                "strategy":  signal.strategy,
                "qty":       qty,
                "side":      signal.action,
                "reason":    reason,
            })

            # SL/TP placement (algo orders)
            sl_order = await self._place_stop_loss(
                side=signal.action,
                qty=qty,
                stop_price=signal.stop_loss,
            )
            tp_order = await self._place_take_profit(
                side=signal.action,
                qty=qty,
                limit_price=signal.take_profit,
            )

            # Track position
            self._open_positions[signal.strategy] = {
                "order_id":       order_id,
                "strategy":       signal.strategy,
                "symbol":         self.SYMBOL,
                "side":           signal.action,
                "qty":            qty,
                "entry_price":    entry_price,
                "stop_loss":      signal.stop_loss,
                "take_profit":    signal.take_profit,
                "sl_order_id":    sl_order.get("id"),
                "tp_order_id":    tp_order.get("id"),
                "dollar_risk":    sizing["dollar_risk"],
                "position_value": sizing["position_value_usd"],
                "reason":         reason,
                "timestamp":      asyncio.get_event_loop().time(),
            }

            return {
                "success":    True,
                "order_id":   order_id,
                "qty":        qty,
                "entry_price": entry_price,
                "sl":         signal.stop_loss,
                "tp":         signal.take_profit,
                "dollar_risk": sizing["dollar_risk"],
            }

        except ccxt.InsufficientFunds as e:
            logger.error("Insufficient funds", extra={"error": str(e)})
            return {"success": False, "error": "insufficient_funds"}
        except ccxt.NetworkError as e:
            logger.error("Network error", extra={"error": str(e)})
            return {"success": False, "error": f"network_error: {e}"}
        except ccxt.ExchangeError as e:
            logger.error("Exchange error", extra={"error": str(e)})
            return {"success": False, "error": f"exchange_error: {e}"}
        except Exception as e:
            logger.exception("Unexpected execution error")
            return {"success": False, "error": str(e)}

    # ─────────────────────────────────
    # STOP LOSS
    # ─────────────────────────────────
    async def _place_stop_loss(self, side: str, qty: float, stop_price: float) -> dict:
        try:
            close_side = "sell" if side == "buy" else "buy"
            order = await self.exchange.create_order(
                symbol=self.SYMBOL,
                type="stop_market",
                side=close_side,
                amount=qty,
                params={
                    "stopPrice":  stop_price,
                    "reduceOnly": True,
                    "posSide":    "long" if side == "buy" else "short",
                }
            )
            logger.info("SL placed", extra={"sl_price": stop_price, "qty": qty})
            return order
        except Exception as e:
            logger.error("SL placement failed", extra={"error": str(e)})
            return {"error": str(e)}

    # ─────────────────────────────────
    # TAKE PROFIT
    # ─────────────────────────────────
    async def _place_take_profit(self, side: str, qty: float, limit_price: float) -> dict:
        try:
            close_side = "sell" if side == "buy" else "buy"
            order = await self.exchange.create_order(
                symbol=self.SYMBOL,
                type="take_profit_market",
                side=close_side,
                amount=qty,
                params={
                    "stopPrice":  limit_price,
                    "reduceOnly": True,
                    "posSide":    "long" if side == "buy" else "short",
                }
            )
            logger.info("TP placed", extra={"tp_price": limit_price, "qty": qty})
            return order
        except Exception as e:
            logger.error("TP placement failed", extra={"error": str(e)})
            return {"error": str(e)}

    # ─────────────────────────────────
    # LEVERAGE
    # ─────────────────────────────────
    async def _set_leverage(self, leverage: int):
        try:
            await self.exchange.set_leverage(leverage, self.SYMBOL)
            logger.info("Leverage set", extra={"leverage": leverage})
        except Exception as e:
            logger.warning("Leverage set failed (may already be set)", extra={"error": str(e)})

    # ─────────────────────────────────
    # ACCOUNT INFO
    # ─────────────────────────────────
    async def _get_usdt_balance(self) -> float:
        balance = await self.exchange.fetch_balance({"type": "swap"})
        return float(balance["USDT"]["total"] or 0)

    async def _get_available_margin(self) -> float:
        balance = await self.exchange.fetch_balance({"type": "swap"})
        return float(balance["USDT"]["free"] or 0)

    async def get_account_summary(self) -> dict:
        try:
            await self._ensure_initialized()
            balance = await self.exchange.fetch_balance({"type": "swap"})
            return {
                "total_usdt": float(balance["USDT"]["total"] or 0),
                "free_usdt":  float(balance["USDT"]["free"] or 0),
                "used_usdt":  float(balance["USDT"]["used"] or 0),
            }
        except Exception as e:
            return {"error": str(e)}

    async def get_open_positions(self) -> list[dict]:
        try:
            await self._ensure_initialized()
            positions = await self.exchange.fetch_positions([self.SYMBOL])
            active    = [p for p in positions if float(p.get("contracts", 0)) != 0]
            return active
        except Exception as e:
            logger.error("Fetch positions failed", extra={"error": str(e)})
            return []

    async def close_all_positions(self) -> dict:
        """Emergency close all open positions."""
        try:
            await self._ensure_initialized()
            positions = await self.get_open_positions()
            results   = []
            for pos in positions:
                side_to_close = "sell" if pos["side"] == "long" else "buy"
                qty           = abs(float(pos["contracts"]))
                order = await self.exchange.create_order(
                    symbol=self.SYMBOL,
                    type="market",
                    side=side_to_close,
                    amount=qty,
                    params={"reduceOnly": True, "posSide": pos["side"]},
                )
                results.append({"closed": pos["side"], "qty": qty, "order_id": order["id"]})
            self._open_positions.clear()
            return {"closed": len(results), "orders": results}
        except Exception as e:
            logger.exception("Emergency close failed")
            return {"error": str(e)}

    async def _ensure_initialized(self):
        if not self._initialized:
            await self.initialize()
