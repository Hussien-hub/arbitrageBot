"""Run from the project root: python -m tests.test_zecarb   (or: python -m pytest tests)"""
import asyncio, os, tempfile
from decimal import Decimal as D
from time import monotonic

from zecarb.config import Config
from zecarb.engine import ArbitrageEngine, plan_leg
from zecarb.exchange import BinanceError, sign
from zecarb.executor import Executor, State
from zecarb.models import Book

FILTERS = {  # real Binance values for these symbols (Nov 2026)
    "ZECUSDT": {"base": "ZEC", "quote": "USDT", "tick": D("0.01"), "step": D("0.001"), "min_qty": D("0.001"), "min_notional": D("5")},
    "ZECBTC": {"base": "ZEC", "quote": "BTC", "tick": D("0.0000001"), "step": D("0.001"), "min_qty": D("0.001"), "min_notional": D("0.0001")},
    "BTCUSDT": {"base": "BTC", "quote": "USDT", "tick": D("0.01"), "step": D("0.00001"), "min_qty": D("0.00001"), "min_notional": D("5")},
}
FEES = {s: D("0.001") for s in FILTERS}
CFG = Config(mode="PAPER", max_slippage=D("0.001"), max_book_age=5.0, max_failures=2, daily_loss_limit=D("10"))


def book(symbol, bids, asks):
    now = monotonic()
    return Book(symbol, [(D(p), D(q)) for p, q in bids], [(D(p), D(q)) for p, q in asks], now, now)


def books():
    """Route A (USDT>ZEC>BTC>USDT) is very profitable here; route B loses."""
    return {
        "ZECUSDT": book("ZECUSDT", [("99.9", "50")], [("100", "50")]),
        "ZECBTC": book("ZECBTC", [("0.0011", "50")], [("0.0012", "50")]),
        "BTCUSDT": book("BTCUSDT", [("100000", "5")], [("100010", "5")]),
        "BNBUSDT": book("BNBUSDT", [("600", "10")], [("601", "10")]),
    }


def test_plan_buy_walks_levels_and_rounds():
    b = book("ZECUSDT", [], [("100", "1"), ("101", "10")])
    leg = plan_leg(b, "BUY", D("150"), D("0.001"), FILTERS["ZECUSDT"], D("0.001"))
    # worst ask reached is 101 -> collar floor(101*1.001) = 101.10; qty floor(150/101.10) = 1.483
    assert (leg.price, leg.qty) == (D("101.10"), D("1.483"))
    assert leg.amount_in == D("148.783")      # 1@100 + 0.483@101
    assert leg.amount_out == D("1.481517")    # 1.483 * 0.999


def test_plan_sell_walks_levels_and_rounds():
    b = book("ZECUSDT", [("100", "1"), ("99", "10")], [])
    leg = plan_leg(b, "SELL", D("1.4815"), D("0.001"), FILTERS["ZECUSDT"], D("0.001"))
    assert (leg.qty, leg.price) == (D("1.481"), D("98.91"))   # collar ceil(99*0.999)
    assert leg.amount_out == D("147.471381")                  # (100 + 0.481*99) * 0.999


def test_plan_rejects_thin_book_and_min_notional():
    thin = book("ZECUSDT", [("100", "0.5")], [("100", "0.5")])
    assert plan_leg(thin, "BUY", D("1000"), D("0.001"), FILTERS["ZECUSDT"], D("0.001")) is None
    assert plan_leg(thin, "SELL", D("0.04"), D("0.001"), FILTERS["ZECUSDT"], D("0.001")) is None  # $4 < $5


def test_routes_match_hand_calculation():
    opps = ArbitrageEngine(CFG, FILTERS, FEES).evaluate(books(), D("1000"))
    a, b = opps
    assert a.route.startswith("A") and b.net_profit < 0
    # spend 999.0 (9.990 ZEC @100); end 1094.904 + leftovers 0.000998001 ZEC-value + 0.7014978 BTC-value
    assert a.start_usdt == D("999.000") and abs(a.net_profit - D("96.606495801")) < D("1e-9")


def test_stale_or_already_traded_books_are_ignored():
    bs = books()
    bs["ZECBTC"].updated_at -= 10  # quiet book on a live feed is still current
    assert ArbitrageEngine(CFG, FILTERS, FEES).evaluate(bs, D("1000")) != []
    bs["ZECBTC"].alive_at -= 10    # feed went silent: stale
    assert ArbitrageEngine(CFG, FILTERS, FEES).evaluate(bs, D("1000")) == []
    engine, bs = ArbitrageEngine(CFG, FILTERS, FEES), books()
    engine.not_before = monotonic()  # a cycle ran after these books arrived
    assert engine.evaluate(bs, D("1000")) == []


def test_ws_signature_matches_binance_doc_vector():
    params = {"symbol": "BTCUSDT", "side": "SELL", "type": "LIMIT", "timeInForce": "GTC", "quantity": "0.01000000",
              "price": "52000.00", "recvWindow": 100, "timestamp": 1645423376532, "apiKey": "YOUR_API_KEY"}
    # = the doc's own check: echo -n 'apiKey=YOUR_API_KEY&price=52000.00&...&type=LIMIT' | openssl dgst -sha256 -hmac 'YOUR_SECRET_KEY'
    assert sign(params, "YOUR_SECRET_KEY") == "d7ab7164420657782715adc22ec709eb55da95ae9296d2a224bdf3d08caa0967"


# ---- executor against a fake exchange -------------------------------------------------------

MARKET_PX = {"ZECUSDT": D("99.9"), "BTCUSDT": D("100000")}


class FakeWS:
    connected = True

    def __init__(self, fill_ratio=None, errors=None):
        self.sent, self.fill_ratio, self.errors = [], fill_ratio or {}, list(errors or [])

    async def place_order(self, p, timeout):
        self.sent.append(p)
        if self.errors:
            err = self.errors.pop(0)
            if err:
                raise err
        return fill(p, self.fill_ratio.get(p["symbol"] + p["side"], D(1)))


def fill(p, ratio):
    """Fills at the limit price (or a fixed market price); commission 0.1% in the received asset."""
    f = FILTERS[p["symbol"]]
    qty = D(p["quantity"]) * ratio
    quote = qty * D(p.get("price") or MARKET_PX[p["symbol"]])
    fee = (qty * D("0.001"), f["base"]) if p["side"] == "BUY" else (quote * D("0.001"), f["quote"])
    return {"orderId": 7, "executedQty": str(qty), "cummulativeQuoteQty": str(quote),
            "fills": [{"commission": str(fee[0]), "commissionAsset": fee[1]}]}


class FakeREST:
    filters = FILTERS

    def __init__(self, order=None):
        self.order = order

    async def query_order(self, symbol, cid):
        if self.order is None:
            raise BinanceError(400, -2013, "Order does not exist.")
        return self.order

    async def my_trades(self, symbol, order_id):
        return self.order["fills"]


def run_cycle(ws, rest=None, state=None, route="A", forced=False):
    state = state or State(os.path.join(tempfile.mkdtemp(), "state.json"))
    ex = Executor(CFG, rest or FakeREST(), ws, state)
    ex.reconcile_delay = 0
    bs = books()
    opp = next(o for o in ArbitrageEngine(CFG, FILTERS, FEES).evaluate(bs, D("1000")) if o.route.startswith(route))
    asyncio.run(ex.execute(opp, bs, forced))
    return state


def test_full_cycle_sends_three_ioc_legs_sized_from_fills():
    ws = FakeWS()
    state = run_cycle(ws)
    assert [(p["symbol"], p["side"], p["type"], p["timeInForce"]) for p in ws.sent] == [
        ("ZECUSDT", "BUY", "LIMIT", "IOC"), ("ZECBTC", "SELL", "LIMIT", "IOC"), ("BTCUSDT", "SELL", "LIMIT", "IOC")]
    assert ws.sent[1]["quantity"] == "9.98"   # 9.990 ZEC bought minus 0.1% commission, floored to step
    assert D(state.data["pnl"]) > 0 and state.data.get("failures", 0) == 0 and not state.killed


def test_partial_middle_leg_unwinds_leftover_to_usdt():
    ws = FakeWS(fill_ratio={"ZECBTCSELL": D("0.5")})
    state = run_cycle(ws)
    last = ws.sent[-1]
    assert len(ws.sent) == 4 and (last["symbol"], last["side"], last["type"], last["quantity"]) == ("ZECUSDT", "SELL", "MARKET", "4.99")
    assert not state.killed


def test_lost_response_is_reconciled_not_resent():
    ws = FakeWS(errors=[asyncio.TimeoutError()])  # leg 1 times out, never reached the exchange
    state = run_cycle(ws)
    assert len(ws.sent) == 1 and D(state.data["pnl"]) == 0 and not state.killed
    # same timeout, but the order did fill: the cycle continues from the reconciled fill
    filled = fill({"symbol": "ZECUSDT", "side": "BUY", "quantity": "9.99", "price": "100.1"}, D(1))
    ws = FakeWS(errors=[asyncio.TimeoutError()])
    run_cycle(ws, rest=FakeREST(order=filled))
    assert [p["symbol"] for p in ws.sent] == ["ZECUSDT", "ZECBTC", "BTCUSDT"]


def test_repeated_rejections_trip_persistent_kill_switch():
    state = run_cycle(FakeWS(errors=[BinanceError(400, -2010, "insufficient balance")]))
    assert state.data["failures"] == 1 and not state.killed
    run_cycle(FakeWS(errors=[BinanceError(400, -2010, "insufficient balance")]), state=state)
    assert state.killed and State(state.path).killed  # survives a restart


def test_forced_test_cycle_loss_is_not_a_failure_but_counts_toward_daily_limit():
    ws = FakeWS()
    state = run_cycle(ws, route="B", forced=True)  # route B loses ~17% on these books
    assert [(p["symbol"], p["side"]) for p in ws.sent] == [("BTCUSDT", "BUY"), ("ZECBTC", "BUY"), ("ZECUSDT", "SELL")]
    assert D(state.data["pnl"]) < 0 and state.data.get("failures", 0) == 0
    assert "DAILY_LOSS_LIMIT" in state.data["reason"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
