"""Build (warm) the local yfinance Industry/Sector cache offline.

This script pre-fetches yfinance `industry` / `sector` for a broad universe
of Indian equities so that dashboard/CLI startup doesn't block on yfinance.

Universe:
  - Union of symbols from NSE index constituent CSVs that we already use
    for the on-demand Industry mapping (Nifty 50/100/200/500 etc).

Output:
  - Writes `./.cache/yfinance_industry_cache.json`
    (same file used by `app.instruments`).

Usage:
  python scripts/build_yfinance_cache.py
  python scripts/build_yfinance_cache.py --limit 200
  python scripts/build_yfinance_cache.py --workers 6
  python scripts/build_yfinance_cache.py --backfill-sector
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


try:
    import yfinance as yf  # type: ignore
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "yfinance is not installed. Install it first (recommended):\n"
        "  python -m pip install --user yfinance\n"
        f"Details: {exc}"
    ) from exc


NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://www.nseindia.com/",
}

NSE_INDEX_CSV_URLS: tuple[str, ...] = (
    "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap50list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_niftylargemidcap250list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_niftymidsmallcap400list.csv",
)


def _normalise_name(raw: Any) -> str:
    return str(raw or "").strip()


def _normalise_symbol(raw: Any) -> str:
    return str(raw or "").strip().upper()


def _fetch_csv(url: str) -> str:
    req = Request(url, headers=NSE_HEADERS)
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_universe_symbols() -> list[str]:
    symbols: set[str] = set()
    ordered: list[str] = []
    for url in NSE_INDEX_CSV_URLS:
        body = _fetch_csv(url)
        reader = csv.DictReader(io.StringIO(body))
        for row in reader:
            sym = _normalise_symbol((row or {}).get("Symbol"))
            if not sym or sym in symbols:
                continue
            symbols.add(sym)
            ordered.append(sym)
    return ordered


def cache_paths(project_root: Path) -> Path:
    cache_dir = project_root / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "yfinance_industry_cache.json"


def read_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"mapping": {}, "last_refresh_epoch": 0.0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"mapping": {}, "last_refresh_epoch": 0.0}


def write_cache(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def yf_lookup(symbol: str, exchange: str = "NSE") -> tuple[str, dict[str, str]]:
    # NSE symbol -> .NS ; BSE -> .BO
    suffix = ".NS" if exchange.upper() == "NSE" else ".BO"
    yf_symbol = f"{symbol}{suffix}"
    info = yf.Ticker(yf_symbol).info or {}
    industry = _normalise_name(info.get("industry") or info.get("sector"))
    sector = _normalise_name(info.get("sector"))
    return yf_symbol, {"industry": industry, "sector": sector}


def _entry_needs_yfinance(
    key: str,
    mapping: dict[str, Any],
    *,
    backfill_sector: bool,
) -> bool:
    """True if we should call yfinance for this cache key."""
    if key not in mapping:
        return True
    v = mapping[key]
    if isinstance(v, str):
        return True
    if not isinstance(v, dict):
        return True
    if backfill_sector and not (str(v.get("sector") or "").strip()):
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="limit number of symbols (0=all)")
    parser.add_argument("--workers", type=int, default=6, help="parallel workers")
    parser.add_argument("--exchange", type=str, default="NSE", choices=["NSE", "BSE"], help="yfinance suffix")
    parser.add_argument("--sleep-ms", type=int, default=0, help="optional sleep per completed request")
    parser.add_argument(
        "--backfill-sector",
        action="store_true",
        help="Refetch yfinance for keys already in the cache but missing sector "
        "(or legacy string-only entries).",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    cache_file = cache_paths(project_root)
    payload = read_cache(cache_file)
    mapping: dict[str, dict[str, str]] = payload.get("mapping") or {}

    universe = load_universe_symbols()
    if args.limit and args.limit > 0:
        universe = universe[: args.limit]

    # Keys to fetch: new entries, and optionally sector backfill / legacy rows.
    want_keys = [f"{args.exchange}|{sym}" for sym in universe]
    missing = [
        k
        for k in want_keys
        if _entry_needs_yfinance(k, mapping, backfill_sector=args.backfill_sector)
    ]
    total = len(want_keys)
    print(
        f"Universe: {total} symbols. Cache has: {len(mapping)} entries. "
        f"To fetch: {len(missing)} (backfill_sector={args.backfill_sector})."
    )
    if not missing:
        print("Nothing to do.")
        return 0

    started = time.time()
    done = 0
    saved_at = time.time()

    def _job(k: str) -> tuple[str, str, dict[str, str]]:
        exch, sym = k.split("|", 1)
        yf_sym, out = yf_lookup(sym, exchange=exch)
        return k, yf_sym, out

    with ThreadPoolExecutor(max_workers=max(1, int(args.workers))) as ex:
        futures = {ex.submit(_job, k): k for k in missing}
        for fut in as_completed(futures):
            k = futures[fut]
            try:
                k2, yf_sym, out = fut.result()
                mapping[k2] = out
                done += 1
                ind_s = out.get("industry") or ""
                sec_s = out.get("sector") or ""
                if ind_s or sec_s:
                    print(
                        f"[{done}/{len(missing)}] {yf_sym} -> "
                        f"industry={ind_s or '—'} | sector={sec_s or '—'}"
                    )
                else:
                    print(f"[{done}/{len(missing)}] {yf_sym} -> (empty)")
            except Exception as exc:
                done += 1
                print(f"[{done}/{len(missing)}] {k} FAILED: {exc}")

            # Periodically persist so we can resume if interrupted.
            now = time.time()
            if now - saved_at >= 5.0:
                payload["mapping"] = mapping
                # Consider this a baseline refresh time once we start warming.
                payload["last_refresh_epoch"] = float(payload.get("last_refresh_epoch") or 0.0) or time.time()
                write_cache(cache_file, payload)
                saved_at = now

            if args.sleep_ms and args.sleep_ms > 0:
                time.sleep(args.sleep_ms / 1000.0)

    payload["mapping"] = mapping
    payload["last_refresh_epoch"] = float(payload.get("last_refresh_epoch") or 0.0) or time.time()
    write_cache(cache_file, payload)

    elapsed = time.time() - started
    print(f"Done. Added/attempted: {len(missing)}. Took {elapsed:.1f}s. Cache file: {cache_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

