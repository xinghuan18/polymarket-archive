from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import orjson
import websockets
from websockets.exceptions import WebSocketException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from feed_common import _as_float, _as_int, _loads_json_object


DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_WS_BASE = "wss://stream.binance.com:9443/stream"


def _latency_ms(now_ms: int, event_ms: Optional[int]) -> Optional[int]:
    if event_ms is None:
        return None
    return now_ms - event_ms


def _build_combined_stream_url(ws_base: str, symbol: str) -> str:
    streams = [
        f"{symbol.lower()}@bookTicker",
        f"{symbol.lower()}@trade",
    ]
    base = ws_base.rstrip("/")
    joined = "/".join(streams)

    if "streams=" in base:
        return base
    if base.endswith("/stream"):
        return f"{base}?streams={joined}"
    if base.endswith("/ws"):
        return f"{base[:-3]}/stream?streams={joined}"
    return f"{base}/stream?streams={joined}"


def _logger() -> logging.Logger:
    logger = logging.getLogger("binance_price_monitor")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
    return logger


@dataclass
class LastSeen:
    book_bid: Optional[float] = None
    book_ask: Optional[float] = None
    trade_price: Optional[float] = None


def _log_book_ticker(
    data: dict[str, Any],
    state: LastSeen,
    logger: logging.Logger,
) -> None:
    bid = _as_float(data["b"])
    ask = _as_float(data["a"])

    changed = False
    if state.book_bid != bid:
        state.book_bid = bid
        changed = True
    if state.book_ask != ask:
        state.book_ask = ask
        changed = True
    if not changed:
        return

    now_ms = int(time.time() * 1000)
    event_ms = _as_int(data["E"])
    update_id = _as_int(data["u"])
    logger.info(
        "[BOOK_TICKER] symbol=%s bid=%.2f ask=%.2f update_id=%s event_ms=%s latency_ms=%s",
        data["s"],
        bid,
        ask,
        update_id,
        event_ms,
        _latency_ms(now_ms, event_ms),
    )


def _log_trade(
    data: dict[str, Any],
    state: LastSeen,
    logger: logging.Logger,
) -> None:
    price = _as_float(data["p"])
    qty = _as_float(data["q"])

    if state.trade_price == price:
        return
    state.trade_price = price

    now_ms = int(time.time() * 1000)
    event_ms = _as_int(data["E"])
    trade_ms = _as_int(data["T"])
    trade_id = _as_int(data["t"])
    is_buyer_maker = bool(data["m"])
    taker_side = "SELL" if is_buyer_maker else "BUY"

    logger.info(
        "[TRADE] symbol=%s price=%.2f qty=%s taker_side=%s trade_id=%s event_ms=%s trade_ms=%s "
        "event_latency_ms=%s trade_latency_ms=%s",
        data["s"],
        price,
        f"{qty:.6f}",
        taker_side,
        trade_id,
        event_ms,
        trade_ms,
        _latency_ms(now_ms, event_ms),
        _latency_ms(now_ms, trade_ms),
    )


def _handle_payload(
    payload: dict[str, Any],
    state: LastSeen,
    logger: logging.Logger,
) -> None:
    if "result" in payload:
        return

    stream = payload["stream"].lower()
    data = payload["data"]

    event_type = data["e"].lower()

    if stream.endswith("@bookticker") or event_type == "bookticker":
        _log_book_ticker(data, state, logger)
        return
    if stream.endswith("@trade") or event_type == "trade":
        _log_trade(data, state, logger)


async def run_monitor(
    symbol: str,
    ws_base: str,
    reconnect_delay: float,
) -> None:
    logger = _logger()
    state = LastSeen()
    url = _build_combined_stream_url(ws_base=ws_base, symbol=symbol)
    logger.info(
        "[BOOT] starting monitor symbol=%s url=%s channels=bookTicker,trade",
        symbol.upper(),
        url,
    )

    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=10,
                max_queue=None,
            ) as ws:
                logger.info("[WS] connected")
                async for message in ws:
                    payload = _loads_json_object(message)
                    _handle_payload(payload=payload, state=state, logger=logger)
        except asyncio.CancelledError:
            raise
        except (WebSocketException, OSError, asyncio.TimeoutError, orjson.JSONDecodeError) as exc:
            logger.warning("[WS] error=%s reconnect_in_s=%.2f", exc, reconnect_delay)
            await asyncio.sleep(reconnect_delay)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor Binance BTCUSDT price updates from bookTicker and trade streams.",
    )
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help="Symbol (default: BTCUSDT)")
    parser.add_argument(
        "--ws-base",
        default=DEFAULT_WS_BASE,
        help="Websocket base (default: wss://stream.binance.com:9443/stream)",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=1.0,
        help="Reconnect delay in seconds (default: 1.0)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(
        run_monitor(
            symbol=args.symbol.upper(),
            ws_base=args.ws_base,
            reconnect_delay=args.reconnect_delay,
        )
    )


if __name__ == "__main__":
    main()
