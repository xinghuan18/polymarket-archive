from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import orjson
import requests


BTC_RE = re.compile(r"\b(bitcoin|btc)\b")
UPDOWN_RE = re.compile(r"up[- ]?or[- ]?down|updown")


def parse_iso_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _parse_json_array(value: Any) -> List[Any]:
    return orjson.loads(value)


def _infer_outcome_side(outcome: str) -> Optional[str]:
    tokens = set(outcome.lower().replace("-", " ").replace("_", " ").split())
    if "up" in tokens or "yes" in tokens:
        return "UP"
    if "down" in tokens or "no" in tokens:
        return "DOWN"
    return None


def _parse_optional_iso_utc(value: Any) -> Optional[datetime]:
    if value:
        return parse_iso_utc(value)
    return None


def _extract_close_dt(raw: Dict[str, Any], open_dt: datetime) -> Optional[datetime]:
    for key in ("endDate", "marketEndDate"):
        close_dt = _parse_optional_iso_utc(raw[key]) if key in raw else None
        if close_dt is not None and close_dt > open_dt:
            return close_dt
    return None


def _btc_hourly_slug_for_utc_hour(now_utc: datetime, hour_offset: int) -> str:
    et = now_utc.astimezone(ZoneInfo("America/New_York")) + timedelta(hours=hour_offset)
    month = et.strftime("%B").lower()
    day = str(et.day)
    hour_12 = et.hour % 12 or 12
    am_pm = "am" if et.hour < 12 else "pm"
    return f"bitcoin-up-or-down-{month}-{day}-{hour_12}{am_pm}-et"


@dataclass(frozen=True)
class MarketSession:
    group: str
    gamma_id: str
    condition_id: str
    slug: str
    question: str
    open_time_iso_utc: str
    close_time_iso_utc: str
    open_dt: datetime
    close_dt: datetime
    up_token_id: str
    up_outcome: str
    down_token_id: str
    down_outcome: str

    @property
    def token_ids(self) -> List[str]:
        return [self.up_token_id, self.down_token_id]


def _parse_market(raw: Dict[str, Any]) -> Optional[MarketSession]:
    slug = raw["slug"]
    question = raw["question"]
    text = f"{slug} {question}".lower()
    if not BTC_RE.search(text) or not UPDOWN_RE.search(text):
        return None

    open_dt = parse_iso_utc(raw["eventStartTime"])

    close_dt = _extract_close_dt(raw, open_dt)
    if close_dt is None:
        return None
    duration_minutes = (close_dt - open_dt).total_seconds() / 60.0
    if not (59.5 <= duration_minutes <= 60.5):
        return None

    token_ids = _parse_json_array(raw["clobTokenIds"])
    outcomes = _parse_json_array(raw["outcomes"])
    if len(token_ids) < 2 or len(outcomes) < 2:
        return None

    up_pair = None
    down_pair = None
    for token_id, outcome in zip(token_ids, outcomes):
        side = _infer_outcome_side(outcome)
        if side == "UP":
            up_pair = (token_id, outcome)
        elif side == "DOWN":
            down_pair = (token_id, outcome)
    if up_pair is None or down_pair is None:
        return None

    return MarketSession(
        group="crypto_1h",
        gamma_id=raw["id"],
        condition_id=raw["conditionId"],
        slug=slug,
        question=question,
        open_time_iso_utc=open_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        close_time_iso_utc=close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        open_dt=open_dt,
        close_dt=close_dt,
        up_token_id=up_pair[0],
        up_outcome=up_pair[1],
        down_token_id=down_pair[0],
        down_outcome=down_pair[1],
    )


def fetch_1h_btc_markets(
    gamma_base: str,
    hour_offsets: Optional[Iterable[int]] = None,
) -> List[MarketSession]:
    now = datetime.now(timezone.utc)
    offsets = list(hour_offsets) if hour_offsets is not None else list(range(-2, 5))
    url = f"{gamma_base.rstrip('/')}/markets"

    sessions: List[MarketSession] = []
    with requests.Session() as session:
        for hour_offset in offsets:
            slug = _btc_hourly_slug_for_utc_hour(now, hour_offset)
            resp = session.get(url, params={"slug": slug}, timeout=20)
            resp.raise_for_status()
            for raw in resp.json():
                item = _parse_market(raw)
                if item is not None:
                    sessions.append(item)

    dedup: Dict[str, MarketSession] = {}
    for market in sessions:
        if market.condition_id:
            dedup[market.condition_id] = market
    return sorted(dedup.values(), key=lambda m: m.open_dt)


def select_active_or_next(
    markets: List[MarketSession],
    now_utc: Optional[datetime] = None,
    schedule_lead_seconds: float = 3600.0,
) -> Optional[MarketSession]:
    now = now_utc or datetime.now(timezone.utc)

    active = [m for m in markets if m.open_dt <= now < m.close_dt]
    if active:
        return sorted(active, key=lambda m: m.close_dt)[0]

    lead = max(0.0, schedule_lead_seconds)
    upcoming = [m for m in markets if m.open_dt > now and (m.open_dt - now).total_seconds() <= lead]
    if upcoming:
        return sorted(upcoming, key=lambda m: m.open_dt)[0]
    return None
