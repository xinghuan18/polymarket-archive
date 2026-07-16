from __future__ import annotations

import asyncio
import logging
from typing import Any

import orjson
import websockets

from market_selector import MarketSession
from polymarket_up_l1_core import CsvLogger, TopOfBook, parse_frames


def _best_bid_ask_change(
    prev_bid: float | None,
    prev_ask: float | None,
    bid: float,
    ask: float,
) -> str:
    if prev_bid is None or prev_ask is None:
        return "no_change"
    bid_changed = bid != prev_bid
    ask_changed = ask != prev_ask
    if bid_changed and ask_changed:
        return "both_change"
    if bid_changed:
        return "bid_change"
    if ask_changed:
        return "ask_change"
    return "no_change"


def _up_only_price_change(payload: dict[str, Any], up_token_id: str) -> dict[str, Any] | None:
    up_changes = [change for change in payload["price_changes"] if change["asset_id"] == up_token_id]
    if not up_changes:
        return None
    return {
        "event_type": "price_change",
        "timestamp": payload["timestamp"],
        "price_changes": up_changes,
    }


def _event_l1(top: TopOfBook, payload: dict[str, Any]) -> tuple[float | None, float | None, float | None, float | None]:
    event_type = payload["event_type"]
    if event_type == "book":
        return top.apply_book(payload)
    if event_type == "price_change":
        return top.apply_price_change(payload)
    return top.from_best_bid_ask(payload)


def _log_row(logger: logging.Logger, row: dict[str, Any]) -> None:
    if row["event_type"] == "last_trade_price":
        logger.info(
            "[%s] recv_utc=%s msg_ts_ms=%s bid=%s bid_qty=%s ask=%s ask_qty=%s trade_price=%s trade_size=%s side=%s tx=%s",
            row["event_type"],
            row["recv_iso_utc"],
            row["msg_ts_ms"],
            row["bid_price"],
            row["bid_qty"],
            row["ask_price"],
            row["ask_qty"],
            row["trade_price"],
            row["trade_size"],
            row["trade_side"],
            row["trade_tx_hash"],
        )
        return
    if row["event_type"] == "best_bid_ask":
        logger.info(
            "[%s] recv_utc=%s msg_ts_ms=%s bid=%s bid_qty=%s ask=%s ask_qty=%s change=%s",
            row["event_type"],
            row["recv_iso_utc"],
            row["msg_ts_ms"],
            row["bid_price"],
            row["bid_qty"],
            row["ask_price"],
            row["ask_qty"],
            row["change"],
        )
        return
    logger.info(
        "[%s] recv_utc=%s msg_ts_ms=%s bid=%s bid_qty=%s ask=%s ask_qty=%s",
        row["event_type"],
        row["recv_iso_utc"],
        row["msg_ts_ms"],
        row["bid_price"],
        row["bid_qty"],
        row["ask_price"],
        row["ask_qty"],
    )


async def _heartbeat(ws: Any, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        await ws.send("PING")


async def run_ws_once(
    *,
    ws_url: str,
    session: MarketSession,
    ping_interval_s: float,
    logger: logging.Logger,
    csv_logger: CsvLogger,
) -> None:
    subscribe_message = {
        "assets_ids": [session.up_token_id],
        "type": "market",
        "custom_feature_enabled": True,
    }
    top = TopOfBook()
    prev_bba_bid: float | None = None
    prev_bba_ask: float | None = None
    pending_bba: dict[str, Any] | None = None

    def _flush_pending(force: bool) -> None:
        nonlocal pending_bba
        if pending_bba is None:
            return
        bid_qty, ask_qty = top.qty_for_prices(
            pending_bba["bid_price"],
            pending_bba["ask_price"],
        )
        if bid_qty is not None:
            pending_bba["bid_qty"] = bid_qty
        if ask_qty is not None:
            pending_bba["ask_qty"] = ask_qty
        if not force and (pending_bba["bid_qty"] is None or pending_bba["ask_qty"] is None):
            return
        row = csv_logger.append(
            event_type="best_bid_ask",
            msg_ts_ms=pending_bba["msg_ts_ms"],
            bid_price=pending_bba["bid_price"],
            bid_qty=pending_bba["bid_qty"],
            ask_price=pending_bba["ask_price"],
            ask_qty=pending_bba["ask_qty"],
            change=pending_bba["change"],
        )
        _log_row(logger, row)
        pending_bba = None

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
            [session.up_token_id],
        )
        await ws.send(orjson.dumps(subscribe_message).decode("utf-8"))
        logger.info("[WS] subscribed payload=%s", subscribe_message)

        heartbeat_task = asyncio.create_task(_heartbeat(ws, interval_s=ping_interval_s))
        try:
            async for message in ws:
                raw = message.decode("utf-8") if type(message) is bytes else message
                for payload in parse_frames(raw):
                    event_type = payload["event_type"]
                    if event_type not in {"best_bid_ask", "book", "price_change", "last_trade_price"}:
                        continue
                    payload_up = payload
                    if event_type == "price_change":
                        payload_up = _up_only_price_change(payload, session.up_token_id)
                        if payload_up is None:
                            continue
                    elif payload["asset_id"] != session.up_token_id:
                        continue

                    if event_type == "best_bid_ask":
                        _flush_pending(force=True)
                        bid_price, bid_qty, ask_price, ask_qty = _event_l1(top, payload_up)
                        change = _best_bid_ask_change(prev_bba_bid, prev_bba_ask, bid_price, ask_price)
                        prev_bba_bid, prev_bba_ask = bid_price, ask_price
                        if bid_qty is None or ask_qty is None:
                            pending_bba = {
                                "msg_ts_ms": int(payload_up["timestamp"]),
                                "bid_price": bid_price,
                                "bid_qty": bid_qty,
                                "ask_price": ask_price,
                                "ask_qty": ask_qty,
                                "change": change,
                            }
                        else:
                            row = csv_logger.append(
                                event_type="best_bid_ask",
                                msg_ts_ms=int(payload_up["timestamp"]),
                                bid_price=bid_price,
                                bid_qty=bid_qty,
                                ask_price=ask_price,
                                ask_qty=ask_qty,
                                change=change,
                            )
                            _log_row(logger, row)
                        continue

                    if event_type == "last_trade_price":
                        bid_price, bid_qty, ask_price, ask_qty = top.snapshot()
                        row = csv_logger.append(
                            event_type="last_trade_price",
                            msg_ts_ms=int(payload_up["timestamp"]),
                            bid_price=bid_price,
                            bid_qty=bid_qty,
                            ask_price=ask_price,
                            ask_qty=ask_qty,
                            change="",
                            trade_price=float(payload_up["price"]),
                            trade_size=float(payload_up["size"]),
                            trade_side=payload_up["side"],
                            trade_tx_hash=payload_up["transaction_hash"],
                        )
                        _log_row(logger, row)
                        _flush_pending(force=False)
                        continue

                    bid_price, bid_qty, ask_price, ask_qty = _event_l1(top, payload_up)
                    row = csv_logger.append(
                        event_type=payload_up["event_type"],
                        msg_ts_ms=int(payload_up["timestamp"]),
                        bid_price=bid_price,
                        bid_qty=bid_qty,
                        ask_price=ask_price,
                        ask_qty=ask_qty,
                        change="",
                    )
                    _log_row(logger, row)
                    _flush_pending(force=False)
        finally:
            _flush_pending(force=True)
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
