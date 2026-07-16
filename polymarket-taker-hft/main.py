from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Tuple

import yaml

from event_log import EventLogger, default_events_csv_path
from feeds import BinanceL1Feed, PolymarketBookFeed
from mock_exec import MockExecutor
from runtime_helpers import (
    iv_ref_price_for_binance_tick as _iv_ref_price_for_binance_tick,
    select_subscription_tokens as _select_subscription_tokens,
)
from runtime_loop import run_runtime_loop
from strategy import TakerStrategy


DEFAULT_CONFIG: Dict[str, Any] = {
    "runtime": {"rollover_poll_seconds": 5},
    "market": {
        "gamma_base": "https://gamma-api.polymarket.com",
        "schedule_lead_seconds": 3600,
    },
    "feeds": {
        "binance_symbol": "BTCUSDT",
        "binance_rest": "https://api.binance.com",
        "binance_ws": "wss://stream.binance.com:9443/ws",
        "binance_emit_on_price_change_only": True,
        "poly_ws": "wss://ws-subscriptions-clob.polymarket.com/ws",
        "poly_emit_on_price_change_only": True,
        "poly_channel": "market",
        "ping_interval": 20,
        "ping_timeout": 10,
        "reconnect_delay": 1,
    },
    "strategy": {
        "df": 3,
        "delta_threshold": 0.005,
        "order_price": 0.99,
        "order_size": 2,
        "min_order_interval_ms": 250,
        "mock_latency_ms": 100,
    },
    "logging": {"events_csv": "market_data_test.csv"},
}


def _merge_dicts(base: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in update.items():
        if key in merged:
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
            continue
        merged[key] = value
    return merged


def _load_config(path: str) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    config_path = Path(path)
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as fh:
            cfg = _merge_dicts(cfg, yaml.safe_load(fh))
    return cfg


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("taker_hft")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(stream_handler)
    return logger


def _build_feeds(
    feeds_cfg: Dict[str, Any],
    logger: logging.Logger,
) -> Tuple[BinanceL1Feed, PolymarketBookFeed, str, str]:
    ping_interval = feeds_cfg["ping_interval"]
    ping_timeout = feeds_cfg["ping_timeout"]
    reconnect_delay = feeds_cfg["reconnect_delay"]
    binance_symbol = feeds_cfg["binance_symbol"]
    binance_rest = feeds_cfg["binance_rest"]

    binance_feed = BinanceL1Feed(
        symbol=binance_symbol,
        ws_base=feeds_cfg["binance_ws"],
        emit_on_price_change_only=feeds_cfg["binance_emit_on_price_change_only"],
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
        reconnect_delay=reconnect_delay,
        logger=logger,
    )
    poly_feed = PolymarketBookFeed(
        ws_base=feeds_cfg["poly_ws"],
        channel=feeds_cfg["poly_channel"],
        emit_on_price_change_only=feeds_cfg["poly_emit_on_price_change_only"],
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
        reconnect_delay=reconnect_delay,
        logger=logger,
    )
    return binance_feed, poly_feed, binance_symbol, binance_rest


def _build_strategy_executor(
    strategy_cfg: Dict[str, Any],
    event_logger: EventLogger,
    logger: logging.Logger,
) -> Tuple[TakerStrategy, MockExecutor]:
    strategy = TakerStrategy(
        df=strategy_cfg["df"],
        delta_threshold=strategy_cfg["delta_threshold"],
        order_price=strategy_cfg["order_price"],
        order_size=strategy_cfg["order_size"],
        min_order_interval_ms=strategy_cfg["min_order_interval_ms"],
    )
    executor = MockExecutor(
        event_logger=event_logger,
        mock_latency_ms=strategy_cfg["mock_latency_ms"],
        logger=logger,
    )
    return strategy, executor


def _log_init(
    *,
    logger: logging.Logger,
    config_path: str,
    events_path: Path,
    feeds_cfg: Dict[str, Any],
    strategy_cfg: Dict[str, Any],
    binance_symbol: str,
) -> None:
    logger.info("[BOOT] starting taker-hft mock bot")
    logger.info("[INIT] config=%s csv(events)=%s", config_path, events_path)
    logger.info(
        "[INIT] feed binance_symbol=%s poly_channel=%s",
        binance_symbol,
        feeds_cfg["poly_channel"],
    )
    logger.info(
        "[INIT] strategy df=%s delta_threshold=%s order_price=%s order_size=%s min_order_interval_ms=%s mock_latency_ms=%s",
        strategy_cfg["df"],
        strategy_cfg["delta_threshold"],
        strategy_cfg["order_price"],
        strategy_cfg["order_size"],
        strategy_cfg["min_order_interval_ms"],
        strategy_cfg["mock_latency_ms"],
    )


async def run(config_path: str) -> None:
    cfg = _load_config(config_path)
    logger = _build_logger()

    events_csv = cfg["logging"]["events_csv"]
    events_path = Path(events_csv) if events_csv else default_events_csv_path()
    event_logger = EventLogger(events_path)

    feeds_cfg = cfg["feeds"]
    runtime_cfg = cfg["runtime"]
    market_cfg = cfg["market"]
    strategy_cfg = cfg["strategy"]

    binance_feed, poly_feed, binance_symbol, binance_rest = _build_feeds(feeds_cfg, logger)
    strategy, executor = _build_strategy_executor(strategy_cfg, event_logger, logger)

    _log_init(
        logger=logger,
        config_path=config_path,
        events_path=events_path,
        feeds_cfg=feeds_cfg,
        strategy_cfg=strategy_cfg,
        binance_symbol=binance_symbol,
    )

    await run_runtime_loop(
        strategy=strategy,
        executor=executor,
        event_logger=event_logger,
        binance_feed=binance_feed,
        poly_feed=poly_feed,
        runtime_cfg=runtime_cfg,
        market_cfg=market_cfg,
        binance_symbol=binance_symbol,
        binance_rest=binance_rest,
        logger=logger,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket taker-only HFT mock bot")
    parser.add_argument("--config", default="config.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run(args.config))


if __name__ == "__main__":
    main()
