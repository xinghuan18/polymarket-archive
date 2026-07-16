from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import requests

from binance_l1_feed import BinanceL1Feed
from feed_common import BinanceQuote, PolyBook, _as_float, _as_int
from polymarket_book_feed import PolymarketBookFeed


def fetch_binance_1h_open_price(
    symbol: str,
    candle_open_dt: datetime,
    rest_base: str = "https://api.binance.com",
    timeout_s: float = 10.0,
) -> Optional[float]:
    open_dt_utc = candle_open_dt.astimezone(timezone.utc)

    start_ms = int(open_dt_utc.timestamp() * 1000)
    url = f"{rest_base.rstrip('/')}/api/v3/klines"
    params = {
        "symbol": symbol.upper(),
        "interval": "1h",
        "startTime": start_ms,
        "limit": 1,
    }
    resp = requests.get(url, params=params, timeout=timeout_s)
    resp.raise_for_status()

    row = resp.json()[0]
    open_time_ms = _as_int(row[0])
    open_px = _as_float(row[1])
    if open_time_ms != start_ms or open_px <= 0.0:
        return None
    return open_px


__all__ = [
    "BinanceL1Feed",
    "BinanceQuote",
    "PolyBook",
    "PolymarketBookFeed",
    "fetch_binance_1h_open_price",
]
