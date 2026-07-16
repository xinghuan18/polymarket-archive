from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import orjson
import pytest

from feeds import BinanceL1Feed, PolymarketBookFeed, fetch_binance_1h_open_price


def test_poly_update_dedup_when_price_unchanged() -> None:
    feed = PolymarketBookFeed(
        ws_base="wss://ws-subscriptions-clob.polymarket.com/ws",
        channel="market",
        emit_on_price_change_only=True,
    )
    feed._update_token("T1", 0.50, 0.51)
    feed._update_token("T1", 0.50, 0.51)
    feed._update_token("T1", 0.50, 0.52)
    assert feed.queue.qsize() == 2


def test_poly_update_no_dedup_when_disabled() -> None:
    feed = PolymarketBookFeed(
        ws_base="wss://ws-subscriptions-clob.polymarket.com/ws",
        channel="market",
        emit_on_price_change_only=False,
    )
    feed._update_token("T1", 0.50, 0.51)
    feed._update_token("T1", 0.50, 0.51)
    assert feed.queue.qsize() == 2


def test_poly_update_tokens_unchanged_does_not_resubscribe() -> None:
    feed = PolymarketBookFeed(
        ws_base="wss://ws-subscriptions-clob.polymarket.com/ws",
        channel="market",
    )
    feed._ws = Mock(closed=False)
    feed._ws.send_json = AsyncMock()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(feed.update_tokens(["T1", "T2"]))
    loop.run_until_complete(feed.update_tokens(["T2", "T1"]))

    assert feed._ws.send_json.await_count == 1


def test_poly_update_tokens_resubscribe_delta_unsubscribe_then_subscribe() -> None:
    feed = PolymarketBookFeed(
        ws_base="wss://ws-subscriptions-clob.polymarket.com/ws",
        channel="market",
    )
    feed._ws = Mock(closed=False)
    feed._ws.send_json = AsyncMock()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(feed.update_tokens(["T1", "T2"]))
    loop.run_until_complete(feed.update_tokens(["T2", "T3"]))

    sent = [call.kwargs["data"] if "data" in call.kwargs else call.args[0] for call in feed._ws.send_json.await_args_list]
    assert sent[0] == {"assets_ids": ["T1", "T2"], "type": "market", "operation": "subscribe"}
    assert sent[1] == {"assets_ids": ["T1"], "type": "market", "operation": "unsubscribe"}
    assert sent[2] == {"assets_ids": ["T3"], "type": "market", "operation": "subscribe"}


def test_poly_handle_message_non_json_control_frame_raises() -> None:
    feed = PolymarketBookFeed(
        ws_base="wss://ws-subscriptions-clob.polymarket.com/ws",
        channel="market",
    )

    with pytest.raises(orjson.JSONDecodeError):
        feed._handle_message("INVALID OPERATION")


def test_poly_handle_message_accepts_list_payload() -> None:
    feed = PolymarketBookFeed(
        ws_base="wss://ws-subscriptions-clob.polymarket.com/ws",
        channel="market",
    )

    feed._handle_message(
        orjson.dumps(
            [
                {
                    "event_type": "book",
                    "asset_id": "T1",
                    "bids": [{"price": "0.50"}],
                    "asks": [{"price": "0.51"}],
                }
            ]
        ).decode("utf-8")
    )

    assert feed.get_best_bid("T1") == 0.50
    assert feed.get_best_ask("T1") == 0.51
    assert feed.queue.qsize() == 1


def test_fetch_binance_1h_open_price_success() -> None:
    open_dt = datetime(2026, 2, 25, 5, 0, 0, tzinfo=timezone.utc)
    start_ms = int(open_dt.timestamp() * 1000)
    response = Mock()
    response.json.return_value = [[start_ms, "88650.12"]]
    response.raise_for_status.return_value = None

    with patch("feeds.requests.get", return_value=response) as mock_get:
        px = fetch_binance_1h_open_price("BTCUSDT", open_dt)

    assert px == 88650.12
    mock_get.assert_called_once()


def test_fetch_binance_1h_open_price_requires_matching_candle_open_time() -> None:
    open_dt = datetime(2026, 2, 25, 5, 0, 0, tzinfo=timezone.utc)
    start_ms = int(open_dt.timestamp() * 1000)
    response = Mock()
    response.json.return_value = [[start_ms + 3600000, "88650.12"]]
    response.raise_for_status.return_value = None

    with patch("feeds.requests.get", return_value=response):
        px = fetch_binance_1h_open_price("BTCUSDT", open_dt)

    assert px is None


def test_binance_reset_cache_clears_snapshot_and_queue() -> None:
    feed = BinanceL1Feed(symbol="BTCUSDT", ws_base="wss://stream.binance.com:9443/ws")
    feed._last_quote = Mock()
    feed.queue.put_nowait(Mock())

    feed.reset_cache()

    assert feed.snapshot() is None
    assert feed.queue.qsize() == 0


def test_poly_reset_cache_clears_quotes_tokens_and_queue() -> None:
    feed = PolymarketBookFeed(
        ws_base="wss://ws-subscriptions-clob.polymarket.com/ws",
        channel="market",
    )
    feed._update_token("T1", 0.5, 0.51)
    feed._desired_tokens = {"T1", "T2"}

    feed.reset_cache()

    assert feed.get_best_bid("T1") is None
    assert feed.get_best_ask("T1") is None
    assert feed._desired_tokens == set()
    assert feed.queue.qsize() == 0
