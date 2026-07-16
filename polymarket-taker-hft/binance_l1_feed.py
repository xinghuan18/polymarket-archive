from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import time
from typing import Optional

import orjson
import websockets
from websockets.exceptions import WebSocketException

from feed_common import BinanceQuote, _as_float, _loads_json_object, _queue_put_latest


class BinanceL1Feed:
    def __init__(
        self,
        symbol: str,
        ws_base: str,
        emit_on_price_change_only: bool = True,
        ping_interval: int = 20,
        ping_timeout: int = 10,
        reconnect_delay: float = 1.0,
        logger: Optional[logging.Logger] = None,
    ):
        self.symbol = symbol
        self._symbol_stream = symbol.lower()
        self._url = f"{ws_base.rstrip('/')}/{self._symbol_stream}@bookTicker"
        self._emit_on_price_change_only = emit_on_price_change_only
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._reconnect_delay = reconnect_delay
        self._logger = logger or logging.getLogger("taker_hft")

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self._last_quote: Optional[BinanceQuote] = None

    @property
    def queue(self) -> asyncio.Queue:
        return self._queue

    def snapshot(self) -> Optional[BinanceQuote]:
        return self._last_quote

    def reset_cache(self) -> None:
        self._last_quote = None
        while True:
            with suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                continue
            return

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                async with websockets.connect(
                    self._url,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_timeout,
                    max_queue=None,
                ) as ws:
                    self._logger.info("[BINANCE] connected bookTicker")
                    async for msg in ws:
                        if stop_event.is_set():
                            break
                        payload = _loads_json_object(msg)
                        bid = _as_float(payload["b"])
                        ask = _as_float(payload["a"])

                        quote = BinanceQuote(
                            bid=bid,
                            ask=ask,
                            ts_local_ms=int(time.time() * 1000),
                        )
                        if (
                            self._emit_on_price_change_only
                            and self._last_quote is not None
                            and quote.bid == self._last_quote.bid
                            and quote.ask == self._last_quote.ask
                        ):
                            continue

                        self._last_quote = quote
                        _queue_put_latest(self._queue, quote)
            except asyncio.CancelledError:
                raise
            except (WebSocketException, OSError, asyncio.TimeoutError, orjson.JSONDecodeError) as exc:
                self._logger.warning(
                    "[BINANCE] ws error=%s reconnect in %ss",
                    exc,
                    self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
