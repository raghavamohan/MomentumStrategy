"""Instrument metadata helpers for display labels and industry classification.

Builds a cached lookup of cash-equity company names from
``kite.instruments("NSE")`` and ``kite.instruments("BSE")``, and resolves
**industry** using Kite instrument rows, merged NSE index CSVs (Industry +
ISIN), and ISIN alignment for BSE cash equities.
"""

from __future__ import annotations

import csv
import io
import threading
import time
from typing import Any
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
import logging


EQUITY_EXCHANGES = ("NSE", "BSE")
_CACHE_TTL_SECONDS = 60 * 60
_CACHE_LOCK = threading.Lock()
_CACHE_EXPIRES_AT = 0.0
_CACHED_TOKEN_TO_NAME: dict[int, str] = {}
_CACHED_SYMBOL_TO_NAME: dict[tuple[str, str], str] = {}
_CACHED_SYMBOL_TO_TOKEN: dict[tuple[str, str], int] = {}
_CACHED_TOKEN_TO_INDUSTRY: dict[int, str] = {}
_CACHED_SYMBOL_TO_INDUSTRY: dict[tuple[str, str], str] = {}
_CACHED_TOKEN_TO_KITE_SECTOR: dict[int, str] = {}
_CACHED_SYMBOL_TO_KITE_SECTOR: dict[tuple[str, str], str] = {}
_CACHED_TOKEN_TO_ISIN: dict[int, str] = {}
_CACHED_SYMBOL_TO_ISIN: dict[tuple[str, str], str] = {}

_NIFTY50_CSV_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
_NSE_INDUSTRY_CSV_URLS: tuple[str, ...] = (
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
# NSE CSV downloads are relatively expensive; keep these lookups warm longer.
_NIFTY50_CACHE_TTL_SECONDS = 12 * 60 * 60
_NIFTY50_CACHE_EXPIRES_AT = 0.0
_CACHED_NIFTY50_SYMBOLS: list[str] = []
_NSE_MERGED_INDUSTRY_EXPIRES_AT = 0.0
_CACHED_NSE_SYMBOL_TO_INDUSTRY: dict[str, str] = {}
_CACHED_ISIN_TO_INDUSTRY: dict[str, str] = {}
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://www.nseindia.com/",
}

logger = logging.getLogger(__name__)

try:
    import yfinance as yf  # type: ignore
except Exception:  # pragma: no cover - yfinance is optional at runtime
    yf = None

_CACHED_YFINANCE_KEY_TO_INDUSTRY: dict[tuple[str, str], str] = {}
_CACHED_YFINANCE_KEY_TO_SECTOR: dict[tuple[str, str], str] = {}

_YFINANCE_CACHE_LOCK = threading.Lock()
_YFINANCE_CACHE_LOADED = False
_YFINANCE_CACHE_REFRESH_IN_PROGRESS = False
_YFINANCE_REFRESH_THREAD_STARTED = False
_YFINANCE_SYMBOL_REFRESH_IN_PROGRESS: set[tuple[str, str]] = set()

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_YFINANCE_CACHE_DIR = _PROJECT_ROOT / ".cache"
_YFINANCE_CACHE_FILE = _YFINANCE_CACHE_DIR / "yfinance_industry_cache.json"
_YFINANCE_CACHE_REFRESH_DAYS = 30.0

_YFINANCE_CACHE_LAST_REFRESH_EPOCH = 0.0


def _yfinance_cache_key(exchange: str, symbol: str) -> tuple[str, str]:
    return (exchange.upper().strip(), _normalise_symbol(symbol))


def _load_yfinance_cache_if_needed() -> None:
    global _YFINANCE_CACHE_LOADED
    if _YFINANCE_CACHE_LOADED:
        return

    with _YFINANCE_CACHE_LOCK:
        if _YFINANCE_CACHE_LOADED:
            return

        _YFINANCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if _YFINANCE_CACHE_FILE.exists():
            try:
                import json

                payload = json.loads(_YFINANCE_CACHE_FILE.read_text(encoding="utf-8"))
                mapping = payload.get("mapping") or {}
                for k, v in mapping.items():
                    # key is stored as "EXCHANGE|SYMBOL"
                    try:
                        exch, sym = str(k).split("|", 1)
                    except ValueError:
                        continue
                    key = _yfinance_cache_key(exch, sym)
                    if isinstance(v, dict):
                        ind = _normalise_name(v.get("industry"))
                        sec = _normalise_name(v.get("sector"))
                    else:
                        ind = _normalise_name(v)
                        sec = ""
                    _CACHED_YFINANCE_KEY_TO_INDUSTRY[key] = ind
                    _CACHED_YFINANCE_KEY_TO_SECTOR[key] = sec
                globals()["_YFINANCE_CACHE_LAST_REFRESH_EPOCH"] = float(
                    payload.get("last_refresh_epoch") or 0.0
                )
            except Exception as exc:
                logger.warning("Failed to read yfinance cache file: %s", exc)

        _YFINANCE_CACHE_LOADED = True


def _persist_yfinance_cache() -> None:
    """Persist cached yfinance mapping to disk (best-effort)."""
    with _YFINANCE_CACHE_LOCK:
        try:
            import json

            _YFINANCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _YFINANCE_CACHE_FILE.with_suffix(".tmp")
            mapping: dict[str, dict[str, str]] = {}
            all_keys = set(_CACHED_YFINANCE_KEY_TO_INDUSTRY) | set(
                _CACHED_YFINANCE_KEY_TO_SECTOR
            )
            for exch, sym in all_keys:
                ind = _CACHED_YFINANCE_KEY_TO_INDUSTRY.get((exch, sym), "")
                sec = _CACHED_YFINANCE_KEY_TO_SECTOR.get((exch, sym), "")
                mapping[f"{exch}|{sym}"] = {
                    "industry": ind,
                    "sector": sec,
                }

            payload = {
                "mapping": mapping,
                "last_refresh_epoch": _YFINANCE_CACHE_LAST_REFRESH_EPOCH,
            }
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_YFINANCE_CACHE_FILE)
        except Exception as exc:
            logger.warning("Failed to persist yfinance cache: %s", exc)


def _maybe_start_monthly_yfinance_refresh() -> None:
    """Start a background refresh once per ~month."""
    global _YFINANCE_REFRESH_THREAD_STARTED, _YFINANCE_CACHE_REFRESH_IN_PROGRESS

    _load_yfinance_cache_if_needed()
    with _YFINANCE_CACHE_LOCK:
        if _YFINANCE_REFRESH_THREAD_STARTED:
            return

        cache_has_entries = bool(_CACHED_YFINANCE_KEY_TO_INDUSTRY)
        if not cache_has_entries:
            return

        if (
            _YFINANCE_CACHE_LAST_REFRESH_EPOCH
            and (time.time() - _YFINANCE_CACHE_LAST_REFRESH_EPOCH)
            < (_YFINANCE_CACHE_REFRESH_DAYS * 24.0 * 3600.0)
        ):
            return

        _YFINANCE_REFRESH_THREAD_STARTED = True

    def _refresh_job() -> None:
        global _YFINANCE_CACHE_REFRESH_IN_PROGRESS, _YFINANCE_CACHE_LAST_REFRESH_EPOCH, _YFINANCE_REFRESH_THREAD_STARTED
        with _YFINANCE_CACHE_LOCK:
            if _YFINANCE_CACHE_REFRESH_IN_PROGRESS:
                return
            _YFINANCE_CACHE_REFRESH_IN_PROGRESS = True

        try:
            if yf is None:
                return

            with _YFINANCE_CACHE_LOCK:
                keys = list(_CACHED_YFINANCE_KEY_TO_INDUSTRY.keys())

            updated = 0
            for exch, sym in keys:
                if not sym:
                    continue
                yf_symbol = sym
                if "." not in yf_symbol:
                    suffix = ".NS" if exch == "NSE" else ".BO"
                    yf_symbol = f"{yf_symbol}{suffix}"

                try:
                    info = yf.Ticker(yf_symbol).info or {}
                    ind = _normalise_name(info.get("industry") or info.get("sector"))
                    sec = _normalise_name(info.get("sector"))
                    with _YFINANCE_CACHE_LOCK:
                        _CACHED_YFINANCE_KEY_TO_INDUSTRY[(exch, sym)] = ind
                        _CACHED_YFINANCE_KEY_TO_SECTOR[(exch, sym)] = sec
                    if ind:
                        updated += 1
                except Exception:
                    # Ignore per-ticker failures in background refresh.
                    continue

            _YFINANCE_CACHE_LAST_REFRESH_EPOCH = time.time()
            if updated:
                logger.info("yfinance cache refreshed for %d symbols", updated)
            _persist_yfinance_cache()
        finally:
            with _YFINANCE_CACHE_LOCK:
                _YFINANCE_CACHE_REFRESH_IN_PROGRESS = False
                _YFINANCE_REFRESH_THREAD_STARTED = False

    threading.Thread(target=_refresh_job, daemon=True).start()


def _maybe_start_single_symbol_yfinance_refresh(exchange: str, symbol: str) -> None:
    """Best-effort async refresh for one symbol when cache row is missing."""
    clean_exchange = str(exchange or "").strip().upper()
    clean_symbol = _normalise_symbol(symbol)
    if yf is None or clean_exchange not in EQUITY_EXCHANGES or not clean_symbol:
        return

    key = _yfinance_cache_key(clean_exchange, clean_symbol)
    with _YFINANCE_CACHE_LOCK:
        if key in _YFINANCE_SYMBOL_REFRESH_IN_PROGRESS:
            return
        _YFINANCE_SYMBOL_REFRESH_IN_PROGRESS.add(key)

    def _refresh_one() -> None:
        try:
            yf_symbol = clean_symbol
            if "." not in yf_symbol:
                suffix = ".NS" if clean_exchange == "NSE" else ".BO"
                yf_symbol = f"{yf_symbol}{suffix}"
            info = yf.Ticker(yf_symbol).info or {}
            y_ind = _normalise_name(info.get("industry") or info.get("sector"))
            y_sec = _normalise_name(info.get("sector"))

            global _YFINANCE_CACHE_LAST_REFRESH_EPOCH
            with _YFINANCE_CACHE_LOCK:
                _CACHED_YFINANCE_KEY_TO_INDUSTRY[key] = y_ind
                _CACHED_YFINANCE_KEY_TO_SECTOR[key] = y_sec
                if not _YFINANCE_CACHE_LAST_REFRESH_EPOCH:
                    _YFINANCE_CACHE_LAST_REFRESH_EPOCH = time.time()
            _persist_yfinance_cache()
        except Exception as exc:
            logger.warning(
                "background yfinance refresh failed for %s:%s: %s",
                clean_exchange,
                clean_symbol,
                exc,
            )
        finally:
            with _YFINANCE_CACHE_LOCK:
                _YFINANCE_SYMBOL_REFRESH_IN_PROGRESS.discard(key)

    threading.Thread(target=_refresh_one, daemon=True).start()


def _normalise_name(raw: Any) -> str:
    """Return a trimmed company name or empty string."""
    return str(raw or "").strip()


def _normalise_symbol(raw: Any) -> str:
    """Return a trimmed symbol in uppercase or empty string."""
    return str(raw or "").strip().upper()


def _normalise_isin(raw: Any) -> str:
    s = str(raw or "").strip().upper().replace(" ", "")
    return s


def _kite_row_is_equity_cash(row: dict) -> bool:
    return str(row.get("instrument_type") or "").strip().upper() == "EQ"


def _kite_row_industry(row: dict) -> str:
    """Industry / sector string from a Kite instrument row (EQ only)."""
    if not _kite_row_is_equity_cash(row):
        return ""
    return _normalise_name(
        row.get("industry")
        or row.get("Industry")
        or row.get("sector")
        or row.get("Sector")
    )


def _kite_row_sector_only(row: dict) -> str:
    """Exchange ``sector`` field from Kite instrument row (EQ only)."""
    if not _kite_row_is_equity_cash(row):
        return ""
    return _normalise_name(row.get("sector") or row.get("Sector"))


def _refresh_cash_equity_cache(kite) -> None:
    """Refresh equity instrument caches when TTL expires."""
    global _CACHE_EXPIRES_AT, _CACHED_TOKEN_TO_NAME, _CACHED_SYMBOL_TO_NAME, _CACHED_SYMBOL_TO_TOKEN
    global _CACHED_TOKEN_TO_INDUSTRY, _CACHED_SYMBOL_TO_INDUSTRY
    global _CACHED_TOKEN_TO_KITE_SECTOR, _CACHED_SYMBOL_TO_KITE_SECTOR
    global _CACHED_TOKEN_TO_ISIN, _CACHED_SYMBOL_TO_ISIN
    now = time.time()
    if now < _CACHE_EXPIRES_AT and _CACHED_TOKEN_TO_NAME and _CACHED_SYMBOL_TO_NAME:
        return

    token_to_name: dict[int, str] = {}
    symbol_to_name: dict[tuple[str, str], str] = {}
    symbol_to_token: dict[tuple[str, str], int] = {}
    token_to_industry: dict[int, str] = {}
    symbol_to_industry: dict[tuple[str, str], str] = {}
    token_to_kite_sector: dict[int, str] = {}
    symbol_to_kite_sector: dict[tuple[str, str], str] = {}
    token_to_isin: dict[int, str] = {}
    symbol_to_isin: dict[tuple[str, str], str] = {}

    for exchange in EQUITY_EXCHANGES:
        try:
            instruments = kite.instruments(exchange) or []
        except Exception:
            continue

        for row in instruments:
            symbol = _normalise_symbol(row.get("tradingsymbol"))
            token = int(row.get("instrument_token") or 0)
            if symbol and token > 0:
                symbol_to_token[(exchange, symbol)] = token

            k_ind = _kite_row_industry(row)
            if k_ind:
                if token > 0:
                    token_to_industry[token] = k_ind
                if symbol:
                    symbol_to_industry[(exchange, symbol)] = k_ind

            k_sec = _kite_row_sector_only(row)
            if k_sec:
                if token > 0:
                    token_to_kite_sector[token] = k_sec
                if symbol:
                    symbol_to_kite_sector[(exchange, symbol)] = k_sec

            if _kite_row_is_equity_cash(row):
                isin = _normalise_isin(row.get("isin") or row.get("ISIN"))
                if isin:
                    if token > 0:
                        token_to_isin[token] = isin
                    if symbol:
                        symbol_to_isin[(exchange, symbol)] = isin

            name = _normalise_name(row.get("name"))
            if not name:
                continue

            if symbol:
                symbol_to_name[(exchange, symbol)] = name
            if token > 0:
                token_to_name[token] = name

    _CACHED_TOKEN_TO_NAME = token_to_name
    _CACHED_SYMBOL_TO_NAME = symbol_to_name
    _CACHED_SYMBOL_TO_TOKEN = symbol_to_token
    _CACHED_TOKEN_TO_INDUSTRY = token_to_industry
    _CACHED_SYMBOL_TO_INDUSTRY = symbol_to_industry
    _CACHED_TOKEN_TO_KITE_SECTOR = token_to_kite_sector
    _CACHED_SYMBOL_TO_KITE_SECTOR = symbol_to_kite_sector
    _CACHED_TOKEN_TO_ISIN = token_to_isin
    _CACHED_SYMBOL_TO_ISIN = symbol_to_isin
    _CACHE_EXPIRES_AT = now + _CACHE_TTL_SECONDS


def get_cash_equity_name_lookups(kite) -> tuple[dict[int, str], dict[tuple[str, str], str]]:
    """Return cached (token->name, (exchange,symbol)->name) mappings.

    The cache is refreshed at most once per hour to avoid re-downloading
    the large instrument master on every dashboard refresh.
    """
    with _CACHE_LOCK:
        _refresh_cash_equity_cache(kite)
        return (dict(_CACHED_TOKEN_TO_NAME), dict(_CACHED_SYMBOL_TO_NAME))


def get_cash_equity_industry_lookups(kite) -> tuple[dict[int, str], dict[tuple[str, str], str]]:
    """Cached (token->industry, (exchange,symbol)->industry) from Kite EQ rows."""
    with _CACHE_LOCK:
        _refresh_cash_equity_cache(kite)
        return (dict(_CACHED_TOKEN_TO_INDUSTRY), dict(_CACHED_SYMBOL_TO_INDUSTRY))


def get_cash_equity_kite_sector_lookups(kite) -> tuple[dict[int, str], dict[tuple[str, str], str]]:
    """Cached Kite ``sector`` column only (EQ rows)."""
    with _CACHE_LOCK:
        _refresh_cash_equity_cache(kite)
        return (dict(_CACHED_TOKEN_TO_KITE_SECTOR), dict(_CACHED_SYMBOL_TO_KITE_SECTOR))


def get_cash_equity_isin_lookups(kite) -> tuple[dict[int, str], dict[tuple[str, str], str]]:
    """Cached (token->ISIN, (exchange,symbol)->ISIN) for cash EQ instruments."""
    with _CACHE_LOCK:
        _refresh_cash_equity_cache(kite)
        return (dict(_CACHED_TOKEN_TO_ISIN), dict(_CACHED_SYMBOL_TO_ISIN))


def get_nse_symbol_to_token_lookup(kite) -> dict[str, int]:
    """Return cached NSE ``tradingsymbol -> instrument_token`` mapping."""
    with _CACHE_LOCK:
        _refresh_cash_equity_cache(kite)
        return {
            symbol: token
            for (exchange, symbol), token in _CACHED_SYMBOL_TO_TOKEN.items()
            if exchange == "NSE" and token > 0
        }


def _fetch_nse_industry_csv_body(url: str) -> str | None:
    req = Request(url, headers=_NSE_HEADERS)
    try:
        with urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("Failed to fetch NSE industry CSV %s: %s", url, exc)
        return None


def _merge_industry_rows(body: str, nse_symbol_out: dict[str, str], isin_out: dict[str, str]) -> None:
    reader = csv.DictReader(io.StringIO(body))
    for row in reader:
        r = row or {}
        symbol = _normalise_symbol(r.get("Symbol"))
        industry = _normalise_name(r.get("Industry"))
        isin = _normalise_isin(r.get("ISIN Code") or r.get("ISIN"))
        if symbol and industry:
            nse_symbol_out[symbol] = industry
        if isin and industry:
            isin_out[isin] = industry


def _refresh_nse_merged_industry_unlocked() -> bool:
    """Merge Industry + ISIN from multiple NSE index CSVs. Returns False if all failed."""
    global _NSE_MERGED_INDUSTRY_EXPIRES_AT, _CACHED_NSE_SYMBOL_TO_INDUSTRY, _CACHED_ISIN_TO_INDUSTRY
    now = time.time()
    if now < _NSE_MERGED_INDUSTRY_EXPIRES_AT and (
        _CACHED_NSE_SYMBOL_TO_INDUSTRY or _CACHED_ISIN_TO_INDUSTRY
    ):
        return True

    nse_sym: dict[str, str] = {}
    isin_map: dict[str, str] = {}
    any_ok = False
    for url in _NSE_INDUSTRY_CSV_URLS:
        body = _fetch_nse_industry_csv_body(url)
        if not body:
            continue
        any_ok = True
        _merge_industry_rows(body, nse_sym, isin_map)

    if not any_ok:
        return False

    _CACHED_NSE_SYMBOL_TO_INDUSTRY = nse_sym
    _CACHED_ISIN_TO_INDUSTRY = isin_map
    _NSE_MERGED_INDUSTRY_EXPIRES_AT = now + _NIFTY50_CACHE_TTL_SECONDS
    return True


def get_nse_symbol_to_industry() -> dict[str, str]:
    """NSE ``Symbol -> Industry`` merged from broad NSE index constituent CSVs."""
    with _CACHE_LOCK:
        if not _refresh_nse_merged_industry_unlocked():
            return {}
        return dict(_CACHED_NSE_SYMBOL_TO_INDUSTRY)


def get_isin_to_industry() -> dict[str, str]:
    """``ISIN -> Industry`` merged from the same NSE index CSVs (covers cross-listed names)."""
    with _CACHE_LOCK:
        if not _refresh_nse_merged_industry_unlocked():
            return {}
        return dict(_CACHED_ISIN_TO_INDUSTRY)


def _refresh_nifty50_cache_unlocked() -> bool:
    """Load Nifty50 symbol order from NSE CSV (watch list)."""
    global _NIFTY50_CACHE_EXPIRES_AT, _CACHED_NIFTY50_SYMBOLS
    now = time.time()
    if now < _NIFTY50_CACHE_EXPIRES_AT and _CACHED_NIFTY50_SYMBOLS:
        return True

    body = _fetch_nse_industry_csv_body(_NIFTY50_CSV_URL)
    if not body:
        return False

    reader = csv.DictReader(io.StringIO(body))
    ordered_unique: list[str] = []
    seen: set[str] = set()
    for row in reader:
        symbol = _normalise_symbol((row or {}).get("Symbol"))
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        ordered_unique.append(symbol)

    _CACHED_NIFTY50_SYMBOLS = ordered_unique
    _NIFTY50_CACHE_EXPIRES_AT = now + _NIFTY50_CACHE_TTL_SECONDS
    return True


def get_nifty50_symbols() -> list[str]:
    """Fetch and cache Nifty50 constituents from NSE archive CSV."""
    with _CACHE_LOCK:
        if not _refresh_nifty50_cache_unlocked():
            return []
        return list(_CACHED_NIFTY50_SYMBOLS)


def warm_reference_caches(kite) -> None:
    """Best-effort warmup for heavy instrument/NSE reference lookups."""
    try:
        get_cash_equity_name_lookups(kite)
        get_cash_equity_kite_sector_lookups(kite)
        get_cash_equity_isin_lookups(kite)
    except Exception as exc:
        logger.warning("Instrument lookup warmup failed: %s", exc)
    try:
        get_nse_symbol_to_industry()
        get_isin_to_industry()
        get_nifty50_symbols()
    except Exception as exc:
        logger.warning("NSE reference lookup warmup failed: %s", exc)


def resolve_equity_industry(
    symbol: str,
    exchange: str | None,
    instrument_token: int | None,
    token_to_industry: dict[int, str],
    symbol_to_industry: dict[tuple[str, str], str],
    nse_symbol_to_industry: dict[str, str],
    isin_to_industry: dict[str, str],
    token_to_isin: dict[int, str],
    symbol_to_isin: dict[tuple[str, str], str],
) -> str:
    """Resolve industry for NSE/BSE cash equities (empty string if unknown)."""
    clean_symbol = _normalise_symbol(symbol)
    clean_exchange = str(exchange or "").strip().upper()
    token = int(instrument_token or 0)

    if clean_exchange not in EQUITY_EXCHANGES:
        return ""

    # yfinance first (requested). Only if it isn't in cache do we call yfinance.
    ind = ""
    if clean_symbol and clean_exchange in EQUITY_EXCHANGES:
        _maybe_start_monthly_yfinance_refresh()
        y_key = _yfinance_cache_key(clean_exchange, clean_symbol)

        _load_yfinance_cache_if_needed()
        with _YFINANCE_CACHE_LOCK:
            cached_ind = _CACHED_YFINANCE_KEY_TO_INDUSTRY.get(y_key)
        if cached_ind is not None:
            ind = cached_ind
            if ind:
                logger.info(
                    "Industry for %s resolved via yfinance cache: %s",
                    f"{clean_exchange}:{clean_symbol}",
                    ind,
                )
        else:
            # Do not block request path with yfinance network fetches.
            # Queue a best-effort background refresh and fall back to other sources.
            _maybe_start_single_symbol_yfinance_refresh(clean_exchange, clean_symbol)

    # Other sources first-choice after yfinance miss.
    if not ind and token > 0:
        ind = _normalise_name(token_to_industry.get(token))
    if not ind and clean_symbol:
        ind = _normalise_name(symbol_to_industry.get((clean_exchange, clean_symbol)))
    if not ind and clean_exchange == "NSE" and clean_symbol:
        ind = _normalise_name(nse_symbol_to_industry.get(clean_symbol))
    if not ind:
        isin = ""
        if token > 0:
            isin = _normalise_isin(token_to_isin.get(token))
        if not isin and clean_symbol:
            isin = _normalise_isin(symbol_to_isin.get((clean_exchange, clean_symbol)))
        if isin:
            ind = _normalise_name(isin_to_industry.get(isin))

    return ind


def resolve_equity_sector(
    symbol: str,
    exchange: str | None,
    instrument_token: int | None,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
    token_to_kite_sector: dict[int, str],
    symbol_to_kite_sector: dict[tuple[str, str], str],
    nse_symbol_to_industry: dict[str, str],
    isin_to_industry: dict[str, str],
    token_to_isin: dict[int, str],
    symbol_to_isin: dict[tuple[str, str], str],
) -> str:
    """Resolve a display **sector** for NSE/BSE cash equities.

    Order: yfinance ``sector`` (cache, then network if no cache row), Kite
    ``sector`` column, then NSE index ``Industry`` as a coarse fallback label.
    """
    clean_symbol = _normalise_symbol(symbol)
    clean_exchange = str(exchange or "").strip().upper()
    token = int(instrument_token or 0)

    if clean_exchange not in EQUITY_EXCHANGES:
        return ""

    instrument_name = ""
    if token > 0:
        instrument_name = _normalise_name(token_to_name.get(token))
    if not instrument_name and clean_symbol:
        instrument_name = _normalise_name(symbol_to_name.get((clean_exchange, clean_symbol)))

    # ETFs often lack reliable sector metadata. If symbol/name indicates ETF,
    # force a dedicated sector label.
    if "ETF" in clean_symbol or "ETF" in instrument_name.upper():
        return "ETF"

    sec = ""
    if clean_symbol:
        _maybe_start_monthly_yfinance_refresh()
        y_key = _yfinance_cache_key(clean_exchange, clean_symbol)

        _load_yfinance_cache_if_needed()
        with _YFINANCE_CACHE_LOCK:
            cached_sec = _CACHED_YFINANCE_KEY_TO_SECTOR.get(y_key)
            yf_has_row = y_key in _CACHED_YFINANCE_KEY_TO_INDUSTRY

        if cached_sec:
            sec = cached_sec
            logger.info(
                "Sector for %s resolved via yfinance cache: %s",
                f"{clean_exchange}:{clean_symbol}",
                sec,
            )
        elif not yf_has_row:
            # Do not block request path with yfinance network fetches.
            # Queue a best-effort background refresh and fall back to other sources.
            _maybe_start_single_symbol_yfinance_refresh(clean_exchange, clean_symbol)

    if not sec and token > 0:
        sec = _normalise_name(token_to_kite_sector.get(token))
    if not sec and clean_symbol:
        sec = _normalise_name(symbol_to_kite_sector.get((clean_exchange, clean_symbol)))
    if not sec and clean_exchange == "NSE" and clean_symbol:
        sec = _normalise_name(nse_symbol_to_industry.get(clean_symbol))
    if not sec:
        isin = ""
        if token > 0:
            isin = _normalise_isin(token_to_isin.get(token))
        if not isin and clean_symbol:
            isin = _normalise_isin(symbol_to_isin.get((clean_exchange, clean_symbol)))
        if isin:
            sec = _normalise_name(isin_to_industry.get(isin))

    return sec


def symbol_with_company_name(
    symbol: str,
    exchange: str | None,
    instrument_token: int | None,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
) -> str:
    """Return display label as ``Company Name`` for cash equities."""
    clean_symbol = _normalise_symbol(symbol)
    clean_exchange = str(exchange or "").strip().upper()
    token = int(instrument_token or 0)

    if clean_exchange not in EQUITY_EXCHANGES:
        return clean_symbol

    name = ""
    if token > 0:
        name = _normalise_name(token_to_name.get(token))
    if not name and clean_symbol:
        name = _normalise_name(symbol_to_name.get((clean_exchange, clean_symbol)))

    if name and name.lower() != clean_symbol.lower():
        return name
    return clean_symbol
