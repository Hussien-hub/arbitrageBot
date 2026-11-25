import argparse, asyncio, logging
from decimal import Decimal
from time import monotonic
from zecarb.config import Config
from zecarb.engine import ArbitrageEngine, SYMBOLS
from zecarb.exchange import BinanceREST, BinanceWSAPI
from zecarb.executor import Executor, State
from zecarb.market import MarketData

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zecarb")
REPORT_EVERY = 10  # seconds between "best route" status lines
TEST_WAIT = 30     # seconds a test cycle waits for fresh books and the order connection


async def main(args):
    cfg = Config()
    cfg.validate()
    if args.test_cycle is not None:
        if not cfg.live:
            raise SystemExit("--test-cycle sends REAL orders: needs MODE=LIVE and LIVE_TRADING_ENABLED=true "
                             "(run it against the testnet URLs first)")
        if not 0 < args.test_cycle <= cfg.max_cycle:
            raise SystemExit(f"--test-cycle amount must be > 0 and <= MAX_CYCLE_USDT ({cfg.max_cycle})")
    capital = args.test_cycle or cfg.max_cycle
    async with BinanceREST(cfg) as rest:
        await rest.load_exchange_info(SYMBOLS)
        fees = {s: cfg.taker_fee for s in SYMBOLS}
        executor, tasks = None, []
        if cfg.live:
            state = State(cfg.state_file)
            if state.killed:
                raise RuntimeError(f"Kill switch active: {state.data.get('reason')}. "
                                   f"Check the account, then delete {cfg.state_file} to resume.")
            await rest.sync_time()
            free = await rest.balances()
            if free.get("USDT", 0) < capital:
                raise RuntimeError(f"Free USDT {free.get('USDT', 0)} is below the {capital} USDT a cycle needs")
            fees = await rest.taker_fees(SYMBOLS, pay_in_bnb=free.get("BNB", 0) > 0)
            ws_api = BinanceWSAPI(cfg, rest)
            tasks.append(asyncio.create_task(ws_api.run()))
            executor = Executor(cfg, rest, ws_api, state)
        log.info("Taker fee rates: %s", {s: str(f) for s, f in fees.items()})

        market = MarketData(cfg, rest.session, SYMBOLS + ("BNBUSDT",))  # BNBUSDT values BNB commission
        tasks.append(asyncio.create_task(market.run()))
        engine = ArbitrageEngine(cfg, rest.filters, fees)
        if args.test_cycle is not None:
            return await test_cycle(args, capital, market, engine, executor)
        log.info("Started %s scanner.", "LIVE" if cfg.live else "PAPER")

        last_report, peaks = float("-inf"), {}
        while not (executor and executor.state.killed):
            await market.changed.wait()
            market.changed.clear()
            opps = engine.evaluate(market.books, capital)
            for o in opps:  # arbitrage gaps last ~100ms; report each route's best in the window, not a sample
                if o.route not in peaks or o.net_return > peaks[o.route].net_return:
                    peaks[o.route] = o
            if monotonic() - last_report >= REPORT_EVERY:
                last_report = monotonic()
                if peaks:
                    log.info("Best in window: %s", " | ".join(
                        f"{r} return={o.net_return * 100:.4f}% profit={o.net_profit:.4f}"
                        for r, o in sorted(peaks.items())))
                else:
                    log.info("No fresh, fully depth-covered route in this window.")
                peaks = {}
            signal = next((o for o in opps if o.net_profit >= cfg.min_profit and o.net_return >= cfg.min_return), None)
            if signal is None:
                continue
            log.warning("SIGNAL %s start=%s end=%s profit=%s return=%.4f%%", signal.route,
                        round(signal.start_usdt, 4), round(signal.end_usdt, 4),
                        round(signal.net_profit, 4), signal.net_return * 100)
            if executor:
                await executor.execute(signal, market.books)
            engine.not_before = monotonic()


async def test_cycle(args, capital, market, engine, executor):
    """One forced cycle of real orders, ignoring profit thresholds, to prove the order path with a small amount."""
    deadline = monotonic() + TEST_WAIT
    while monotonic() < deadline:
        await asyncio.sleep(0.2)
        opps = [o for o in engine.evaluate(market.books, capital) if o.route.startswith(args.route or "")]
        if opps and executor.ws.connected:
            o = opps[0]
            log.warning("TEST CYCLE (real orders) %s with %s USDT; expected profit %s (%.4f%%)",
                        o.route, capital, round(o.net_profit, 4), o.net_return * 100)
            for leg in o.legs:
                log.info("  plan: %s %s qty=%s limit=%s", leg.symbol, leg.side, leg.qty, leg.price)
            await executor.execute(o, market.books, forced=True)
            log.warning("TEST CYCLE finished. Compare the orders above with your Binance trade history.")
            return
    raise SystemExit(f"No fresh, fillable route {args.route or ''} and order connection within {TEST_WAIT}s "
                     f"(is the amount above every leg's exchange minimum?)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="ZEC triangular arbitrage bot (PAPER by default).")
    p.add_argument("--test-cycle", type=Decimal, metavar="USDT",
                   help="LIVE only: run ONE cycle of real orders with this much USDT regardless of profit, then exit")
    p.add_argument("--route", choices=("A", "B"),
                   help="with --test-cycle: A = USDT>ZEC>BTC>USDT, B = USDT>BTC>ZEC>USDT (default: the better one)")
    a = p.parse_args()
    if a.route and a.test_cycle is None:
        p.error("--route only applies to --test-cycle")
    asyncio.run(main(a))
