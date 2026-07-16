from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Iterable, Optional

import orjson
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_selector import MarketSession, fetch_1h_btc_markets, select_active_or_next
from polymarket_monitor_csv import MonitorCsvAppender

DEFAULT_GAMMA_BASE = "https://gamma-api.polymarket.com"
DEFAULT_WS_BASE = "wss://ws-subscriptions-clob.polymarket.com/ws"
DEFAULT_CHANNEL = "market"
DEFAULT_CSV_PATH = str(Path(__file__).resolve().parent / "polymarket_monitor_events.csv")
RUNTIME_EVENT_TYPE = "best_bid_ask"
BOOTSTRAP_EVENT_TYPE = "book"

def _logger() -> logging.Logger:
    logger = logging.getLogger("polymarket_btc_1h_monitor")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
    return logger


def _parse_offsets(raw: str) -> list[int]:
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    return [int(part) for part in parts]


def _event_timestamp_ms(payload: dict[str, Any]) -> int:
    return int(payload["timestamp"])

def _best_price(levels: Any, side: str) -> Optional[float]:
    values = [float(level["price"]) for level in levels]
    return max(values) if side == "bid" else min(values)


def _best_bid_ask_from_book(payload: dict[str, Any]) -> dict[str, Any]:
    best_bid = _best_price(payload["bids"], side="bid")
    best_ask = _best_price(payload["asks"], side="ask")
    return {
        "event_type": RUNTIME_EVENT_TYPE,
        "timestamp": payload["timestamp"],
        "asset_id": payload["asset_id"],
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": best_ask - best_bid,
    }

def _log_event(payload: dict[str, Any], logger: logging.Logger, print_raw: bool) -> None:
    logger.info(
        "[best_bid_ask] recv_utc=%s msg_ts_ms=%s asset_id=%s best_bid=%s best_ask=%s spread=%s",
        datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        _event_timestamp_ms(payload),
        payload["asset_id"],
        payload["best_bid"],
        payload["best_ask"],
        payload["spread"],
    )

    if print_raw:
        logger.info("[raw] %s", orjson.dumps(payload).decode("utf-8"))


async def _heartbeat(ws: Any, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        await ws.send("PING")


def _load_market_session(
    gamma_base: str,
    hour_offsets: Iterable[int],
    schedule_lead_seconds: float,
) -> MarketSession:
    markets = fetch_1h_btc_markets(gamma_base=gamma_base, hour_offsets=hour_offsets)
    selected = select_active_or_next(markets, schedule_lead_seconds=schedule_lead_seconds)
    if selected is None:
        raise RuntimeError("No active/upcoming BTC 1h market found in lookup window")
    return selected


async def _run_once(
    *,
    ws_url: str,
    session: MarketSession,
    custom_feature_enabled: bool,
    ping_interval_s: float,
    logger: logging.Logger,
    print_raw: bool,
    csv_appender: MonitorCsvAppender,
) -> None:
    subscribe_message: dict[str, Any] = {
        "assets_ids": session.token_ids,
        "type": "market",
    }
    if custom_feature_enabled:
        subscribe_message["custom_feature_enabled"] = True

    async with websockets.connect(
        ws_url,
        ping_interval=None,
        ping_timeout=None,
        max_queue=None,
    ) as ws:
        logger.info(
            "[WS] connected slug=%s condition_id=%s tokens=%s",
            session.slug,
            session.condition_id,
            session.token_ids,
        )
        await ws.send(orjson.dumps(subscribe_message).decode("utf-8"))
        logger.info("[WS] subscribed payload=%s", subscribe_message)

        heartbeat_task = asyncio.create_task(_heartbeat(ws, interval_s=ping_interval_s))
        bootstrap_pending = set(session.token_ids)
        try:
            async for message in ws:
                events = orjson.loads(message)
                for payload in events:
                    event_type = payload["event_type"]
                    if event_type == RUNTIME_EVENT_TYPE:
                        csv_appender.append_event(payload)
                        _log_event(payload=payload, logger=logger, print_raw=print_raw)
                        continue
                    if event_type != BOOTSTRAP_EVENT_TYPE:
                        continue
                    asset_id = payload["asset_id"]
                    if asset_id not in bootstrap_pending:
                        continue
                    snapshot_event = _best_bid_ask_from_book(payload)
                    csv_appender.append_event(snapshot_event)
                    _log_event(payload=snapshot_event, logger=logger, print_raw=print_raw)
                    bootstrap_pending.remove(asset_id)
                    if not bootstrap_pending:
                        logger.info("[BOOTSTRAP] initial best_bid_ask seeded from book snapshots")
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)


async def run_monitor(
    *,
    gamma_base: str,
    ws_base: str,
    channel: str,
    hour_offsets: list[int],
    schedule_lead_seconds: float,
    custom_feature_enabled: bool,
    ping_interval_s: float,
    reconnect_delay_s: float,
    print_raw: bool,
    csv_path: str,
) -> None:
    logger = _logger()
    csv_appender = MonitorCsvAppender(csv_path)
    base = ws_base.rstrip("/")
    ws_url = f"{base}/{channel}" if not base.endswith(f"/{channel}") else base

    logger.info(
        "[BOOT] gamma=%s ws_url=%s runtime_event=%s bootstrap_event=%s",
        gamma_base,
        ws_url,
        RUNTIME_EVENT_TYPE,
        BOOTSTRAP_EVENT_TYPE,
    )
    logger.info("[CSV] append_path=%s", csv_path)

    try:
        while True:
            session = _load_market_session(
                gamma_base=gamma_base,
                hour_offsets=hour_offsets,
                schedule_lead_seconds=schedule_lead_seconds,
            )
            logger.info(
                "[MARKET] selected slug=%s open=%s close=%s up=%s down=%s",
                session.slug,
                session.open_time_iso_utc,
                session.close_time_iso_utc,
                session.up_token_id,
                session.down_token_id,
            )
            try:
                await _run_once(
                    ws_url=ws_url,
                    session=session,
                    custom_feature_enabled=custom_feature_enabled,
                    ping_interval_s=ping_interval_s,
                    logger=logger,
                    print_raw=print_raw,
                    csv_appender=csv_appender,
                )
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, WebSocketException, OSError, orjson.JSONDecodeError) as exc:
                logger.warning("[WS] error=%s reconnect_in_s=%.2f", exc, reconnect_delay_s)
                await asyncio.sleep(reconnect_delay_s)
    finally:
        csv_appender.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor BTC 1h Polymarket best_bid_ask with one-time book bootstrap snapshots.",
    )
    parser.add_argument("--gamma-base", default=DEFAULT_GAMMA_BASE)
    parser.add_argument("--ws-base", default=DEFAULT_WS_BASE)
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--hour-offsets", default="-2,-1,0,1,2,3,4")
    parser.add_argument("--schedule-lead-seconds", type=float, default=3600.0)
    parser.add_argument("--ping-interval", type=float, default=10.0)
    parser.add_argument("--reconnect-delay", type=float, default=1.0)
    parser.add_argument("--csv-path", default=DEFAULT_CSV_PATH)
    parser.add_argument("--disable-custom-features", action="store_true", help="Disable custom_feature_enabled (best_bid_ask will not be emitted).")
    parser.add_argument("--print-raw", action="store_true", help="Also print compact raw JSON payload for watched event types.")
    return parser.parse_args()

def main() -> None:
    args = parse_args()
    asyncio.run(
        run_monitor(
            gamma_base=args.gamma_base,
            ws_base=args.ws_base,
            channel=args.channel,
            hour_offsets=_parse_offsets(args.hour_offsets),
            schedule_lead_seconds=args.schedule_lead_seconds,
            custom_feature_enabled=not bool(args.disable_custom_features),
            ping_interval_s=args.ping_interval,
            reconnect_delay_s=args.reconnect_delay,
            print_raw=bool(args.print_raw),
            csv_path=args.csv_path,
        )
    )


if __name__ == "__main__":
    main()
