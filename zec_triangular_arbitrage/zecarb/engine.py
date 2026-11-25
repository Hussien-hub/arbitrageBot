from decimal import Decimal, ROUND_DOWN, ROUND_UP
from .models import Leg, Opportunity

SYMBOLS = ("ZECUSDT", "ZECBTC", "BTCUSDT")
# Each leg spends what the previous leg received.
ROUTES = {
    "A: USDT>ZEC>BTC>USDT": (("ZECUSDT", "BUY"), ("ZECBTC", "SELL"), ("BTCUSDT", "SELL")),
    "B: USDT>BTC>ZEC>USDT": (("BTCUSDT", "BUY"), ("ZECBTC", "BUY"), ("ZECUSDT", "SELL")),
}
ONE = Decimal(1)


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def tradable(qty, price, f) -> bool:
    """Passes LOT_SIZE minQty and NOTIONAL minNotional."""
    return qty > 0 and qty >= f["min_qty"] and qty * price >= f["min_notional"]


def walk(levels, qty):
    """Fill a base qty against price levels; returns (filled, quote, worst_price)."""
    filled = quote = worst = Decimal(0)
    for price, size in levels:
        if filled >= qty:
            break
        take = min(size, qty - filled)
        filled += take
        quote += take * price
        worst = price
    return filled, quote, worst


def plan_leg(book, side, amount_in, fee, f, slippage):
    """Size one IOC leg exactly as the executor sends it (tick/step rounding, filters, collar).
    Returns None if the book can't fill it or the exchange would reject it."""
    if side == "BUY":
        reach, worst = Decimal(0), None  # deepest ask that amount_in reaches
        for price, size in book.asks:
            reach += price * size
            worst = price
            if reach >= amount_in:
                break
        else:
            return None
        price = floor_to(worst * (ONE + slippage), f["tick"])
        qty = floor_to(amount_in / price, f["step"])  # qty*price <= amount_in, so the balance lock always fits
        levels = book.asks
    else:
        qty = floor_to(amount_in, f["step"])
        _, _, worst = walk(book.bids, qty)
        price = ceil_to(worst * (ONE - slippage), f["tick"])
        levels = book.bids
    if not tradable(qty, price, f):
        return None
    filled, quote, _ = walk(levels, qty)
    if filled < qty:
        return None
    if side == "BUY":
        return Leg(book.symbol, side, qty, price, quote, filled * (ONE - fee))
    return Leg(book.symbol, side, qty, price, filled, quote * (ONE - fee))


class ArbitrageEngine:
    def __init__(self, cfg, filters, fees):
        self.cfg, self.filters, self.fees = cfg, filters, fees
        self.not_before = float("-inf")  # books older than the last signal are ignored, so one move isn't traded twice

    def evaluate(self, books, capital):
        if any(not books[s].fresh(self.cfg.max_book_age) or books[s].updated_at <= self.not_before
               for s in SYMBOLS):
            return []
        opportunities = []
        for name, steps in ROUTES.items():
            amount, legs = capital, []
            for symbol, side in steps:
                leg = plan_leg(books[symbol], side, amount, self.fees[symbol],
                               self.filters[symbol], self.cfg.max_slippage)
                if leg is None:
                    break
                legs.append(leg)
                amount = leg.amount_out
            else:
                spent = legs[0].amount_in
                end = legs[-1].amount_out + self.leftover_value(legs, books)
                opportunities.append(Opportunity(name, spent, end, end - spent, (end - spent) / spent, legs))
        return sorted(opportunities, key=lambda o: o.net_profit, reverse=True)

    def leftover_value(self, legs, books):
        """Intermediate amounts the next leg can't use (step rounding, BUY collar), marked at best bid."""
        value = Decimal(0)
        for prev, nxt in zip(legs, legs[1:]):
            f = self.filters[prev.symbol]
            symbol = (f["base"] if prev.side == "BUY" else f["quote"]) + "USDT"
            value += (prev.amount_out - nxt.amount_in) * books[symbol].bids[0][0] * (ONE - self.fees[symbol])
        return value
