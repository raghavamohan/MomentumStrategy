Technical Email Version

**Subject:** Technical Analysis: Centralized Live Price Pipeline + 1000-Price Indicator

Hi Raghava,

Completed analysis of live-price processing and future indicator needs.

**Key findings**

- Kite supports batched live subscriptions via WebSocket (`subscribe([...tokens])` + `set_mode(MODE_LTP, [...tokens]))`).
- `app/live_prices.py` already batches token subscription.
- Live price handling is still duplicated:
  - REST quote (`kite.quote`) bootstrap values
  - WebSocket LTP overlay on the request/render path
- Under heavier compute or load, WebSocket listener fan-out and bounded-queue drops can become limiting.
- The subscription set grows during runtime and is not actively reconciled down.

**Recommended architecture** (separate worker + fixed interval)

- Single central tick hub for normalized events (`token`, `ltp`, `ts`).
- Dedicated indicator worker: rolling last 1000 prices per token; compute on a fixed cadence (e.g. 1s / 5s).
- Shared state: latest LTP per token; latest indicator value plus timestamp/staleness.
- Dashboard/WebSocket consumes centralized state only.

**Why this helps**

- Keeps expensive compute out of HTTP/WebSocket request paths.
- Avoids per-tick compute explosions.
- Improves scalability and isolation.
- Clarifies data ownership and reduces duplicate merge logic.

**Suggested next-week scope (MVP)**

- Central tick hub abstraction.
- Ring buffer (size 1000) per token.
- Fixed-interval indicator worker.
- Expose indicator snapshot to WebSocket consumers.
- Basic metrics: tick rate, queue depth, compute duration, drops.

Detailed analysis is below (this document).

Thanks,  
Raghava

---

# Live Price Optimization Analysis

**Date:** 2026-05-09  
**Project:** MomentumStrategy

## Goal

Optimize live price handling so expensive per-stock indicators (based on the last 1000 prices) can be computed reliably and efficiently, while centralizing live-price processing across all stocks.

## Kite API capability

**Yes.** Kite supports this pattern:

- WebSocket: subscribe to multiple instrument tokens in one call with a token list.
- `MODE_LTP` can be set for a list of instruments.
- REST `/quote/ltp` can fetch LTP for multiple instruments in one call; for ongoing updates, WebSocket is the right mechanism.

A single central streaming pipeline aligns with Kite’s APIs.

## Current application flow

### 1. Tick ingestion

- `app/live_prices.py` hosts `LivePriceStream`, a process-wide `KiteTicker` wrapper.
- `ensure_running()` starts one threaded WebSocket client.
- `_on_ticks()` receives tick batches, extracts `instrument_token` and `last_price`, updates `_ltp_by_token`, and notifies listeners.

### 2. Dashboard integration

**`/dashboard` (`app/web.py`)**

- Fetches holdings, positions, margins, and profile via REST.
- Fetches watch/index quotes via `kite.quote()` (day cache in `_get_cached_quotes`).
- Builds token set from holdings, open positions, indices, and watchlist.
- Calls `live_price_stream.subscribe(tokens)`.
- Reads immediate `snapshot_ltp(...)` to overlay streamed prices when available.

**`/ws/live-prices` (`app/web.py`)**

- Registers a listener on `live_price_stream`.
- Coalesces tick batches with `_LtpCoalescer`.
- Sends browser updates as `{ "ltp": { "<token>": price } }`.

### 3. Row-level model overlay

- `app/portfolio_model.py` `overlay_live_ltp()` injects WebSocket LTP into holdings/positions rows.
- `build_equity_holding()` / `build_position()` recompute selected metrics when `_live_ltp_applied` is true.

### 4. CLI path

- `app/main.py` watchlist uses REST `kite.quote(...)` snapshots only (no WebSocket stream).

## Findings

1. **Subscriptions are already batched.**  
   `live_price_stream.subscribe(...)` computes a fresh token set and calls `ticker.subscribe(list(fresh))` and `ticker.set_mode(MODE_LTP, list(fresh))`. On reconnect, `on_connect` re-subscribes the full list at once.

2. **Live LTP is duplicated during dashboard render.**  
   REST quote `last_price` bootstraps; streamed LTP overlays it. That helps first-render resilience but duplicates live-price handling and merge points.

3. **Tick fan-out cost scales with listeners.**  
   Each WebSocket client adds a listener; fan-out runs from the ticker thread. Under heavy tick load and many clients, this can become a pressure point.

4. **Backpressure drops batches.**  
   `/ws/live-prices` uses a bounded queue with drop-oldest on overflow—good for UI liveness, but there is no centralized flow-control contract yet.

5. **Subscription set grows for the process lifetime.**  
   `LivePriceStream.subscribe()` adds tokens but does not prune removed symbols unless the stream is closed or reset.

## Recommended target architecture

**Direction:** separate worker/service; updates on a fixed interval (not per tick).

### Centralized design

1. **Central tick hub** — one ingestion point for Kite ticks; normalize each update to (`token`, `ltp`, `ts`); publish to consumers via an internal bus or queue.

2. **Indicator worker** — consumes events; per-token rolling buffer (size 1000); run expensive indicators on a fixed cadence (e.g. every 1s or 5s); emit latest indicator snapshot to shared state.

3. **Read path** — UI/WebSocket reads latest LTP and latest computed indicator; no expensive indicators inline in WebSocket callbacks or HTTP handlers.

4. **State model**

   - `latest_ltp[token]`
   - `rolling_prices[token]` (ring buffer of 1000)
   - `indicator_state[token]` (value, timestamp, staleness)

## Additional suggestions

1. **Phasing** — start with in-process boundaries (central hub + worker thread/process), then externalize (e.g. Redis/NATS/Kafka) when load warrants it.

2. **Compute queues** — use “latest wins”: do not queue every tick; keep at most one pending event per token per interval window.

3. **Token lifecycle** — add something like `set_subscriptions(desired_tokens)` to reconcile add/remove and avoid stale long-lived subscriptions.

4. **REST bootstrap** — use REST for metadata, previous close, and first-paint fallback; avoid REST and stream competing as intraday truth for live fields.

5. **Observability before scaling** — counters: ticks/sec ingested, compute interval duration, queue depth/drops, active token count, stale indicator count. Logs: slow compute, reconnects, auth/token failures.

6. **Dashboard responsiveness** — keep coalescing WebSocket sends; optionally send indicators on a separate channel or cadence from raw LTP.

7. **SLOs** — e.g. LTP propagation P95 < 250 ms; indicator freshness ≤ configured interval + compute budget; no unbounded queues.

8. **Tests for rolling windows** — ring buffer edge cases: fewer than 1000 samples, exactly 1000, rollover, missing-tick intervals.

## Implementation phases

### Phase 1 (next-week MVP)

- Central tick hub abstraction.
- Rolling ring buffer (1000 prices per token).
- Fixed-interval indicator worker in a separate runtime unit.
- In-memory read API for latest indicator state.
- Optional: wire dashboard WebSocket to include indicator values.

### Phase 2

- Subscription reconciliation and token pruning.
- Instrumentation and alerting thresholds.
- Tune CPU/memory of the compute loop.

### Phase 3

- Dedicated worker service if needed.
- Durable/event middleware only when scale or reliability requires it.

## Risks

- CPU spikes from synchronized interval compute across many tokens.
- Memory growth with many active tokens if cleanup is weak.
- Tick bursts and queue contention without coalescing.
- Hidden coupling if UI and compute use different LTP sources.

## Email-ready summary (copy/paste)

**Subject:** Live price processing refactor analysis (MomentumStrategy)

I reviewed how live prices are handled today. Kite supports batched streaming for multiple instruments in one WebSocket subscription call. The app already batches token subscriptions in `app/live_prices.py`, but live price handling still spans dashboard request logic, WebSocket fan-out, and REST quote overlays.

For the upcoming expensive indicator (last 1000 prices per stock), I recommend centralizing tick ingestion in one hub and moving indicator computation to a separate worker on a fixed interval. That avoids heavy per-tick work, reduces coupling in request handlers, and scales cleanly. An MVP can ship next week with an in-process hub and worker boundary, then move out-of-process if needed.

The write-up covers bottlenecks (listener fan-out, queue drops, growing subscription set), implementation phases, observability, and risks.
