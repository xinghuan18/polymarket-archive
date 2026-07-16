from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from event_log import EventLogger
from feeds import PolyBook, PolymarketBookFeed
from market_selector import MarketSession
from mock_exec import MockExecutor
from runtime_helpers import fmt_float, iv_per_year
from strategy import StrategyEvaluation, TakerStrategy


def _track_task(task_set: set[asyncio.Task], task: asyncio.Task) -> None:
    task_set.add(task)

    def drop_done(done_task: asyncio.Task) -> None:
        task_set.discard(done_task)

    task.add_done_callback(drop_done)


def _base_event_fields(
    *,
    session: MarketSession,
    poly: PolyBook,
    quote: Any,
    evaluation: StrategyEvaluation,
    iv_year: Optional[float],
    target_price: Optional[float],
) -> dict[str, Any]:
    signal = evaluation.signal_state
    return {
        "market_slug": session.slug,
        "condition_id": session.condition_id,
        "binance_bid": quote.bid,
        "binance_ask": quote.ask,
        "binance_spread": evaluation.spread,
        "binance_ref_price": evaluation.binance_ref_price,
        "poly_up_bid": poly.up_bid,
        "poly_up_ask": poly.up_ask,
        "poly_down_bid": poly.down_bid,
        "poly_down_ask": poly.down_ask,
        "poly_p_up": signal.p_poly_up,
        "target_price": target_price,
        "p_model_up": signal.p_model_up,
        "delta_p": signal.delta,
        "kept_iv_per_s": signal.kept_iv_per_s,
        "iv_year": iv_year,
        "horizon_s": evaluation.horizon_s,
        "up_trigger_ref": evaluation.up_trigger_ref,
        "down_trigger_ref": evaluation.down_trigger_ref,
        "note": evaluation.note,
    }


def _build_trigger_reason(evaluation: StrategyEvaluation, quote: Any) -> str:
    if evaluation.note == "trigger_up" and evaluation.up_trigger_ref is not None:
        return (
            f"binance_bid({fmt_float(quote.bid, decimals=2)}) > "
            f"up_trigger_ref({fmt_float(evaluation.up_trigger_ref, decimals=2)})"
        )
    if evaluation.note == "trigger_down" and evaluation.down_trigger_ref is not None:
        return (
            f"binance_bid({fmt_float(quote.bid, decimals=2)}) < "
            f"down_trigger_ref({fmt_float(evaluation.down_trigger_ref, decimals=2)})"
        )
    return evaluation.note


def _order_fields(evaluation: StrategyEvaluation, trigger_reason: str) -> dict[str, Any]:
    if evaluation.order is None:
        return {}
    return {
        "token_side": evaluation.order.token_side,
        "token_id": evaluation.order.token_id,
        "trigger_reason": trigger_reason,
        "order_id": evaluation.order.order_id,
        "order_price": evaluation.order.price,
        "order_size": evaluation.order.size,
    }


async def evaluate_and_log_tick(
    *,
    strategy: TakerStrategy,
    executor: MockExecutor,
    event_logger: EventLogger,
    poly_feed: PolymarketBookFeed,
    order_tasks: set[asyncio.Task],
    logger: logging.Logger,
    current_session: Optional[MarketSession],
    last_poly_book: Optional[PolyBook],
    eval_quote: Any,
    event_source: str,
    allow_order_submission: bool,
    iv_ref_price: Optional[float] = None,
) -> None:
    if current_session is None or last_poly_book is None:
        return

    now_eval = datetime.now(timezone.utc)
    if now_eval < current_session.open_dt or now_eval >= current_session.close_dt:
        return

    eval_result = strategy.evaluate(
        market=current_session,
        quote=eval_quote,
        poly=last_poly_book,
        now_utc=now_eval,
        iv_ref_price=iv_ref_price,
        allow_order_submission=allow_order_submission,
    )
    st = eval_result.signal_state
    iv_year = iv_per_year(st.kept_iv_per_s)
    target_price = st.anchor_price
    shared_fields = _base_event_fields(
        session=current_session,
        poly=last_poly_book,
        quote=eval_quote,
        evaluation=eval_result,
        iv_year=iv_year,
        target_price=target_price,
    )

    await event_logger.write_event(
        "TICK",
        event_source=event_source,
        **shared_fields,
    )

    logger.info(
        "[TICK] market=%s event_source=%s binance_bid=%s binance_ask=%s spread=%s "
        "poly_up_bid=%s poly_up_ask=%s target_price=%s "
        "iv_year=%s p_poly_up=%s p_model_up=%s delta=%s note=%s",
        current_session.slug,
        event_source,
        fmt_float(eval_quote.bid, decimals=2),
        fmt_float(eval_quote.ask, decimals=2),
        fmt_float(eval_result.spread, decimals=2),
        fmt_float(last_poly_book.up_bid),
        fmt_float(last_poly_book.up_ask),
        fmt_float(target_price, decimals=2),
        fmt_float(iv_year),
        fmt_float(st.p_poly_up),
        fmt_float(st.p_model_up),
        fmt_float(st.delta),
        eval_result.note,
    )

    if eval_result.order is None:
        return

    trigger_reason = _build_trigger_reason(eval_result, eval_quote)
    order_fields = _order_fields(eval_result, trigger_reason)

    if not allow_order_submission:
        await event_logger.write_event(
            "ORDER_SUPPRESSED",
            event_source=event_source,
            **shared_fields,
            **order_fields,
        )
        logger.info(
            "[ORDER_SUPPRESSED] market=%s event_source=%s note=%s "
            "binance_bid=%s target_price=%s p_poly_up=%s p_model_up=%s delta=%s",
            current_session.slug,
            event_source,
            eval_result.note,
            fmt_float(eval_quote.bid, decimals=2),
            fmt_float(st.anchor_price, decimals=2),
            fmt_float(st.p_poly_up),
            fmt_float(st.p_model_up),
            fmt_float(st.delta),
        )
        return

    await event_logger.write_event(
        "ORDER_TRIGGER",
        event_source=event_source,
        **shared_fields,
        **order_fields,
    )

    logger.info(
        "[ORDER_TRIGGER] id=%s market=%s event_source=%s side=%s token_side=%s price=%s size=%s "
        "reason=%s note=%s binance_bid=%s target_price=%s up_trigger_ref=%s down_trigger_ref=%s "
        "p_poly_up=%s p_model_up=%s delta=%s iv_year=%s",
        eval_result.order.order_id,
        eval_result.order.market_slug,
        event_source,
        eval_result.order.side,
        eval_result.order.token_side,
        fmt_float(eval_result.order.price),
        fmt_float(eval_result.order.size),
        trigger_reason,
        eval_result.note,
        fmt_float(eval_quote.bid, decimals=2),
        fmt_float(st.anchor_price, decimals=2),
        fmt_float(eval_result.up_trigger_ref, decimals=2),
        fmt_float(eval_result.down_trigger_ref, decimals=2),
        fmt_float(st.p_poly_up),
        fmt_float(st.p_model_up),
        fmt_float(st.delta),
        fmt_float(iv_year),
    )
    task = asyncio.create_task(
        executor.submit_ioc(
            order=eval_result.order,
            poly_snapshot_getter=poly_feed.get_best_ask,
            market_close_ts=current_session.close_dt,
        )
    )
    _track_task(order_tasks, task)
