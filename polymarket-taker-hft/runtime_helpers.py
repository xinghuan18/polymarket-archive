from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from feeds import BinanceQuote
from market_selector import MarketSession

SECONDS_PER_YEAR = 365.0 * 24.0 * 60.0 * 60.0


def fmt_float(value: Optional[float], decimals: int = 6) -> str:
    if value is None:
        return "NA"
    return f"{value:.{decimals}f}"


def iv_per_year(vol_per_s: Optional[float]) -> Optional[float]:
    if vol_per_s is None:
        return None
    return vol_per_s * math.sqrt(SECONDS_PER_YEAR)


def iv_ref_price_for_binance_tick(
    quote: BinanceQuote,
    last_quote: Optional[BinanceQuote],
) -> float:
    if last_quote is not None and quote.bid == last_quote.bid:
        return quote.ask
    if last_quote is not None and quote.ask == last_quote.ask:
        return quote.bid
    return (quote.bid + quote.ask) / 2.0


def select_subscription_tokens(
    markets: list[MarketSession],
    selected: Optional[MarketSession],
    now_utc: datetime,
    schedule_lead_seconds: float,
) -> list[str]:
    picked: list[str] = []
    seen: set[str] = set()

    def append_tokens(session: MarketSession) -> None:
        for token_id in session.token_ids:
            if token_id not in seen:
                seen.add(token_id)
                picked.append(token_id)

    if selected is not None:
        append_tokens(selected)

    lead = max(0.0, schedule_lead_seconds)
    upcoming = [
        market
        for market in markets
        if market.open_dt > now_utc and (market.open_dt - now_utc).total_seconds() <= lead
    ]
    if upcoming:
        next_market = sorted(upcoming, key=lambda market: market.open_dt)[0]
        if selected is None or next_market.condition_id != selected.condition_id:
            append_tokens(next_market)

    return picked
