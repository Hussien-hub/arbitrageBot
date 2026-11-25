from dataclasses import dataclass, field
from decimal import Decimal
from time import monotonic


@dataclass
class Book:
    symbol: str
    bids: list = field(default_factory=list)  # [(price, qty)], best first
    asks: list = field(default_factory=list)
    updated_at: float = 0.0  # monotonic time this book's content last arrived
    alive_at: float = 0.0    # last message on the connection carrying it; the stream only pushes changes,
                             # so a quiet book on a live connection is current, not stale

    def fresh(self, max_age: float) -> bool:
        return bool(self.bids and self.asks) and monotonic() - self.alive_at <= max_age


@dataclass
class Leg:
    symbol: str
    side: str            # BUY or SELL
    qty: Decimal         # base quantity sent to the exchange
    price: Decimal       # IOC limit price (the slippage collar)
    amount_in: Decimal   # asset spent: quote for BUY, base for SELL
    amount_out: Decimal  # asset received after fee: base for BUY, quote for SELL


@dataclass
class Opportunity:
    route: str
    start_usdt: Decimal
    end_usdt: Decimal
    net_profit: Decimal
    net_return: Decimal
    legs: list
