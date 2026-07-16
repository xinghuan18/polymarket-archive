#!/usr/bin/env python3
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import pandas as pd

BASE_URL = "https://api.binance.com/api/v3/klines"
ONE_MIN_MS = 60_000

COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "qav", "num_trades", "tbbv", "tbqv", "ignore"
]

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT"]
INTERVAL = "1m"
OUT_DIR = Path("/home/ec2-user/polymarket-bot/backtest")
START_DATE_UTC = datetime(2020, 1, 1, tzinfo=timezone.utc)

SLEEP_S = 0.2   # increase if you hit 429
LIMIT = 1000     # Binance max


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fetch_klines(session: requests.Session, symbol: str, start_ms: int, end_ms: int) -> list[list]:
    r = session.get(
        BASE_URL,
        params={
            "symbol": symbol,
            "interval": INTERVAL,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": LIMIT,
        },
        timeout=20,
    )

    # very simple rate-limit handling
    if r.status_code == 429:
        time.sleep(2.0)
        return fetch_klines(session, symbol, start_ms, end_ms)

    r.raise_for_status()
    return r.json()


def download_day(session: requests.Session, symbol: str, day: datetime) -> pd.DataFrame:
    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1) - timedelta(milliseconds=1)

    start_ms = ms(day_start)
    end_ms = ms(day_end)

    rows: list[list] = []
    cur = start_ms
    while cur <= end_ms:
        chunk = fetch_klines(session, symbol, cur, end_ms)
        if not chunk:
            break
        rows.extend(chunk)
        last_open = int(chunk[-1][0])
        nxt = last_open + ONE_MIN_MS
        if nxt <= cur:
            break
        cur = nxt
        if SLEEP_S:
            time.sleep(SLEEP_S)

    if not rows:
        return pd.DataFrame(columns=COLS)

    df = pd.DataFrame(rows, columns=COLS)

    # Make types reasonable (keep it simple)
    df["open_time"] = df["open_time"].astype("int64")
    df["close_time"] = df["close_time"].astype("int64")
    for c in ["open", "high", "low", "close", "volume", "qav", "tbbv", "tbqv"]:
        df[c] = df[c].astype("float64")
    df["num_trades"] = df["num_trades"].astype("int64")

    return df


def main() -> None:
    today_utc = datetime.now(timezone.utc).date()
    day = START_DATE_UTC.date()

    with requests.Session() as session:
        while day <= today_utc:
            day_dt = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            ymd = day_dt.strftime("%Y%m%d")

            for sym in SYMBOLS:
                sym_dir = OUT_DIR / sym
                sym_dir.mkdir(parents=True, exist_ok=True)

                out_path = sym_dir / f"{ymd}.feather"
                if out_path.exists():
                    continue  # skip already-downloaded day file

                print(f"{sym} {ymd}")
                df = download_day(session, sym, day_dt)

                # Save even if empty? Usually better to skip empties.
                if not df.empty:
                    df.to_feather(out_path)

            day = (day_dt + timedelta(days=1)).date()


if __name__ == "__main__":
    main()
