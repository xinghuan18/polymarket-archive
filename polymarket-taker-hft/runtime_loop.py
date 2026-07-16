from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from event_log import EventLogger
from feeds import BinanceL1Feed, PolyBook, PolymarketBookFeed, fetch_binance_1h_open_price
from market_selector import MarketSession, fetch_1h_btc_markets, select_active_or_next
from mock_exec import MockExecutor
from runtime_helpers import iv_ref_price_for_binance_tick, select_subscription_tokens
from runtime_tick import evaluate_and_log_tick
from strategy import TakerStrategy


async def _bootstrap_anchor_price(
    *,
    strategy: TakerStrategy,
    session: MarketSession,
    now_utc: datetime,
    binance_symbol: str,
    binance_rest: str,
    logger: logging.Logger,
) -> None:
    if strategy.state.anchor_price is not None or now_utc < session.open_dt:
        return

    anchor_price = await asyncio.to_thread(
        fetch_binance_1h_open_price,
        binance_symbol,
        session.open_dt,
        binance_rest,
    )

    if anchor_price is None:
        logger.warning(
            "[ANCHOR] unavailable symbol=%s open=%s source=binance_1h_open",
            binance_symbol,
            session.open_dt.isoformat(),
        )
        return

    strategy.state.anchor_price = anchor_price
    logger.info(
        "[ANCHOR] source=binance_1h_open symbol=%s open=%s anchor_price=%.2f",
        binance_symbol,
        session.open_dt.isoformat(),
        anchor_price,
    )


async def _fetch_market_selection(
    *,
    market_cfg: Dict[str, Any],
    now_utc: datetime,
) -> tuple[Optional[MarketSession], list[str]]:
    schedule_lead_seconds = market_cfg["schedule_lead_seconds"]
    markets = await asyncio.to_thread(
        fetch_1h_btc_markets,
        market_cfg["gamma_base"],
    )
    selected = select_active_or_next(markets, now_utc, schedule_lead_seconds)
    tokens = select_subscription_tokens(
        markets=markets,
        selected=selected,
        now_utc=now_utc,
        schedule_lead_seconds=schedule_lead_seconds,
    )
    return selected, tokens


async def _refresh_market_state(
    *,
    strategy: TakerStrategy,
    binance_feed: BinanceL1Feed,
    poly_feed: PolymarketBookFeed,
    market_cfg: Dict[str, Any],
    current_session: Optional[MarketSession],
    last_poly_book: Optional[PolyBook],
    now_utc: datetime,
    binance_symbol: str,
    binance_rest: str,
    logger: logging.Logger,
) -> tuple[Optional[MarketSession], Optional[PolyBook], bool]:
    selected, subscription_tokens = await _fetch_market_selection(
        market_cfg=market_cfg,
        now_utc=now_utc,
    )
    market_switched = False

    if selected is None:
        if current_session is not None:
            logger.info("[MARKET] no active/upcoming session; resetting state")
            market_switched = True
        current_session = None
    elif current_session is None or selected.condition_id != current_session.condition_id:
        market_switched = True
        current_session = selected
        logger.info(
            "[MARKET] selected slug=%s condition_id=%s open=%s close=%s",
            selected.slug,
            selected.condition_id,
            selected.open_dt.isoformat(),
            selected.close_dt.isoformat(),
        )

    if market_switched:
        strategy.reset_for_market()
        binance_feed.reset_cache()
        last_poly_book = None

    if current_session is not None:
        await _bootstrap_anchor_price(
            strategy=strategy,
            session=current_session,
            now_utc=now_utc,
            binance_symbol=binance_symbol,
            binance_rest=binance_rest,
            logger=logger,
        )

    await poly_feed.update_tokens(subscription_tokens)
    if current_session is not None and last_poly_book is None:
        last_poly_book = poly_feed.build_poly_book(current_session)

    return current_session, last_poly_book, market_switched


def _drain_queue(queue: asyncio.Queue) -> bool:
    drained = False
    while True:
        with suppress(asyncio.QueueEmpty):
            queue.get_nowait()
            drained = True
            continue
        return drained


def _sync_latest_poly_book(
    *,
    poly_feed: PolymarketBookFeed,
    current_session: Optional[MarketSession],
    last_poly_book: Optional[PolyBook],
) -> Optional[PolyBook]:
    if current_session is None:
        return last_poly_book
    return poly_feed.build_poly_book(current_session)


async def _shutdown(
    *,
    stop_event: asyncio.Event,
    feed_tasks: set[asyncio.Task],
    order_tasks: set[asyncio.Task],
    event_logger: EventLogger,
    logger: logging.Logger,
) -> None:
    stop_event.set()
    for task in feed_tasks:
        task.cancel()
    for task in order_tasks:
        task.cancel()

    await asyncio.gather(*feed_tasks, return_exceptions=True)
    if order_tasks:
        await asyncio.gather(*order_tasks, return_exceptions=True)

    event_logger.close()
    logger.info("[SHUTDOWN] stopped")


async def run_runtime_loop(
    *,
    strategy: TakerStrategy,
    executor: MockExecutor,
    event_logger: EventLogger,
    binance_feed: BinanceL1Feed,
    poly_feed: PolymarketBookFeed,
    runtime_cfg: Dict[str, Any],
    market_cfg: Dict[str, Any],
    binance_symbol: str,
    binance_rest: str,
    logger: logging.Logger,
) -> None:
    stop_event = asyncio.Event()
    feed_tasks = {
        asyncio.create_task(binance_feed.run(stop_event)),
        asyncio.create_task(poly_feed.run(stop_event)),
    }
    order_tasks: set[asyncio.Task] = set()

    current_session: Optional[MarketSession] = None
    last_poly_book: Optional[PolyBook] = None
    last_binance_quote = binance_feed.snapshot()
    next_rollover_scan = 0.0
    rollover_poll_seconds = runtime_cfg["rollover_poll_seconds"]

    try:
        while True:
            now_utc = datetime.now(timezone.utc)
            should_refresh = (
                time.monotonic() >= next_rollover_scan
                or current_session is None
                or now_utc >= current_session.close_dt
            )
            if should_refresh:
                current_session, last_poly_book, market_switched = await _refresh_market_state(
                    strategy=strategy,
                    binance_feed=binance_feed,
                    poly_feed=poly_feed,
                    market_cfg=market_cfg,
                    current_session=current_session,
                    last_poly_book=last_poly_book,
                    now_utc=now_utc,
                    binance_symbol=binance_symbol,
                    binance_rest=binance_rest,
                    logger=logger,
                )
                if market_switched:
                    last_binance_quote = None
                next_rollover_scan = time.monotonic() + rollover_poll_seconds

            poly_changed = _drain_queue(poly_feed.queue)
            if poly_changed:
                last_poly_book = _sync_latest_poly_book(
                    poly_feed=poly_feed,
                    current_session=current_session,
                    last_poly_book=last_poly_book,
                )
                if last_binance_quote is not None:
                    await evaluate_and_log_tick(
                        strategy=strategy,
                        executor=executor,
                        event_logger=event_logger,
                        poly_feed=poly_feed,
                        order_tasks=order_tasks,
                        logger=logger,
                        current_session=current_session,
                        last_poly_book=last_poly_book,
                        eval_quote=last_binance_quote,
                        event_source="poly",
                        allow_order_submission=False,
                    )

            binance_quote = None
            try:
                binance_quote = await asyncio.wait_for(binance_feed.queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                binance_quote = None

            if binance_quote is not None:
                prev_binance_quote = last_binance_quote
                last_binance_quote = binance_quote
                iv_ref_price = iv_ref_price_for_binance_tick(binance_quote, prev_binance_quote)
                await evaluate_and_log_tick(
                    strategy=strategy,
                    executor=executor,
                    event_logger=event_logger,
                    poly_feed=poly_feed,
                    order_tasks=order_tasks,
                    logger=logger,
                    current_session=current_session,
                    last_poly_book=last_poly_book,
                    eval_quote=binance_quote,
                    event_source="binance",
                    allow_order_submission=True,
                    iv_ref_price=iv_ref_price,
                )
    finally:
        await _shutdown(
            stop_event=stop_event,
            feed_tasks=feed_tasks,
            order_tasks=order_tasks,
            event_logger=event_logger,
            logger=logger,
        )
