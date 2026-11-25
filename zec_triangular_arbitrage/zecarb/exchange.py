import asyncio, hashlib, hmac, json, logging, time, uuid
from decimal import Decimal
from urllib.parse import urlencode
import aiohttp

log = logging.getLogger("zecarb")
RECV_WINDOW_MS = 5000


class BinanceError(RuntimeError):
    def __init__(self, status, code, msg):
        super().__init__(f"Binance {status} {code}: {msg}")
        self.status, self.code = status, code


def sign(params, secret):
    """WebSocket API HMAC signature: params sorted by name, joined as k=v&k=v."""
    payload = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class BinanceREST:
    """Startup and reconciliation calls only; the hot path uses WebSockets."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.session = None
        self.filters = {}
        self.time_offset = 0

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8),
            headers={"X-MBX-APIKEY": self.cfg.api_key} if self.cfg.api_key else {})
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    def now_ms(self):
        return int(time.time() * 1000) + self.time_offset

    async def request(self, method, path, params=None, signed=False):
        params = dict(params or {})
        url = self.cfg.base_url + path
        if signed:
            params.update(timestamp=self.now_ms(), recvWindow=RECV_WINDOW_MS)
            query = urlencode(params)
            url += "?" + query + "&signature=" + hmac.new(
                self.cfg.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            params = None
        async with self.session.request(method, url, params=params) as r:
            text = await r.text()
        try:
            data = json.loads(text)
        except ValueError:  # e.g. an HTML error page from a proxy/WAF
            data = {"msg": text[:200]}
        if r.status >= 400:
            raise BinanceError(r.status, data.get("code"), data.get("msg"))
        return data

    async def load_exchange_info(self, symbols):
        data = await self.request("GET", "/api/v3/exchangeInfo",
                                  {"symbols": json.dumps(list(symbols), separators=(",", ":"))})
        for s in data["symbols"]:
            if s["status"] != "TRADING":
                raise RuntimeError(f"{s['symbol']} is not TRADING")
            f = {x["filterType"]: x for x in s["filters"]}
            self.filters[s["symbol"]] = {
                "base": s["baseAsset"],
                "quote": s["quoteAsset"],
                "tick": Decimal(f["PRICE_FILTER"]["tickSize"]),
                "step": Decimal(f["LOT_SIZE"]["stepSize"]),
                "min_qty": Decimal(f["LOT_SIZE"]["minQty"]),
                "min_notional": Decimal(f.get("NOTIONAL", f.get("MIN_NOTIONAL", {})).get("minNotional", "0")),
            }
        missing = set(symbols) - set(self.filters)
        if missing:
            raise RuntimeError(f"Required spot symbols unavailable: {sorted(missing)}")

    async def sync_time(self):
        t0 = time.time()
        server = (await self.request("GET", "/api/v3/time"))["serverTime"]
        self.time_offset = server - int((t0 + time.time()) * 500)  # server minus local midpoint, in ms
        log.info("Clock offset to Binance: %d ms", self.time_offset)

    async def balances(self):
        acct = await self.request("GET", "/api/v3/account", signed=True)
        return {b["asset"]: Decimal(b["free"]) for b in acct["balances"]}

    async def taker_fees(self, symbols, pay_in_bnb):
        """Effective taker rate per symbol. The BNB discount multiplies the standard part only."""
        fees = {}
        for symbol in symbols:
            c = await self.request("GET", "/api/v3/account/commission", {"symbol": symbol}, signed=True)

            def rate(block):
                b = c.get(block) or {}
                return Decimal(b.get("taker", "0")) + max(Decimal(b.get("buyer", "0")), Decimal(b.get("seller", "0")))

            standard, d = rate("standardCommission"), c.get("discount") or {}
            if pay_in_bnb and d.get("enabledForAccount") and d.get("enabledForSymbol"):
                standard *= Decimal(d["discount"])
            fees[symbol] = standard + rate("taxCommission") + rate("specialCommission")
        return fees

    async def query_order(self, symbol, client_id):
        return await self.request("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    async def my_trades(self, symbol, order_id):
        return await self.request("GET", "/api/v3/myTrades", {"symbol": symbol, "orderId": order_id}, signed=True)


class BinanceWSAPI:
    """One persistent, signed WebSocket API connection, so an order costs no HTTP request setup."""

    def __init__(self, cfg, rest):
        self.cfg, self.rest = cfg, rest
        self.ws = None
        self.pending = {}

    @property
    def connected(self):
        return self.ws is not None and not self.ws.closed

    async def run(self):
        while True:
            try:
                async with self.rest.session.ws_connect(self.cfg.ws_api_url, heartbeat=20) as ws:
                    self.ws = ws
                    log.info("WebSocket API connected")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            fut = self.pending.get(data.get("id"))
                            if fut and not fut.done():
                                fut.set_result(data)
            except Exception as e:
                log.warning("WebSocket API error: %r", e)
            finally:
                self.ws = None
                for fut in self.pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("WebSocket API disconnected"))
            log.warning("WebSocket API disconnected; reconnecting in 5s")
            await asyncio.sleep(5)  # Binance allows 300 connection attempts per 5 min per IP

    async def place_order(self, params, timeout):
        if not self.connected:
            raise ConnectionError("WebSocket API not connected")
        params = dict(params, apiKey=self.cfg.api_key, recvWindow=RECV_WINDOW_MS, timestamp=self.rest.now_ms())
        params["signature"] = sign(params, self.cfg.api_secret)
        rid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            await self.ws.send_str(json.dumps({"id": rid, "method": "order.place", "params": params}))
            data = await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(rid, None)
        if data.get("status") != 200:
            err = data.get("error") or {}
            raise BinanceError(data.get("status"), err.get("code"), err.get("msg"))
        return data["result"]
