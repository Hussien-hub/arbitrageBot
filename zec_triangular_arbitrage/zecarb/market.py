import asyncio, json, logging
from decimal import Decimal
from time import monotonic
import aiohttp
from .models import Book

log = logging.getLogger("zecarb")


class MarketData:
    """Order books held in memory, updated from one combined WebSocket stream.
    `changed` is set on every update so the engine evaluates immediately instead of polling."""

    def __init__(self, cfg, session, symbols):
        self.cfg, self.session = cfg, session
        self.books = {s: Book(s) for s in symbols}
        self.changed = asyncio.Event()

    async def run(self):
        # shortcut: partial-depth stream = top DEPTH_LEVELS (max 20) levels, 100ms snapshots;
        # switch to diff-depth + REST snapshot sync if MAX_CYCLE_USDT ever needs deeper books.
        streams = "/".join(f"{s.lower()}@depth{self.cfg.depth_levels}@100ms" for s in self.books)
        url = f"{self.cfg.stream_url}/stream?streams={streams}"
        while True:
            try:
                async with self.session.ws_connect(url) as ws:
                    log.info("Market stream connected: %s", ", ".join(self.books))
                    while True:
                        msg = await ws.receive(timeout=10)  # silence on an active stream = dead connection
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            break
                        m = json.loads(msg.data)
                        d, book = m["data"], self.books[m["stream"].split("@", 1)[0].upper()]
                        book.bids = [(Decimal(p), Decimal(q)) for p, q in d["bids"]]
                        book.asks = [(Decimal(p), Decimal(q)) for p, q in d["asks"]]
                        book.updated_at = now = monotonic()
                        for b in self.books.values():
                            b.alive_at = now
                        self.changed.set()
            except Exception as e:  # stale books block trading until the feed is back
                log.warning("Market stream error: %r", e)
            log.warning("Market stream disconnected; reconnecting in 5s")
            await asyncio.sleep(5)
