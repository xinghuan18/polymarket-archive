from __future__ import annotations

import asyncio
import csv
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def iso_utc_ms(local_ts_ms: int) -> str:
    return datetime.fromtimestamp(local_ts_ms / 1000.0, tz=timezone.utc).isoformat(timespec="milliseconds")


def utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def default_events_csv_path() -> Path:
    return Path(f"events_{utc_now_compact()}.csv")


EVENT_COLUMNS = [
    "local_ts_ms",
    "iso_utc",
    "event_type",
    "event_source",
    "market_slug",
    "condition_id",
    "token_side",
    "token_id",
    "binance_bid",
    "binance_ask",
    "binance_spread",
    "binance_ref_price",
    "poly_up_bid",
    "poly_up_ask",
    "poly_down_bid",
    "poly_down_ask",
    "poly_p_up",
    "target_price",
    "p_model_up",
    "delta_p",
    "kept_iv_per_s",
    "iv_year",
    "horizon_s",
    "up_trigger_ref",
    "down_trigger_ref",
    "trigger_reason",
    "order_id",
    "order_price",
    "order_size",
    "mock_latency_ms",
    "mock_fill_price",
    "order_status",
    "note",
]


class EventLogger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self.path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fp, fieldnames=EVENT_COLUMNS, extrasaction="ignore")
        self._writer.writeheader()
        self._lock = asyncio.Lock()

    async def write_event(
        self,
        event_type: str,
        local_ts_ms: Optional[int] = None,
        **fields: Any,
    ) -> None:
        ts_ms = int(time.time() * 1000) if local_ts_ms is None else int(local_ts_ms)
        row: Dict[str, Any] = {k: "" for k in EVENT_COLUMNS}
        row["local_ts_ms"] = ts_ms
        row["iso_utc"] = iso_utc_ms(ts_ms)
        row["event_type"] = event_type

        for key, value in fields.items():
            if key in row and value is not None:
                row[key] = value

        async with self._lock:
            self._writer.writerow(row)
            self._fp.flush()

    def close(self) -> None:
        self._fp.close()
