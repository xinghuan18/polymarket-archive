from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


CSV_COLUMNS = [
    "local_ts_ms",
    "recv_iso_utc",
    "msg_ts_ms",
    "event_type",
    "side",
    "price",
    "size",
    "best_bid",
    "best_ask",
    "spread",
    "bids_levels",
    "asks_levels",
]


def _best_price(levels: Any, side: str) -> Optional[float]:
    prices = [float(level["price"]) for level in levels]
    return max(prices) if side == "bid" else min(prices)


class MonitorCsvAppender:
    def __init__(self, path: str):
        self._path = Path(path)
        self._fh = self._path.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._fh, fieldnames=CSV_COLUMNS)
        self._writer.writeheader()
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def append_event(self, payload: dict[str, Any]) -> None:
        recv_dt = datetime.now(timezone.utc)
        recv_iso = recv_dt.isoformat(timespec="milliseconds")
        local_ts_ms = int(recv_dt.timestamp() * 1000)
        row_base = {
            "local_ts_ms": local_ts_ms,
            "recv_iso_utc": recv_iso,
            "msg_ts_ms": int(payload["timestamp"]),
            "event_type": payload["event_type"],
            "side": None,
            "price": None,
            "size": None,
            "best_bid": None,
            "best_ask": None,
            "spread": None,
            "bids_levels": None,
            "asks_levels": None,
        }

        event_type = payload["event_type"]
        if event_type == "best_bid_ask":
            row = dict(row_base)
            row["best_bid"] = payload["best_bid"]
            row["best_ask"] = payload["best_ask"]
            row["spread"] = payload["spread"]
            self._writer.writerow(row)
            self._fh.flush()
            return

        if event_type == "book":
            bids = payload["bids"]
            asks = payload["asks"]
            row = dict(row_base)
            row["best_bid"] = _best_price(bids, side="bid")
            row["best_ask"] = _best_price(asks, side="ask")
            row["bids_levels"] = len(bids)
            row["asks_levels"] = len(asks)
            self._writer.writerow(row)
            self._fh.flush()
            return

        if event_type != "price_change":
            return
        for change in payload["price_changes"]:
            row = dict(row_base)
            row["side"] = change["side"]
            row["price"] = change["price"]
            row["size"] = change["size"]
            row["best_bid"] = change["best_bid"]
            row["best_ask"] = change["best_ask"]
            self._writer.writerow(row)
        self._fh.flush()
