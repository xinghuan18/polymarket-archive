from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from feeds import BinanceQuote, PolyBook
from market_selector import MarketSession
from strategy import TakerStrategy

def _market() -> MarketSession:
    now = datetime.now(timezone.utc)
    open_dt = now - timedelta(minutes=5)
    close_dt = now + timedelta(minutes=30)
    return MarketSession(
        group="crypto_1h",
        gamma_id="g1",
        condition_id="c1",
        slug="bitcoin-up-or-down-test",
        question="Bitcoin Up or Down - Test",
        open_time_iso_utc=open_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        close_time_iso_utc=close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        open_dt=open_dt,
        close_dt=close_dt,
        up_token_id="UP_TOKEN",
        up_outcome="Up",
        down_token_id="DOWN_TOKEN",
        down_outcome="Down",
    )

def test_wide_spread_is_processed_as_normal_tick() -> None:
    strat = TakerStrategy(df=3, delta_threshold=0.01, order_price=0.99, order_size=2)
    strat.state.anchor_price = 0.01
    market = _market()
    poly = PolyBook(up_bid=0.49, up_ask=0.51, down_bid=0.49, down_ask=0.51, ts_local_ms=1)

    with patch("strategy.invert_implied_vol_per_s", return_value=0.02), patch(
        "strategy.student_t.ppf", return_value=0.0
    ), patch("strategy.forward_p_up", return_value=0.521):
        eval_result = strat.evaluate(
            market=market,
            quote=BinanceQuote(bid=0.011, ask=0.5, ts_local_ms=1),
            poly=poly,
        )

    assert eval_result.note == "trigger_up"
    assert eval_result.order is not None
    assert eval_result.order.token_side == "UP"
    assert abs(float(eval_result.spread or 0.0) - 0.489) < 1.0e-12

def test_trigger_up() -> None:
    strat = TakerStrategy(df=3, delta_threshold=0.01, order_price=0.99, order_size=2)
    strat.state.anchor_price = 0.01
    market = _market()
    poly = PolyBook(up_bid=0.49, up_ask=0.51, down_bid=0.49, down_ask=0.51, ts_local_ms=1)

    with patch("strategy.invert_implied_vol_per_s", return_value=0.02), patch(
        "strategy.student_t.ppf", return_value=0.0
    ), patch("strategy.forward_p_up", return_value=0.521):
        eval_result = strat.evaluate(
            market=market,
            quote=BinanceQuote(bid=0.011, ask=0.021, ts_local_ms=1),
            poly=poly,
        )

    assert eval_result.order is not None
    assert eval_result.order.token_side == "UP"
    assert eval_result.note == "trigger_up"

def test_trigger_down() -> None:
    strat = TakerStrategy(df=3, delta_threshold=0.01, order_price=0.99, order_size=2)
    strat.state.anchor_price = 0.01
    market = _market()
    poly = PolyBook(up_bid=0.49, up_ask=0.51, down_bid=0.49, down_ask=0.51, ts_local_ms=1)

    with patch("strategy.invert_implied_vol_per_s", return_value=0.02), patch(
        "strategy.student_t.ppf", return_value=0.0
    ), patch("strategy.forward_p_up", return_value=0.479):
        eval_result = strat.evaluate(
            market=market,
            quote=BinanceQuote(bid=0.009, ask=0.019, ts_local_ms=1),
            poly=poly,
        )

    assert eval_result.order is not None
    assert eval_result.order.token_side == "DOWN"
    assert eval_result.note == "trigger_down"

def test_min_order_interval_throttles_repeated_triggers() -> None:
    strat = TakerStrategy(
        df=3,
        delta_threshold=0.01,
        order_price=0.99,
        order_size=2,
        min_order_interval_ms=250,
    )
    strat.state.anchor_price = 0.01
    market = _market()
    poly = PolyBook(up_bid=0.49, up_ask=0.51, down_bid=0.49, down_ask=0.51, ts_local_ms=1)
    now = datetime.now(timezone.utc)

    with patch("strategy.invert_implied_vol_per_s", return_value=0.02), patch(
        "strategy.student_t.ppf", return_value=0.0
    ), patch("strategy.forward_p_up", return_value=0.52):
        first = strat.evaluate(
            market=market,
            quote=BinanceQuote(bid=0.011, ask=0.021, ts_local_ms=1),
            poly=poly,
            now_utc=now,
        )
        second = strat.evaluate(
            market=market,
            quote=BinanceQuote(bid=0.011, ask=0.021, ts_local_ms=2),
            poly=poly,
            now_utc=now + timedelta(milliseconds=100),
        )
        third = strat.evaluate(
            market=market,
            quote=BinanceQuote(bid=0.011, ask=0.021, ts_local_ms=3),
            poly=poly,
            now_utc=now + timedelta(milliseconds=300),
        )

    assert first.order is not None
    assert first.note == "trigger_up"
    assert second.order is None
    assert second.note == "trigger_up_throttled"
    assert third.order is not None
    assert third.note == "trigger_up"

def test_anchor_required_before_evaluation() -> None:
    strat = TakerStrategy(df=3, delta_threshold=0.01, order_price=0.99, order_size=2)
    eval_result = strat.evaluate(
        market=_market(),
        quote=BinanceQuote(bid=63843.19, ask=63843.20, ts_local_ms=1),
        poly=PolyBook(up_bid=0.49, up_ask=0.51, down_bid=0.49, down_ask=0.51, ts_local_ms=1),
    )
    assert eval_result.order is None
    assert eval_result.note == "anchor_not_ready"

def test_no_trigger_inside_bid_ask_threshold_band() -> None:
    strat = TakerStrategy(df=3, delta_threshold=0.005, order_price=0.99, order_size=2)
    strat.state.anchor_price = 0.01
    market = _market()
    poly = PolyBook(up_bid=0.64, up_ask=0.66, down_bid=0.34, down_ask=0.36, ts_local_ms=1)

    with patch("strategy.invert_implied_vol_per_s", return_value=0.02), patch(
        "strategy.student_t.ppf", return_value=0.0
    ):
        eval_result = strat.evaluate(
            market=market,
            quote=BinanceQuote(bid=0.01, ask=0.02, ts_local_ms=1),
            poly=poly,
        )

    assert eval_result.order is None
    assert eval_result.note == "no_trigger"

def test_no_trigger_still_populates_model_prob_and_delta() -> None:
    strat = TakerStrategy(df=3, delta_threshold=0.005, order_price=0.99, order_size=2)
    strat.state.anchor_price = 0.01
    market = _market()
    poly = PolyBook(up_bid=0.64, up_ask=0.66, down_bid=0.34, down_ask=0.36, ts_local_ms=1)

    with patch("strategy.invert_implied_vol_per_s", return_value=0.02), patch(
        "strategy.student_t.ppf", return_value=0.0
    ), patch("strategy.forward_p_up", return_value=0.651):
        eval_result = strat.evaluate(
            market=market,
            quote=BinanceQuote(bid=0.01, ask=0.02, ts_local_ms=1),
            poly=poly,
        )

    assert eval_result.order is None
    assert eval_result.note == "no_trigger"
    assert eval_result.signal_state.p_model_up == 0.651
    assert eval_result.signal_state.delta is not None
    assert abs(eval_result.signal_state.delta - 0.001) < 1.0e-12

def test_valid_bid_checks_trigger_before_refreshing_iv() -> None:
    strat = TakerStrategy(df=3, delta_threshold=0.01, order_price=0.99, order_size=2)
    strat.state.anchor_price = 0.01
    strat.state.kept_iv_per_s = 0.0001
    strat.state.p_poly_up = 0.4
    market = _market()
    poly = PolyBook(up_bid=0.49, up_ask=0.51, down_bid=0.49, down_ask=0.51, ts_local_ms=1)

    refresh_calls: list[tuple[float, float]] = []
    forward_vols: list[float] = []

    def _refresh_side_effect(*, p_up: float, ref_price: float, horizon_s: float) -> bool:
        refresh_calls.append((p_up, ref_price))
        assert horizon_s > 0.0
        strat.state.kept_iv_per_s = 1.0
        return True

    def _forward_side_effect(*, anchor: float, ref_price: float, horizon_s: float, vol_per_s: float, df: int) -> float:
        forward_vols.append(vol_per_s)
        return 0.6

    with patch("strategy.student_t.ppf", return_value=-1.0), patch(
        "strategy.forward_p_up", side_effect=_forward_side_effect
    ), patch.object(strat, "_refresh_kept_iv", side_effect=_refresh_side_effect):
        eval_result = strat.evaluate(
            market=market,
            quote=BinanceQuote(bid=0.011, ask=0.021, ts_local_ms=1),
            poly=poly,
        )

    assert eval_result.order is not None
    assert eval_result.order.token_side == "UP"
    assert eval_result.note == "trigger_up"
    assert refresh_calls == [(0.5, 0.011)]
    assert forward_vols == [0.0001]
    assert strat.state.kept_iv_per_s == 1.0

def test_iv_refresh_uses_override_price_when_provided() -> None:
    strat = TakerStrategy(df=3, delta_threshold=0.01, order_price=0.99, order_size=2)
    strat.state.anchor_price = 0.01
    strat.state.kept_iv_per_s = 0.0001
    market = _market()
    poly = PolyBook(up_bid=0.49, up_ask=0.51, down_bid=0.49, down_ask=0.51, ts_local_ms=1)
    refresh_calls: list[tuple[float, float]] = []

    def _refresh_side_effect(*, p_up: float, ref_price: float, horizon_s: float) -> bool:
        refresh_calls.append((p_up, ref_price))
        assert horizon_s > 0.0
        return True

    with patch("strategy.student_t.ppf", return_value=0.0), patch(
        "strategy.forward_p_up", return_value=0.5
    ), patch.object(strat, "_refresh_kept_iv", side_effect=_refresh_side_effect):
        eval_result = strat.evaluate(
            market=market,
            quote=BinanceQuote(bid=0.011, ask=0.021, ts_local_ms=1),
            poly=poly,
            iv_ref_price=0.019,
        )

    assert eval_result.note in {"no_trigger", "trigger_up", "trigger_down"}
    assert refresh_calls == [(0.5, 0.019)]

def test_trigger_ref_overflow_returns_failed_note() -> None:
    strat = TakerStrategy(df=3, delta_threshold=0.01, order_price=0.99, order_size=2)
    strat.state.anchor_price = 100.0
    strat.state.kept_iv_per_s = 1.0e6
    market = _market()
    poly = PolyBook(up_bid=0.49, up_ask=0.51, down_bid=0.49, down_ask=0.51, ts_local_ms=1)

    eval_result = strat.evaluate(
        market=market,
        quote=BinanceQuote(bid=101.0, ask=101.01, ts_local_ms=1),
        poly=poly,
    )

    assert eval_result.order is None
    assert eval_result.note == "trigger_ref_failed"

def test_suppressed_trigger_does_not_arm_throttle() -> None:
    strat = TakerStrategy(df=3, delta_threshold=0.01, order_price=0.99, order_size=2, min_order_interval_ms=250)
    strat.state.anchor_price = 0.01
    strat.state.kept_iv_per_s = 0.02
    market = _market()
    poly = PolyBook(up_bid=0.49, up_ask=0.51, down_bid=0.49, down_ask=0.51, ts_local_ms=1)
    now = datetime.now(timezone.utc)

    with patch("strategy.student_t.ppf", return_value=0.0), patch(
        "strategy.forward_p_up", return_value=0.52
    ), patch.object(strat, "_refresh_kept_iv", return_value=True):
        suppressed = strat.evaluate(
            market=market,
            quote=BinanceQuote(bid=0.011, ask=0.021, ts_local_ms=1),
            poly=poly,
            now_utc=now,
            allow_order_submission=False,
        )
        live = strat.evaluate(
            market=market,
            quote=BinanceQuote(bid=0.011, ask=0.021, ts_local_ms=2),
            poly=poly,
            now_utc=now + timedelta(milliseconds=100),
            allow_order_submission=True,
        )
        throttled = strat.evaluate(
            market=market,
            quote=BinanceQuote(bid=0.011, ask=0.021, ts_local_ms=3),
            poly=poly,
            now_utc=now + timedelta(milliseconds=200),
            allow_order_submission=True,
        )

    assert suppressed.order is not None
    assert suppressed.note == "trigger_up"
    assert live.order is not None
    assert live.note == "trigger_up"
    assert throttled.order is None
    assert throttled.note == "trigger_up_throttled"
