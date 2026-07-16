from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import orjson


CSV_COLUMNS = [
    "local_ts_ms",
    "recv_iso_utc",
    "msg_ts_ms",
    "event_type",
    "bid_price",
    "bid_qty",
    "ask_price",
    "ask_qty",
    "change",
    "trade_price",
    "trade_size",
    "trade_side",
    "trade_tx_hash",
]


class TopOfBook:
    def __init__(self) -> None:
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}

    @staticmethod
    def _best_bid(levels: dict[float, float]) -> tuple[Optional[float], Optional[float]]:
        if not levels:
            return None, None
        price = max(levels)
        return price, levels[price]

    @staticmethod
    def _best_ask(levels: dict[float, float]) -> tuple[Optional[float], Optional[float]]:
        if not levels:
            return None, None
        price = min(levels)
        return price, levels[price]

    def snapshot(self) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        bid_price, bid_qty = self._best_bid(self._bids)
        ask_price, ask_qty = self._best_ask(self._asks)
        return bid_price, bid_qty, ask_price, ask_qty

    def apply_book(self, payload: dict[str, Any]) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        self._bids.clear()
        self._asks.clear()
        for level in payload["bids"]:
            self._bids[float(level["price"])] = float(level["size"])
        for level in payload["asks"]:
            self._asks[float(level["price"])] = float(level["size"])
        return self.snapshot()

    def apply_price_change(self, payload: dict[str, Any]) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        for change in payload["price_changes"]:
            side = change["side"].lower()
            levels = self._bids if side in {"buy", "bid"} else self._asks
            price = float(change["price"])
            size = float(change["size"])
            if size == 0.0:
                levels.pop(price, None)
            else:
                levels[price] = size
        last_change = payload["price_changes"][-1]
        bid_price = float(last_change["best_bid"])
        ask_price = float(last_change["best_ask"])
        bid_qty = self._bids.get(bid_price)
        ask_qty = self._asks.get(ask_price)
        return bid_price, bid_qty, ask_price, ask_qty

    def from_best_bid_ask(
        self,
        payload: dict[str, Any],
    ) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        bid_price = float(payload["best_bid"])
        ask_price = float(payload["best_ask"])
        bid_qty = self._bids.get(bid_price)
        ask_qty = self._asks.get(ask_price)
        return bid_price, bid_qty, ask_price, ask_qty

    def qty_for_prices(
        self,
        bid_price: float,
        ask_price: float,
    ) -> tuple[Optional[float], Optional[float]]:
        return self._bids.get(bid_price), self._asks.get(ask_price)


class CsvLogger:
    def __init__(self, path: str):
        self._path = Path(path)
        self._fh = self._path.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._fh, fieldnames=CSV_COLUMNS)
        self._writer.writeheader()
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def append(
        self,
        *,
        event_type: str,
        msg_ts_ms: int,
        bid_price: Optional[float],
        bid_qty: Optional[float],
        ask_price: Optional[float],
        ask_qty: Optional[float],
        change: str = "",
        trade_price: Optional[float] = None,
        trade_size: Optional[float] = None,
        trade_side: str = "",
        trade_tx_hash: str = "",
    ) -> dict[str, Any]:
        recv_dt = datetime.now(timezone.utc)
        recv_iso = recv_dt.isoformat(timespec="milliseconds")
        local_ts_ms = int(recv_dt.timestamp() * 1000)
        row = {
            "local_ts_ms": local_ts_ms,
            "recv_iso_utc": recv_iso,
            "msg_ts_ms": msg_ts_ms,
            "event_type": event_type,
            "bid_price": bid_price,
            "bid_qty": bid_qty,
            "ask_price": ask_price,
            "ask_qty": ask_qty,
            "change": change,
            "trade_price": trade_price,
            "trade_size": trade_size,
            "trade_side": trade_side,
            "trade_tx_hash": trade_tx_hash,
        }
        self._writer.writerow(row)
        self._fh.flush()
        return row


def parse_frames(raw: str) -> list[dict[str, Any]]:
    stripped = raw.strip()
    if not stripped:
        return []
    if stripped.upper() in {"PING", "PONG"}:
        return []
    if stripped[0] not in "[{":
        return []
    payload = orjson.loads(stripped)
    if type(payload) is dict:
        return [payload]
    if type(payload) is list:
        return payload
    return []
