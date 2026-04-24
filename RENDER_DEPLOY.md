# Render Deployment Guide
## BTC Multi-Strategy Webhook Bridge

No VPS, no SSH, no SSL certificates. Just GitHub + Render.

---

## What Render gives you

- Public HTTPS URL out of the box (TradingView compatible)
- Automatic deploys on every `git push`
- Built-in environment variable management (no `.env` file needed)
- Free tier available for testing (note: free tier sleeps after 15 min inactivity — use Starter $7/mo for production)

---

## Step 1 — Create a GitHub repo

1. Go to https://github.com/new
2. Create a **private** repo called `btc-webhook-bridge`
3. Upload all these files to the root of the repo:
   - `webhook_server.py`
   - `risk_manager.py`
   - `execution_engine.py`
   - `performance_logger.py`
   - `requirements.txt`
   - `render.yaml`
   - `Procfile`
   - `runtime.txt`

The quickest way via command line:
```bash
git init
git add .
git commit -m "initial"
git branch -M main
git remote add origin https://github.com/YOURUSERNAME/btc-webhook-bridge.git
git push -u origin main
```

---

## Step 2 — Deploy to Render

### Option A — Blueprint (one click)
1. Go to https://dashboard.render.com/blueprints
2. Click **New Blueprint Instance**
3. Connect your GitHub repo
4. Render reads `render.yaml` automatically — it creates the service for you
5. You'll be prompted to fill in the secret env vars (API keys, etc.)

### Option B — Manual (takes 3 minutes)
1. Go to https://dashboard.render.com
2. Click **New → Web Service**
3. Connect your GitHub repo
4. Set:
   - **Name**: `btc-webhook-bridge`
   - **Runtime**: Python 3
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `uvicorn webhook_server:app --host 0.0.0.0 --port $PORT --workers 1`
   - **Plan**: Starter ($7/mo) for production, Free for testing

---

## Step 3 — Set environment variables

In Render dashboard → your service → **Environment** tab, add:

| Key | Value | Notes |
|-----|-------|-------|
| `BLOFIN_API_KEY` | your key | From BloFin API settings |
| `BLOFIN_SECRET` | your secret | From BloFin API settings |
| `BLOFIN_PASSPHRASE` | your passphrase | BloFin requires this |
| `WEBHOOK_SECRET` | any random string | e.g. `openssl rand -hex 16` |
| `MAX_TOTAL_RISK_PCT` | `1.0` | Max % equity at risk |
| `DEFAULT_LEVERAGE` | `7` | Futures leverage |

Click **Save Changes** — Render redeploys automatically.

---

## Step 4 — Find your webhook URL

Once deployed, Render gives you a URL like:
```
https://btc-webhook-bridge.onrender.com
```

Your webhook endpoint is:
```
https://btc-webhook-bridge.onrender.com/webhook
```

Test it:
```bash
curl https://btc-webhook-bridge.onrender.com/health
```

You should see JSON with `"status": "ok"`.

---

## Step 5 — Add Pine Script to TradingView

1. Open TradingView on a BTCUSDT Perpetual chart
2. Open Pine Editor → paste `btc_multi_strategy.pine` → Add to chart
3. Use 1H timeframe (recommended)

---

## Step 6 — Create TradingView alerts

Create 3 alerts — one per strategy:

**Right-click chart → Add Alert → set condition to the signal plotshape**

For each alert:
- Trigger: **Once Per Bar Close**
- Notifications → Webhook URL:
  ```
  https://btc-webhook-bridge.onrender.com/webhook
  ```
- Add header: `X-Webhook-Secret: your_secret_here`

**Alert message for Breakout:**
```json
{"strategy":"breakout","action":"buy","symbol":"BTCUSDT","price":"{{close}}","stop_loss":"{{plot("Active SL")}}","take_profit":"{{plot("Active TP")}}","timeframe":"{{interval}}","rsi":"{{plot("RSI")}}"}
```

**Alert message for Divergence:**
```json
{"strategy":"divergence","action":"buy","symbol":"BTCUSDT","price":"{{close}}","stop_loss":"{{plot("Active SL")}}","take_profit":"{{plot("Active TP")}}","timeframe":"{{interval}}"}
```

**Alert message for Mean Reversion:**
```json
{"strategy":"mean_reversion","action":"buy","symbol":"BTCUSDT","price":"{{close}}","stop_loss":"{{plot("Active SL")}}","take_profit":"{{plot("Active TP")}}","timeframe":"{{interval}}"}
```

---

## Step 7 — Test with a fake signal

```bash
curl -X POST https://btc-webhook-bridge.onrender.com/webhook \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your_secret_here" \
  -d '{
    "strategy": "breakout",
    "action": "buy",
    "symbol": "BTCUSDT",
    "price": 65000.0,
    "stop_loss": 63500.0,
    "take_profit": 68000.0,
    "timeframe": "60"
  }'
```

---

## Monitoring

| URL | What it shows |
|-----|---------------|
| `/health` | Server alive, account balance, open risk % |
| `/positions` | Open BloFin positions |
| `/performance` | Win rate, profit factor, drawdown per strategy |
| `/risk` | Current risk slots, remaining budget |

Emergency stop:
```bash
curl -X POST https://btc-webhook-bridge.onrender.com/close-all
```

---

## Free vs Starter tier

| | Free | Starter ($7/mo) |
|--|------|-----------------|
| Always on | No (sleeps after 15min) | Yes |
| TradingView safe | Risky (first alert may timeout) | Yes |
| Recommended for | Testing only | Live trading |

The free tier will miss the first webhook after inactivity (cold start ~30s). For live trading, use Starter.

---

## Auto-deploy

Every `git push` to `main` triggers a redeploy automatically. Zero downtime deploys on Starter.

---

## Logs

View real-time logs in Render dashboard → your service → **Logs** tab.
Each log line is structured JSON — you can filter by `"strategy"`, `"status": "RISK_BLOCKED"`, etc.
