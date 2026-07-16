from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import market_selector
from market_selector import MarketSession, _parse_market, fetch_1h_btc_markets, select_active_or_next


def _raw_market(
    *,
    slug: str,
    question: str,
    start: str,
    end: str,
    condition_id: str,
    trading_start: str = "",
) -> Dict[str, Any]:
    return {
        "id": f"g-{condition_id}",
        "conditionId": condition_id,
        "slug": slug,
        "question": question,
        "startDate": trading_start,
        "eventStartTime": start,
        "endDate": end,
        "clobTokenIds": '["UP_TOKEN","DOWN_TOKEN"]',
        "outcomes": '["Up","Down"]',
    }


def test_parse_market_1h_only_excludes_5m() -> None:
    raw = _raw_market(
        slug="btc-updown-5m-1772026800",
        question="Bitcoin Up or Down - February 25, 8:40AM-8:45AM ET",
        start="2026-02-25T13:40:00Z",
        end="2026-02-25T13:45:00Z",
        condition_id="c-5m",
    )
    assert _parse_market(raw) is None


def test_parse_market_1h_uses_end_date_for_close() -> None:
    raw = _raw_market(
        slug="bitcoin-up-or-down-february-26-8am-et",
        question="Bitcoin Up or Down - February 26, 8AM ET",
        start="2026-02-26T13:00:00Z",
        end="2026-02-26T14:00:00Z",
        condition_id="c-1h",
    )
    session = _parse_market(raw)
    assert session is not None
    assert session.open_dt == datetime(2026, 2, 26, 13, 0, 0, tzinfo=timezone.utc)
    assert session.close_dt == datetime(2026, 2, 26, 14, 0, 0, tzinfo=timezone.utc)


def test_parse_market_ignores_start_date_for_open() -> None:
    raw = _raw_market(
        slug="bitcoin-up-or-down-february-26-8am-et",
        question="Bitcoin Up or Down - February 26, 8AM ET",
        trading_start="2026-02-25T13:00:00Z",
        start="2026-02-26T13:00:00Z",
        end="2026-02-26T14:00:00Z",
        condition_id="c-open",
    )
    session = _parse_market(raw)
    assert session is not None
    assert session.open_dt == datetime(2026, 2, 26, 13, 0, 0, tzinfo=timezone.utc)
    assert session.close_dt == datetime(2026, 2, 26, 14, 0, 0, tzinfo=timezone.utc)


def test_fetch_1h_btc_markets_honors_include_filter(monkeypatch) -> None:
    slug_page: List[Dict[str, Any]] = [
        _raw_market(
            slug="btc-updown-5m-1772026800",
            question="Bitcoin Up or Down - February 25, 8:40AM-8:45AM ET",
            start="2026-02-25T13:40:00Z",
            end="2026-02-25T13:45:00Z",
            condition_id="c-5m",
        ),
        _raw_market(
            slug="bitcoin-up-or-down-february-26-8am-et",
            question="Bitcoin Up or Down - February 26, 8AM ET",
            start="2026-02-26T13:00:00Z",
            end="2026-02-26T14:00:00Z",
            condition_id="c-1h",
        ),
    ]

    class _FakeResponse:
        def __init__(self, payload: List[Dict[str, Any]]):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> List[Dict[str, Any]]:
            return self._payload

    class _FakeSession:
        def __enter__(self) -> "_FakeSession":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def get(self, url: str, params: Dict[str, Any], timeout: int) -> _FakeResponse:
            if params.get("slug") == "target-slug":
                return _FakeResponse(slug_page)
            return _FakeResponse([])

    monkeypatch.setattr(market_selector.requests, "Session", _FakeSession)
    monkeypatch.setattr(
        market_selector,
        "_btc_hourly_slug_for_utc_hour",
        lambda _now, hour_offset: "target-slug" if hour_offset == 0 else f"other-{hour_offset}",
    )

    markets = fetch_1h_btc_markets(
        gamma_base="https://gamma-api.polymarket.com",
        hour_offsets=[0],
    )

    assert len(markets) == 1
    assert markets[0].condition_id == "c-1h"


def test_select_active_or_next_respects_schedule_lead() -> None:
    now = datetime(2026, 2, 24, 14, 0, 0, tzinfo=timezone.utc)
    near = MarketSession(
        group="crypto_1h",
        gamma_id="g-near",
        condition_id="c-near",
        slug="bitcoin-up-or-down-near",
        question="near",
        open_time_iso_utc="2026-02-24T14:30:00Z",
        close_time_iso_utc="2026-02-24T15:30:00Z",
        open_dt=now + timedelta(minutes=30),
        close_dt=now + timedelta(minutes=90),
        up_token_id="u1",
        up_outcome="Up",
        down_token_id="d1",
        down_outcome="Down",
    )
    far = MarketSession(
        group="crypto_1h",
        gamma_id="g-far",
        condition_id="c-far",
        slug="bitcoin-up-or-down-far",
        question="far",
        open_time_iso_utc="2026-02-24T16:30:00Z",
        close_time_iso_utc="2026-02-24T17:30:00Z",
        open_dt=now + timedelta(hours=2, minutes=30),
        close_dt=now + timedelta(hours=3, minutes=30),
        up_token_id="u2",
        up_outcome="Up",
        down_token_id="d2",
        down_outcome="Down",
    )
    selected = select_active_or_next([far, near], now, schedule_lead_seconds=3600)
    assert selected is not None
    assert selected.condition_id == "c-near"

    selected_none = select_active_or_next([far], now, schedule_lead_seconds=3600)
    assert selected_none is None
