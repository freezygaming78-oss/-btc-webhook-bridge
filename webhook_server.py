"""
BTC Multi-Strategy Webhook Server — Render Edition
FastAPI server optimised for Render free/paid web services
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

# ─────────────────────────────────────────────────────────
# STRUCTURED JSON LOGGING
# ─────────────────────────────────────────────────────────
class JSONFormatter(logging.Formatter):
    def format(self, record):
        obj = {
            "ts":     datetime.utcnow().isoformat() + "Z",
            "level":  record.levelname,
            "logger": record.name,
            "msg":    record.getMessage(),
        }
        if hasattr(record, "extra"):
            obj.update(record.extra)
        return json.dumps(obj)

_h = logging.StreamHandler()
_h.setFormatter(JSONFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_h])
logger = logging.getLogger("webhook")

# ─────────────────────────────────────────────────────────
# SIGNAL MODEL
# ─────────────────────────────────────────────────────────
VALID_STRATEGIES  = {"breakout", "divergence", "mean_reversion"}
STRATEGY_PRIORITY = {"breakout": 3, "divergence": 2, "mean_reversion": 1}
STRATEGY_ALLOC    = {"breakout": 0.40, "divergence": 0.40, "mean_reversion": 0.20}

class TradingSignal(BaseModel):
    strategy:    str
    action:      str
    symbol:      str
    price:       float
    stop_loss:   float
    take_profit: float
    timeframe:   str
    rsi:         Optional[float] = None
    adx:         Optional[float] = None
    allocation:  Optional[float] = None

    @field_validator("strategy")
    @classmethod
    def strat_valid(cls, v):
        if v not in VALID_STRATEGIES:
            raise ValueError(f"strategy must be one of {VALID_STRATEGIES}")
        return v

    @field_validator("action")
    @classmethod
    def action_valid(cls, v):
        if v not in {"buy", "sell"}:
            raise ValueError("action must be 'buy' or 'sell'")
        return v

# ─────────────────────────────────────────────────────────
# DEDUP CACHE
# ─────────────────────────────────────────────────────────
class DeduplicationCache:
    def __init__(self, ttl: int = 300):
        self.cache: dict[str, float] = {}
        self.ttl = ttl

    def _key(self, s: TradingSignal) -> str:
        raw = f"{s.strategy}:{s.symbol}:{s.action}:{s.timeframe}:{round(s.price, -1)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def is_duplicate(self, s: TradingSignal) -> bool:
        self._evict()
        return self._key(s) in self.cache

    def mark(self, s: TradingSignal):
        self.cache[self._key(s)] = time.time()

    def _evict(self):
        now = time.time()
        for k in [k for k, t in self.cache.items() if now - t > self.ttl]:
            del self.cache[k]

# ─────────────────────────────────────────────────────────
# SIGNAL RESOLVER
# ─────────────────────────────────────────────────────────
class SignalResolver:
    def __init__(self, window: int = 60):
        self.pending: dict[str, TradingSignal] = {}
        self.opened_at: Optional[float] = None
        self.window = window

    def submit(self, s: TradingSignal):
        self.pending[s.strategy] = s
        if self.opened_at is None:
            self.opened_at = time.time()

    def expired(self) -> bool:
        return self.opened_at is not None and time.time() - self.opened_at >= self.window

    def resolve(self) -> list[dict]:
        if not self.pending:
            return []
        signals = sorted(self.pending.values(), key=lambda s: STRATEGY_PRIORITY[s.strategy], reverse=True)
        aligned = len(signals) > 1
        scale   = min(1.0 / len(signals) + 0.15, 1.0) if aligned else 1.0
        primary = signals[0].strategy
        result  = [{"signal": s, "scale": scale, "aligned": aligned, "primary": s.strategy == primary} for s in signals]
        self.pending   = {}
        self.opened_at = None
        return result

# ─────────────────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────────────────
app = FastAPI(title="BTC Multi-Strategy Webhook", version="1.0.0")

risk_mgr = None
exec_eng = None
perf_log = None
dedup    = DeduplicationCache(ttl=300)
resolver = SignalResolver(window=60)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

@app.on_event("startup")
async def startup():
    global risk_mgr, exec_eng, perf_log
    from risk_manager import RiskManager
    from execution_engine import BloFinExecutionEngine
    from performance_logger import PerformanceLogger
    os.makedirs("logs", exist_ok=True)
    risk_mgr = RiskManager(max_total_risk_pct=float(os.getenv("MAX_TOTAL_RISK_PCT", "1.0")))
    exec_eng = BloFinExecutionEngine()
    perf_log = PerformanceLogger(db_path="logs/performance.db")
    logger.info("Server started", extra={"port": os.getenv("PORT", "8080")})
    asyncio.create_task(_flush_loop())

# ─────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "ok", "service": "BTC Multi-Strategy Webhook Bridge"}

@app.get("/health")
async def health():
    acct = {}
    try:
        acct = await exec_eng.get_account_summary()
    except Exception:
        pass
    return {
        "status":    "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "open_risk": risk_mgr.current_risk_pct() if risk_mgr else 0,
        "account":   acct,
    }

@app.post("/webhook")
async def webhook(
    request: Request,
    x_webhook_secret: Optional[str] = Header(None)
):
    if WEBHOOK_SECRET and x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        body   = await request.json()
        signal = TradingSignal(**body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    logger.info("Signal received", extra={
        "strategy": signal.strategy, "action": signal.action,
        "price": signal.price, "tf": signal.timeframe,
    })

    if dedup.is_duplicate(signal):
        return JSONResponse({"status": "rejected", "reason": "duplicate"})

    dedup.mark(signal)
    resolver.submit(signal)

    if STRATEGY_PRIORITY[signal.strategy] == 3 or resolver.expired():
        instructions = resolver.resolve()
        results = [await _execute(i) for i in instructions]
        return JSONResponse({"status": "processed", "executions": results})

    return JSONResponse({"status": "queued", "window_open": True})

@app.get("/positions")
async def positions():
    return await exec_eng.get_open_positions()

@app.get("/performance")
async def performance():
    return perf_log.get_summary()

@app.post("/close-all")
async def close_all():
    result = await exec_eng.close_all_positions()
    risk_mgr.reset()
    logger.warning("EMERGENCY CLOSE ALL", extra={"result": result})
    return {"status": "closed", **result}

@app.get("/risk")
async def risk_status():
    return {
        "current_risk_pct": risk_mgr.current_risk_pct(),
        "max_risk_pct":     float(os.getenv("MAX_TOTAL_RISK_PCT", "1.0")),
        "slots":            risk_mgr.get_slots(),
    }

# ─────────────────────────────────────────────────────────
# EXECUTION
# ─────────────────────────────────────────────────────────
async def _execute(instr: dict) -> dict:
    sig   = instr["signal"]
    scale = instr["scale"]
    alloc = STRATEGY_ALLOC[sig.strategy] * scale

    check = risk_mgr.check_and_reserve(
        strategy=sig.strategy,
        allocation_pct=alloc,
        stop_distance_pct=abs(sig.price - sig.stop_loss) / sig.price,
    )

    if not check["approved"]:
        logger.warning("RISK BLOCKED", extra={"strategy": sig.strategy, "reason": check["reason"]})
        return {"strategy": sig.strategy, "status": "RISK_BLOCKED", "reason": check["reason"]}

    try:
        result = await exec_eng.execute_signal(
            signal=sig,
            risk_allocation_pct=alloc,
            reason="aligned" if instr["aligned"] else "single",
        )
        if result["success"]:
            perf_log.record_entry(
                strategy=sig.strategy,
                entry_price=sig.price,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                position_size=result["qty"],
                trade_id=result["order_id"],
            )
        else:
            risk_mgr.release(sig.strategy)
        return {"strategy": sig.strategy, **result}
    except Exception as e:
        risk_mgr.release(sig.strategy)
        logger.exception("Execution error")
        return {"strategy": sig.strategy, "status": "error", "error": str(e)}

async def _flush_loop():
    while True:
        await asyncio.sleep(30)
        if resolver.expired() and resolver.pending:
            for i in resolver.resolve():
                await _execute(i)

# ─────────────────────────────────────────────────────────
# ENTRYPOINT — Render injects $PORT automatically
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("webhook_server:app", host="0.0.0.0", port=port, workers=1)
