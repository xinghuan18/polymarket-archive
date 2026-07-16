from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from polymarket_book_feed import PolymarketBookFeed
from market_selector import MarketSession
from runtime_loop import _refresh_market_state
from strategy import TakerStrategy


def _session(
    *,
    condition_id: str,
    slug: str,
    open_dt: datetime,
    close_dt: datetime,
    up_token_id: str,
    down_token_id: str,
) -> MarketSession:
    return MarketSession(
        group="crypto_1h",
        gamma_id=f"g-{condition_id}",
        condition_id=condition_id,
        slug=slug,
        question="BTC up or down?",
        open_time_iso_utc=open_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        close_time_iso_utc=close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        open_dt=open_dt,
        close_dt=close_dt,
        up_token_id=up_token_id,
        up_outcome="Up",
        down_token_id=down_token_id,
        down_outcome="Down",
    )


def test_refresh_market_state_hydrates_poly_book_from_cached_quotes_on_switch() -> None:
    old_session = _session(
        condition_id="old",
        slug="old-market",
        open_dt=datetime(2026, 2, 28, 9, 0, 0, tzinfo=timezone.utc),
        close_dt=datetime(2026, 2, 28, 10, 0, 0, tzinfo=timezone.utc),
        up_token_id="OLD_UP",
        down_token_id="OLD_DOWN",
    )
    new_session = _session(
        condition_id="new",
        slug="new-market",
        open_dt=datetime(2026, 2, 28, 10, 0, 0, tzinfo=timezone.utc),
        close_dt=datetime(2026, 2, 28, 11, 0, 0, tzinfo=timezone.utc),
        up_token_id="NEW_UP",
        down_token_id="NEW_DOWN",
    )
    now_utc = datetime(2026, 2, 28, 10, 0, 1, tzinfo=timezone.utc)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        strategy = TakerStrategy(df=3, delta_threshold=0.005, order_price=0.99, order_size=2.0)
        binance_feed = Mock()
        binance_feed.reset_cache = Mock()

        poly_feed = PolymarketBookFeed(
            ws_base="wss://ws-subscriptions-clob.polymarket.com/ws",
            channel="market",
        )
        poly_feed.reset_cache = Mock(side_effect=AssertionError("poly_feed.reset_cache should not be called"))
        poly_feed._update_token("NEW_UP", 0.64, 0.66)
        poly_feed._update_token("NEW_DOWN", 0.34, 0.36)

        logger = Mock()
        market_cfg = {"schedule_lead_seconds": 3600, "gamma_base": "https://gamma-api.polymarket.com"}

        with patch(
            "runtime_loop._fetch_market_selection",
            AsyncMock(return_value=(new_session, new_session.token_ids)),
        ), patch("runtime_loop._bootstrap_anchor_price", AsyncMock()):
            current_session, last_poly_book, market_switched = loop.run_until_complete(
                _refresh_market_state(
                    strategy=strategy,
                    binance_feed=binance_feed,
                    poly_feed=poly_feed,
                    market_cfg=market_cfg,
                    current_session=old_session,
                    last_poly_book=None,
                    now_utc=now_utc,
                    binance_symbol="BTCUSDT",
                    binance_rest="https://api.binance.com",
                    logger=logger,
                )
            )

        assert market_switched is True
        assert current_session == new_session
        assert last_poly_book is not None
        assert last_poly_book.up_bid == 0.64
        assert last_poly_book.up_ask == 0.66
        assert last_poly_book.down_bid == 0.34
        assert last_poly_book.down_ask == 0.36
        assert strategy.state.anchor_price is None
        assert set(poly_feed._desired_tokens) == set(new_session.token_ids)
        binance_feed.reset_cache.assert_called_once()
    finally:
        loop.close()
        asyncio.set_event_loop(None)
