import os
from dataclasses import dataclass
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()


def D(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default))


@dataclass(frozen=True)
class Config:
    api_key: str = os.getenv("BINANCE_API_KEY", "")
    api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    base_url: str = os.getenv("BINANCE_BASE_URL", "https://api.binance.com").rstrip("/")
    stream_url: str = os.getenv("BINANCE_STREAM_URL", "wss://stream.binance.com:9443").rstrip("/")
    ws_api_url: str = os.getenv("BINANCE_WS_API_URL", "wss://ws-api.binance.com:443/ws-api/v3")
    mode: str = os.getenv("MODE", "PAPER").upper()
    live_enabled: bool = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
    max_cycle: Decimal = D("MAX_CYCLE_USDT", "1000")
    min_profit: Decimal = D("MIN_NET_PROFIT_USDT", "3")
    min_return: Decimal = D("MIN_NET_RETURN", "0.003")
    max_slippage: Decimal = D("MAX_SLIPPAGE", "0.001")
    max_book_age: float = float(os.getenv("MAX_BOOK_AGE_SECONDS", "0.5"))
    depth_levels: int = int(os.getenv("DEPTH_LEVELS", "20"))
    taker_fee: Decimal = D("TAKER_FEE_RATE", "0.00075")  # paper-mode estimate; LIVE reads the account's real rates
    order_timeout: float = float(os.getenv("ORDER_TIMEOUT_SECONDS", "3"))
    daily_loss_limit: Decimal = D("DAILY_LOSS_LIMIT_USDT", "10")
    max_failures: int = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "2"))
    state_file: str = os.getenv("STATE_FILE", "state.json")

    @property
    def live(self) -> bool:
        return self.mode == "LIVE" and self.live_enabled

    def validate(self):
        if self.mode not in {"PAPER", "LIVE"}:
            raise ValueError("MODE must be PAPER or LIVE")
        if self.mode == "LIVE" and not self.live_enabled:
            raise ValueError("MODE=LIVE also requires LIVE_TRADING_ENABLED=true")
        if self.live and (not self.api_key or not self.api_secret):
            raise ValueError("Live mode requires Binance API credentials")
        if self.max_cycle <= 0:
            raise ValueError("MAX_CYCLE_USDT must be > 0")
        if self.min_return <= 0 or self.max_slippage <= 0:
            raise ValueError("Profit and slippage thresholds must be positive")
        if self.depth_levels not in (5, 10, 20):
            raise ValueError("DEPTH_LEVELS must be 5, 10 or 20")
