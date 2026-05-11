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
| `*_reference_debug_snapshot(now)` | Single row (`dict`) or multiple rows (`dict[str, dict]`) for observability — each row SHOULD include `source`, `expires_in_ms`, `refresh_in_progress`, and `cache_day` where applicable. Use `get_source_label` from `model_cache_store.py` for `source`. |

Orchestration: `app.domain.reference_snapshot.warm_reference_snapshot` invokes providers in a fixed order. New providers that participate in dashboard reference data SHOULD be wired there (see `REFERENCE_PROVIDER_WARMUPS` in that module).

## Persistence: choose one family

### A. Model-section style (`read_section` / `update_section`)

Use for self-contained blobs keyed by their own JSON shape. This is the preferred style for all providers.

- Prefer a dedicated section name (e.g. `nse`, `kite`, `mfdata`, `marketsmith`, `yfinance`).
- Use a **module-local** `threading.Lock`.
- Store a day token under `meta.cache_day` or within the section structure so IST rollover is explicit.
- **Examples:** [`nse_provider`](nse_provider.py), [`kite_provider`](kite_provider.py), [`yfinance_provider`](yfinance_provider.py), [`mfdata_provider`](mfdata_provider.py), [`marketsmith_provider`](marketsmith_provider.py).

### B. Joined / Domain Providers

Use when data must be aggregated across multiple source providers.

- Coordinate resolution logic in a dedicated provider (e.g., `equity_metadata_provider`).
- Consumes snapshots or direct lookups from source providers.
- **Example:** [`equity_metadata_provider`](equity_metadata_provider.py).

## IST day boundary (09:00)

- **All entries** should reuse `current_effective_day_ist` / `next_cutoff_epoch_ist` from `model_cache_store.py`. They use `cutoff_hour=9` by default to agree on the same "business day".

## Background network work

- Schedule work with `start_background_refresh_job` in `model_cache_store.py` and a **unique, stable** job name (e.g. `reference-nse-merged-industry`, `marketsmith-daily`).
- Use an in-progress flag per logical resource to avoid duplicate fetches (see NSE / Kite / MarketSmith).

## Dashboard revision

After a **successful** update that should refresh the dashboard’s reference snapshot, call `notify_reference_cache_refresh` in `app/reference_notifications.py`.

- **mfdata:** notifications are sent when the disk section is **flushed** after a successful persist (`flush_mfdata_disk_cache`), not on every in-memory search/holdings hit, to avoid excessive revision bumps.

## Source labels and debug rows

- Use `get_source_label` from `model_cache_store.py` to determine the `source` string based on memory warmth, disk cache day, and refresh status.
- Implement `*_reference_debug_snapshot` and register rows in `get_reference_cache_debug_snapshot` in `app/domain/portfolio_model.py`.

## Optional typing

`reference_provider.py` defines a `typing.Protocol` for `warmup`; modules do not need to subclass anything.
