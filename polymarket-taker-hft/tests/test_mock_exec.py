from __future__ import annotations

import csv
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_log import EventLogger
from mock_exec import MockExecutor
from strategy import MockOrder


class TestMockExec(unittest.IsolatedAsyncioTestCase):
    async def test_fill_when_ask_le_order_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.csv"
            logger = EventLogger(path)
            executor = MockExecutor(logger, mock_latency_ms=1)

            order = MockOrder(
                order_id="o1",
                market_slug="m",
                condition_id="c",
                token_side="UP",
                token_id="T1",
                side="BUY",
                price=0.99,
                size=2,
            )

            result = await executor.submit_ioc(
                order=order,
                poly_snapshot_getter=lambda _tid: 0.98,
                market_close_ts=datetime.now(timezone.utc) + timedelta(seconds=5),
            )
            logger.close()

            self.assertEqual(result.order_status, "FILLED")
            self.assertEqual(result.mock_fill_price, 0.98)
            self.assertTrue(os.path.exists(path))

    async def test_unfilled_when_ask_above_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.csv"
            logger = EventLogger(path)
            executor = MockExecutor(logger, mock_latency_ms=1)

            order = MockOrder(
                order_id="o2",
                market_slug="m",
                condition_id="c",
                token_side="DOWN",
                token_id="T2",
                side="BUY",
                price=0.99,
                size=2,
            )

            result = await executor.submit_ioc(
                order=order,
                poly_snapshot_getter=lambda _tid: 0.995,
                market_close_ts=datetime.now(timezone.utc) + timedelta(seconds=5),
            )
            logger.close()

            self.assertEqual(result.order_status, "UNFILLED")
            self.assertIsNone(result.mock_fill_price)

            with path.open("r", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[-1]["event_type"], "ORDER_RESULT")

    async def test_market_close_before_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.csv"
            logger = EventLogger(path)
            executor = MockExecutor(logger, mock_latency_ms=1)

            order = MockOrder(
                order_id="o3",
                market_slug="m",
                condition_id="c",
                token_side="UP",
                token_id="T1",
                side="BUY",
                price=0.99,
                size=2,
            )

            result = await executor.submit_ioc(
                order=order,
                poly_snapshot_getter=lambda _tid: 0.50,
                market_close_ts=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
            logger.close()

            self.assertEqual(result.order_status, "CANCELED_MARKET_CLOSED")
            self.assertIsNone(result.mock_fill_price)


if __name__ == "__main__":
    unittest.main()
