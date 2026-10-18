"""Read-only DhanHQ market-data adapter.

The only POST calls in this module target the allowlisted Dhan market-data/chart
endpoints. This module deliberately contains no account-order API functions.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

DHAN_BASE = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")

# Explicit allowlist. Do not add order, super-order, trade, or position endpoints here.
READ_ONLY_MARKET_ENDPOINTS = {
    "quote": "/marketfeed/quote",
    "ltp": "/marketfeed/ltp",
    "ohlc": "/marketfeed/ohlc",
    "intraday": "/charts/intraday",
    "historical": "/charts/historical",
}
ALLOWED_SEGMENTS = {
    "NSE_EQ", "NSE_FNO", "NSE_CURRENCY", "BSE_EQ", "BSE_FNO",
    "BSE_CURRENCY", "MCX_COMM", "IDX_I",
}
ALLOWED_STRATEGIES = {
    "Zen Credit Spread Overnight",
    "Curvature Credit Spread Overnight",
}


class MarketDataError(RuntimeError):
    """Safe-to-display market-data error; does not include headers or credentials."""


def _auth_headers() -> dict[str, str]:
    client_id = os.getenv("DHAN_CLIENT_ID", "").strip()
    token = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
    if not client_id or not token:
        raise MarketDataError("Dhan credentials are not configured in the Render environment.")
    return {
        "access-token": token,
        "client-id": client_id,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _post_readonly(endpoint_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST only to a named, statically allowlisted data-retrieval endpoint."""
    if endpoint_key not in READ_ONLY_MARKET_ENDPOINTS:
        raise MarketDataError("Blocked non-allowlisted market-data endpoint.")
    url = DHAN_BASE + READ_ONLY_MARKET_ENDPOINTS[endpoint_key]
    try:
        response = requests.post(url, headers=_auth_headers(), json=payload, timeout=15)
        response.raise_for_status()
        result = response.json()
    except MarketDataError:
        raise
    except requests.RequestException as exc:
        # Do not surface raw request exceptions that may contain request details.
        raise MarketDataError(f"Dhan market-data request failed ({type(exc).__name__}).") from None
    except ValueError:
        raise MarketDataError("Dhan returned a non-JSON market-data response.") from None
    if not isinstance(result, dict):
        raise MarketDataError("Dhan returned an unexpected market-data response shape.")
    status = str(result.get("status", "")).lower()
    if status in {"failure", "failed", "error"}:
        raise MarketDataError("Dhan rejected the market-data request; verify IDs, permissions and API status.")
    return result


def _payload_records(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        lowkeys = {str(k).lower() for k in payload}
        if lowkeys.intersection({"securityid", "security_id", "netqty", "net_quantity"}):
            return [payload]
    return []


def _first(row: dict[str, Any], *keys: str) -> Any:
    lower = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        value = lower.get(key.lower())
        if value is not None and value != "":
            return value
    return None


def _valid_instrument(instrument: dict[str, Any], *, allow_fetch_candles: bool = True) -> dict[str, Any]:
    if not isinstance(instrument, dict):
        raise ValueError("Each watchlist item must be a JSON object.")
    segment = str(instrument.get("exchange_segment", "")).strip().upper()
    security_id = str(instrument.get("security_id", "")).strip()
    if segment not in ALLOWED_SEGMENTS:
        raise ValueError(f"Unsupported exchange_segment in market watchlist: {segment or '(blank)'}.")
    if not re.fullmatch(r"\d+", security_id):
        raise ValueError("Each security_id must be numeric and verified against Dhan's instrument list.")
    name = str(instrument.get("name") or f"{segment}:{security_id}").strip()[:120]
    role = str(instrument.get("role", "watchlist")).strip()[:60]
    instrument_type = str(instrument.get("instrument_type", "")).strip().upper()
    fetch_candles = bool(instrument.get("fetch_candles", False)) if allow_fetch_candles else False
    if fetch_candles and not instrument_type:
        raise ValueError(f"instrument_type is required when fetch_candles is true for {name}.")
    return {
        "name": name,
        "exchange_segment": segment,
        "security_id": security_id,
        "instrument_type": instrument_type,
        "role": role,
        "fetch_candles": fetch_candles,
    }


def parse_json_list(value: str, variable_name: str) -> list[dict[str, Any]]:
    """Parse an optional Render environment variable that must be a JSON list."""
    if not value or not value.strip():
        return []
    import json
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError(f"{variable_name} must contain valid JSON.") from None
    if not isinstance(result, list):
        raise ValueError(f"{variable_name} must be a JSON array (a list in square brackets).")
    return result


def parse_spread_configs(value: str) -> list[dict[str, Any]]:
    """Validate optional manual spread definitions; never infers ownership."""
    import json
    if not value or not value.strip():
        return []
    try:
        configs = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError("MARKET_SPREADS_JSON must contain valid JSON.") from None
    if not isinstance(configs, list):
        raise ValueError("MARKET_SPREADS_JSON must be a JSON array.")
    clean: list[dict[str, Any]] = []
    for index, config in enumerate(configs, start=1):
        if not isinstance(config, dict):
            raise ValueError(f"Spread configuration {index} must be a JSON object.")
        strategy = str(config.get("strategy_name", "")).strip()
        if strategy not in ALLOWED_STRATEGIES:
            raise ValueError(f"Spread configuration {index} must use one of the two configured strategy names.")
        clean_config = dict(config)
        clean_config["name"] = str(config.get("name") or strategy)[:120]
        clean_config["strategy_name"] = strategy
        clean_config["verified"] = config.get("verified") is True
        for leg_name in ("short_leg", "long_leg"):
            leg = config.get(leg_name)
            if not isinstance(leg, dict):
                raise ValueError(f"Spread configuration {index} requires {leg_name} as an object.")
            clean_config[leg_name] = _valid_instrument(leg, allow_fetch_candles=False)
            try:
                entry = float(leg.get("entry_price"))
            except (TypeError, ValueError):
                entry = 0
            clean_config[leg_name]["entry_price"] = entry
        try:
            quantity = float(config.get("quantity"))
            strike_width = float(config.get("strike_width"))
        except (TypeError, ValueError):
            quantity, strike_width = 0, 0
        clean_config["quantity"] = quantity
        clean_config["strike_width"] = strike_width
        clean.append(clean_config)
    return clean


def watchlist_from_positions(positions_payload: Any) -> list[dict[str, Any]]:
    """Turn actual non-zero Dhan positions into quote candidates; no strategy assignment."""
    candidates: list[dict[str, Any]] = []
    for row in _payload_records(positions_payload, ("positions", "data")):
        segment = _first(row, "exchangeSegment", "exchange_segment", "exchangeSegmentId")
        security_id = _first(row, "securityId", "security_id")
        if segment is None or security_id is None:
            continue
        net_qty = _first(row, "netQty", "netqty", "net_quantity", "netQuantity")
        try:
            if net_qty is not None and float(net_qty) == 0:
                continue
        except (TypeError, ValueError):
            pass
        candidate = {
            "name": str(_first(row, "tradingSymbol", "trading_symbol", "displayName") or f"{segment}:{security_id}"),
            "exchange_segment": str(segment),
            "security_id": str(security_id),
            "instrument_type": str(_first(row, "instrument", "instrumentType", "instrument_type") or ""),
            "role": "open_position_unattributed",
            "fetch_candles": False,
        }
        try:
            candidates.append(_valid_instrument(candidate))
        except ValueError:
            # Skip unsupported/malformed position rows rather than guess an ID mapping.
            continue
    return candidates


def build_watchlist(
    positions_payload: Any,
    manual_watchlist_json: str = "",
    spread_configs_json: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge configured watchlist, observed open legs, and explicit spread legs."""
    manual = [
        _valid_instrument(item)
        for item in parse_json_list(manual_watchlist_json, "MARKET_WATCHLIST_JSON")
    ]
    spreads = parse_spread_configs(spread_configs_json)
    candidates = watchlist_from_positions(positions_payload) + manual
    for spread in spreads:
        for leg_name in ("short_leg", "long_leg"):
            leg = dict(spread[leg_name])
            leg.pop("entry_price", None)
            leg["role"] = f"configured_{leg_name}"
            leg["fetch_candles"] = False
            candidates.append(_valid_instrument(leg))

    # Manual config/explicitly verified spread legs override auto-position labels for same ID.
    merged: dict[str, dict[str, Any]] = {}
    for item in candidates:
        key = f"{item['exchange_segment']}:{item['security_id']}"
        if key not in merged or item["fetch_candles"] or item["role"] != "open_position_unattributed":
            merged[key] = item
    if len(merged) > 1000:
        raise ValueError("The Dhan quote request supports up to 1,000 instruments; reduce the watchlist.")
    return list(merged.values()), spreads


def _security_id_for_payload(value: str) -> int | str:
    # Dhan examples accept numeric IDs. Keep very long values as strings to avoid precision loss.
    try:
        number = int(value)
        return number if number < 10**15 else value
    except (TypeError, ValueError):
        return value


def fetch_quotes(watchlist: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fetch all configured quotes in one read-only market-feed request."""
    if not watchlist:
        return {}
    request_payload: dict[str, list[int | str]] = {}
    for item in watchlist:
        segment = item["exchange_segment"]
        request_payload.setdefault(segment, []).append(_security_id_for_payload(item["security_id"]))
    response = _post_readonly("quote", request_payload)
    data = response.get("data", {})
    if not isinstance(data, dict):
        raise MarketDataError("Dhan quote response did not include a data object.")
    results: dict[str, dict[str, Any]] = {}
    for instrument in watchlist:
        segment = instrument["exchange_segment"]
        security_id = instrument["security_id"]
        segment_data = data.get(segment, {})
        row = None
        if isinstance(segment_data, dict):
            row = segment_data.get(security_id)
            if row is None:
                row = segment_data.get(str(_security_id_for_payload(security_id)))
        if isinstance(row, dict):
            results[f"{segment}:{security_id}"] = row
    return results


def _chart_payload(instrument: dict[str, Any], key: str, now: datetime) -> dict[str, Any]:
    if key == "intraday":
        from_dt = now.replace(hour=9, minute=15, second=0, microsecond=0)
        # If this runs before 09:15 IST, request a small valid window rather than reversed times.
        if now < from_dt:
            from_dt = now - timedelta(minutes=30)
        return {
            "securityId": instrument["security_id"],
            "exchangeSegment": instrument["exchange_segment"],
            "instrument": instrument["instrument_type"],
            "interval": "5",
            "oi": instrument["exchange_segment"] == "NSE_FNO",
            "fromDate": from_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "toDate": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
    start = (now.date() - timedelta(days=14)).strftime("%Y-%m-%d")
    return {
        "securityId": instrument["security_id"],
        "exchangeSegment": instrument["exchange_segment"],
        "instrument": instrument["instrument_type"],
        "expiryCode": 0,
        "oi": instrument["exchange_segment"] == "NSE_FNO",
        "fromDate": start,
        "toDate": now.date().strftime("%Y-%m-%d"),  # Dhan documents toDate as non-inclusive.
    }


def _unpack_candles(payload: dict[str, Any], instrument: dict[str, Any], interval_minutes: int) -> list[dict[str, Any]]:
    import math
    from datetime import timezone
    from datetime import datetime as dt

    keys = ("timestamp", "open", "high", "low", "close", "volume", "open_interest")
    arrays = {key: payload.get(key, []) for key in keys}
    if not all(isinstance(arrays[key], list) for key in keys):
        return []
    count = min((len(arrays[key]) for key in keys if arrays[key]), default=0)
    rows = []
    for index in range(count):
        try:
            timestamp = dt.fromtimestamp(float(arrays["timestamp"][index]), tz=timezone.utc).astimezone(IST)
            values = {}
            for key in ("open", "high", "low", "close", "volume", "open_interest"):
                value = arrays[key][index] if index < len(arrays[key]) else None
                values[key] = float(value) if value is not None and math.isfinite(float(value)) else None
            rows.append({
                "instrument_key": f"{instrument['exchange_segment']}:{instrument['security_id']}",
                "instrument_name": instrument["name"],
                "exchange_segment": instrument["exchange_segment"],
                "security_id": instrument["security_id"],
                "interval_minutes": interval_minutes,
                "candle_time": timestamp.isoformat(),
                **values,
            })
        except (IndexError, TypeError, ValueError, OverflowError):
            continue
    return rows


def fetch_candles(instrument: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch same-day 5-minute candles and daily history for explicitly opted-in references."""
    now = datetime.now(IST)
    intraday_response = _post_readonly("intraday", _chart_payload(instrument, "intraday", now))
    daily_response = _post_readonly("historical", _chart_payload(instrument, "historical", now))
    intraday = _unpack_candles(intraday_response, instrument, 5)
    daily = _unpack_candles(daily_response, instrument, 1440)
    return intraday, daily
