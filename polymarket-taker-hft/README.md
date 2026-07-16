# polymarket-taker-hft

Taker-only **mock** trading bot for Polymarket 1-hour BTC “Up or Down” markets.

- Consumes Binance `bookTicker` (BTCUSDT L1 bid/ask).
- Consumes Polymarket CLOB websocket book updates for the currently selected market tokens.
- Evaluates a simple probability/IV model and **emits mock IOC orders** (no real order placement).

## Quick Start

Requirements:

- Python 3.9+
- `requests`, `aiohttp`, `websockets`, `scipy`, `orjson`
- `pytest` (tests)

Install:

```bash
pip install requests aiohttp websockets scipy orjson pytest
```

Run the bot:

```bash
python3 main.py --config config.yaml
```

The default event output file is `market_data_test.csv` (from `config.yaml`).

Optional: run the Binance diagnostics monitor (bookTicker + trade stream):

```bash
python3 research_scripts/binance_price_monitor.py --symbol BTCUSDT
```

## System Overview

The runtime is an async loop with two live websocket feeds and one decision loop.

**Data sources**

- Binance websocket: L1 `bookTicker` snapshots (`binance_l1_feed.py`).
- Polymarket websocket: best bid/ask updates per token (`polymarket_book_feed.py`).
- Polymarket Gamma REST: market discovery + scheduling (`market_selector.py`).
- Binance REST: 1h candle open used as an “anchor” price (`feeds.fetch_binance_1h_open_price`).

**Outputs**

- CSV event log (`event_log.py`) with `TICK` + order lifecycle events.
- Mock IOC order submission + fill simulation (`mock_exec.py`).

## Runtime Loop (Detailed)

`main.py` builds components and calls `run_runtime_loop(...)` in `runtime_loop.py`.

High-level responsibilities:

1. Periodically discover the active/next 1h BTC Up/Down market via Gamma.
2. Maintain the latest Binance quote + latest Polymarket best bid/ask for the selected market tokens.
3. Evaluate strategy on feed changes and (optionally) submit a mock IOC order.

Concrete flow:

1. Boot (`main.py`)
   - Load and merge config (`DEFAULT_CONFIG` + YAML overlay).
   - Create: `BinanceL1Feed`, `PolymarketBookFeed`, `TakerStrategy`, `MockExecutor`, `EventLogger`.
   - Start feed tasks: `binance_feed.run(stop_event)` and `poly_feed.run(stop_event)`.
2. Market selection / rollover (`runtime_loop.py`)
   - Every `runtime.rollover_poll_seconds` (or when no market is selected / market closed), fetch candidates and select:
     - the currently active market, else
     - the next market starting within `market.schedule_lead_seconds`.
   - Subscribe Polymarket websocket to the selected market token IDs (and optionally the next upcoming market token IDs).
3. Anchor bootstrap
   - Once a market is open, fetch the Binance 1h candle open at the market open time.
   - Store it as `strategy.state.anchor_price` (used to parameterize the model).
4. Tick evaluation and order gating (`runtime_tick.py`)
   - On Polymarket book change:
     - build a synthetic `PolyBook` (UP/DOWN best bid/ask) from the feed’s per-token cache
     - evaluate in “observe-only” mode (`allow_order_submission=False`) and log `ORDER_SUPPRESSED` if a trigger would fire
   - On Binance quote:
     - compute an IV refresh reference price (bid/ask/mid) via `iv_ref_price_for_binance_tick(...)`
     - evaluate in “live” mode (`allow_order_submission=True`)
5. Shutdown
   - Cancel feed tasks and any outstanding order tasks, flush/close the CSV, stop the process.

## Strategy (Detailed)

`TakerStrategy.evaluate(...)` is a pure “decision” function: it reads the latest market state + quotes, updates `SignalState`,
and either returns an order to submit or “no order”.

Evaluation gates, in order:

1. Market time gate: only evaluate for `open_dt <= now < close_dt`.
2. Anchor gate: require `state.anchor_price` to be set (bootstrapped from Binance 1h open).
3. Polymarket gate: require UP best bid/ask, and compute `p_poly_up = (up_bid + up_ask) / 2`.
4. Horizon gate: require positive `horizon_s = (close_dt - now).total_seconds()`.
5. IV bootstrap (once per market):
   - Invert an implied vol `kept_iv_per_s` from `p_poly_up`, `anchor_price`, and a chosen reference price.
6. Trigger thresholds:
   - Using Student-t(`df`) quantiles and `delta_threshold`, compute `up_trigger_ref` and `down_trigger_ref`
     (price levels above/below which we want to buy UP/DOWN).
7. Trigger:
   - If `binance_bid > up_trigger_ref` => BUY `UP`
   - If `binance_bid < down_trigger_ref` => BUY `DOWN`
8. Model output:
   - Compute `p_model_up` from the forward model and set `delta = p_model_up - p_poly_up`.
9. IV refresh (after signal computation)
   - Refresh `kept_iv_per_s` so the next tick uses an updated IV, while the current tick’s `p_model_up` / `delta`
     reflect the pre-refresh IV (this ordering is unit-tested).

## Mock IOC Execution

`MockExecutor.submit_ioc(...)` simulates an IOC order:

1. Log `ORDER_SUBMIT`, then `sleep(mock_latency_ms)`.
2. If market is closed at wake-up time => `CANCELED_MARKET_CLOSED`.
3. Else read the latest Polymarket ask via `poly_feed.get_best_ask(token_id)`:
   - if `ask <= order.price` => `FILLED` at `ask`
   - else => `UNFILLED`
4. Log `ORDER_RESULT`.

## Configuration

The repo ships a working `config.yaml` overlay. All keys have defaults in `main.DEFAULT_CONFIG`.

Key sections:

- `runtime.rollover_poll_seconds`: market rescan interval.
- `market.gamma_base`: Gamma API base.
- `market.schedule_lead_seconds`: “lookahead” window for upcoming markets.
- `feeds.*`: websocket URLs, ping/pong, reconnect delay, dedup toggles.
- `strategy.*`: model/trigger settings and mock order settings.
- `logging.events_csv`: output CSV path.

Strategy keys:

- `strategy.df`: Student-t degrees of freedom.
- `strategy.delta_threshold`: probability-space trigger band around the Polymarket quote.
- `strategy.order_price`: mock IOC limit price.
- `strategy.order_size`: mock IOC size.
- `strategy.mock_latency_ms`: mock execution delay.

## Event CSV

Event types written:

- `TICK`
- `ORDER_TRIGGER`
- `ORDER_SUPPRESSED`
- `ORDER_SUBMIT`
- `ORDER_RESULT`

Columns match `event_log.EVENT_COLUMNS`:

1. `local_ts_ms`, `iso_utc`, `event_type`, `event_source`
2. Market identifiers: `market_slug`, `condition_id`, `token_side`, `token_id`
3. Binance fields: `binance_bid`, `binance_ask`, `binance_spread`, `binance_ref_price`
4. Polymarket fields: `poly_up_bid`, `poly_up_ask`, `poly_down_bid`, `poly_down_ask`, `poly_p_up`
5. Model fields: `target_price`, `p_model_up`, `delta_p`, `kept_iv_per_s`, `iv_year`, `horizon_s`
6. Trigger fields: `up_trigger_ref`, `down_trigger_ref`, `trigger_reason`
7. Order fields: `order_id`, `order_price`, `order_size`, `mock_latency_ms`, `mock_fill_price`, `order_status`, `note`

## Repo Map

- Entrypoint: `main.py`
- Runtime: `runtime_loop.py`, `runtime_tick.py`, `runtime_helpers.py`
- Feeds: `binance_l1_feed.py`, `polymarket_book_feed.py`, `feeds.py`, `feed_common.py`
- Market selection: `market_selector.py`
- Strategy/model: `strategy.py`, `model.py`
- Mock execution: `mock_exec.py`
- Logging: `event_log.py`
- Tooling: `research_scripts/binance_price_monitor.py`
- Tests: `tests/`
