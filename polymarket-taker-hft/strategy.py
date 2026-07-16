from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from feeds import BinanceQuote, PolyBook
from market_selector import MarketSession
from model import forward_p_up, invert_implied_vol_per_s
from scipy.stats import t as student_t

_MAX_EXP_ARG = math.log(sys.float_info.max)
@dataclass
class SignalState:
    anchor_price: Optional[float] = None
    kept_iv_per_s: Optional[float] = None
    p_poly_up: Optional[float] = None
    p_model_up: Optional[float] = None
    delta: Optional[float] = None
@dataclass
class MockOrder:
    order_id: str
    market_slug: str
    condition_id: str
    token_side: str
    token_id: str
    side: str
    price: float
    size: float
@dataclass
class StrategyEvaluation:
    order: Optional[MockOrder]
    note: str
    spread: Optional[float]
    horizon_s: Optional[float]
    binance_ref_price: Optional[float]
    signal_state: SignalState
    up_trigger_ref: Optional[float] = None
    down_trigger_ref: Optional[float] = None
class TakerStrategy:
    def __init__(
        self,
        df: int,
        delta_threshold: float,
        order_price: float,
        order_size: float,
        min_order_interval_ms: int = 0,
    ):
        self._df = df
        self._delta_threshold = delta_threshold
        self._order_price = order_price
        self._order_size = order_size
        self._min_order_interval_ms = max(0, min_order_interval_ms)
        self._prob_eps = 1.0e-6
        self._t_scale = math.sqrt(self._df / (self._df - 2.0)) if self._df > 2 else 1.0

        self.state = SignalState()
        self._order_counter = 0
        self._last_order_ts_ms: Optional[int] = None
        self._trigger_key: Optional[tuple[float, float, float]] = None
        self._up_k: Optional[float] = None
        self._down_k: Optional[float] = None

    def reset_for_market(self) -> None:
        self.state = SignalState()
        self._last_order_ts_ms = None
        self._trigger_key = None
        self._up_k = None
        self._down_k = None

    def _next_order_id(self, condition_id: str) -> str:
        self._order_counter += 1
        return f"MOCK-{condition_id}-{int(time.time() * 1000)}-{self._order_counter}"

    @staticmethod
    def _poly_up_mid(poly: PolyBook) -> Optional[float]:
        if poly.up_bid is None or poly.up_ask is None:
            return None
        return (poly.up_bid + poly.up_ask) / 2.0

    def _refresh_kept_iv(
        self,
        p_up: float,
        ref_price: float,
        horizon_s: float,
    ) -> bool:
        if self.state.anchor_price is None:
            return False

        refreshed = invert_implied_vol_per_s(
            p_up=p_up,
            anchor=self.state.anchor_price,
            ref_price=ref_price,
            horizon_s=horizon_s,
            df=self._df,
        )
        if refreshed is None:
            return False

        self.state.kept_iv_per_s = refreshed
        return True

    def _build_order(
        self,
        market: MarketSession,
        token_side: str,
        token_id: str,
    ) -> MockOrder:
        return MockOrder(
            order_id=self._next_order_id(market.condition_id),
            market_slug=market.slug,
            condition_id=market.condition_id,
            token_side=token_side,
            token_id=token_id,
            side="BUY",
            price=self._order_price,
            size=self._order_size,
        )

    def _clamp_prob(self, p: float) -> float:
        return max(self._prob_eps, min(1.0 - self._prob_eps, p))

    def _order_interval_elapsed(self, now_ms: int) -> bool:
        if self._min_order_interval_ms <= 0:
            return True
        if self._last_order_ts_ms is None:
            return True
        return now_ms - self._last_order_ts_ms >= self._min_order_interval_ms

    def _update_trigger_coefficients(self, up_bid: float, up_ask: float) -> bool:
        if self.state.kept_iv_per_s is None:
            return False

        iv = self.state.kept_iv_per_s
        key = (up_bid, up_ask, iv)
        if self._trigger_key == key and self._up_k is not None and self._down_k is not None:
            return True

        up_prob_trigger = self._clamp_prob(up_ask + self._delta_threshold)
        down_prob_trigger = self._clamp_prob(up_bid - self._delta_threshold)

        up_q = student_t.ppf(1.0 - up_prob_trigger, self._df)
        down_q = student_t.ppf(1.0 - down_prob_trigger, self._df)
        if not math.isfinite(up_q) or not math.isfinite(down_q):
            return False

        self._up_k = (up_q * iv) / self._t_scale
        self._down_k = (down_q * iv) / self._t_scale
        self._trigger_key = key
        return True

    def _trigger_ref_price(self, k: Optional[float], horizon_s: float) -> Optional[float]:
        if k is None or self.state.anchor_price is None or horizon_s <= 0.0:
            return None
        x = k * math.sqrt(horizon_s)
        if not math.isfinite(x) or abs(x) >= _MAX_EXP_ARG:
            return None
        den = math.exp(x)
        if not math.isfinite(den) or den <= 0.0:
            return None
        ref_px = self.state.anchor_price / den
        if not math.isfinite(ref_px) or ref_px <= 0.0:
            return None
        return ref_px

    def evaluate(
        self,
        market: MarketSession,
        quote: BinanceQuote,
        poly: PolyBook,
        now_utc: Optional[datetime] = None,
        iv_ref_price: Optional[float] = None,
        allow_order_submission: bool = True,
    ) -> StrategyEvaluation:
        now = now_utc or datetime.now(timezone.utc)
        spread = quote.ask - quote.bid

        if now < market.open_dt:
            return StrategyEvaluation(None, "market_not_open", spread, None, None, self.state)
        if now >= market.close_dt:
            return StrategyEvaluation(None, "market_closed", spread, 0.0, None, self.state)

        ref_price = quote.bid
        iv_price = ref_price if iv_ref_price is None else iv_ref_price
        if self.state.anchor_price is None:
            return StrategyEvaluation(None, "anchor_not_ready", spread, None, ref_price, self.state)

        p_old = self._poly_up_mid(poly)
        if p_old is None:
            return StrategyEvaluation(None, "no_poly_p_up", spread, None, ref_price, self.state)
        self.state.p_poly_up = p_old

        horizon_s = (market.close_dt - now).total_seconds()
        if horizon_s <= 0.0:
            return StrategyEvaluation(None, "market_closed", spread, 0.0, ref_price, self.state)

        # Bootstrap once; after that, trigger checks must run before refreshing IV on each valid quote.
        if self.state.kept_iv_per_s is None:
            if not self._refresh_kept_iv(
                p_up=p_old,
                ref_price=iv_price,
                horizon_s=horizon_s,
            ):
                return StrategyEvaluation(None, "iv_bootstrap_failed", spread, horizon_s, ref_price, self.state)

        if self.state.kept_iv_per_s is None:
            return StrategyEvaluation(None, "iv_bootstrap_failed", spread, horizon_s, ref_price, self.state)

        if poly.up_bid is None or poly.up_ask is None:
            return StrategyEvaluation(None, "no_poly_p_up", spread, horizon_s, ref_price, self.state)
        if not self._update_trigger_coefficients(poly.up_bid, poly.up_ask):
            return StrategyEvaluation(None, "trigger_coeff_failed", spread, horizon_s, ref_price, self.state)

        up_trigger_ref = self._trigger_ref_price(self._up_k, horizon_s)
        down_trigger_ref = self._trigger_ref_price(self._down_k, horizon_s)
        if up_trigger_ref is None or down_trigger_ref is None:
            return StrategyEvaluation(
                None,
                "trigger_ref_failed",
                spread,
                horizon_s,
                ref_price,
                self.state,
                up_trigger_ref=up_trigger_ref,
                down_trigger_ref=down_trigger_ref,
            )

        order: Optional[MockOrder] = None
        note = "no_trigger"
        now_ms = int(now.timestamp() * 1000)
        if ref_price > up_trigger_ref:
            if not allow_order_submission:
                order = self._build_order(
                    market=market,
                    token_side="UP",
                    token_id=market.up_token_id,
                )
                note = "trigger_up"
            elif self._order_interval_elapsed(now_ms):
                order = self._build_order(
                    market=market,
                    token_side="UP",
                    token_id=market.up_token_id,
                )
                self._last_order_ts_ms = now_ms
                note = "trigger_up"
            else:
                note = "trigger_up_throttled"
        elif ref_price < down_trigger_ref:
            if not allow_order_submission:
                order = self._build_order(
                    market=market,
                    token_side="DOWN",
                    token_id=market.down_token_id,
                )
                note = "trigger_down"
            elif self._order_interval_elapsed(now_ms):
                order = self._build_order(
                    market=market,
                    token_side="DOWN",
                    token_id=market.down_token_id,
                )
                self._last_order_ts_ms = now_ms
                note = "trigger_down"
            else:
                note = "trigger_down_throttled"

        p_new = forward_p_up(
            anchor=self.state.anchor_price,
            ref_price=ref_price,
            horizon_s=horizon_s,
            vol_per_s=self.state.kept_iv_per_s,
            df=self._df,
        )
        if p_new is None:
            return StrategyEvaluation(None, "model_prob_failed", spread, horizon_s, ref_price, self.state)

        self.state.p_model_up = p_new
        self.state.delta = p_new - p_old

        # Refresh IV after signal/order evaluation so p_model_up and delta reflect pre-refresh IV.
        self._refresh_kept_iv(
            p_up=p_old,
            ref_price=iv_price,
            horizon_s=horizon_s,
        )

        return StrategyEvaluation(
            order,
            note,
            spread,
            horizon_s,
            ref_price,
            self.state,
            up_trigger_ref=up_trigger_ref,
            down_trigger_ref=down_trigger_ref,
        )
