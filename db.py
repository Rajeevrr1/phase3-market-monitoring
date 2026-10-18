"""PostgreSQL persistence layer for the read-only Dhan supervisor.

This module only stores observations fetched by app.py. It has no Dhan HTTP
client and cannot place, modify, or cancel orders.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not configured.")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def connect():
    """Open a short-lived database connection; do not retain stale connections."""
    return psycopg.connect(_database_url(), connect_timeout=10)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS raw_events (
    event_id BIGSERIAL PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL,
    UNIQUE (cycle_id, event_type)
);
CREATE TABLE IF NOT EXISTS orders (
    order_key TEXT PRIMARY KEY,
    broker_order_id TEXT,
    status TEXT,
    transaction_type TEXT,
    security_id TEXT,
    quantity NUMERIC,
    traded_quantity NUMERIC,
    average_price NUMERIC,
    source_timestamp TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_broker_order_id ON orders (broker_order_id);
CREATE INDEX IF NOT EXISTS idx_orders_last_seen ON orders (last_seen_at);
CREATE TABLE IF NOT EXISTS fills (
    trade_key TEXT PRIMARY KEY,
    broker_trade_id TEXT,
    broker_order_id TEXT,
    security_id TEXT,
    transaction_type TEXT,
    quantity NUMERIC,
    traded_price NUMERIC,
    source_timestamp TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_broker_order_id ON fills (broker_order_id);
CREATE INDEX IF NOT EXISTS idx_fills_last_seen ON fills (last_seen_at);
CREATE TABLE IF NOT EXISTS strategy_trades (
    trade_key TEXT PRIMARY KEY REFERENCES fills(trade_key) ON DELETE CASCADE,
    strategy_name TEXT,
    attribution_status TEXT NOT NULL DEFAULT 'UNMATCHED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes TEXT
);
CREATE TABLE IF NOT EXISTS position_snapshots (
    snapshot_id BIGSERIAL PRIMARY KEY,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    position_count INTEGER NOT NULL,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_position_snapshots_captured ON position_snapshots (captured_at DESC);
CREATE TABLE IF NOT EXISTS attribution_decisions (
    decision_key TEXT PRIMARY KEY,
    trade_key TEXT NOT NULL REFERENCES fills(trade_key) ON DELETE CASCADE,
    strategy_name TEXT,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_attribution_trade_key ON attribution_decisions (trade_key, decided_at DESC);
CREATE TABLE IF NOT EXISTS alerts (
    alert_key TEXT PRIMARY KEY,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'INFO',
    message TEXT NOT NULL,
    related_key TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at DESC);
CREATE TABLE IF NOT EXISTS daily_reports (
    report_date DATE PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'PENDING',
    generated_at TIMESTAMPTZ,
    report_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    delivery_status TEXT NOT NULL DEFAULT 'NOT_SENT',
    notes TEXT
);
"""

MARKET_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    snapshot_id BIGSERIAL PRIMARY KEY,
    captured_at TIMESTAMPTZ NOT NULL,
    instrument_key TEXT NOT NULL,
    instrument_name TEXT NOT NULL,
    exchange_segment TEXT NOT NULL,
    security_id TEXT NOT NULL,
    role TEXT,
    last_price NUMERIC,
    average_price NUMERIC,
    volume NUMERIC,
    open_interest NUMERIC,
    best_bid NUMERIC,
    best_ask NUMERIC,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_captured ON market_snapshots (captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_instrument ON market_snapshots (instrument_key, captured_at DESC);
CREATE TABLE IF NOT EXISTS market_candles (
    candle_key TEXT PRIMARY KEY,
    instrument_key TEXT NOT NULL,
    instrument_name TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    candle_time TIMESTAMPTZ NOT NULL,
    open NUMERIC,
    high NUMERIC,
    low NUMERIC,
    close NUMERIC,
    volume NUMERIC,
    open_interest NUMERIC,
    payload JSONB NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_market_candles_time ON market_candles (instrument_key, candle_time DESC);
CREATE TABLE IF NOT EXISTS indicator_snapshots (
    indicator_id BIGSERIAL PRIMARY KEY,
    captured_at TIMESTAMPTZ NOT NULL,
    instrument_key TEXT NOT NULL,
    instrument_name TEXT NOT NULL,
    last_price NUMERIC,
    vwap NUMERIC,
    vwap_source TEXT,
    vwap_position TEXT,
    current_day_high NUMERIC,
    current_day_low NUMERIC,
    previous_session_high NUMERIC,
    previous_session_low NUMERIC,
    previous_session_close NUMERIC,
    recent_candle_high NUMERIC,
    recent_candle_low NUMERIC,
    volume NUMERIC,
    open_interest NUMERIC,
    level_status TEXT,
    data_quality TEXT,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_indicator_snapshots_time ON indicator_snapshots (instrument_key, captured_at DESC);
CREATE TABLE IF NOT EXISTS spread_risk_snapshots (
    risk_id BIGSERIAL PRIMARY KEY,
    captured_at TIMESTAMPTZ NOT NULL,
    spread_name TEXT NOT NULL,
    strategy_name TEXT,
    status TEXT NOT NULL,
    quantity NUMERIC,
    entry_credit NUMERIC,
    estimated_close_debit NUMERIC,
    estimated_pnl NUMERIC,
    max_theoretical_loss NUMERIC,
    message TEXT,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spread_risk_time ON spread_risk_snapshots (captured_at DESC);
"""


def ensure_schema() -> None:
    statements = [statement.strip() for statement in (SCHEMA_SQL + MARKET_SCHEMA_SQL).split(";") if statement.strip()]
    with connect() as conn:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)


def _records(payload: Any, endpoint: str) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in (endpoint, "data", "orders", "trades", "positions"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        known_fields = {str(k).lower() for k in payload}
        if known_fields.intersection({"orderid", "order_id", "tradeid", "trade_id", "securityid", "security_id", "positiontype", "netqty", "net_quantity"}):
            return [payload]
    return []


def _get(row: dict, *keys: str):
    lowered = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        if key.lower() in lowered and lowered[key.lower()] is not None:
            return lowered[key.lower()]
    return None


def _str(value):
    return None if value is None else str(value)


def _number(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default)


def _stable_key(prefix: str, supplied_id: Any, payload: dict) -> str:
    if supplied_id is not None and str(supplied_id).strip():
        return f"{prefix}:{str(supplied_id).strip()}"
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return f"{prefix}:hash:{digest}"


def _decision_key(trade_key: str, status: str, strategy_name: str | None, reason: str) -> str:
    raw = "|".join([trade_key, status, strategy_name or "", reason])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def persist_snapshot(data: dict[str, Any]) -> dict[str, int]:
    """Persist one successful poll cycle. Broker entities are upserted by stable keys."""
    ensure_schema()
    cycle_id = str(uuid.uuid4())
    counts = {"raw_events": 0, "orders": 0, "fills": 0, "position_snapshots": 0,
              "strategy_trades": 0, "attribution_decisions": 0, "alerts": 0}
    endpoint_payloads = {"positions": data.get("positions", []), "orders": data.get("orders", []), "trades": data.get("trades", [])}

    with connect() as conn:
        with conn.cursor() as cur:
            for endpoint, payload in endpoint_payloads.items():
                cur.execute(
                    """INSERT INTO raw_events (cycle_id, event_type, payload) VALUES (%s,%s,%s)
                       ON CONFLICT (cycle_id, event_type) DO NOTHING""",
                    (cycle_id, endpoint, Jsonb(payload)),
                )
                counts["raw_events"] += cur.rowcount

            position_rows = _records(endpoint_payloads["positions"], "positions")
            # Store the complete response even when it contains zero positions.
            cur.execute("INSERT INTO position_snapshots (position_count, payload) VALUES (%s,%s)",
                        (len(position_rows), Jsonb(endpoint_payloads["positions"])))
            counts["position_snapshots"] += 1

            for row in _records(endpoint_payloads["orders"], "orders"):
                broker_id = _get(row, "orderId", "order_id", "orderID")
                key = _stable_key("order", broker_id, row)
                cur.execute(
                    """INSERT INTO orders
                       (order_key,broker_order_id,status,transaction_type,security_id,quantity,traded_quantity,average_price,source_timestamp,payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (order_key) DO UPDATE SET status=EXCLUDED.status,
                         transaction_type=EXCLUDED.transaction_type, security_id=EXCLUDED.security_id,
                         quantity=EXCLUDED.quantity, traded_quantity=EXCLUDED.traded_quantity,
                         average_price=EXCLUDED.average_price, source_timestamp=EXCLUDED.source_timestamp,
                         last_seen_at=NOW(), payload=EXCLUDED.payload""",
                    (key, _str(broker_id), _str(_get(row, "orderStatus", "status", "order_status")),
                     _str(_get(row, "transactionType", "transaction_type")),
                     _str(_get(row, "securityId", "security_id", "securityID")),
                     _number(_get(row, "quantity", "qty", "orderQuantity")),
                     _number(_get(row, "filledQty", "tradedQuantity", "filled_quantity")),
                     _number(_get(row, "averageTradedPrice", "avgTradedPrice", "average_price")),
                     _str(_get(row, "createTime", "updateTime", "orderTime", "timestamp")), Jsonb(row)),
                )
                counts["orders"] += 1

            for row in _records(endpoint_payloads["trades"], "trades"):
                broker_trade_id = _get(row, "tradeId", "trade_id", "tradeID", "exchangeTradeId")
                broker_order_id = _get(row, "orderId", "order_id", "orderID")
                key = _stable_key("trade", broker_trade_id, row)
                security_id = _get(row, "securityId", "security_id", "securityID")
                cur.execute(
                    """INSERT INTO fills
                       (trade_key,broker_trade_id,broker_order_id,security_id,transaction_type,quantity,traded_price,source_timestamp,payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (trade_key) DO UPDATE SET broker_order_id=EXCLUDED.broker_order_id,
                         security_id=EXCLUDED.security_id, transaction_type=EXCLUDED.transaction_type,
                         quantity=EXCLUDED.quantity, traded_price=EXCLUDED.traded_price,
                         source_timestamp=EXCLUDED.source_timestamp, last_seen_at=NOW(), payload=EXCLUDED.payload""",
                    (key, _str(broker_trade_id), _str(broker_order_id), _str(security_id),
                     _str(_get(row, "transactionType", "transaction_type")),
                     _number(_get(row, "tradedQuantity", "quantity", "qty")),
                     _number(_get(row, "tradedPrice", "price", "averageTradedPrice")),
                     _str(_get(row, "exchangeTime", "tradeTime", "timestamp")), Jsonb(row)),
                )
                counts["fills"] += 1

                reason = "No verified Stratzy strategy identifier/mapping configured; contract, side, quantity, and time alone are insufficient."
                cur.execute(
                    """INSERT INTO strategy_trades (trade_key,strategy_name,attribution_status,notes)
                       VALUES (%s,NULL,'UNMATCHED',%s) ON CONFLICT (trade_key) DO NOTHING""",
                    (key, reason),
                )
                counts["strategy_trades"] += cur.rowcount

                decision_key = _decision_key(key, "UNMATCHED", None, reason)
                cur.execute(
                    """INSERT INTO attribution_decisions (decision_key,trade_key,strategy_name,status,reason,evidence)
                       VALUES (%s,%s,NULL,'UNMATCHED',%s,%s) ON CONFLICT (decision_key) DO NOTHING""",
                    (decision_key, key, reason, Jsonb({"broker_order_id": broker_order_id, "security_id": security_id})),
                )
                counts["attribution_decisions"] += cur.rowcount

                alert_key = f"unmatched-attribution:{key}"
                cur.execute(
                    """INSERT INTO alerts (alert_key,alert_type,severity,message,related_key,payload)
                       VALUES (%s,'ATTRIBUTION_UNMATCHED','WARNING',%s,%s,%s)
                       ON CONFLICT (alert_key) DO NOTHING""",
                    (alert_key, f"Trade {key} is not attributed to Zen or Curvature.", key,
                     Jsonb({"broker_order_id": broker_order_id, "security_id": security_id})),
                )
                counts["alerts"] += cur.rowcount
    return counts


def get_table_counts() -> dict[str, int]:
    ensure_schema()
    tables = ("raw_events", "orders", "fills", "strategy_trades", "position_snapshots",
              "attribution_decisions", "alerts", "daily_reports", "market_snapshots",
              "market_candles", "indicator_snapshots", "spread_risk_snapshots")
    with connect() as conn:
        with conn.cursor() as cur:
            result = {}
            for table in tables:  # Fixed internal allowlist; not user-supplied.
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                result[table] = int(cur.fetchone()[0])
            return result



def persist_market_snapshot(snapshot: dict[str, Any]) -> dict[str, int]:
    """Persist the read-only market observations produced by market_monitor.py."""
    ensure_schema()
    from datetime import datetime
    from zoneinfo import ZoneInfo

    def parse_dt(value: Any):
        if not value:
            return datetime.now(ZoneInfo("Asia/Kolkata"))
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        except (TypeError, ValueError):
            return datetime.now(ZoneInfo("Asia/Kolkata"))

    def num(value: Any):
        if value is None or value == "":
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None

    def candle_key(row: dict[str, Any]) -> str:
        raw = "|".join(str(row.get(field, "")) for field in ("instrument_key", "interval_minutes", "candle_time"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    counts = {"market_snapshots": 0, "market_candles": 0, "indicator_snapshots": 0, "spread_risk_snapshots": 0}
    with connect() as conn:
        with conn.cursor() as cur:
            for item in snapshot.get("quote_rows", []):
                instrument = item.get("instrument", {})
                quote = item.get("quote", {})
                key = str(item.get("instrument_key", ""))
                if not key or not isinstance(quote, dict) or not isinstance(instrument, dict):
                    continue
                indicator = next((x for x in snapshot.get("indicator_rows", []) if x.get("instrument_key") == key), {})
                cur.execute(
                    """INSERT INTO market_snapshots
                       (captured_at,instrument_key,instrument_name,exchange_segment,security_id,role,
                        last_price,average_price,volume,open_interest,best_bid,best_ask,payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (parse_dt(item.get("captured_at")), key, str(instrument.get("name", key)),
                     str(instrument.get("exchange_segment", "")), str(instrument.get("security_id", "")),
                     str(instrument.get("role", "")), num(quote.get("last_price")), num(quote.get("average_price")),
                     num(quote.get("volume")), num(quote.get("oi")), num(indicator.get("best_bid")), num(indicator.get("best_ask")),
                     Jsonb({"instrument": instrument, "quote": quote})),
                )
                counts["market_snapshots"] += 1

            for row in snapshot.get("candle_rows", []):
                if not isinstance(row, dict) or not row.get("instrument_key") or not row.get("candle_time"):
                    continue
                cur.execute(
                    """INSERT INTO market_candles
                       (candle_key,instrument_key,instrument_name,interval_minutes,candle_time,open,high,low,close,volume,open_interest,payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (candle_key) DO UPDATE SET
                         open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close,
                         volume=EXCLUDED.volume, open_interest=EXCLUDED.open_interest,
                         last_seen_at=NOW(), payload=EXCLUDED.payload""",
                    (candle_key(row), str(row.get("instrument_key")), str(row.get("instrument_name", row.get("instrument_key"))),
                     int(row.get("interval_minutes", 5)), parse_dt(row.get("candle_time")), num(row.get("open")),
                     num(row.get("high")), num(row.get("low")), num(row.get("close")), num(row.get("volume")),
                     num(row.get("open_interest")), Jsonb(row)),
                )
                counts["market_candles"] += 1

            for row in snapshot.get("indicator_rows", []):
                if not isinstance(row, dict) or not row.get("instrument_key"):
                    continue
                cur.execute(
                    """INSERT INTO indicator_snapshots
                       (captured_at,instrument_key,instrument_name,last_price,vwap,vwap_source,vwap_position,
                        current_day_high,current_day_low,previous_session_high,previous_session_low,previous_session_close,
                        recent_candle_high,recent_candle_low,volume,open_interest,level_status,data_quality,payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (parse_dt(row.get("captured_at")), str(row.get("instrument_key")), str(row.get("instrument_name", "")),
                     num(row.get("last_price")), num(row.get("vwap")), row.get("vwap_source"), row.get("vwap_position"),
                     num(row.get("current_day_high")), num(row.get("current_day_low")), num(row.get("previous_session_high")),
                     num(row.get("previous_session_low")), num(row.get("previous_session_close")),
                     num(row.get("recent_12_candle_high")), num(row.get("recent_12_candle_low")), num(row.get("volume")),
                     num(row.get("open_interest")), row.get("level_status"), row.get("data_quality"), Jsonb(row)),
                )
                counts["indicator_snapshots"] += 1

            for row in snapshot.get("spread_risk_rows", []):
                if not isinstance(row, dict):
                    continue
                cur.execute(
                    """INSERT INTO spread_risk_snapshots
                       (captured_at,spread_name,strategy_name,status,quantity,entry_credit,estimated_close_debit,
                        estimated_pnl,max_theoretical_loss,message,payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (parse_dt(row.get("captured_at")), str(row.get("spread_name", "Unlabelled spread")),
                     row.get("strategy_name"), str(row.get("status", "UNKNOWN")), num(row.get("quantity")),
                     num(row.get("entry_credit")), num(row.get("estimated_close_debit")), num(row.get("estimated_pnl")),
                     num(row.get("max_theoretical_loss")), row.get("message"), Jsonb(row)),
                )
                counts["spread_risk_snapshots"] += 1
    return counts
