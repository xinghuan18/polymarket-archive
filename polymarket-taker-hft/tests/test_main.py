from __future__ import annotations

from datetime import datetime, timedelta, timezone

from feeds import BinanceQuote
from main import _iv_ref_price_for_binance_tick, _select_subscription_tokens
from market_selector import MarketSession


def _session(
    *,
    condition_id: str,
    open_dt: datetime,
    close_dt: datetime,
    up_token: str,
    down_token: str,
) -> MarketSession:
    return MarketSession(
        group="crypto_1h",
        gamma_id=f"g-{condition_id}",
        condition_id=condition_id,
        slug=f"slug-{condition_id}",
        question="Bitcoin Up or Down",
        open_time_iso_utc=open_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        close_time_iso_utc=close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        open_dt=open_dt,
        close_dt=close_dt,
        up_token_id=up_token,
        up_outcome="Up",
        down_token_id=down_token,
        down_outcome="Down",
    )


def test_select_subscription_tokens_includes_selected_and_next_upcoming() -> None:
    now = datetime(2026, 2, 25, 8, 0, 0, tzinfo=timezone.utc)
    active = _session(
        condition_id="A",
        open_dt=now - timedelta(minutes=5),
        close_dt=now + timedelta(minutes=55),
        up_token="A_UP",
        down_token="A_DOWN",
    )
    upcoming = _session(
        condition_id="B",
        open_dt=now + timedelta(minutes=55),
        close_dt=now + timedelta(minutes=115),
        up_token="B_UP",
        down_token="B_DOWN",
    )

    token_ids = _select_subscription_tokens(
        markets=[active, upcoming],
        selected=active,
        now_utc=now,
        schedule_lead_seconds=3600,
    )

    assert token_ids == ["A_UP", "A_DOWN", "B_UP", "B_DOWN"]


def test_select_subscription_tokens_ignores_upcoming_outside_lead_window() -> None:
    now = datetime(2026, 2, 25, 8, 0, 0, tzinfo=timezone.utc)
    selected = _session(
        condition_id="A",
        open_dt=now - timedelta(minutes=5),
        close_dt=now + timedelta(minutes=55),
        up_token="A_UP",
        down_token="A_DOWN",
    )
    far_upcoming = _session(
        condition_id="C",
        open_dt=now + timedelta(hours=2),
        close_dt=now + timedelta(hours=3),
        up_token="C_UP",
        down_token="C_DOWN",
    )

    token_ids = _select_subscription_tokens(
        markets=[selected, far_upcoming],
        selected=selected,
        now_utc=now,
        schedule_lead_seconds=3600,
    )

    assert token_ids == ["A_UP", "A_DOWN"]


def test_iv_ref_price_for_binance_tick_uses_ask_when_bid_unchanged() -> None:
    last_quote = BinanceQuote(bid=100.0, ask=100.01, ts_local_ms=1)
    quote = BinanceQuote(bid=100.0, ask=100.02, ts_local_ms=2)
    assert _iv_ref_price_for_binance_tick(quote, last_quote) == 100.02


def test_iv_ref_price_for_binance_tick_uses_bid_when_ask_unchanged() -> None:
    last_quote = BinanceQuote(bid=100.0, ask=100.01, ts_local_ms=1)
    quote = BinanceQuote(bid=99.99, ask=100.01, ts_local_ms=2)
    assert _iv_ref_price_for_binance_tick(quote, last_quote) == 99.99


def test_iv_ref_price_for_binance_tick_uses_mid_otherwise() -> None:
    last_quote = BinanceQuote(bid=100.0, ask=100.01, ts_local_ms=1)
    quote = BinanceQuote(bid=99.99, ask=100.02, ts_local_ms=2)
    assert _iv_ref_price_for_binance_tick(quote, last_quote) == (99.99 + 100.02) / 2.0
