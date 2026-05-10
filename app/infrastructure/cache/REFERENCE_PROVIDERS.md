# Reference data providers

Modules named `*_provider.py` under `app/infrastructure/cache/` supply cached lookups used by `app.domain.reference_snapshot` and `app.domain.portfolio_model`. Follow this convention when adding a new provider.

## Module API

Each provider module SHOULD export:

| Export | Purpose |
|--------|---------|
| `warmup(ctx: WarmupContext) -> None` | Preload disk state and optionally trigger refresh; see `app/domain/reference_context.py` for which fields each provider reads |
| Stable read helpers | Explicit names such as `get_*`, `lookup_*`; avoid hiding I/O behind generic getters |
| `__all__` | Marks the stable public surface for importers |

Optional but recommended:

| Export | Purpose |
|--------|---------|
| `*_reference_debug_snapshot(now)` | Single row (`dict`) or multiple rows (`dict[str, dict]`) for observability — each row SHOULD include `source`, `expires_in_ms`, `refresh_in_progress` where applicable |

Orchestration: `app.domain.reference_snapshot.warm_reference_snapshot` invokes providers in a fixed order. New providers that participate in dashboard reference data SHOULD be wired there (see `REFERENCE_PROVIDER_WARMUPS` in that module).

## Persistence: choose one family

### A. Reference-disk style (`reference_data` in `model_cache.json`)

Use when data is shared with instrument / NSE reference resolution and must stay consistent with other reference entries.

- Coordinate through [`reference_cache_internal`](reference_cache_internal.py): `instrument_reference_lock`, `_reference_cache_get_entry_unlocked`, `_reference_cache_set_entry_unlocked`.
- Record provenance via `REFERENCE_CACHE_LAST_SOURCE` keys (with `set_reference_last_source` in `reference_cache_internal.py` when updating outside the unlocked reference-disk path).
- **Examples:** [`kite_provider`](kite_provider.py), [`nse_provider`](nse_provider.py).

### B. Model-section style (`read_section` / `update_section`)

Use for self-contained blobs keyed by their own JSON shape.

- Prefer a dedicated section name (e.g. `mfdata`, `marketsmith`, `yfinance`).
- Use a **module-local** `threading.Lock`; do not share `instrument_reference_lock` unless you are also mutating reference-disk payloads.
- Store a day token under `meta.cache_day` or `cache_day` so IST rollover (see below) is explicit.
- **Examples:** [`yfinance_provider`](yfinance_provider.py), [`mfdata_provider`](mfdata_provider.py), [`marketsmith_provider`](marketsmith_provider.py).

## IST day boundary (09:00)

- **Reference-disk entries** reuse `current_effective_day_ist` / `next_cutoff_epoch_ist` from `model_cache_store.py` via `_current_reference_day_token()` in `reference_cache_internal`.
- **Model sections** SHOULD use `current_effective_day_ist(cutoff_hour=9)` (or the same convention) for `cache_day` fields so all providers agree on the “business day”.

## Background network work

- Schedule work with `start_background_refresh_job` in `model_cache_store.py` and a **unique, stable** job name (e.g. `reference-nse-merged-industry`, `marketsmith-daily`).
- Use an in-progress flag per logical resource to avoid duplicate fetches (see NSE / Kite / MarketSmith).

## Dashboard revision

After a **successful** update that should refresh the dashboard’s reference snapshot, call `notify_reference_cache_refresh` in `app/reference_notifications.py`.

- **mfdata:** notifications are sent when the disk section is **flushed** after a successful persist (`flush_mfdata_disk_cache`), not on every in-memory search/holdings hit, to avoid excessive revision bumps.

## Source labels and debug rows

- **Reference-disk keys** use `REFERENCE_CACHE_LAST_SOURCE` (and may be mirrored for model-cache providers — e.g. `yfinance` — for a unified debug view).
- Implement `*_reference_debug_snapshot` and register rows in `get_reference_cache_debug_snapshot` in `app/domain/portfolio_model.py`.

## Optional typing

`reference_provider.py` defines a `typing.Protocol` for `warmup`; modules do not need to subclass anything.
