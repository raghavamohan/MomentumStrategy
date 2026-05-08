"""Shared portfolio data model and transformations for CLI + web."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as URLRequest, urlopen

from app.instruments import resolve_equity_sector, symbol_with_company_name

EQUITY_EXCHANGES = {"NSE", "BSE"}
FNO_EXCHANGES = {"NFO", "BFO", "CDS", "BCD", "MCX"}

MFDATA_BASE_URL = "https://mfdata.in"
MFDATA_HTTP_TIMEOUT_SECONDS = 20
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MFDATA_CACHE_FILE = PROJECT_ROOT / ".cache" / "mfdata_underlyings_cache.json"
_MFDATA_CACHE_LOCK = threading.Lock()
_MFDATA_SEARCH_CACHE: dict[str, list[dict[str, Any]]] = {}
_MFDATA_HOLDINGS_CACHE: dict[int, dict[str, Any] | None] = {}
_MFDATA_DISK_CACHE_LOADED = False
_MFDATA_DISK_CACHE: dict[str, Any] = {"meta": {"cache_month": ""}, "search": {}, "holdings": {}}
_MFDATA_DISK_CACHE_DIRTY = False


@dataclass
class EquityHolding:
    tradingsymbol: str
    symbol_label: str
    sector: str
    exchange: str
    instrument_token: int
    quantity: float
    average_price: float
    last_price: float
    close_price: float
    invested: float
    current: float
    pnl: float
    day_change_percentage: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MfHolding:
    fund: str
    folio: str
    units: float
    average_price: float
    last_price: float
    invested: float
    current: float
    pnl: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Position:
    tradingsymbol: str
    symbol_label: str
    sector: str
    exchange: str
    product: str
    instrument_token: int
    quantity: int
    average_price: float
    last_price: float
    buy_value: float
    sell_value: float
    multiplier: float
    close_price: float
    pnl: float
    m2m: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_equity_sector(symbol: str, sector: str) -> str:
    """Normalize sector labels and classify selected ETFs explicitly."""
    compact = "".join(ch for ch in symbol.upper() if ch.isalnum())
    normalized_sector = _normalize_match_text(sector or "")
    if compact in {"GOLDBEES", "GOLDETF", "GOLDSHARE"}:
        return "Gold"
    if compact in {"LIQUIDBEES", "LIQUIDBESS", "LIQUIDETF", "BHARATBOND"}:
        return "Debt"
    if _is_gold_sector_label(normalized_sector):
        return "Gold"
    if _is_debt_sector_label(normalized_sector):
        return "Debt"
    # Some APIs report category as plain "ETF". Reclassify common debt/gold ETF symbols.
    if normalized_sector == "etf":
        if any(token in compact for token in {"GOLD", "SILVER"}):
            return "Gold"
        if any(token in compact for token in {"LIQUID", "BOND", "GILT", "DEBT", "SDL", "TREPS"}):
            return "Debt"
    return (sector or "").strip() or "Uncategorized"


def overlay_live_ltp(row: dict[str, Any], live_ltp_by_token: dict[int, float]) -> dict[str, Any]:
    """Return row with websocket LTP overlaid when available."""
    out = dict(row)
    token = int(out.get("instrument_token") or 0)
    if token > 0 and token in live_ltp_by_token:
        out["last_price"] = float(live_ltp_by_token[token])
        out["_live_ltp_applied"] = True
    return out


def build_equity_holding(
    holding: dict[str, Any],
    *,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
    token_to_kite_sector: dict[int, str],
    symbol_to_kite_sector: dict[tuple[str, str], str],
    nse_symbol_to_industry: dict[str, str],
    isin_to_industry: dict[str, str],
    token_to_isin: dict[int, str],
    symbol_to_isin: dict[tuple[str, str], str],
) -> EquityHolding:
    quantity = (holding.get("quantity") or 0) + (holding.get("t1_quantity") or 0)
    avg = float(holding.get("average_price") or 0.0)
    ltp = float(holding.get("last_price") or 0.0)
    close_price = float(holding.get("close_price") or 0.0)
    live_ltp_applied = bool(holding.get("_live_ltp_applied"))
    if live_ltp_applied and close_price > 0:
        day_change_percentage = ((ltp - close_price) / close_price) * 100.0
        pnl = (ltp - avg) * quantity
    else:
        day_change_percentage = float(holding.get("day_change_percentage") or 0.0)
        pnl = float(holding.get("pnl") or 0.0)

    symbol = str(holding.get("tradingsymbol", "")).strip()
    exchange = str(holding.get("exchange", ""))
    token = int(holding.get("instrument_token") or 0)
    symbol_label = symbol_with_company_name(
        symbol=symbol,
        exchange=exchange,
        instrument_token=token,
        token_to_name=token_to_name,
        symbol_to_name=symbol_to_name,
    )
    sector = normalize_equity_sector(
        symbol,
        resolve_equity_sector(
            symbol=symbol,
            exchange=exchange,
            instrument_token=token,
            token_to_name=token_to_name,
            symbol_to_name=symbol_to_name,
            token_to_kite_sector=token_to_kite_sector,
            symbol_to_kite_sector=symbol_to_kite_sector,
            nse_symbol_to_industry=nse_symbol_to_industry,
            isin_to_industry=isin_to_industry,
            token_to_isin=token_to_isin,
            symbol_to_isin=symbol_to_isin,
        ),
    )
    return EquityHolding(
        tradingsymbol=symbol,
        symbol_label=symbol_label,
        sector=sector,
        exchange=exchange,
        instrument_token=token,
        quantity=quantity,
        average_price=avg,
        last_price=ltp,
        close_price=close_price,
        invested=avg * quantity,
        current=ltp * quantity,
        pnl=pnl,
        day_change_percentage=day_change_percentage,
    )


def build_mf_holding(holding: dict[str, Any]) -> MfHolding:
    units = float(holding.get("quantity") or 0.0)
    avg = float(holding.get("average_price") or 0.0)
    ltp = float(holding.get("last_price") or 0.0)
    invested = avg * units
    current = ltp * units
    api_pnl = holding.get("pnl")
    pnl = float(api_pnl) if api_pnl not in (None, "") and float(api_pnl) != 0.0 else (current - invested)
    return MfHolding(
        fund=str(holding.get("fund", "")),
        folio=str(holding.get("folio", "")),
        units=units,
        average_price=avg,
        last_price=ltp,
        invested=invested,
        current=current,
        pnl=pnl,
    )


def build_position(
    position: dict[str, Any],
    *,
    token_to_name: dict[int, str],
    symbol_to_name: dict[tuple[str, str], str],
    token_to_kite_sector: dict[int, str],
    symbol_to_kite_sector: dict[tuple[str, str], str],
    nse_symbol_to_industry: dict[str, str],
    isin_to_industry: dict[str, str],
    token_to_isin: dict[int, str],
    symbol_to_isin: dict[tuple[str, str], str],
) -> Position:
    qty = int(position.get("quantity") or 0)
    ltp = float(position.get("last_price") or 0.0)
    close_price = float(position.get("close_price") or 0.0)
    multiplier = float(position.get("multiplier") or 1.0)
    live_ltp_applied = bool(position.get("_live_ltp_applied"))
    if live_ltp_applied:
        buy_value = float(position.get("buy_value") or 0.0)
        sell_value = float(position.get("sell_value") or 0.0)
        pnl = (sell_value - buy_value) + (qty * ltp * multiplier)
        m2m = (ltp - close_price) * qty * multiplier if close_price > 0 else float(position.get("m2m") or 0.0)
    else:
        pnl = float(position.get("pnl") or 0.0)
        m2m = float(position.get("m2m") or 0.0)

    symbol = str(position.get("tradingsymbol", "")).strip()
    exchange = str(position.get("exchange", ""))
    token = int(position.get("instrument_token") or 0)
    symbol_label = symbol_with_company_name(
        symbol=symbol,
        exchange=exchange,
        instrument_token=token,
        token_to_name=token_to_name,
        symbol_to_name=symbol_to_name,
    )
    if exchange in EQUITY_EXCHANGES:
        sector = normalize_equity_sector(
            symbol,
            resolve_equity_sector(
                symbol=symbol,
                exchange=exchange,
                instrument_token=token,
                token_to_name=token_to_name,
                symbol_to_name=symbol_to_name,
                token_to_kite_sector=token_to_kite_sector,
                symbol_to_kite_sector=symbol_to_kite_sector,
                nse_symbol_to_industry=nse_symbol_to_industry,
                isin_to_industry=isin_to_industry,
                token_to_isin=token_to_isin,
                symbol_to_isin=symbol_to_isin,
            ),
        )
    else:
        sector = ""

    return Position(
        tradingsymbol=symbol,
        symbol_label=symbol_label,
        sector=sector,
        exchange=exchange,
        product=str(position.get("product", "")),
        instrument_token=token,
        quantity=qty,
        average_price=float(position.get("average_price") or 0.0),
        last_price=ltp,
        buy_value=float(position.get("buy_value") or 0.0),
        sell_value=float(position.get("sell_value") or 0.0),
        multiplier=multiplier,
        close_price=close_price,
        pnl=pnl,
        m2m=m2m,
    )


def summarise(rows: list[dict[str, Any]], *fields: str) -> dict[str, float]:
    return {field: sum(float(r.get(field) or 0.0) for r in rows) for field in fields}


def summarise_equity_by_sector(rows: list[dict[str, Any]]) -> list[dict[str, float | str]]:
    bucket: dict[str, dict[str, float | str]] = {}
    for row in rows:
        sector = str(row.get("sector") or "Uncategorized").strip() or "Uncategorized"
        entry = bucket.setdefault(
            sector,
            {"sector": sector, "invested": 0.0, "current": 0.0, "pnl": 0.0},
        )
        entry["invested"] = float(entry["invested"]) + float(row.get("invested") or 0.0)
        entry["current"] = float(entry["current"]) + float(row.get("current") or 0.0)
        entry["pnl"] = float(entry["pnl"]) + float(row.get("pnl") or 0.0)
    return sorted(bucket.values(), key=lambda r: float(r["invested"]), reverse=True)


def _is_debt_sector_label(normalized_label: str) -> bool:
    label = str(normalized_label or "").strip()
    if not label:
        return False
    debt_markers = (
        "debt",
        "bond",
        "gilt",
        "liquid",
        "money market",
        "fixed income",
        "overnight",
        "treasury",
        "t bill",
        "tbill",
        "sdl",
    )
    return any(marker in label for marker in debt_markers)


def _is_gold_sector_label(normalized_label: str) -> bool:
    label = str(normalized_label or "").strip()
    if not label:
        return False
    # Keep this strict to avoid classifying ordinary equities by symbol/company names.
    if label == "gold":
        return True
    if "gold" in label and ("etf" in label or "commodity" in label or "precious" in label):
        return True
    return False


def split_top_level_allocations(
    sector_rows: list[dict[str, float | str]],
) -> list[dict[str, float | str]]:
    debt = {"sector": "Debt", "invested": 0.0, "current": 0.0, "pnl": 0.0}
    gold = {"sector": "Gold", "invested": 0.0, "current": 0.0, "pnl": 0.0}
    equity = {"sector": "Equity", "invested": 0.0, "current": 0.0, "pnl": 0.0}
    for row in sector_rows:
        key = _normalize_match_text(str(row.get("sector") or ""))
        if _is_debt_sector_label(key):
            debt["invested"] = float(debt["invested"]) + float(row.get("invested") or 0.0)
            debt["current"] = float(debt["current"]) + float(row.get("current") or 0.0)
            debt["pnl"] = float(debt["pnl"]) + float(row.get("pnl") or 0.0)
        elif _is_gold_sector_label(key):
            gold["invested"] = float(gold["invested"]) + float(row.get("invested") or 0.0)
            gold["current"] = float(gold["current"]) + float(row.get("current") or 0.0)
            gold["pnl"] = float(gold["pnl"]) + float(row.get("pnl") or 0.0)
        else:
            equity["invested"] = float(equity["invested"]) + float(row.get("invested") or 0.0)
            equity["current"] = float(equity["current"]) + float(row.get("current") or 0.0)
            equity["pnl"] = float(equity["pnl"]) + float(row.get("pnl") or 0.0)
    return [debt, gold, equity]


def equity_sector_breakdown(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, float | str]]]:
    sector_rows = summarise_equity_by_sector(rows)
    top_level = split_top_level_allocations(sector_rows)
    equity_subsectors = [
        r
        for r in sector_rows
        if not _is_debt_sector_label(_normalize_match_text(str(r.get("sector") or "")))
        and not _is_gold_sector_label(_normalize_match_text(str(r.get("sector") or "")))
    ]
    return {"top_level": top_level, "equity_subsectors": equity_subsectors}


def _normalize_match_text(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", (value or "").lower())
    return " ".join(cleaned.split())


def _canonicalize_mf_scheme_name(value: str) -> str:
    normalized = _normalize_match_text(value)
    drop_tokens = {
        "direct", "regular", "growth", "plan", "option", "idcw", "dividend", "payout",
        "reinvestment", "reinvest", "bonus", "inst", "institutional",
    }
    kept = [token for token in normalized.split() if token not in drop_tokens]
    return " ".join(kept)


def _parse_pct(value: Any) -> float:
    if value is None:
        return 0.0
    raw = str(value).strip().replace("%", "").replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _current_month_token() -> str:
    return time.strftime("%Y-%m")


def _save_mfdata_disk_cache_locked() -> None:
    MFDATA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MFDATA_CACHE_FILE.write_text(
        json.dumps(_MFDATA_DISK_CACHE, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _prepare_mfdata_cache_locked() -> None:
    global _MFDATA_DISK_CACHE_LOADED, _MFDATA_DISK_CACHE, _MFDATA_DISK_CACHE_DIRTY
    if not _MFDATA_DISK_CACHE_LOADED:
        if MFDATA_CACHE_FILE.exists():
            try:
                loaded = json.loads(MFDATA_CACHE_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                _MFDATA_DISK_CACHE = {
                    "meta": loaded.get("meta") if isinstance(loaded.get("meta"), dict) else {"cache_month": ""},
                    "search": loaded.get("search") if isinstance(loaded.get("search"), dict) else {},
                    "holdings": loaded.get("holdings") if isinstance(loaded.get("holdings"), dict) else {},
                }
        _MFDATA_DISK_CACHE_LOADED = True

    current_month = _current_month_token()
    cached_month = str((_MFDATA_DISK_CACHE.get("meta") or {}).get("cache_month") or "")
    if cached_month == current_month:
        return
    _MFDATA_DISK_CACHE["meta"] = {"cache_month": current_month}
    _MFDATA_DISK_CACHE["search"] = {}
    _MFDATA_DISK_CACHE["holdings"] = {}
    _MFDATA_SEARCH_CACHE.clear()
    _MFDATA_HOLDINGS_CACHE.clear()
    _MFDATA_DISK_CACHE_DIRTY = True
    try:
        _save_mfdata_disk_cache_locked()
        _MFDATA_DISK_CACHE_DIRTY = False
    except OSError:
        pass


def _flush_mfdata_disk_cache() -> None:
    global _MFDATA_DISK_CACHE_DIRTY
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        if not _MFDATA_DISK_CACHE_DIRTY:
            return
        try:
            _save_mfdata_disk_cache_locked()
            _MFDATA_DISK_CACHE_DIRTY = False
        except OSError:
            pass


def _mfdata_json_get(path: str, query: dict[str, Any] | None = None) -> Any:
    url = f"{MFDATA_BASE_URL}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    req = URLRequest(
        url,
        headers={"Accept": "application/json", "User-Agent": "MomentumStrategy/1.0 (+shared-model)"},
    )
    with urlopen(req, timeout=MFDATA_HTTP_TIMEOUT_SECONDS) as resp:
        payload = resp.read().decode("utf-8", errors="replace")
    return json.loads(payload)


def _mfdata_search_fund(fund_name: str) -> list[dict[str, Any]]:
    key = _normalize_match_text(fund_name)
    if not key:
        return []
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        cached_mem = _MFDATA_SEARCH_CACHE.get(key)
        if cached_mem is not None:
            return list(cached_mem)
        cached_disk = (_MFDATA_DISK_CACHE.get("search") or {}).get(key)
        if isinstance(cached_disk, list):
            rows = [row for row in cached_disk if isinstance(row, dict)]
            _MFDATA_SEARCH_CACHE[key] = rows
            return list(rows)
    try:
        payload = _mfdata_json_get("/api/v1/search", {"q": fund_name})
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        payload = {}
    rows = payload.get("data") if isinstance(payload, dict) else []
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    global _MFDATA_DISK_CACHE_DIRTY
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        _MFDATA_SEARCH_CACHE[key] = rows
        (_MFDATA_DISK_CACHE.get("search") or {})[key] = rows
        _MFDATA_DISK_CACHE_DIRTY = True
    return rows


def _mfdata_holdings_for_family(family_id: int) -> dict[str, Any] | None:
    family_key = str(int(family_id))
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        if family_id in _MFDATA_HOLDINGS_CACHE:
            return _MFDATA_HOLDINGS_CACHE[family_id]
        holdings_disk = (_MFDATA_DISK_CACHE.get("holdings") or {})
        if family_key in holdings_disk:
            cached_disk = holdings_disk.get(family_key)
            result = cached_disk if isinstance(cached_disk, dict) else None
            _MFDATA_HOLDINGS_CACHE[family_id] = result
            return result
    try:
        payload = _mfdata_json_get(f"/api/v1/families/{family_id}/holdings")
    except HTTPError as exc:
        if exc.code == 404:
            payload = {}
        else:
            raise
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        payload = {}
    data = payload.get("data") if isinstance(payload, dict) else None
    result = data if isinstance(data, dict) else None
    global _MFDATA_DISK_CACHE_DIRTY
    with _MFDATA_CACHE_LOCK:
        _prepare_mfdata_cache_locked()
        _MFDATA_HOLDINGS_CACHE[family_id] = result
        (_MFDATA_DISK_CACHE.get("holdings") or {})[family_key] = result
        _MFDATA_DISK_CACHE_DIRTY = True
    return result


def _rank_mfdata_variants(fund_name: str, variants: list[dict[str, Any]]) -> list[int]:
    canonical_fund = _canonicalize_mf_scheme_name(fund_name)
    if not canonical_fund:
        return []
    fund_tokens = set(canonical_fund.split())
    scored: list[tuple[float, int]] = []
    seen_family_ids: set[int] = set()
    for row in variants:
        family_id = int(row.get("family_id") or 0)
        if family_id <= 0 or family_id in seen_family_ids:
            continue
        seen_family_ids.add(family_id)
        candidate_name = str(row.get("name") or "").strip()
        candidate = _canonicalize_mf_scheme_name(candidate_name)
        if not candidate:
            continue
        candidate_tokens = set(candidate.split())
        overlap = len(fund_tokens & candidate_tokens)
        score = float(overlap * 10)
        if canonical_fund in candidate or candidate in canonical_fund:
            score += 5
        score += SequenceMatcher(None, canonical_fund, candidate).ratio()
        if "growth" in candidate_name.lower() or "growth" in str(row.get("option_type") or "").lower():
            score += 0.25
        if str(row.get("plan_type") or "").lower() == "direct":
            score += 0.05
        scored.append((score, family_id))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [family_id for _, family_id in scored]


def build_mf_underlying_breakdown(
    mf_holdings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str, list[str], int, int]:
    if not mf_holdings:
        return [], "", [], 0, 0
    fund_current_by_name: dict[str, float] = {}
    for row in mf_holdings:
        fund_name = str(row.get("fund") or "").strip()
        if not fund_name:
            continue
        fund_current_by_name[fund_name] = fund_current_by_name.get(fund_name, 0.0) + float(row.get("current") or 0.0)
    total_current = sum(fund_current_by_name.values())
    if total_current <= 0:
        return [], "", [], 0, len(fund_current_by_name)

    combined: dict[tuple[str, str], float] = {}
    used_months: set[str] = set()
    not_aggregated: list[str] = []
    aggregated_funds: set[str] = set()
    all_funds: set[str] = set(fund_current_by_name)

    for fund_name, fund_current in fund_current_by_name.items():
        if fund_current <= 0:
            not_aggregated.append(fund_name)
            continue
        variants = _mfdata_search_fund(fund_name)
        family_candidates = _rank_mfdata_variants(fund_name, variants)
        selected_holdings: dict[str, Any] | None = None
        for family_id in family_candidates:
            try:
                payload = _mfdata_holdings_for_family(family_id)
            except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
                payload = None
            equity_rows = (payload or {}).get("equity_holdings") if isinstance(payload, dict) else []
            if equity_rows:
                selected_holdings = payload
                break
        if not selected_holdings:
            not_aggregated.append(fund_name)
            continue
        month = str(selected_holdings.get("month") or "").strip()
        if month:
            used_months.add(month)
        fund_weight = fund_current / total_current
        for row in selected_holdings.get("equity_holdings") or []:
            instrument = str(row.get("stock_name") or row.get("isin") or "").strip() or "Unknown"
            sector = str(row.get("sector") or "Unspecified").strip() or "Unspecified"
            instrument_weight = max(0.0, _parse_pct(row.get("weight_pct") or row.get("weight")))
            overall_weight = fund_weight * (instrument_weight / 100.0)
            if overall_weight <= 0:
                continue
            key = (instrument, sector)
            combined[key] = combined.get(key, 0.0) + overall_weight
        aggregated_funds.add(fund_name)

    table_rows = [
        {"instrument": instrument, "sector": sector, "overall_weight": weight * 100.0}
        for (instrument, sector), weight in combined.items()
    ]
    table_rows.sort(
        key=lambda row: (float(row.get("overall_weight") or 0.0), str(row.get("instrument") or "").lower()),
        reverse=True,
    )
    sorted_months = sorted(used_months, reverse=True)
    latest_month = sorted_months[0] if sorted_months else ""
    seen_missing: set[str] = set()
    missing_unique = [name for name in not_aggregated if not (name in seen_missing or seen_missing.add(name))]
    _flush_mfdata_disk_cache()
    return table_rows, latest_month, missing_unique, len(aggregated_funds), len(all_funds)
