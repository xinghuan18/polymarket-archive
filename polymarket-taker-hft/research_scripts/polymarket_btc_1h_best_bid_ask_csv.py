from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
import sys

import orjson
from websockets.exceptions import ConnectionClosed, WebSocketException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_selector import MarketSession, fetch_1h_btc_markets, select_active_or_next
from polymarket_up_l1_core import CsvLogger
from polymarket_up_l1_ws import run_ws_once

DEFAULT_GAMMA_BASE = "https://gamma-api.polymarket.com"
DEFAULT_WS_BASE = "wss://ws-subscriptions-clob.polymarket.com/ws"
DEFAULT_CHANNEL = "market"
DEFAULT_CSV_PATH = str(Path(__file__).resolve().parent / "polymarket_up_l1_events.csv")


def _logger() -> logging.Logger:
    logger = logging.getLogger("polymarket_btc_1h_up_l1_logger")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
    return logger


def _parse_offsets(raw: str) -> list[int]:
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def _load_market_session(
    gamma_base: str,
    hour_offsets: list[int],
    schedule_lead_seconds: float,
) -> MarketSession:
    markets = fetch_1h_btc_markets(gamma_base=gamma_base, hour_offsets=hour_offsets)
    selected = select_active_or_next(markets, schedule_lead_seconds=schedule_lead_seconds)
    if selected is None:
        raise RuntimeError("No active/upcoming BTC 1h market found in lookup window")
    return selected


async def run_monitor(
    *,
    gamma_base: str,
    ws_base: str,
    channel: str,
    hour_offsets: list[int],
    schedule_lead_seconds: float,
    ping_interval_s: float,
    reconnect_delay_s: float,
    csv_path: str,
) -> None:
    logger = _logger()
    base = ws_base.rstrip("/")
    ws_url = f"{base}/{channel}" if not base.endswith(f"/{channel}") else base

    logger.info("[BOOT] gamma=%s ws_url=%s", gamma_base, ws_url)
    logger.info("[CSV] write_path=%s", csv_path)
    csv_logger = CsvLogger(csv_path)
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
                await run_ws_once(
                    ws_url=ws_url,
                    session=session,
                    ping_interval_s=ping_interval_s,
                    logger=logger,
                    csv_logger=csv_logger,
                )
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, WebSocketException, OSError, orjson.JSONDecodeError) as exc:
                logger.warning("[WS] error=%s reconnect_in_s=%.2f", exc, reconnect_delay_s)
                await asyncio.sleep(reconnect_delay_s)
    finally:
        csv_logger.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Log UP-token bid/ask price and quantity from best_bid_ask, book, and price_change; "
            "includes best_bid_ask change label."
        ),
    )
    parser.add_argument("--gamma-base", default=DEFAULT_GAMMA_BASE)
    parser.add_argument("--ws-base", default=DEFAULT_WS_BASE)
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--hour-offsets", default="-2,-1,0,1,2,3,4")
    parser.add_argument("--schedule-lead-seconds", type=float, default=3600.0)
    parser.add_argument("--ping-interval", type=float, default=10.0)
    parser.add_argument("--reconnect-delay", type=float, default=1.0)
    parser.add_argument("--csv-path", default=DEFAULT_CSV_PATH)
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
            ping_interval_s=args.ping_interval,
            reconnect_delay_s=args.reconnect_delay,
            csv_path=args.csv_path,
        )
    )


if __name__ == "__main__":
    main()
