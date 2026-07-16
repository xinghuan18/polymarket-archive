from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import time
from typing import Any, Dict, Optional, Set

import aiohttp
import orjson

from feed_common import PolyBook, _as_float, _queue_put_latest
from market_selector import MarketSession


class PolymarketBookFeed:
    def __init__(
        self,
        ws_base: str,
        channel: str,
        emit_on_price_change_only: bool = True,
        ping_interval: int = 20,
        ping_timeout: int = 10,
        reconnect_delay: float = 1.0,
        logger: Optional[logging.Logger] = None,
    ):
        self._ws_base = ws_base.rstrip("/")
        self._channel = channel
        self._emit_on_price_change_only = emit_on_price_change_only
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._reconnect_delay = reconnect_delay
        self._logger = logger or logging.getLogger("taker_hft")

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self._best_bid: Dict[str, float] = {}
        self._best_ask: Dict[str, float] = {}
        self._desired_tokens: Set[str] = set()
        self._subscribed_tokens: Set[str] = set()
        self._sync_required = False
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None

    @property
    def queue(self) -> asyncio.Queue:
        return self._queue

    async def update_tokens(self, token_ids: list[str]) -> None:
        next_tokens = set(token_ids)
        changed = next_tokens != self._desired_tokens
        if changed:
            self._desired_tokens = next_tokens
        if self._ws is None or self._ws.closed:
            return
        if changed or self._sync_required:
            await self._sync_subscriptions()
            self._sync_required = False

    def get_best_bid(self, token_id: str) -> Optional[float]:
        return self._best_bid.get(token_id)

    def get_best_ask(self, token_id: str) -> Optional[float]:
        return self._best_ask.get(token_id)

    def build_poly_book(self, session: Optional[MarketSession]) -> Optional[PolyBook]:
        if session is None:
            return None
        return PolyBook(
            up_bid=self.get_best_bid(session.up_token_id),
            up_ask=self.get_best_ask(session.up_token_id),
            down_bid=self.get_best_bid(session.down_token_id),
            down_ask=self.get_best_ask(session.down_token_id),
            ts_local_ms=int(time.time() * 1000),
        )

    def reset_cache(self) -> None:
        self._best_bid.clear()
        self._best_ask.clear()
        self._desired_tokens.clear()
        self._sync_required = True
        while True:
            with suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                continue
            return

    async def run(self, stop_event: asyncio.Event) -> None:
        base = self._ws_base if self._ws_base.endswith("/ws") else f"{self._ws_base}/ws"
        url = f"{base}/{self._channel}"

        while not stop_event.is_set():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        url,
                        heartbeat=self._ping_interval,
                        timeout=self._ping_timeout,
                    ) as ws:
                        self._ws = ws
                        await self._sync_subscriptions()
                        self._sync_required = False

                        async for msg in ws:
                            if stop_event.is_set():
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._handle_message(msg.data)
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                break
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError, orjson.JSONDecodeError) as exc:
                self._logger.warning(
                    "[POLY] ws error=%s reconnect in %ss",
                    exc,
                    self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
            finally:
                self._ws = None
                self._subscribed_tokens.clear()
                self._sync_required = False

    async def _sync_subscriptions(self) -> None:
        if self._ws is None or self._ws.closed:
            return
        to_unsubscribe = sorted(self._subscribed_tokens - self._desired_tokens)
        if to_unsubscribe:
            await self._send_subscription(to_unsubscribe, operation="unsubscribe")
            self._subscribed_tokens.difference_update(to_unsubscribe)
        to_subscribe = sorted(self._desired_tokens - self._subscribed_tokens)
        if to_subscribe:
            await self._send_subscription(to_subscribe, operation="subscribe")
            self._subscribed_tokens.update(to_subscribe)

    async def _send_subscription(self, token_ids: list[str], operation: str) -> None:
        if self._ws is None or self._ws.closed or not token_ids:
            return
        await self._ws.send_json(
            {
                "assets_ids": token_ids,
                "type": self._channel,
                "operation": operation,
            }
        )

    def _handle_message(self, data: str) -> None:
        payload = orjson.loads(data)
        if type(payload) is list:
            for event in payload:
                self._process_event(event)
            return
        self._process_event(payload)

    def _process_event(self, msg: Dict[str, Any]) -> None:
        event_type = msg["event_type"]
        if event_type == "price_change":
            for change in msg["price_changes"]:
                self._update_token(
                    change["asset_id"],
                    change["best_bid"],
                    change["best_ask"],
                )
            return
        if event_type == "book":
            token_id = msg["asset_id"]
            bid = self._best_from_levels(msg["bids"], side="bid")
            ask = self._best_from_levels(msg["asks"], side="ask")
            self._update_token(token_id, bid, ask)

    def _best_from_levels(self, levels: list[Any], side: str) -> float:
        vals = [_as_float(level["price"]) for level in levels]
        return max(vals) if side == "bid" else min(vals)

    def _update_token(self, token_id: Any, bid: Any, ask: Any) -> None:
        tid = token_id

        updated = False
        prev_bid = self._best_bid.get(tid)
        prev_ask = self._best_ask.get(tid)

        new_bid = _as_float(bid)
        if (not self._emit_on_price_change_only) or prev_bid is None or new_bid != prev_bid:
            self._best_bid[tid] = new_bid
            updated = True

        new_ask = _as_float(ask)
        if (not self._emit_on_price_change_only) or prev_ask is None or new_ask != prev_ask:
            self._best_ask[tid] = new_ask
            updated = True

        if updated:
            _queue_put_latest(
                self._queue,
                {
                    "token_id": tid,
                    "bid": self._best_bid.get(tid),
                    "ask": self._best_ask.get(tid),
                    "ts_local_ms": int(time.time() * 1000),
                },
            )
