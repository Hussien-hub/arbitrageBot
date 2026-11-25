# arbitrageBot

Triangular arbitrage bot for Binance Spot. It trades the ZEC triangle in both directions:

- **A:** USDT → ZEC → BTC → USDT
- **B:** USDT → BTC → ZEC → USDT

Order books are held in memory from a WebSocket stream, and both routes are checked on every update.
Orders go through Binance's WebSocket API. It runs in **paper mode** (no orders) by default.
A cycle trades only when its profit after fees is at least `MIN_NET_RETURN` (0.3%) and
`MIN_NET_PROFIT_USDT` (3 USDT).

The bot lives in [`zec_triangular_arbitrage/`](zec_triangular_arbitrage/). Its
[README](zec_triangular_arbitrage/README.md) covers how it works, server deployment, test cycles
with real orders, running as a service, and known limits.

## Quick start

```bash
git clone https://github.com/Hussien-hub/arbitrageBot.git
cd arbitrageBot/zec_triangular_arbitrage
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m tests.test_zecarb      # every line should start with "ok"
cp .env.example .env && chmod 600 .env     # add API keys; keep MODE=PAPER
.venv/bin/python main.py                   # paper mode: logs the best return per route every 10s
```

Before trading for real, follow "Test cycles" and "Run as a service" in the bot's README.
`zec_triangular_arbitrage.zip` is the original scaffold, kept for reference only.
