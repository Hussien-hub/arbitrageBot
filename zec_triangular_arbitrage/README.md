# ZEC triangular arbitrage bot (Binance Spot)

Watches both triangles and, in LIVE mode, trades them:

- A: USDT -> ZEC -> BTC -> USDT
- B: USDT -> BTC -> ZEC -> USDT

## How it works

- **Order books in memory.** One combined WebSocket stream (`<symbol>@depth20@100ms`) keeps the top
  20 levels of ZECUSDT, ZECBTC, BTCUSDT (and BNBUSDT, to value BNB commission) in memory. Every update
  triggers an evaluation of both routes; there is no polling.
- **Exact sizing.** Each leg is simulated exactly as it will be sent: walked through the book, rounded
  to tick/step, checked against minQty/minNotional, with an IOC limit collar of `MAX_SLIPPAGE` beyond
  the worst level reached. Amounts the next leg can't use are valued at the best bid.
- **Trading rule.** A cycle trades only when net profit (after fees, slippage and rounding) is
  ≥ `MIN_NET_RETURN` **and** ≥ `MIN_NET_PROFIT_USDT`.
- **Orders over the WebSocket API.** In LIVE mode, orders go through one persistent, signed
  WebSocket API connection (`order.place`), so there is no HTTP request setup per order. REST is
  only used at startup and for reconciliation.
- **Execution.** Three sequential IOC legs, each sized from what the previous leg actually filled
  (net of commission). On a partial fill or a rejection it stops, and any ZEC/BTC left is sold to
  USDT with a MARKET order.
- **Lost responses.** If an order gets no response, the bot waits until `recvWindow` has expired
  (after that, the exchange can no longer accept it), then looks the order up by client order ID.
  It never re-sends.
- **Kill switch.** `state.json` holds the day's realized P&L and a count of consecutive failures.
  Hitting `DAILY_LOSS_LIMIT_USDT` or `MAX_CONSECUTIVE_FAILURES`, an unexpected error, a 418/429 rate
  limit, or an unwind that can't get flat stops the bot. The stop persists across restarts until
  you check the account and delete `state.json`.

## Deploy on a Linux server

Requires Python 3.9+. The server must be in a country Binance serves. AWS Tokyo
(ap-northeast-1) is generally reported to be closest to Binance's matching engine.

**1. Get the code and install**

If the repo is private, first give the server read access. Either add the server's SSH key under the
repo's Settings → Deploy keys and clone with the `git@github.com:` URL, or use a personal access token.

```bash
git clone https://github.com/Hussien-hub/arbitrageBot.git
cd arbitrageBot/zec_triangular_arbitrage
sudo apt install -y python3-venv          # Debian/Ubuntu, only if venv is missing
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m tests.test_zecarb     # every line should start with "ok"
```

**2. Add your keys (`.env` stays in PAPER mode)**

Create the API key with **Spot trading only**, withdrawals disabled and an IP restriction to the
server's IP.

```bash
cp .env.example .env && chmod 600 .env
nano .env        # fill BINANCE_API_KEY and BINANCE_API_SECRET; leave MODE=PAPER
```

**3. Paper check**

This confirms the server can reach Binance. Wait for a `Best in window` line, then press Ctrl+C.

```bash
.venv/bin/python main.py
```

**4. Test cycles (REAL orders)**

Each command sends one real cycle of 20 USDT regardless of profit, then exits. It costs about
0.06 USDT. You need ≥ 20 USDT free, some BNB for fees, and ideally no other ZEC/BTC in the
account, so the results are easy to read. LIVE is switched on for that one command only; `.env`
stays PAPER.

```bash
MODE=LIVE LIVE_TRADING_ENABLED=true .venv/bin/python main.py --test-cycle 20 --route A 2>&1 | tee test-A.log
MODE=LIVE LIVE_TRADING_ENABLED=true .venv/bin/python main.py --test-cycle 20 --route B 2>&1 | tee test-B.log
```

Match the logged `clientOrderId`s, quantities and BNB commission against your Binance trade
history. If a run trips the kill switch, check the account, then `rm state.json`.

**5. Run as a service (after the test cycles look right)**

Run this from inside `zec_triangular_arbitrage`. It runs whatever mode `.env` says. Switch `.env`
to `MODE=LIVE` and `LIVE_TRADING_ENABLED=true`, with a small `MAX_CYCLE_USDT`, only when you
mean to trade.

```bash
sudo tee /etc/systemd/system/zecarb.service >/dev/null <<EOF
[Unit]
Description=ZEC triangular arbitrage bot
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
User=$USER
WorkingDirectory=$PWD
ExecStart=$PWD/.venv/bin/python -u main.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now zecarb
journalctl -u zecarb -f                   # live logs; stop with: sudo systemctl stop zecarb
```

A tripped kill switch exits cleanly, so systemd does not restart it.

**Checking results**

Every 10s the log records each route's best net return in that window. `gap_report.py` summarizes
them: best and median per route, how many windows came within reach of the +0.3% threshold, and the
10 best moments.

```bash
journalctl -u zecarb -o cat | .venv/bin/python gap_report.py                          # whole run
journalctl -u zecarb -o cat --since "24 hours ago" | .venv/bin/python gap_report.py   # last day
journalctl -u zecarb -o cat | grep -E "SIGNAL|CYCLE|KILL"                             # trades and stops
```

**Updating later**

```bash
cd arbitrageBot/zec_triangular_arbitrage
git pull && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m tests.test_zecarb
sudo systemctl restart zecarb             # if running as a service
```

`.env` and `state.json` are git-ignored, so a pull never touches your keys or kill-switch state.

## Testnet (optional)

Testnet is a free rehearsal of step 4. Set the testnet URLs from `.env.example`, a testnet key
from testnet.binance.vision, and `STATE_FILE=state.testnet.json` (so testnet P&L doesn't count
toward mainnet limits). Testnet can't verify your real fee rates or BNB commission; only mainnet
can.

## Known limits

- Only the top 20 levels are in memory. Plenty for ~$1000 cycles. For much larger cycles, switch to
  diff-depth plus snapshot sync.
- Dust below exchange minimums (and route B's collar leftover in BTC) stays in the account. Sweep it
  occasionally ("Convert small balance to BNB").
- If BNB runs out mid-session, commission switches to the received asset. Fills are still accounted
  exactly, but the scan's fee estimate becomes optimistic.
- The three legs are not atomic. Profit is not guaranteed even when a signal is real.
