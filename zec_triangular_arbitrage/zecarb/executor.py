import asyncio, json, logging, os, uuid
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import aiohttp
from .engine import floor_to, tradable
from .exchange import BinanceError, RECV_WINDOW_MS

log = logging.getLogger("zecarb")
HOME = "USDT"


def fmt(x: Decimal) -> str:
    return f"{x.normalize():f}"  # Binance rejects scientific notation


class State:
    """Daily realized P&L, consecutive failures and the kill switch, persisted so a restart can't
    silently resume trading after a stop."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path) as fh:
                self.data = json.load(fh)
        except FileNotFoundError:
            self.data = {}

    @property
    def killed(self):
        return bool(self.data.get("killed"))

    def kill(self, reason):
        log.critical("KILL SWITCH: %s", reason)
        self.data.update(killed=True, reason=reason)
        self.save()

    def record(self, pnl, failed, cfg):
        today = datetime.now(timezone.utc).date().isoformat()
        if self.data.get("date") != today:
            self.data.update(date=today, pnl="0")
        total = Decimal(self.data["pnl"]) + pnl
        self.data["pnl"] = str(total)
        if failed:
            self.data["failures"] = self.data.get("failures", 0) + 1
        elif pnl > 0:
            self.data["failures"] = 0
        self.save()
        if total <= -cfg.daily_loss_limit:
            self.kill(f"daily P&L {total} USDT reached DAILY_LOSS_LIMIT_USDT")
        elif self.data.get("failures", 0) >= cfg.max_failures:
            self.kill(f"{self.data['failures']} consecutive failed cycles")

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh, indent=2)
        os.replace(tmp, self.path)


class Executor:
    """Runs one triangle as sequential IOC orders over the WebSocket API. Each leg is sized from what
    the previous leg actually delivered (net of commission); whatever ZEC/BTC is left is sold to USDT."""

    def __init__(self, cfg, rest, ws_api, state):
        self.cfg, self.rest, self.ws, self.state = cfg, rest, ws_api, state
        self.filters = rest.filters
        self.reconcile_delay = RECV_WINDOW_MS / 1000 + 1

    async def execute(self, opp, books, forced=False):
        """forced: a test cycle run regardless of profit, so its expected loss isn't counted as a failure."""
        if self.state.killed or not self.ws.connected:
            return
        pos = defaultdict(Decimal)  # this cycle's balance change per asset
        rejected = None
        try:
            for i, leg in enumerate(opp.legs):
                f = self.filters[leg.symbol]
                if i == 0:
                    qty = leg.qty
                elif leg.side == "BUY":
                    qty = floor_to(pos[f["quote"]] / leg.price, f["step"])
                else:
                    qty = floor_to(pos[f["base"]], f["step"])
                if not tradable(qty, leg.price, f):
                    break
                try:
                    filled = await self.order(pos, leg.symbol, leg.side, qty, leg.price)
                except BinanceError as e:
                    log.error("%s %s rejected: %s", leg.symbol, leg.side, e)
                    rejected = e
                    break
                if not filled:
                    break
            await self.unwind(pos, books)
        except Exception as e:
            log.exception("Cycle failed")
            self.state.kill(f"cycle error {e!r}; known balance change {self.show(pos)}; check the account")
            return
        pnl = self.mark(pos, books)
        log.warning("CYCLE %s pnl=%s USDT change=%s", opp.route, pnl, self.show(pos))
        self.state.record(pnl, rejected is not None or (pnl < 0 and not forced), self.cfg)
        if rejected is not None and rejected.status in (418, 429):
            self.state.kill(f"rate limited by Binance: {rejected}")

    async def unwind(self, pos, books):
        """Sell any ZEC/BTC this cycle still holds back to USDT."""
        for asset in [a for a, amt in pos.items() if a not in (HOME, "BNB") and amt > 0]:
            symbol = asset + HOME
            f, bid = self.filters[symbol], books[symbol].bids[0][0]
            qty = floor_to(pos[asset], f["step"])
            if not tradable(qty, bid, f):
                continue  # dust below exchange minimums stays in the account
            await self.order(pos, symbol, "SELL", qty)  # MARKET: getting flat beats waiting for a price
            left = floor_to(pos[asset], f["step"])
            if tradable(left, bid, f):
                raise RuntimeError(f"unwind left {left} {asset} unsold")

    async def order(self, pos, symbol, side, qty, price=None):
        """Send one order, book its fills and commissions into pos, return base qty filled.
        A lost response is reconciled by client order id after recvWindow, so nothing is sent twice."""
        cid = "arb" + uuid.uuid4().hex[:20]
        params = {"symbol": symbol, "side": side, "quantity": fmt(qty),
                  "newClientOrderId": cid, "newOrderRespType": "FULL"}
        if price is None:
            params["type"] = "MARKET"
        else:
            params.update(type="LIMIT", timeInForce="IOC", price=fmt(price))
        try:
            r = await self.ws.place_order(params, self.cfg.order_timeout)
        except BinanceError as e:
            if e.status is not None and 400 <= e.status < 500:
                raise  # rejected by the exchange: the order was never placed
            r = await self.reconcile(symbol, cid)
        except (asyncio.TimeoutError, ConnectionError, aiohttp.ClientError):
            log.warning("No response for %s %s %s; reconciling", symbol, side, cid)
            r = await self.reconcile(symbol, cid)
        if r is None:
            return Decimal(0)
        f = self.filters[symbol]
        base, quote = Decimal(r["executedQty"]), Decimal(r["cummulativeQuoteQty"])
        direction = 1 if side == "BUY" else -1
        pos[f["base"]] += direction * base
        pos[f["quote"]] -= direction * quote
        for fill in r.get("fills", []):
            pos[fill["commissionAsset"]] -= Decimal(fill["commission"])
        log.info("%s %s %s qty=%s filled=%s quote=%s clientOrderId=%s", symbol, side, params["type"],
                 qty, base, quote, cid)
        return base

    async def reconcile(self, symbol, cid):
        await asyncio.sleep(self.reconcile_delay)  # past recvWindow the exchange can no longer accept it
        try:
            r = await self.rest.query_order(symbol, cid)
        except BinanceError as e:
            if e.code == -2013:  # order does not exist: it never reached the matching engine
                return None
            raise
        if Decimal(r["executedQty"]):
            r["fills"] = await self.rest.my_trades(symbol, r["orderId"])  # per-trade commission
        return r

    def mark(self, pos, books):
        """Cycle P&L in USDT: USDT change plus leftovers and BNB commission valued at best bid."""
        pnl = pos[HOME]
        for asset, amt in pos.items():
            if asset == HOME or not amt:
                continue
            book = books.get(asset + HOME)
            if book and book.bids:
                pnl += amt * book.bids[0][0]
            else:
                log.warning("No %s%s price: %s %s left out of P&L", asset, HOME, amt, asset)
        return pnl

    @staticmethod
    def show(pos):
        return {a: str(v) for a, v in pos.items() if v}
