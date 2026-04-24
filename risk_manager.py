"""
Global Risk Manager
Centralized risk tracking and enforcement for multi-strategy portfolio
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger("risk_manager")


@dataclass
class StrategyRiskSlot:
    strategy:         str
    allocated_pct:    float     # % of account equity at risk
    stop_distance_pct: float
    reserved_at:      datetime = field(default_factory=datetime.utcnow)
    is_active:        bool     = True


class RiskManager:
    """
    Thread-safe global risk manager.

    Rules enforced:
    - Total open risk ≤ max_total_risk_pct (default 1%)
    - No single strategy can exceed its max allocation
    - Tracks active slots per strategy
    - Margin safety buffer checks
    """

    STRATEGY_MAX_ALLOC = {
        "breakout":      0.40,   # 40% of total risk budget
        "divergence":    0.40,
        "mean_reversion": 0.20,
    }

    def __init__(self, max_total_risk_pct: float = 1.0):
        self.max_total_risk_pct = max_total_risk_pct
        self._slots: dict[str, StrategyRiskSlot] = {}
        self._lock  = threading.Lock()

    # ─────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────
    def check_and_reserve(
        self,
        strategy: str,
        allocation_pct: float,
        stop_distance_pct: float,
        account_balance: Optional[float] = None,
        available_margin: Optional[float] = None,
    ) -> dict:
        """
        Checks risk rules and, if approved, reserves a slot.
        Returns: {"approved": bool, "reason": str, "actual_allocation": float}
        """
        with self._lock:
            # Already have open position for this strategy
            if strategy in self._slots and self._slots[strategy].is_active:
                return {
                    "approved": False,
                    "reason":   f"Strategy {strategy} already has an active position",
                    "actual_allocation": 0.0,
                }

            current_used = self._current_risk_pct_locked()
            remaining    = self.max_total_risk_pct - current_used
            max_for_strat = self.max_total_risk_pct * self.STRATEGY_MAX_ALLOC.get(strategy, 0.20)

            # Scale down if needed
            effective_alloc = min(allocation_pct, remaining, max_for_strat)

            if effective_alloc <= 0.001:
                reason = (
                    f"Risk budget exhausted: used={current_used:.3f}% "
                    f"max={self.max_total_risk_pct:.3f}%"
                )
                logger.warning("RISK BLOCKED", extra={
                    "strategy":      strategy,
                    "requested_pct": allocation_pct,
                    "used_pct":      current_used,
                    "reason":        reason,
                })
                return {"approved": False, "reason": reason, "actual_allocation": 0.0}

            # Margin check (optional, when account info available)
            if account_balance is not None and available_margin is not None:
                required_margin = account_balance * (effective_alloc / 100)
                if available_margin < required_margin * 1.2:   # 20% buffer
                    reason = (
                        f"Insufficient margin: required={required_margin:.2f} "
                        f"available={available_margin:.2f}"
                    )
                    logger.warning("RISK BLOCKED — MARGIN", extra={
                        "strategy":        strategy,
                        "required_margin": required_margin,
                        "available_margin": available_margin,
                    })
                    return {"approved": False, "reason": reason, "actual_allocation": 0.0}

            # Was scaled down?
            if effective_alloc < allocation_pct * 0.95:
                logger.warning("Allocation scaled down due to risk budget", extra={
                    "strategy":          strategy,
                    "requested":         allocation_pct,
                    "approved":          effective_alloc,
                })

            # Reserve slot
            self._slots[strategy] = StrategyRiskSlot(
                strategy=strategy,
                allocated_pct=effective_alloc,
                stop_distance_pct=stop_distance_pct,
            )

            logger.info("Risk slot reserved", extra={
                "strategy":       strategy,
                "allocation_pct": effective_alloc,
                "total_used_pct": current_used + effective_alloc,
            })

            return {
                "approved":          True,
                "reason":            "approved",
                "actual_allocation": effective_alloc,
                "scaled":            effective_alloc < allocation_pct,
            }

    def release(self, strategy: str):
        """Release risk slot when position is closed."""
        with self._lock:
            if strategy in self._slots:
                freed = self._slots[strategy].allocated_pct
                del self._slots[strategy]
                logger.info("Risk slot released", extra={
                    "strategy":      strategy,
                    "freed_pct":     freed,
                    "remaining_pct": self._current_risk_pct_locked(),
                })

    def update_slot(self, strategy: str, new_allocation: float):
        """Update allocation (e.g. after partial close)."""
        with self._lock:
            if strategy in self._slots:
                self._slots[strategy].allocated_pct = new_allocation

    def current_risk_pct(self) -> float:
        with self._lock:
            return self._current_risk_pct_locked()

    def get_slots(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "strategy":         s.strategy,
                    "allocated_pct":    s.allocated_pct,
                    "stop_distance_pct": s.stop_distance_pct,
                    "reserved_at":      s.reserved_at.isoformat(),
                }
                for s in self._slots.values()
            ]

    def reset(self):
        """Emergency reset — clears all slots."""
        with self._lock:
            self._slots.clear()
            logger.warning("RiskManager RESET — all slots cleared")

    def is_strategy_active(self, strategy: str) -> bool:
        with self._lock:
            return strategy in self._slots

    # ─────────────────────────────────
    # PRIVATE
    # ─────────────────────────────────
    def _current_risk_pct_locked(self) -> float:
        return sum(s.allocated_pct for s in self._slots.values())

    def compute_position_size(
        self,
        account_balance: float,
        entry_price:     float,
        stop_loss_price: float,
        risk_allocation: float,     # fraction of total risk (e.g. 0.40 for 40%)
        leverage:        int = 7,
    ) -> dict:
        """
        Compute contract quantity given risk parameters.

        Formula:
          dollar_risk    = account_balance * (total_risk_pct / 100) * risk_allocation
          stop_distance  = |entry - stop| / entry
          position_value = dollar_risk / stop_distance
          qty            = position_value / entry_price * leverage  (not exceeding available margin)
        """
        total_risk_dollars = account_balance * (self.max_total_risk_pct / 100) * risk_allocation
        stop_distance_pct  = abs(entry_price - stop_loss_price) / entry_price

        if stop_distance_pct < 0.001:
            return {"qty": 0, "error": "Stop too tight (< 0.1%)"}

        position_value_usd = total_risk_dollars / stop_distance_pct
        qty_coins          = position_value_usd / entry_price

        return {
            "qty":                  round(qty_coins, 4),
            "dollar_risk":          round(total_risk_dollars, 2),
            "position_value_usd":   round(position_value_usd, 2),
            "stop_distance_pct":    round(stop_distance_pct * 100, 3),
            "leverage_used":        leverage,
        }
