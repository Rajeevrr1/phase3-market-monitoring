"""Indicator calculations and risk estimates for observed market data only."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import market_data

IST = ZoneInfo("Asia/Kolkata")


def instrument_key(instrument: dict[str, Any]) -> str:
    return f"{instrument['exchange_segment']}:{instrument['security_id']}"


def _positive_number(value: Any) -> float | None:
    try:
        parsed = float(value)
        if parsed > 0 and parsed == parsed and parsed not in (float("inf"), float("-inf")):
            return parsed
    except (TypeError, ValueError):
        pass
    return None


def _weighted_candle_vwap(candles: list[dict[str, Any]]) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for candle in candles:
        high = _positive_number(candle.get("high"))
        low = _positive_number(candle.get("low"))
        close = _positive_number(candle.get("close"))
        try:
            volume = float(candle.get("volume") or 0)
        except (TypeError, ValueError):
            volume = 0
        if high is None or low is None or close is None or volume <= 0:
            continue
        typical_price = (high + low + close) / 3.0
        numerator += typical_price * volume
        denominator += volume
    return numerator / denominator if denominator > 0 else None


def _best_price(depth: Any, side: str) -> float | None:
    if not isinstance(depth, dict):
        return None
    entries = depth.get(side)
    if not isinstance(entries, list):
        return None
    prices = [_positive_number(row.get("price")) for row in entries if isinstance(row, dict)]
    prices = [price for price in prices if price is not None]
    if not prices:
        return None
    # Best bid is highest buy price; best ask is lowest sell price.
    return max(prices) if side == "buy" else min(prices)


def calculate_indicators(
    instrument: dict[str, Any],
    quote: dict[str, Any],
    intraday_candles: list[dict[str, Any]],
    daily_candles: list[dict[str, Any]],
    captured_at: str,
) -> dict[str, Any]:
    last_price = _positive_number(quote.get("last_price"))
    average_price = _positive_number(quote.get("average_price"))
    vwap = average_price
    vwap_source = "Dhan quote average_price (volume-weighted average reported by Dhan)" if vwap else None
    if vwap is None:
        vwap = _weighted_candle_vwap(intraday_candles)
        if vwap is not None:
            vwap_source = "Estimated from 5-minute candles: typical price × candle volume"

    ohlc = quote.get("ohlc") if isinstance(quote.get("ohlc"), dict) else {}
    current_high = _positive_number(ohlc.get("high"))
    current_low = _positive_number(ohlc.get("low"))
    current_open = _positive_number(ohlc.get("open"))
    previous_high = previous_low = previous_close = None
    if daily_candles:
        previous = daily_candles[-1]
        previous_high = _positive_number(previous.get("high"))
        previous_low = _positive_number(previous.get("low"))
        previous_close = _positive_number(previous.get("close"))

    recent = intraday_candles[-12:]
    swing_highs = [_positive_number(c.get("high")) for c in recent]
    swing_lows = [_positive_number(c.get("low")) for c in recent]
    swing_highs = [x for x in swing_highs if x is not None]
    swing_lows = [x for x in swing_lows if x is not None]
    recent_swing_high = max(swing_highs) if swing_highs else None
    recent_swing_low = min(swing_lows) if swing_lows else None

    volume = quote.get("volume")
    oi = quote.get("oi")
    bid = _best_price(quote.get("depth"), "buy")
    ask = _best_price(quote.get("depth"), "sell")
    if last_price is None:
        vwap_position = "Unavailable — quote has no valid last_price"
    elif vwap is None:
        vwap_position = "N/A — no valid volume-weighted price for this instrument"
    elif last_price > vwap:
        vwap_position = "Above VWAP"
    elif last_price < vwap:
        vwap_position = "Below VWAP"
    else:
        vwap_position = "At VWAP"

    level_status = "Within previous-session range or levels unavailable"
    if last_price is not None and previous_high is not None and last_price > previous_high:
        level_status = "Above previous session high"
    elif last_price is not None and previous_low is not None and last_price < previous_low:
        level_status = "Below previous session low"

    return {
        "instrument_key": instrument_key(instrument),
        "instrument_name": instrument["name"],
        "exchange_segment": instrument["exchange_segment"],
        "security_id": instrument["security_id"],
        "role": instrument.get("role", "watchlist"),
        "captured_at": captured_at,
        "last_price": last_price,
        "average_price": average_price,
        "vwap": vwap,
        "vwap_source": vwap_source or "N/A — no valid quote VWAP or candle volume",
        "vwap_position": vwap_position,
        "current_open": current_open,
        "current_day_high": current_high,
        "current_day_low": current_low,
        "previous_session_high": previous_high,
        "previous_session_low": previous_low,
        "previous_session_close": previous_close,
        "recent_12_candle_high": recent_swing_high,
        "recent_12_candle_low": recent_swing_low,
        "volume": volume,
        "open_interest": oi,
        "best_bid": bid,
        "best_ask": ask,
        "level_status": level_status,
        "data_quality": "Last quote received; exchange last-trade age is not independently verified by this REST snapshot.",
    }


def calculate_spread_risk(spread: dict[str, Any], quote_map: dict[str, dict[str, Any]], captured_at: str) -> dict[str, Any]:
    name = spread["name"]
    strategy = spread["strategy_name"]
    base = {
        "spread_name": name,
        "strategy_name": strategy,
        "captured_at": captured_at,
        "quantity": spread.get("quantity"),
        "entry_credit": None,
        "estimated_close_debit": None,
        "estimated_pnl": None,
        "max_theoretical_loss": None,
        "status": "UNVERIFIED_CONFIGURATION",
        "message": "Spread risk is not calculated until a human verifies the exact legs, fills, quantity and strike width.",
    }
    if spread.get("verified") is not True:
        return base
    short_leg = spread["short_leg"]
    long_leg = spread["long_leg"]
    quantity = _positive_number(spread.get("quantity"))
    strike_width = _positive_number(spread.get("strike_width"))
    short_entry = _positive_number(short_leg.get("entry_price"))
    long_entry = _positive_number(long_leg.get("entry_price"))
    if not all(x is not None for x in (quantity, strike_width, short_entry, long_entry)):
        base["status"] = "INVALID_CONFIGURATION"
        base["message"] = "Quantity, strike width, and both entry fill prices must be positive verified values."
        return base
    short_key = instrument_key(short_leg)
    long_key = instrument_key(long_leg)
    short_quote = quote_map.get(short_key, {})
    long_quote = quote_map.get(long_key, {})
    short_ask = _best_price(short_quote.get("depth"), "sell")
    long_bid = _best_price(long_quote.get("depth"), "buy")
    entry_credit = short_entry - long_entry
    if entry_credit <= 0 or strike_width <= entry_credit:
        base["status"] = "INVALID_CONFIGURATION"
        base["message"] = "The configured credit/strike width is inconsistent; confirm the actual vertical credit spread before using this estimate."
        return base
    base["entry_credit"] = entry_credit
    base["max_theoretical_loss"] = (strike_width - entry_credit) * quantity
    if short_ask is None or long_bid is None:
        base["status"] = "QUOTE_UNAVAILABLE"
        base["message"] = "A valid short-leg ask or long-leg bid is missing; closing debit and mark-to-market P&L are unavailable."
        return base
    if short_ask < long_bid:
        base["status"] = "QUOTE_CROSSED_CHECK"
        base["message"] = "Displayed spread bid/ask relationship is unexpected. Review quotes and leg configuration; P&L is withheld."
        return base
    close_debit = short_ask - long_bid
    base.update({
        "estimated_close_debit": close_debit,
        "estimated_pnl": (entry_credit - close_debit) * quantity,
        "status": "ESTIMATE_FROM_QUOTES",
        "message": "Indicative estimate only; excludes costs, slippage, exercise/assignment effects and any unmatched quantities.",
    })
    return base


def collect_market_snapshot(watchlist: list[dict[str, Any]], spread_configs: list[dict[str, Any]]) -> dict[str, Any]:
    captured_at = datetime.now(IST).isoformat()
    quote_map = market_data.fetch_quotes(watchlist)
    intraday_by_key: dict[str, list[dict[str, Any]]] = {}
    daily_by_key: dict[str, list[dict[str, Any]]] = {}
    candle_rows: list[dict[str, Any]] = []
    errors: list[str] = []

    candle_instruments = [instrument for instrument in watchlist if instrument.get("fetch_candles")]
    if len(candle_instruments) > 3:
        errors.append("Only the first three explicitly configured candle reference instruments are fetched per refresh.")
        candle_instruments = candle_instruments[:3]
    for instrument in candle_instruments:
        try:
            intraday, daily = market_data.fetch_candles(instrument)
            key = instrument_key(instrument)
            intraday_by_key[key] = intraday
            daily_by_key[key] = daily
            candle_rows.extend(intraday)
            candle_rows.extend(daily)
        except market_data.MarketDataError as exc:
            errors.append(f"Candles unavailable for {instrument['name']}: {exc}")

    quote_rows = []
    indicator_rows = []
    for instrument in watchlist:
        key = instrument_key(instrument)
        quote = quote_map.get(key)
        if not isinstance(quote, dict):
            errors.append(f"Quote unavailable for {instrument['name']} ({key}). Verify the security ID and Dhan data permissions.")
            continue
        quote_rows.append({
            "instrument": instrument,
            "quote": quote,
            "instrument_key": key,
            "captured_at": captured_at,
        })
        indicator_rows.append(calculate_indicators(
            instrument,
            quote,
            intraday_by_key.get(key, []),
            daily_by_key.get(key, []),
            captured_at,
        ))

    risk_rows = [calculate_spread_risk(config, quote_map, captured_at) for config in spread_configs]
    return {
        "captured_at": captured_at,
        "quote_rows": quote_rows,
        "indicator_rows": indicator_rows,
        "candle_rows": candle_rows,
        "spread_risk_rows": risk_rows,
        "errors": errors,
    }
