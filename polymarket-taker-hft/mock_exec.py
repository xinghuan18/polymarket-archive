from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from event_log import EventLogger
from strategy import MockOrder


@dataclass
class MockOrderResult:
    order_id: str
    order_status: str
    mock_fill_price: Optional[float]


class MockExecutor:
    def __init__(
        self,
        event_logger: EventLogger,
        mock_latency_ms: int = 100,
        logger: Optional[logging.Logger] = None,
    ):
        self._event_logger = event_logger
        self._mock_latency_ms = mock_latency_ms
        self._logger = logger or logging.getLogger("taker_hft")

    async def submit_ioc(
        self,
        order: MockOrder,
        poly_snapshot_getter: Callable[[str], Optional[float]],
        market_close_ts: datetime,
    ) -> MockOrderResult:
        self._logger.info(
            "[ORDER_SUBMIT] id=%s market=%s token_side=%s side=%s price=%.6f size=%.6f latency_ms=%s",
            order.order_id,
            order.market_slug,
            order.token_side,
            order.side,
            order.price,
            order.size,
            self._mock_latency_ms,
        )

        await self._event_logger.write_event(
            "ORDER_SUBMIT",
            event_source="executor",
            market_slug=order.market_slug,
            condition_id=order.condition_id,
            token_side=order.token_side,
            token_id=order.token_id,
            order_id=order.order_id,
            order_price=order.price,
            order_size=order.size,
            mock_latency_ms=self._mock_latency_ms,
            order_status="SUBMITTED",
            note="ioc_submit",
        )

        await asyncio.sleep(self._mock_latency_ms / 1000.0)

        now_utc = datetime.now(timezone.utc)
        status = "UNFILLED"
        fill_price: Optional[float] = None

        if now_utc >= market_close_ts:
            status = "CANCELED_MARKET_CLOSED"
        else:
            ask = poly_snapshot_getter(order.token_id)
            if ask is not None and ask <= order.price:
                status = "FILLED"
                fill_price = ask

        await self._event_logger.write_event(
            "ORDER_RESULT",
            event_source="executor",
            market_slug=order.market_slug,
            condition_id=order.condition_id,
            token_side=order.token_side,
            token_id=order.token_id,
            order_id=order.order_id,
            order_price=order.price,
            order_size=order.size,
            mock_latency_ms=self._mock_latency_ms,
            mock_fill_price=fill_price,
            order_status=status,
            note="ioc_done",
        )

        self._logger.info(
            "[ORDER_RESULT] id=%s status=%s fill_price=%s",
            order.order_id,
            status,
            "NA" if fill_price is None else f"{fill_price:.6f}",
        )

        return MockOrderResult(
            order_id=order.order_id,
            order_status=status,
            mock_fill_price=fill_price,
        )
