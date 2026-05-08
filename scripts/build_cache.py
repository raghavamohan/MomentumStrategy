"""Build (warm) local disk caches used by dashboard/CLI.

This script can warm:
1) yfinance industry/sector cache
2) shared reference_data cache (NSE maps, Nifty list, cash-equity maps)

Usage:
  python scripts/build_cache.py
  python scripts/build_cache.py --workers 6
  python scripts/build_cache.py --backfill-sector
  python scripts/build_cache.py --reference-only
  python scripts/build_cache.py --skip-reference-cache
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.auth import (  # noqa: E402
    build_authenticated_client,
    load_cached_access_token,
    load_credentials,
    validate_kite_session,
)
from app.instruments import (  # noqa: E402
    get_cash_equity_isin_lookups,
    get_cash_equity_kite_sector_lookups,
    get_cash_equity_name_lookups,
    get_isin_to_industry,
    get_nifty50_symbols,
    get_nse_symbol_to_industry,
    get_nse_symbol_to_token_lookup,
    get_reference_cache_debug_snapshot,
)

try:
    import yfinance as yf  # type: ignore  # noqa: E402
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


def yfinance_cache_path(project_root: Path) -> Path:
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


def warm_yfinance_cache(args: argparse.Namespace, project_root: Path) -> Path:
    cache_file = yfinance_cache_path(project_root)
    payload = read_cache(cache_file)
    mapping: dict[str, dict[str, str]] = payload.get("mapping") or {}

    universe = load_universe_symbols()
    if args.limit and args.limit > 0:
        universe = universe[: args.limit]

    want_keys = [f"{args.exchange}|{sym}" for sym in universe]
    missing = [
        k
        for k in want_keys
        if _entry_needs_yfinance(k, mapping, backfill_sector=args.backfill_sector)
    ]

    print(
        f"Universe: {len(want_keys)} symbols. Cache has: {len(mapping)} entries. "
        f"To fetch: {len(missing)} (backfill_sector={args.backfill_sector})."
    )
    if not missing:
        print("Yfinance cache already warm for selected universe.")
        return cache_file

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
                        f"industry={ind_s or '-'} | sector={sec_s or '-'}"
                    )
                else:
                    print(f"[{done}/{len(missing)}] {yf_sym} -> (empty)")
            except Exception as exc:
                done += 1
                print(f"[{done}/{len(missing)}] {k} FAILED: {exc}")

            now = time.time()
            if now - saved_at >= 5.0:
                payload["mapping"] = mapping
                payload["last_refresh_epoch"] = float(payload.get("last_refresh_epoch") or 0.0) or time.time()
                write_cache(cache_file, payload)
                saved_at = now

            if args.sleep_ms and args.sleep_ms > 0:
                time.sleep(args.sleep_ms / 1000.0)

    payload["mapping"] = mapping
    payload["last_refresh_epoch"] = float(payload.get("last_refresh_epoch") or 0.0) or time.time()
    write_cache(cache_file, payload)
    print(f"Yfinance warmup done. Added/attempted: {len(missing)}. Took {time.time() - started:.1f}s.")
    return cache_file


def warm_reference_cache() -> None:
    print()
    print("Warming shared reference cache (.cache/reference_data_cache.json)...")
    try:
        nse_symbol_to_industry = get_nse_symbol_to_industry()
        isin_to_industry = get_isin_to_industry()
        nifty50_symbols = get_nifty50_symbols()
        print(
            "NSE references: "
            f"symbol->industry={len(nse_symbol_to_industry)}, "
            f"isin->industry={len(isin_to_industry)}, "
            f"nifty50={len(nifty50_symbols)}"
        )
    except Exception as exc:
        print(f"NSE reference warmup FAILED: {exc}")

    token = load_cached_access_token()
    if not token:
        print("Cash-equity reference warmup skipped: no cached Kite access token.")
    else:
        try:
            api_key, _ = load_credentials()
            kite = build_authenticated_client(api_key, token)
            if not validate_kite_session(kite):
                print("Cash-equity reference warmup skipped: cached Kite token expired.")
            else:
                token_to_name, symbol_to_name = get_cash_equity_name_lookups(kite)
                token_to_sector, symbol_to_sector = get_cash_equity_kite_sector_lookups(kite)
                token_to_isin, symbol_to_isin = get_cash_equity_isin_lookups(kite)
                nse_symbol_to_token = get_nse_symbol_to_token_lookup(kite)
                print(
                    "Cash-equity references: "
                    f"token->name={len(token_to_name)}, "
                    f"symbol->name={len(symbol_to_name)}, "
                    f"token->sector={len(token_to_sector)}, "
                    f"symbol->sector={len(symbol_to_sector)}, "
                    f"token->isin={len(token_to_isin)}, "
                    f"symbol->isin={len(symbol_to_isin)}, "
                    f"nse_symbol->token={len(nse_symbol_to_token)}"
                )
        except Exception as exc:
            print(f"Cash-equity reference warmup FAILED: {exc}")

    try:
        cache_debug = get_reference_cache_debug_snapshot()
        for name in ("cash_equity", "nse_merged_industry", "nifty50_symbols"):
            meta = cache_debug.get(name) or {}
            print(
                f"{name}: source={meta.get('source','unknown')}, "
                f"expires_in_ms={float(meta.get('expires_in_ms') or 0.0):.1f}, "
                f"refreshing={bool(meta.get('refresh_in_progress'))}"
            )
    except Exception as exc:
        print(f"Reference cache debug snapshot unavailable: {exc}")


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
    parser.add_argument(
        "--skip-reference-cache",
        action="store_true",
        help="Skip warming shared reference_data_cache.json entries.",
    )
    parser.add_argument(
        "--reference-only",
        action="store_true",
        help="Warm only reference_data_cache.json (skip yfinance cache warmup).",
    )
    args = parser.parse_args()

    if args.reference_only and args.skip_reference_cache:
        raise SystemExit("Invalid flags: --reference-only cannot be combined with --skip-reference-cache.")

    cache_file = yfinance_cache_path(PROJECT_ROOT)
    if args.reference_only:
        print("Skipping yfinance warmup (--reference-only).")
    else:
        cache_file = warm_yfinance_cache(args, PROJECT_ROOT)

    if not args.skip_reference_cache:
        warm_reference_cache()

    print(f"Done. Yfinance cache file: {cache_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
