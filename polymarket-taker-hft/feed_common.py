from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Dict, Optional

import orjson

def _loads_json_object(msg: Any) -> Dict[str, Any]:
    return orjson.loads(msg)


def _as_float(value: Any) -> float:
    return float(value)


def _as_int(value: Any) -> int:
    return int(value)


def _queue_put_latest(queue: asyncio.Queue, item: Any) -> None:
    if queue.full():
        with suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    queue.put_nowait(item)


@dataclass
class BinanceQuote:
    bid: float
    ask: float
    ts_local_ms: int


@dataclass
class PolyBook:
    up_bid: Optional[float]
    up_ask: Optional[float]
    down_bid: Optional[float]
    down_ask: Optional[float]
    ts_local_ms: int
